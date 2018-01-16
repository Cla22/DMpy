import numpy as np
import pymc3 as pm
from pymc3.distributions import Continuous
from theano import scan, function, printing
import theano.tensor as T
import theano
from pymc3 import fit, sample_approx
from pymc3 import traceplot, find_MAP
import matplotlib.pyplot as plt
import warnings
import re
import pandas as pd
import seaborn as sns
from timeit import default_timer as timer
from collections import OrderedDict
from itertools import product
from DMpy.utils import generate_pymc_distribution, n_returns, function_wrapper, load_data, load_outcomes, \
    parameter_table, model_fit, generate_choices2, n_obs_dynamic, simulated_responses, r2, log_likelihood

theano.config.compute_test_value = "off"

sns.set_style("white")
sns.set_palette("Set1")

# TODO change simulated results to an object? I.e. create a simulation class and use this to store results and do things with them
# TODO scale points in parameter recovery plots by SD if estimated using variational/MCMC
# TODO Check re-entered parameters are given as dynamic and non-reentered ones aren't - getting dynamic setting wrong is too easy and causes problems that aren't easy to spot/solve
# TODO change recovery to a method of the model class which is called manually after fitting

# TODO multiple outcome arrays (sequences are entered as a list so should be possible) - would allow for interesting things to be added to the model (e.g. other stimuli)

# TODO move hierarchical setting to parameters - allow more detail specification and combinations of h/non-h
# TODO more than 2 stimuli - outcome/response for each
# TODO cauchy distribution + others
# TODO subject specific model fits (DIC etc)


def _initialise_parameters(learning_parameters, observation_parameters, n_subjects, n_runs, mle, hierarchical):

    """
    Assigns PyMC3 distributions to provided parameters. PyMC3 distribution is stored in the .pymc_distribution attribute

    Args:
        learning_parameters: Parameters for the learning model
        observation_parameters: Parameters for the observation model
        n_subjects: Number of subjects, used for determining shape of parameter arrays during fitting
        mle: MLE flag - if true, parameters are assigned uniform/flat priors
        hierarchical: Hierarchical flag - if true, hierarchical priors are added

    Returns:
        dynamic_parameters: list of dynamic parameters with distributions assigned
        static_parameters: list of static parameters with distributions assigned
        observation_parameters: the provided observation parameters, there's probably a reason for this but I can't remember
                                it
    """

    if type(learning_parameters) is not list:
        learning_parameters = [learning_parameters]

    learning_parameter_names = []

    for n, p in enumerate(learning_parameters):
        learning_parameters[n] = generate_pymc_distribution(p, n_subjects=n_subjects, mle=mle,
                                                                 hierarchical=hierarchical)
        learning_parameter_names.append(p.name)

    dynamic_parameters = []
    static_parameters = []

    for p in learning_parameters:  # add parameters to static or dynamic lists

        if not hasattr(p, 'pymc_distribution'):
            raise AttributeError("Parameter {0} has no distribution information, make sure at least" \
                                 " the mean is specified".format(p.name))

        if p.dynamic:
            dynamic_parameters.append(p.pymc_distribution)
        else:
            static_parameters.append(p.pymc_distribution)

    if observation_parameters[0] is not None:
        for n, p in enumerate(observation_parameters):

            if not p.name in learning_parameter_names:
                observation_parameters[n] = generate_pymc_distribution(p, n_subjects=n_subjects, mle=mle,
                                                                            hierarchical=hierarchical)
            else:  # awkward - would be better to make shared list of parameters first then split into l/obs lists
                for i in learning_parameters:
                    if i.name == p.name:
                        observation_parameters[n] = i

    return dynamic_parameters, static_parameters, observation_parameters


def _add_noise(timeseries, mean, sd, lower_bound=0, upper_bound=1):

    noisy_timeseries = timeseries + np.random.normal(mean, sd, timeseries.shape)

    noisy_timeseries[noisy_timeseries > upper_bound] = upper_bound
    noisy_timeseries[noisy_timeseries < lower_bound] = lower_bound

    return noisy_timeseries


class _PyMCModel(Continuous):

    """
    Instance of PyMC3 model used to fit models. Used internally by DMModel class - do not use directly.
    """

    def __init__(self, learning_models, learning_parameters, observation_model, observation_parameters, responses, hierarchical,
                 n_subjects, time, n_runs, mle=False, outcomes=None, logp_method='ll', *args, **kwargs):
        super(_PyMCModel, self).__init__(*args, **kwargs)

        self.fit_complete = False
        self.learning_model = learning_models[0]
        self.__learning_model_initial = learning_models[1]
        self.observation_model = observation_model
        self.learning_parameters = learning_parameters
        self.observation_parameters = observation_parameters[0]
        self.__observation_dynamic_inputs = observation_parameters[1]
        self.n_subjects = n_subjects
        self.responses = responses
        self.outcomes = outcomes
        self.time = time
        self.n_runs = n_runs
        self.logp_method = logp_method

        if len(np.unique(n_runs)) != 1:
            raise ValueError("All subjects must have the same number of runs")

        self.n_runs = self.n_runs.min()

        ## initialise parameters
        
        self.dynamic_parameters, self.static_parameters, self.observation_parameters = \
            _initialise_parameters(self.learning_parameters, self.observation_parameters, self.n_subjects, self.n_runs,
                                   mle, hierarchical)

        ## learning models with multiple outputs
        ## check number of dynamic parameters, if number of learning function outputs is longer, add nones to outputs info

        self.__n_dynamic = len(self.dynamic_parameters)
        self.__n_learning_returns, _ = n_returns(self.learning_model)

        self.responses = self.responses.T
        if self.responses.ndim == 1:
            self.responses = self.responses.reshape(self.responses.shape[0], 1)


    def get_value(self, x):

        """
        Function to run the learning and observation models on the provided outcome data

        Need to make sure order of arguments is right
        OR figure out a way to match scan arguments to function arguments

        Args:
            x: Outcome data

        Returns:
            prob: Probability of choosing an option
        """

        # begin awful hack - there is probably a better way to get values on trial+1 while retaining initial value on t = 0

        self.static_parameters_reshaped = [np.repeat(i, self.n_runs) for i in self.static_parameters]
        self.dynamic_parameters_reshaped = [np.repeat(i, self.n_runs) for i in self.dynamic_parameters]
        if self.observation_parameters[0] is not None:
            self.observation_parameters_reshaped = [np.repeat(i.pymc_distribution, self.n_runs) for i in self.observation_parameters]
        else:
            self.observation_parameters_reshaped = None

        try:
            value, _ = scan(fn=self.learning_model,
                            sequences=[dict(input=x, taps=[-1]), dict(input=T.ones_like(x), taps=[-1])],
                            outputs_info=self.dynamic_parameters_reshaped + [None] * (self.__n_learning_returns - self.__n_dynamic),
                            non_sequences=self.static_parameters_reshaped)
            _value, _ = scan(fn=self.__learning_model_initial,
                                    sequences=[dict(input=x[:1, :]), dict(input=T.ones_like(x)[0:2])],
                             outputs_info=self.dynamic_parameters_reshaped + [None] * (self.__n_learning_returns - self.__n_dynamic),
                             non_sequences=self.static_parameters_reshaped)
        except ValueError as e:
            if "None as outputs_info" in e.message:
                raise ValueError("Mismatch between number of dynamic outputs and number of dynamic inputs. \n"
                                 "Make sure function outputs and inputs match (i.e. all dynamic inputs have a corresponding\n"
                                 " returned value, and make sure dynamic parameters are correctly set to be dynamic and\n"
                                 " static parameters are set to be static")
            else:
                raise e

        if not len(value):
            value = [value]
            _value = [_value]

        value = value[:self.__n_dynamic]  # hack, for some reason non-reused outputs don't join properly

        for n, v in enumerate(value):
            value[n] = T.concatenate([_value[n], v])

        # end awful hack
        observation_dynamics = [value[i] for i in self.__observation_dynamic_inputs]

        if self.observation_model is not None:
            prob, obs_outs = self.observation_model(*observation_dynamics + self.observation_parameters_reshaped)
            prob = prob.squeeze()
        else:
            prob = value[0]

        return prob


    def logp(self, x):
        """
        Calls the get_value function and then calculates log likelihood for the model based on estimated probabilities

        Args:
            x: Outcome data

        Returns:
            Log likelihood
        """

        prob = self.get_value(x)

        if self.logp_method == 'll':
            logp = log_likelihood(self.responses, prob)

        elif self.logp_method == 'r2':
            logp = r2(self.responses, prob) * 10000

        else:
            raise ValueError("Invalid likelihood function specified")

        return logp


class DMModel():

    """
    Class used for defining DMpy models

    Args:
        learning_model: Function defining the learning model to be used
        learning_parameters: A list of parameters defined using the Parameter class, given in the order expected by the learning model function
        observation_model: Function defining the observation model to be used. If no observation model is used this can be indicated by providing None
        observation_parameters: A list of parameters defined using the Parameter class, given in the order expected by the observation model function. If no observation model is used this can be indicated by providing None
        name: Optional argument used for labelling the model instance

    """

    def __init__(self, learning_model, learning_parameters, observation_model, observation_parameters, logp_method='ll',
                 name=''):
        self.name = name
        self.learning_model = learning_model
        self.learning_parameters = learning_parameters
        self.observation_model = observation_model
        self.observation_parameters = observation_parameters
        self.logp_method = logp_method
        self.__observation_dynamic_inputs = []
        self.trace = None
        self.simulated = []
        self._simulate_function = None
        self._model = None
        self.fit_complete = False
        self._fit_method = None

        self.__n_learning_returns, self.__learning_returns = n_returns(self.learning_model)

        if self.observation_model is not None:
            self.__n_observation_returns, self.__observation_returns = n_returns(self.observation_model)
        else:
            self.__n_observation_returns, self.__observation_returns = 0, None

        n_dynamic = 0

        for p in self.learning_parameters:
            try:
                if p.dynamic:
                    n_dynamic += 1
            except AttributeError:
                raise ValueError("One or more parameters are not instances of the RLpackage parameter class.\n"
                                 "Failed with parameter value {0}".format(p))

        # record the initial state of the model, used for filling in starting values
        self.__learning_model_initial = function_wrapper(self.learning_model, self.__n_learning_returns, n_dynamic)

        if type(self.observation_parameters) is not list:
            self.observation_parameters = [self.observation_parameters]

        self.__observation_dynamic_inputs = [i for i in self.observation_parameters if isinstance(i, str)]
        self.observation_parameters = [i for i in self.observation_parameters if not isinstance(i, str)]

        for n, i in enumerate(self.__observation_dynamic_inputs):
            for nn, j in enumerate(self.learning_parameters):
                if j.name == i:
                    self.__observation_dynamic_inputs[n] = nn  # get the index of the relevant learning parameter
        self.__observation_dynamic_inputs = [0] + self.__observation_dynamic_inputs  # add zero for value output

        if any([isinstance(i, str) for i in self.__observation_dynamic_inputs]):  # check this worked
            raise ValueError("Observation model dynamic inputs don't match with learning model parameter names")

        if self.observation_model is not None:
            n_obs_params = len(self.observation_parameters)
            self.__n_obs_dynamic = n_obs_dynamic(self.observation_model, n_obs_params)
        else:
            n_obs_params = 0
            self.__n_obs_dynamic = 0


        # create model
        self._pymc3_model = None


    def _create_model(self, mle=False):

        """
        Internally used method for generating PyMC3 model instance - allows model instance to be cached and reused

        Args:
            outcomes: Outcomes (theano shared variable)
            responses: Responses (theano shared variable)
            n_subjects: Number of subjects (theano shared variable)
            n_runs: Number of runs per subject (theano shared variable)
            mle: Specifies whether to use MLE estimation when fitting - if true, parameters are converted to uniform/flat

        """

        with pm.Model(theano_config={'compute_test_value': 'ignore', 'optimizer': 'fast_compile'}) as model:

            m = _PyMCModel('model', learning_models=(self.learning_model, self.__learning_model_initial),
                          learning_parameters=self.learning_parameters,
                          observation_model=self.observation_model,
                          observation_parameters=[self.observation_parameters, self.__observation_dynamic_inputs],
                          responses=self.responses, observed=self.outcomes, outcomes=self.outcomes, time=self.time,
                          n_subjects=self.n_subjects, n_runs=self.n_runs, hierarchical=False, mle=mle,
                          logp_method=self.logp_method)

            self._DMpy_model = m

            params = (m.distribution.learning_parameters, m.distribution.observation_parameters)
            params = [i for j in params for i in j]
            self.params = [p for p in params if p is not None]

        self._pymc3_model = model

        print "Created model"


    def fit(self, responses, outcomes=None, fit_method='MLE', hierarchical=False, plot=True, fit_stats=False, recovery=False,
            exclude=None, fit_kwargs=None, sample_kwargs=None, logp_method='ll', suppress_table=False):

        """
        General fitting method.

        Args:
            outcomes: task outcomes (e.g. A rewarded or not)
            responses: subject responses, as a .txt or .csv file with columns ['Responses', 'Subject']
            fit_method: method to use for fitting, one of 'MLE', 'MAP', 'Variational', 'MCMC', 'mle', 'map',
                        'variational', 'mcmc'. Default = 'MLE'
            hierarchical: Whether to perform hierarchical fitting - has no effect for MLE or MAP estimation. Default =
                          False
            plot: For sampling methods, provide traceplots. Default = True
            fit_stats: Provide fit statistics. Provided because for some reason this can take a while for variational
                        and MCMC methods so it can be convenient to turn it off when testing. Default = False
            recovery: If simulated parameter values are provided in the response file, will calculate correlations between
                      simulated and estimated parameters and produce correlation plots to assess parameter recovery success.
                      Default = True
            exclude: List of subject IDs to exclude from model fitting
            fit_kwargs: Dictionary of keyword arguments passed to underlying MLE, MAP and variational fitting functions. See
            PyMC3 documentation for more details (http://docs.pymc.io/notebooks/getting_started.html)
            sample_kwargs: Dictionary of keyword arguments passed to underlying variational and MCMC sampling functions.
            logp_method: Function to minimise when fitting to data, currently accepts either 'll' for log likelihood or
                         'r2' for R-squared
            suppress_table: If set to true, parameter table will not be printed when model fitting is complete.


        """

        allowed_methods = ['MLE', 'MAP', 'Variational', 'MCMC', 'mle', 'map', 'variational', 'mcmc']

        # Load data
        subjects, n_runs, responses, self.sims, loaded_outcomes = load_data(responses, exclude=exclude)

        if outcomes is None and loaded_outcomes is None:
            raise ValueError("No outcomes provided. Please provide outcomes either as an array or as a column in "
                             "the response file")

        responses = responses.astype(float)

        if loaded_outcomes is not None:
            outcomes = loaded_outcomes
        else:
            outcomes = load_outcomes(outcomes)

        self.subjects = subjects
        n_subjects = len(subjects)

        if fit_kwargs is None:
            fit_kwargs = {}
        if sample_kwargs is None:
            sample_kwargs = {}

        self.logp_method = logp_method

        # make sure outcomes and responses look nice

        if responses.shape[0] == 1:
            responses = responses[0]

        if responses.shape[1] != outcomes.shape[0]:
            raise ValueError("Responses ({0}) and outcomes ({1}) have unequal lengths".format(responses.shape[1],
                                                                                              outcomes.shape[0]))
        if fit_method in ['MLE', 'mle']:
            mle = True
        else:
            mle = False

        time = np.tile(np.arange(0, outcomes.shape[0]), (outcomes.shape[1], 1)).T

        if self._pymc3_model is None or self._fit_method != fit_method.lower() or n_subjects != self.n_subjects:

            # create model if it doesn't exist or the fitting method has been changed
            # turn outcomes and responses into shared variables

            self.responses = theano.shared(responses)
            self.outcomes = theano.shared(outcomes)
            self.time = theano.shared(time)
            self.n_subjects = n_subjects
            self.n_runs = theano.shared(n_runs)
            self._create_model(mle=mle)

        self.responses.set_value(responses)
        self.outcomes.set_value(outcomes)
        self.time.set_value(time)
        self.n_subjects = n_subjects
        self.n_runs.set_value(n_runs)
        self._fit_method = fit_method.lower()

        # run fitting method

        if fit_method in ['MLE', 'mle']:
            # MAP method is used for MLE - only difference is the absence of priors when setting up parameters
            self._fit_MAP(plot=plot, recovery=recovery, suppress_table=suppress_table, mle=True,
                          **fit_kwargs)
            
        elif fit_method in ['MAP', 'map']:
            self._fit_MAP(plot=plot, recovery=recovery, suppress_table=suppress_table, **fit_kwargs)

        elif fit_method in ['variational', 'Variational']:
            self._fit_variational(plot=plot, hierarchical=hierarchical, recovery=recovery, suppress_table=suppress_table,
                                  logp_method=logp_method, fit_stats=fit_stats, fit_kwargs=fit_kwargs, sample_kwargs=sample_kwargs)

        elif fit_method in ['MCMC', 'mcmc']:
            self._fit_MCMC(plot=plot, hierarchical=hierarchical, recovery=recovery, suppress_table=suppress_table,
                           logp_method=logp_method, fit_stats=fit_stats, **sample_kwargs)

        else:
            raise ValueError("Invalid fitting method provided ({0}). Fit method should be one of {1}"
                             .format(fit_method, allowed_methods))



    def _fit_MCMC(self, hierarchical=False, plot=True, fit_stats=False, recovery=True, suppress_table=False,
                  logp_method='ll', **kwargs):

        sns.set_palette("deep")

        print "Fitting model using NUTS"
        start = timer()

        # check data is correct
        # assert len(outcomes) == len(observed), "Outcome and observed data are " \
        #                                        "different lengths ({0} and ({1}".format(len(outcomes), len(observed))

        if hierarchical and self.n_subjects < 2:
            warnings.warn("\nWarning: Hierarchical model fitting only possible with more than one subject, "
                          "fitting individual subject\n")
            hierarchical = False

        elif hierarchical and self.n_subjects > 1:
            print "Performing hierarchical model fitting for {0} subjects".format(self.n_subjects)

        elif not hierarchical and self.n_subjects > 1:
            print "Performing non-hierarchical model fitting for {0} subjects".format(self.n_subjects)

        with self._pymc3_model:

            self.trace = pm.sample(**kwargs)

            if plot:
                traceplot(self.trace)

        self.fit_values = pm.df_summary(self.trace, varnames=self.trace.varnames)['mean'].to_dict()

        print "\nPARAMETER ESTIMATES\n"

        self.parameter_table = parameter_table(pm.df_summary(self.trace), self.subjects)

        if not suppress_table:
            print self.parameter_table

        if recovery and self.sims is not None:
            self.recovery_correlations = self.recovery()
        elif recovery:
            warnings.warn("No simulations have been performed, unable to perform parameter recovery tests")

        # # these seem to take a lot of time...
        # if fit_stats:
        #     print "Calculating DIC..."
        #     self.DIC = pm.dic(self.trace, rl)
        #     print "Calculating WAIC..."
        #     self.WAIC = pm.waic(self.trace, rl)[0]
        #     print "Calculated fit statistics"


        #self.log_likelihood, self.BIC, self.AIC = model_fit(rl.logp, self.fit_values, rl.vars)
        self.fit_complete = True
        end = timer()
        print "Finished model fitting in {0} seconds".format(end - start)


    def _fit_variational(self, plot=True, hierarchical=True, fit_stats=False, recovery=True, suppress_table=False,
                         logp_method='ll', fit_kwargs=None, sample_kwargs=None):

        sns.set_palette("deep")

        print "\n-------------------" \
              "Fitting model using ADVI" \
              "-------------------\n"
        start = timer()
        # check data is correct TODO change for multi-subject data
        # assert len(outcomes) == len(observed), "Outcome and observed data are " \
        #                                        "different lengths ({0} and ({1}".format(len(outcomes), len(observed))

        if hierarchical and self.n_subjects < 2:
            warnings.warn("\nWarning: Hierarchical model fitting only possible with more than one subject, "
                          "fitting individual subject\n")
            hierarchical = False

        elif hierarchical and self.n_subjects > 1:
            print "Performing hierarchical model fitting for {0} subjects".format(self.n_subjects)

        elif not hierarchical and self.n_subjects > 1:
            print "Performing non-hierarchical model fitting for {0} subjects".format(self.n_subjects)

        with self._pymc3_model:

            self.approx = fit(**fit_kwargs)
            self.trace = sample_approx(self.approx, **sample_kwargs)

            print "Done"

        if plot:
            traceplot(self.trace)

        self.fit_values = pm.df_summary(self.trace, varnames=self.trace.varnames)['mean'].to_dict()
        self.fit_complete = True

        print "\nPARAMETER ESTIMATES\n"

        self.parameter_table = parameter_table(pm.df_summary(self.trace), self.subjects)

        if not suppress_table:
            print self.parameter_table

        if recovery and self.sims is not None:
            self.recovery_correlations = self.recovery()

        # if fit_stats:
        #     # these seem to take a lot of time...
        #     print "Calculating DIC..."
        #     self.DIC = pm.dic(self.trace, rl)
        #     print "Calculating WAIC..."
        #     self.WAIC = pm.waic(self.trace, rl)[0]
        #     print "Calculated fit statistics"

        # TODO figure out fit statistics for multi-subject model fits
        # self.log_likelihood, self.BIC, self.AIC = model_fit(rl.logp, self.fit_values, rl.vars)
        end = timer()
        print "Finished model fitting in {0} seconds".format(end - start)


    def _fit_MAP(self, plot=True, mle=False, recovery=True, suppress_table=False, **kwargs):


        if mle:
            print "\n-------------------" \
                  "Finding MLE estimate" \
                  "-------------------\n"
        else:
            print "\n-------------------" \
                  "Finding MAP estimate" \
                  "-------------------\n"

        start = timer()

        # check data is correct TODO change for multi-subject data
        # assert len(outcomes) == len(observed), "Outcome and observed data are " \
        #                                        "different lengths ({0} and ({1}".format(len(outcomes), len(observed))

        print "Performing model fitting for {0} subjects".format(self.n_subjects)

        self._model = {}
        self.map_estimate = {}

        with self._pymc3_model:

            try:
                self.map_estimate = find_MAP(**kwargs)
            except ValueError as err:
                warnings.warn("Fitting failed, this is probably because your model returned NaN values")
                raise err

            self.raw_fit_values = self.map_estimate


        self.fit_complete = True
        # need to backwards transform these values

        untransformed_params = {}

        for p in self.params:
            for m in self.raw_fit_values.keys():
                n = re.search('.+(?=_.+__)', m)
                if (n and n.group() == p.name) or m == p.name:
                    if '__' in m:
                        untransformed_params[p.name] = p.backward(self.raw_fit_values[m]).eval()
                    else:
                        untransformed_params[p.name] = self.raw_fit_values[m]

        self.fit_values = untransformed_params

        print "\nPARAMETER ESTIMATES\n"

        if self.n_subjects > 1:
            self.parameter_table = pd.DataFrame(self.fit_values)
            self.parameter_table['Subject'] = self.subjects
            self.parameter_table.sort_values('Subject')
        else:
            self.parameter_table = pd.DataFrame(data=[self.subjects.tolist() + self.fit_values.values()])
            self.parameter_table.columns = ['Subject'] + self.fit_values.keys()

        if recovery and self.sims is not None:
            self.recovery_correlations = self.recovery()

        if not suppress_table:
            print self.parameter_table

        self.log_likelihood, self.BIC, self.AIC = model_fit(self._pymc3_model.logp, self.map_estimate, self._pymc3_model.vars, self.outcomes)

        self.DIC = None
        self.WAIC = None
        end = timer()
        print "Finished model fitting in {0} seconds".format(end-start)

    def tracePlot(self):

        """
        Returns:
            A traceplot
        """

        sns.set_palette("deep")
        traceplot(self.trace)


    def simulate(self, outcomes=None, learning_parameters=None, observation_parameters=None, plot=False, responses=None,
                 plot_choices=False, output_value=False, plot_outcomes=True, output_file='', n_subjects=1,
                 plot_value=True, legend=False, combinations=False, runs_per_subject=1, palette='Blues',
                 plot_against_true=False, response_format='continuous', noise_mean=0, noise_sd=0):

        """
        Args:
            outcomes: Task outcomes (i.e. rewards/punishments)
            learning_parameters: Parameter values for the learning model to be used in simulation. Should be given as a dictionary of the format {'parameter name': parameter values}. Parameter values can be either single values or a list/numpy array of values.
            observation_parameters: Parameter values for the observation model, provided in the same format as those for the learning model
            plot: If true, plots the simulated trajectories
            responses: Real subjects' responses - only used for plotting to compare simulated responses against real
            plot_choices: Whether to plot simulated choices based on estimated choice probability
            output_value: If true and an output filename is provided, the simulated response will be the estimated value rather than simulated choices
            plot_outcomes: If true, outcomes are represented on plots of simulated trajectories
            output_file: Filename to save simulated response file as
            n_subjects: Number of subjects to simulate. Each parameter combination will be simulated as many times as is specified here
            plot_value: If true, estimated value is represented on plots of simulated trajectories
            legend: Adds a legend to simulation plots
            combinations: If true, every combination of provided parameter values will be simulated
            runs_per_subject: Number of runs to simulate per subject
            palette: Seaborn colour palette to use in plotting
            plot_against_true: If the model has been fit, produces a plot for each subject of their true responses against responses simulated using their best fitted parameter estimates
            response_format: The type of responses to be plotted (either 'continuous' or 'discrete'). If continuous, responses will be plotted as a line, if discrete they will be plotted as points. Default: 'continuous'
            noise_mean: Sets the mean of the noise distribution (can be useful for adding a bias to subjects' responses), default = 0
            noise_sd: Sets the standard deviation of the noise distribution. Default = 0, increasing this will add noise.

        Returns:
            The results of the simulation, containing the parameters given to the learning model, the parameters given to the observation model, and the simulated data
            The output filename, if provided

        """

        # Check that we either have parameters provided or the model has been fit

        if learning_parameters == None and not self.fit_complete:
            raise ValueError("No parameter values provided and model has not been fit. Must explicitly "
                             "provide parameter values for simulation or fit the model first")


        # Using user-defined parameter values & outcomes

        if learning_parameters is not None:  # We're using user-specified parameter values

            self.sim_learning_parameters = OrderedDict(learning_parameters)
            params_from_fit = False

            if observation_parameters is not None:
                self.sim_observation_parameters = OrderedDict(observation_parameters)
            else:
                self.sim_observation_parameters = OrderedDict()

            n_subjects = n_subjects * runs_per_subject

            # check that outcomes are in the right format (i.e. ndarray or list)
            if isinstance(outcomes, np.ndarray) or isinstance(outcomes, list):
                outcomes = load_outcomes(outcomes)
            else:
                raise ValueError("Outcomes are not in the correct format")

        # Using parameter estimates & outcomes from model fit

        else:

            self.sim_learning_parameters = OrderedDict()
            params_from_fit = True  # use best values from model fitting if parameter values aren't provided

            n_runs = self.n_runs.eval()
            n_subjects = self.n_subjects
            outcomes = self.outcomes.eval()

            self.sim_learning_parameters = OrderedDict()
            self.sim_observation_parameters = OrderedDict()

            learning_parameter_names = [i.name for i in self.learning_parameters]
            if self.observation_parameters[0] is not None:
                observation_parameter_names = [i.name for i in self.observation_parameters]
            else:
                observation_parameter_names = None
            for p, v in self.fit_values.iteritems():
                if p in learning_parameter_names:
                    self.sim_learning_parameters[p] = np.repeat(v, n_runs)
                elif observation_parameter_names is not None and p in observation_parameter_names:
                    self.sim_observation_parameters[p] = np.repeat(v, n_runs)
            for p in learning_parameter_names:
                if p not in self.sim_learning_parameters.keys():
                    mean = [i.mean for i in self.learning_parameters if i.name == p]
                    self.sim_learning_parameters[p] = np.repeat(mean, n_runs * n_subjects)
            if observation_parameter_names is not None:
                for p in observation_parameter_names:
                    if p not in self.sim_observation_parameters.keys():
                        mean = [i.mean for i in self.observation_parameters if i.name == p]
                        self.sim_observation_parameters[p] = np.repeat(mean, n_runs * n_subjects)

        if not any([isinstance(i, list) and len(i) > 1 for i in learning_parameters.values()]):
            single_parameter_values = True  # temporary(ish) solution for shape problem with single values
        else:
            single_parameter_values = False


        # Create parameter combinations

        # First convert any single values to lists
        for p, v in self.sim_learning_parameters.iteritems():
            if not hasattr(v, '__len__'):
                self.sim_learning_parameters[p] = [v]

        for p, v in self.sim_observation_parameters.iteritems():
            if not hasattr(v, '__len__'):
                self.sim_observation_parameters[p] = [v]

        # combine learning and observation parameters into a single list - necessary for creating combinations/pairs
        parameter_values = self.sim_learning_parameters.values() + self.sim_observation_parameters.values()

        if combinations:  # create combinations of parameters
            p_combinations = np.array(list(product(*parameter_values))) # get product
            p_combinations = p_combinations.repeat(n_subjects, axis=0)  # repeat x n_subjects

        else: # get pairs of parameters
            p_combinations = []
            if not all(len(i) == len(parameter_values[0]) for i in parameter_values):
                raise ValueError("Each parameter should have the same number of values")
            else:
                for i in range(len(parameter_values[0])):
                    p_combinations.append([j[i] for j in parameter_values])
            p_combinations = np.array(p_combinations)
            if not params_from_fit:
                p_combinations = p_combinations.repeat(n_subjects, axis=0)

        # put combinations of parameters back into dictionaries
        for n, p in enumerate(self.sim_learning_parameters.keys() + self.sim_observation_parameters.keys()):
            if p in self.sim_learning_parameters.keys():
                self.sim_learning_parameters[p] = p_combinations[:, n]
            else:
                self.sim_observation_parameters[p] = p_combinations[:, n]

        # each parameter now has a list of values


        # Generate row names for simulated output (row names reflect parameter values used in simulation)

        row_names = []
        row_names_short = []

        row_numbers = np.arange(0, p_combinations.shape[0] / runs_per_subject)
        row_numbers = np.repeat(row_numbers, runs_per_subject)
        row_numbers = [str(i).zfill(len(str(p_combinations.shape[0]))) for i in row_numbers]

        runs = np.arange(0, runs_per_subject)
        runs = np.tile(runs, p_combinations.shape[0] / runs_per_subject)

        for i in range(0, p_combinations.shape[0]):
            row_names_short.append(str(p_combinations[i, :]))
            rname = [str(x) for t in zip(self.sim_learning_parameters.keys() + self.sim_observation_parameters.keys(),
                                         p_combinations[i, :]) for x in t]
            row_names.append('{0}_'.format(row_numbers[i]) + '.'.join(rname))


        # Set up parameters
        # Parameters need to be given to scan as arrays - here we take them out of their dictionaries and make sure
        # they're in the correct format (i.e. float64)

        sim_dynamic = []
        sim_static = []
        sim_observation = []
        sim_observation_dynamic = []  # TODO does this get used?

        for n, i in enumerate(self.learning_parameters):
            match = False
            for p, v in self.sim_learning_parameters.iteritems():
                if p == i.name:
                    if i.dynamic:
                        sim_dynamic.append(np.float64(v))
                    else:
                        sim_static.append(np.float64(v))
                    match = True
            if not match:
                raise ValueError("Parameter {0} has no value provided".format(i.name))

        if self.observation_model is not None:
            for i in self.observation_parameters:
                match = False
                for p, v in self.sim_observation_parameters.iteritems():
                    if i.name == p:
                        sim_observation.append(v)
                        match = True
                if not match:
                    raise ValueError("Parameter {0} has no value provided".format(i.name))


        # Make sure outcomes are the right format

        outcomes = np.array(outcomes)

        if len(outcomes.shape) < 2:  # if one dimensional, make two-dimensional
            outcomes = outcomes.reshape((1, outcomes.shape[0]))

        if outcomes.shape[0] < p_combinations.shape[0]:
            warnings.warn("Fewer outcome lists than simulated subjects, attempting to use same outcomes for each "
                          "subject", Warning)
            try:
                outcomes = np.tile(outcomes, (p_combinations.shape[0] / outcomes.shape[0], 1))
            except:
                raise ValueError("Unable to repeat outcome arrays to match number of subjects, make sure to either "
                                 "provide a unique list of outcomes for each subject or make sure the number of "
                                 "simulated subjects is divisible by the number of outcomes. Number of outcome arrays"
                                 " = {0}, number of simulated subject = {1}".format(outcomes.shape[0], p_combinations.shape[0]))

        if not (outcomes.shape[0] == p_combinations.shape[0] or outcomes.shape[1] == p_combinations.shape[0]):
            raise ValueError("Number of outcome lists provided does not match number of subjects")

        if not params_from_fit:  # this could fail in some circumstances
            outcomes = outcomes.T

        if outcomes.shape[1] == n_subjects:
            outcomes = outcomes.T

        # Create theano shared variables and scan function
        # when simulation is run for the first time, a theano function based on a scan loop is created and stored
        # this gets reused for subsequent simulations (until the model is redeclared) - this saves compiling on each
        # simulation and speeds the process up a lot

        outputs_info = sim_dynamic + [None] * (self.__n_learning_returns - len(sim_dynamic))
        time = np.tile(np.arange(0, outcomes.shape[0]), (outcomes.shape[1], 1)).T

        sim_static = [np.array([i]) if not isinstance(i, np.ndarray) else i for i in sim_static]
        outputs_info = [np.array([i]) if not isinstance(i, np.ndarray) and i is not None else i for i in outputs_info]

        if self._simulate_function == None:

            # define theano tensors
            time_theano = T.matrix("time")
            outcomes_theano = T.matrix("outcomes")
            sim_static_theano = []
            outputs_info_theano = []
            sim_observation_theano = []

            for n, i in enumerate(outputs_info):
                if i is None:
                    outputs_info_theano.append(None)
                else:
                    outputs_info_theano.append(T.vector("outputs_info_{0}".format(n)))

            for n, i in enumerate(sim_static):
                sim_static_theano.append(T.vector("sim_static_{0}".format(n)))

            for n, i in enumerate(sim_observation):
                sim_observation_theano.append(T.vector("sim_observation_{0}".format(n)))

            # sequences for scan should be in format (n_trials, n_subjects)

            value, updates = scan(fn=self.learning_model,
                                  sequences=[dict(input=outcomes_theano), dict(input=time_theano)],
                                  outputs_info=outputs_info_theano,
                                  non_sequences=sim_static_theano)

            for n, i in enumerate(outputs_info_theano):
                if i is not None:
                    value[n] = T.vertical_stack(i.reshape((1, i.shape[0])), value[n][:-1, :])

            if self.observation_model is not None:
                observation_dynamics = [value[i] for i in self.__observation_dynamic_inputs]
                prob, obs_outs = self.observation_model(*observation_dynamics + sim_observation_theano)
            else:
                prob, obs_outs = theano.shared(np.nan), []

            if len(obs_outs) == 0:
                obs_outs = theano.shared(np.nan)

            out = [T.as_tensor_variable(list(value)), prob, obs_outs]

            self._simulate_function = theano.function(inputs=[outcomes_theano, time_theano] +
                                                              sim_static_theano +
                                                             [i for i in outputs_info_theano if i is not None] +
                                                             sim_observation_theano,
                                                      outputs=out, updates=updates)

        # Call the function

        # if single_parameter_values:  # temporary(ish) solution for shape problem with single values
        #     outcomes = outcomes.T

        sim_data = self._simulate_function(outcomes, time, *(sim_static + [i for i in outputs_info if i is not None] +
                                                             sim_observation))

        # Get the results

        prob = sim_data[1]

        # The simulation function outputs nan for prob and obs_outs if they're not present
        if not np.isnan(sim_data[2]):
            obs_outs = sim_data[2]
        else:
            obs_outs = []

        if np.isnan(prob):
            prob = None

        # Create a list of returned values
        returned_values = [sim_data[0][i, ...] for i in range(sim_data[0].shape[0])] + list(obs_outs)
        value = returned_values[0]  # Handy as we refer to the estimated value a lot

        outcomes = outcomes.T  # transpose outcomes to make plotting easier

        # Add noise
        if output_value:
            value = _add_noise(value, noise_mean, noise_sd, lower_bound=np.min(outcomes), upper_bound=np.max(outcomes))
        elif prob is not None:
            prob = _add_noise(prob, noise_mean, noise_sd, lower_bound=np.min(outcomes), upper_bound=np.max(outcomes))

        # check for nans and warn if found
        if (prob is not None and np.any(np.isnan(prob))) or np.any(np.isnan(returned_values[0])):
            warnings.warn("NaNs present in estimated probabilities. Parameter values are likely in invalid ranges or "
                          "there is an error in the model code.")


        if self.observation_model is not None:
            return_names = self.__learning_returns + self.__observation_returns[1:]
        else:
            return_names = self.__learning_returns
        result_dict = dict(zip(return_names, returned_values))
        if prob is not None:
            result_dict['P'] = prob

        # Create a dictionary of simulated values
        self.simulated = dict(sim_learning_params=self.sim_learning_parameters,
                              sim_obs_params=self.sim_observation_parameters,
                              sim_results=result_dict)

        return_names = [i.replace('[', '') for i in return_names]
        return_names = [i.replace(']', '') for i in return_names]

        if plot:

            fontweight='normal'

            sns.set_palette(palette)

            # if len(prob.shape) < 2:
            #     sns.set_palette(sns.color_palette(['#348ABD']))

            fig, axarr = plt.subplots(len(returned_values), 1, figsize=(8, 1.5 * len(returned_values)))

            try:
                if prob is not None:
                    pal = sns.color_palette(palette, prob.shape[1])
                else:
                    pal = sns.color_palette(palette, returned_values[0].shape[1])
            except IndexError:
                pal = sns.color_palette(palette, 1)

            # plot value

            if value is not None and len(value.shape) < 2 and plot_value:
                value = value.reshape((value.shape[0], 1))

            if plot_value:
                for n in range(0, value.shape[1]):
                    axarr[0].plot(value, label='Value', c=pal[n])
                    axarr[0].set_title('Value', fontweight=fontweight)

            # plot probability

            if prob is not None and len(prob.shape) < 2:
                prob = prob.reshape((prob.shape[0], 1))

            if prob is not None:
                for n in range(0, prob.shape[1]):
                    axarr[0].plot(prob[:, n], label=str(p_combinations[n]), c=pal[n])
                axarr[0].set_title('Choice probability', fontweight=fontweight)

            if plot_choices:
                axarr[0].scatter(np.arange(0, outcomes.shape[0]), generate_choices2(prob), color='#72a23b', alpha=0.5,
                                 label='Simulated choices')

            if responses is not None:
                axarr[0].scatter(np.arange(0, outcomes.shape[0]), responses, color='#f18f01', alpha=0.5, label='Observations')

            if plot_outcomes:
                plotting_outcomes = np.unique(outcomes, axis=0)
                if len(plotting_outcomes.shape) < 2:
                    outcomes = plotting_outcomes.reshape((1, plotting_outcomes.shape[0]))
                outcome_pal = sns.dark_palette('red', plotting_outcomes.shape[0], reverse=True)
                for o in range(0, plotting_outcomes.shape[0]):
                    axarr[0].scatter(np.arange(0, plotting_outcomes.shape[1]), plotting_outcomes[o, :], alpha=0.5,
                                     label='Outcomes', c=outcome_pal[o])

            axarr[0].set_xlim(0, outcomes.shape[1])
            axarr[0].set_ylim(np.min(outcomes) - 0.5, np.max(outcomes) + 0.2)

            if legend:
                axarr[0].legend(frameon=True, fancybox=True)

            # plot other estimated values
            for i in range(1, len(returned_values)):
                plot_values = np.array(returned_values[i])
                if len(plot_values.shape) < 2:
                    plot_values = plot_values.reshape((plot_values.shape[0], 1))
                for n in range(0, plot_values.shape[1]):
                    axarr[i].plot(plot_values[:, n], c=pal[n])
                    axarr[i].set_xlim(0, len(plot_values))
                    axarr[i].set_title(return_names[i], fontweight=fontweight)

            plt.tight_layout()

        if plot_against_true:

            responses_eval = self.responses.eval()

            fontweight='normal'

            if self.fit_complete:

                for n, sub in enumerate(self.subjects):

                    n_runs = int(n_runs)
                    fig, axarr = plt.subplots(n_runs, 1, figsize=(8, 1.5 * n_runs))

                    if n_runs > 1:

                        for i in range(n_runs):

                            index = n * n_runs + i
                            axarr[i].plot(value.T[index], label='Model')
                            if response_format == 'continuous':
                                axarr[i].plot(responses_eval[index], label='Data', color='#3d3d3d')
                            elif response_format == 'discrete':
                                axarr[i].scatter(range(len(responses_eval[index])), responses_eval[index],
                                                 label='Data', color='#3d3d3d')

                            axarr[i].legend(frameon=True)

                            axarr[0].set_title("Subject {0}".format(sub), fontweight=fontweight)
                            axarr[n_runs - 1].set_xlabel("Trial")

                    else:

                        index = n * n_runs
                        axarr.plot(value.T[index], label='Model')
                        if response_format == 'continuous':
                            axarr.plot(responses_eval[index], label='Data', color='#3d3d3d')
                        elif response_format == 'discrete':
                            axarr.scatter(range(len(responses_eval[index])), responses_eval[index],
                                          label='Data', color='#3d3d3d')

                        axarr.legend(frameon=True)

                        axarr.set_title("Subject {0}".format(sub), fontweight=fontweight)
                        axarr.set_xlabel("Trial")

                    plt.tight_layout()

            else:
                warnings.warn("Model not fit, unable to plot simulated against true values")

        if prob is not None:
            choices = generate_choices2(prob)
        else:
            choices = generate_choices2(returned_values[0])
        self.simulated['sim_results']['choices'] = choices

        print "Finished simulating"
        sns.set_palette("deep")

        if len(output_file):
            if output_value:  # need a better way to return raw prob / choices / value
                simulated_responses(value, outcomes.T, row_names, runs, output_file,
                                    (self.sim_learning_parameters.keys(), p_combinations),
                                    (self.sim_observation_parameters.keys(), p_combinations))
            else:
                simulated_responses(choices, outcomes.T, row_names, runs, output_file,
                                    (self.sim_learning_parameters.keys(), p_combinations),
                                    (self.sim_observation_parameters.keys(), p_combinations))

        return self.simulated, output_file

    def recovery(self):

        if self.sims is None:
            raise AttributeError("Response file provided for model fitting does not include simulated parameter values")

        if self.parameter_table is None:
            raise AttributeError("The model has not been fit, this is necessary to run parameter recovery tests")

        sns.set_palette("deep")
        self.sims = self.sims.reset_index(drop=True)
        fit_params = [i for i in self.parameter_table.columns if not 'sd_' in i and not 'Subject' in i]
        self.parameter_table = pd.merge(self.parameter_table, self.sims, on='Subject')
        print "Performing parameter recovery tests..."
        parameter_values = []
        parameter_values_sim = []
        n_p_free = len(fit_params)

        fontweight = 'normal'

        # SCATTER PLOTS - CORRELATIONS
        f, axarr = plt.subplots(1, n_p_free, figsize=(4 * n_p_free, 4))
        for n, p in enumerate(fit_params):  # this code could all be made far more efficient
            if p.replace('mean_', '') + '_sim' not in self.sims.columns:
                raise ValueError("Simulated values for parameter {0} not found in response file".format(p))
            parameter_values.append(self.parameter_table[p])
            parameter_values_sim.append(self.parameter_table[p.replace('mean_', '') + '_sim'])
            if n_p_free > 1:
                ax = axarr[n]
            else:
                ax = axarr

            sns.regplot(self.parameter_table[p.replace('mean_', '') + '_sim'], self.parameter_table[p], ax=ax)
            eq_line_range = np.arange(np.min([ax.get_ylim()[0], ax.get_xlim()[0]]),
                                      np.min([ax.get_ylim()[1], ax.get_xlim()[1]]), 0.01)
            ax.plot(eq_line_range, eq_line_range, linestyle='--', color='black')
            ax.set_xlabel('Simulated {0}'.format(p), fontweight=fontweight)
            ax.set_ylabel('Estimated {0}'.format(p), fontweight=fontweight)
            ax.set_title('Parameter {0} correlations, '
                         'R2 = {1}'.format(p, np.round(r2(self.parameter_table[p.replace('mean_', '') + '_sim'],
                                                          self.parameter_table[p]), 2)), fontweight=fontweight)

            sim_min = np.min(self.parameter_table[p.replace('mean_', '') + '_sim'])
            sim_max = np.max(self.parameter_table[p.replace('mean_', '') + '_sim'])
            true_min = np.min(self.parameter_table[p])
            true_max = np.max(self.parameter_table[p])
            ax.set_xlim([sim_min - np.abs(sim_min) / 10., sim_max + np.abs(sim_max) / 10.])
            ax.set_ylim([true_min - np.abs(true_min) / 10., true_max + np.abs(true_max) / 10.])

            sns.despine()
        plt.tight_layout()

        ## SIMULATED-POSTERIOR CORRELATIONS
        if len(self.parameter_table) > 1:
            if np.sum(np.diff(parameter_values_sim)) == 0:
                warnings.warn("Parameter values used across simulations are identical, unable to calculate "
                              "correlations between simulated and estimated parameter values. Try providing a "
                              "range of parameter values when simulating.")
                se_cor = None
            else:
                se_cor = np.corrcoef(parameter_values, parameter_values_sim)[n_p_free:, :n_p_free]
                fig, ax = plt.subplots(figsize=(n_p_free * 3, n_p_free * 2.5))
                cmap = sns.diverging_palette(220, 10, as_cmap=True)
                sns.heatmap(se_cor, cmap=cmap, square=True, linewidths=.5, xticklabels=fit_params,
                            yticklabels=fit_params, annot=True)  # order might not work here
                ax.set_xlabel('Simulated', fontweight=fontweight)
                ax.set_ylabel('True', fontweight=fontweight)
                ax.set_title("Simulated-Posterior correlations", fontweight=fontweight)

            ## POSTERIOR CORRELATIONS
            ee_cor = np.corrcoef(parameter_values, parameter_values)[n_p_free:, :n_p_free]
            fig, ax = plt.subplots(figsize=(n_p_free * 3, n_p_free * 2.5))
            cmap = sns.diverging_palette(220, 10, as_cmap=True)
            sns.heatmap(ee_cor, cmap=cmap, square=True, linewidths=.5, xticklabels=fit_params,
                        yticklabels=fit_params, annot=True)  # order might not work here
            ax.set_xlabel('Estimated', fontweight=fontweight)
            ax.set_ylabel('Estimated', fontweight=fontweight)
            ax.set_title("Posterior correlations", fontweight=fontweight)

        else:
            se_cor = None
            warnings.warn('Only one parameter value provided, cannot perform recovery correlation tests')

        return se_cor  # TODO add ee cor


class Parameter():

    def __init__(self, name, distribution, lower_bound=None, upper_bound=None, mean=1., variance=None, dynamic=False,
                 **kwargs):

        if distribution == 'uniform' and (lower_bound == None or upper_bound == None):
            raise ValueError("Must specify upper and lower bounds for parameters with uniform distribution")

        elif distribution == 'fixed' and (variance != None and lower_bound != None and upper_bound != None):
            warnings.warn("Parameter is specified as fixed, ignoring variance & bounds")
            self.fixed = True
        elif distribution == 'fixed':
            self.fixed = True
        else:
            self.fixed = False

        self.name = name
        self.distribution = distribution
        self.dynamic = dynamic
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.__pymc_kwargs = kwargs

        if lower_bound:
            self.lower_bound = lower_bound
        if upper_bound:
            self.upper_bound = upper_bound

        self.mean = mean
        self.variance = variance

        self.transform_method = None




