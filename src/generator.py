import pandas as pd
import numpy as np
import os
import yaml
import time
import random
from enum import Enum
import statsmodels.tsa.api as smt
import logging
import multiprocessing
from functools import partial
from src import generator_utils as gutils
from src.model import Model

MODEL_DICT_NAMES = 'fitted_'

# Logger
logging.basicConfig()
logging.getLogger().setLevel(logging.INFO)


class Switch(Enum):
    NONE = -1
    GRADUAL = 0
    ABRUPT = 1
    PREDEFINED = 2


def parse_yaml():
    """ This function parses the config file and returns options, paths, etc."""
    # Read YAML file
    with open("config.yaml", 'r') as stream:
        config = yaml.safe_load(stream)
        input_data_config = config['input']
        global_params = config['params']
        out_format = config['output']
        plot = config['plot']
        armagarch_lib = {'lib': 'rugarch', 'env': config['env']['r_libs_path']}
        print(config)

    return input_data_config, global_params, out_format, armagarch_lib, plot


def instantiate_model(config, show_plt, file_config):
    """
    This handles each thread in 'instantiate_models'
    :param config: from YAML file
    :param show_plt: plot series?
    :param file_config: list of ids, files and probabilities.
    :return: model and desc tuple
    """
    # 1. Read dataset for model
    counter, file, preconf, prob = file_config
    df = pd.read_csv(os.path.join(config['path'], file), sep=';')  # , header=None)
    # df.columns = config['cols']
    df.set_index(keys=config['index_col'], drop=True, inplace=True)

    # 2. Clean nulls and select series
    raw_series = df[[config['sim_col']]]  # .dropna()
    raw_returns_series = 100 * df[[config['sim_col']]].pct_change()  # .dropna()
    # the returns later are calculated in a different way and they use log scale

    # Plot initial df and returns
    if show_plt:
        gutils.plot_input(df, 'Raw dataset')
        gutils.plot_input(raw_series, 'Prices')
        gutils.plot_input(raw_returns_series, 'Returns')

    # 3. Prepare Model and return it to be added to a dictionary
    return Model(id=counter, raw_input_path=os.path.join(config['path'], file),
                 input_ts=gutils.prepare_raw_series(config['parsing_mode'], raw_series),
                 rec_price=list(raw_series[config['sim_col']])[-1],  # last fitting price will be used for reconstruction
                 probability=prob,
                 ARMAGARCH_preconf=preconf),  f'{MODEL_DICT_NAMES}{counter}'


def instantiate_models(config: dict(), show_plt: bool = True):
    """
    This function loads the initial series to feed them to models.
    :param config: from YAML file
    :param show_plt: plot series?
    :return: dict of series
    """
    # Load raw time series for the pre-training of the models
    logging.info('Load models...')
    pool = multiprocessing.Pool(len(config['files']))  # gutils.MyPool(1)
    mapped = pool.map(partial(instantiate_model, config, show_plt), config['files'])
    series_dict = dict(map(reversed, tuple(mapped)))
    return series_dict


def get_best_arma_parameters(ts: list(), config: dict()):
    """
    If selected, this list returns the best ARMA model for the current pre-training period.
    Cos and ARMA-GARCH(1,1) may be good enough:
        https://stats.stackexchange.com/questions/175400/optimal-lag-order-selection-for-a-garch-model
    @:param TS: time series of returns used for pre-training
    """
    best_aic = np.inf
    best_order = None
    best_mdl = None

    for i in range(1, config['pq_rng'] + 1):   # [0,1,2,3,4]
        for d in range(config['d_rng']):  # [0] # we'll use arma-garch, so not d (= 0)
            for j in range(1, config['pq_rng'] + 1):
                try:
                    tmp_mdl = smt.ARIMA(ts, order=(i, d, j)).fit(
                        method='mle', trend='nc'
                    )
                    tmp_aic = tmp_mdl.aic
                    if tmp_aic < best_aic:
                        best_aic = tmp_aic
                        best_order = (i, d, j)
                        best_mdl = tmp_mdl
                except:
                    continue
    print('aic: {:6.5f} | order: {}'.format(best_aic, best_order))
    return best_aic, best_order, best_mdl


def fit_model(show_plt: bool, tool_params: dict(), armagarch_lib: dict(), series_model):
    """
    This handles each thread in 'fit_models'
    :param tool_params: YAML dict with model params
    :param armagarch_lib: library name and environment paths to load an R library for ARMA-GARCH
    :param show_plt: plot series?
    :param series_model: list of series as an object of Model.
    :return: fitted model and description to be added to dictionary
    """
    name_series, current_model = series_model
    # logging.info(f'\n\n 1. Setting ARMAGARCH library for model {current_model.id}')
    if tool_params['param_search'] == 'ARMA':
        _, ARMA_order, ARMA_model = get_best_arma_parameters(ts=list(current_model.input_ts), config=tool_params)  #
        # ARMA_order = (4, 0, 4)
        print(current_model.id)
        print('Best parameters are: ')
        current_model.set_lags(ARMA_order[0], ARMA_order[1], ARMA_order[2])
        print(f'{current_model.get_lags()}')

        if show_plt:
            gutils.tsplot(ARMA_order.resid, lags=30)
            gutils.tsplot(ARMA_order.resid ** 2, lags=30)

        logging.info(f'\n\n 2. Start fitting process for {current_model.id}')

        # Now we can fit the arch model using the best fit ARIMA model parameters. 'o' not in ARMAGARCH
        current_model.fit(current_model.input_ts, armagarch_lib, current_model.p, current_model.q)

    elif tool_params['param_search'] == 'ARMA_GARCH':
        best_aic, best_order, best_model = current_model.get_best(current_model.input_ts, tool_params, armagarch_lib)
        current_model.set_lags(*best_order)
        current_model.set_spec_from_model(best_model)
        # current_model.fit(current_model.input_ts, armagarch_lib,
        #                   current_model.p, current_model.q, current_model.g_p, current_model.g_q)  # not needed
        logging.info('model {} -> aic: {:6.5f} | order: {}'.format(current_model.id, best_aic, best_order))
    else:
        logging.critical('param_search must be provided in config.yaml. Values should be "ARMA" or "ARMA_GARCH"\n\n')

    return current_model, name_series  # name_series = f'{MODEL_DICT_NAMES}{counter}'


def fit_models(series_dict: dict(), input_data_conf: dict(), params: dict(),
               armagarch_lib: dict(), show_plt: bool = False):
    """
    This function triggers the selection of the best parameters and fitting of n models
     (one model per dataset added to the YAML config file).
    :param series_dict - datasets as a single column DF to fit the models
    :param armagarch_lib: library name and environment paths to load an R library for ARMA-GARCH
    :param input_data_conf - dictionary from YAML with input datasets-related configuration
    :param params: YAML dict with model params
    :param plot - plot model?
    :return list of fitted models
    """
    # Fit models in parallel
    logging.info('Fitting models...')
    n_threads = 1
    pool = gutils.MyPool(n_threads)  # multiprocessing.Pool(processes=len(input_data_conf['files']))
    mapped = pool.map(partial(fit_model, show_plt, params, armagarch_lib), series_dict.items())
    return dict(map(reversed, tuple(mapped)))


def update_weights(w, switch_sharpness):
    """
    This function updates weights each iteration depending on the sharpness of the current switch.
    :param w: tuple of weights
    :param switch_sharpness: speed of changes
    :return: tuple of weights updated.
    """
    if switch_sharpness < 0.1:
        print('Minimum switch abrupcy is 0.1, so this is the value being used. ')
        switch_sharpness = 0.1
    incr = switch_sharpness
    w = (w[0] - incr, w[1] + incr)

    # see for reference get_weight and reset_weights.
    w = (0 if w[0] <= 0 else w[0], 1 if w[1] >= 1 else w[1])  # deal with numbers out of range
    return w


def reset_weights():
    """
    This function init weights (or different, depending of gradual or abrupt drifts)
    :return default/initial weight.
    """
    w = (1, 0)  # Initialize
    return w


def get_event_dict(counter, current_model, new_model, new_switch_type, switch_type, tool_params, w):
    return {'n_row': counter,
            'new_switch': new_switch_type.name,
            'cur_switch': switch_type.name if switch_type.name != 'PREDEFINED'
            else '_'.join([switch_type.name, str(int(100/(tool_params['defined_drift_sharpness']*100)) - 1)]),
            'weights': w,
            'current_model_id': current_model.id,  # Add p,o,q to this?
            'new_model_id': -1 if new_model is None else new_model.id}


def random_switch(switch_prob: float, abrupt_prob: float):
    """
    This function flips coins and return if a drift should be triggered and its type.
    :param switch_prob
    :param abrupt_prob
    :return switch event that takes in place. integer represented by an enum.
    """
    if random.random() < switch_prob:
        # Switch ?
        if random.random() < abrupt_prob:
            return Switch.ABRUPT
        else:
            return Switch.GRADUAL
    else:
        # Otherwise
        return Switch.NONE


def start_switch(counter, conf):
    """
    This function manages the decision of switching from one model to another.
    """
    conf['defined_drift_sharpness'] = None

    if conf['use_transition_map']:
        # Set no-switch by default
        switch_shp = (conf['gradual_drift_sharpness'], conf['abrupt_drift_sharpness'], conf['defined_drift_sharpness'])
        new_switch_type, switch_shp, conf, switch_to = Switch.NONE, switch_shp, conf, None

        for (it, length, to_mdl) in conf['transition_map']:
            if counter == it:
                new_switch_type = Switch.PREDEFINED
                conf['defined_drift_sharpness'] = 100.0 / float(length + 1) / 100.0
                switch_shp = (conf['gradual_drift_sharpness'], conf['abrupt_drift_sharpness'],
                              conf['defined_drift_sharpness'])
                switch_to = to_mdl
                return new_switch_type, switch_shp, conf, switch_to

            elif counter < it:
                return new_switch_type, switch_shp, conf, switch_to
    else:
        # No new switch if there is one already in progress
        new_switch_type = random_switch(conf['switching_probability'], conf['abrupt_drift_prob'])

    switch_shp = [conf['gradual_drift_sharpness'], conf['abrupt_drift_sharpness'], conf['defined_drift_sharpness']]
    return new_switch_type, switch_shp, conf, None


def get_new_model(current_id: int, config: dict()):
    """
    This function picks a new model based in their probability to be selected (equal for all by now).
    :param current_id - so there is an actual drift and the id is not repeated.
    :param config - for probabilities
    :return new model id
    """
    # NOT TO BE DEVELOPED (YET)
    # Just here in the case of having different probabilities of transitioning per series.
    # for i in range(len(config)):
    # for i in range(len(config)):
    #     config[i][1]  # TOD: ENUMERATOR SO TRANSITION_PROBABILITIES_POS == 1
    new_model_id = random.randrange(1, len(config)+1)
    return get_new_model(current_id, config) if current_id == new_model_id else new_model_id


def switching_process(tool_params: dict(), models: dict(), data_config: dict(), armagarch_lib, show_plt: bool):
    """
    This function computes transitions between time series and returns the resulting time series.
    :param tool_params: info regarding to stitches from yaml file
    :param models: fitted models
    :param data_config: datasets info from yaml file
    :param show_plt: plot resulting ts?
    :param armagarch_lib: TSpackage for R library to use
    :return: ts - series generated
    :rerurn: rc - dataframe of events (switches flagged, models used and weights)
    """
    # Init params
    switch_type = Switch.NONE
    no_switch = Switch.NONE, None, tool_params, None
    use_sig_w = tool_params['w_func'] == 'sig'

    # Start with model A as initial model
    current_model = models[f'{MODEL_DICT_NAMES}{1}']  # first model -> current_model = A (randomly chosen)
    new_model = None

    # Initialize main series
    ts = list()  # rec_ts = list()
    rc = list()
    w = reset_weights()  # tuple (current, new) of model weights.
    sig_w = reset_weights()
    state_counter = 0
    # tool_params['transition_map']
    logging.info('Start of the context-switching generative process:')
    for it_counter in range(tool_params['periods']):
        # 1 Start forecasting in 1 step horizons using the current model
        old_model_forecast = current_model.forecast(list(current_model.input_ts)
                                                    if it_counter < max(current_model.get_lags()) else list(ts),
                                                    armagarch_lib)
        new_switch_type, new_switch_shp, tool_params, switch_to = no_switch \
            if (0 < w[1] < 1 or state_counter <= tool_params['min_model_len']) \
            else start_switch(it_counter, tool_params)

        # 2 In case of switch, select a new model and reset weights: (1.0, 0.0) at the start (no changes) by default.
        if new_switch_type.value >= 0:
            logging.info(f'There is a {new_switch_type.name} switch.')
            switch_type, switch_shp = new_switch_type, new_switch_shp
            # 'switch_to' is only used if transition_maps are enabled.
            new_mdl_number = get_new_model(current_model.id, data_config["files"]) if switch_to is None else switch_to
            new_model = models[f'{MODEL_DICT_NAMES}{new_mdl_number}'] \

            w = update_weights(w=reset_weights(), switch_sharpness=switch_shp[switch_type.value])
            sig_w = (gutils.get_sigmoid()[int(w[0]*100)], 1 - gutils.get_sigmoid()[int(w[0]*100)])  # kernel to sig func

        # 3 Log switches and events
        rc.append(get_event_dict(it_counter, current_model, new_model, new_switch_type, switch_type, tool_params,
                                 sig_w if use_sig_w else w))
        assert (sig_w[0] + sig_w[1] if use_sig_w else w[0] + w[1]) == 1

        # 4 if it's switching (started now or in other iteration), then forecast with new model and get weighted average
        if 0 < w[1] < 1:
            # print('Update weights:')
            # Forecast and expand current series (current model is the old one, this becomes current when weight == 1)
            new_model_forecast = new_model.forecast(list(new_model.input_ts)
                                                    if it_counter < max(new_model.get_lags()) else list(ts),
                                                    armagarch_lib)
            ts.append(old_model_forecast * (sig_w[0] if use_sig_w else w[0]) +
                      new_model_forecast * (sig_w[1] if use_sig_w else w[1]))

            w = update_weights(w, switch_shp[switch_type.value])
            sig_w = (gutils.get_sigmoid()[int(w[0]*100)], 1 - gutils.get_sigmoid()[int(w[0]*100)])  # kernel to sig func
            # print(sig_w)

            if w[1] == 1:
                current_model = new_model
                new_model = None
                state_counter = 0  # reset of counter for duration of model
                w = reset_weights()
                sig_w = reset_weights()
                switch_type = Switch.NONE

        # 3. Otherwise, use the current forecast
        else:
            ts.append(old_model_forecast)

        state_counter = state_counter + 1
        # logging.info(f'Period {it_counter}: {ts}')

    # 4 Plot simulations
    if show_plt:
        gutils.plot_results(ts)

    return pd.Series(ts),  pd.DataFrame(rc)


def prepare_and_export(global_params, output_format, rc, ts, reconstruction_price):
    """
    This function reconstruct prices, adds noise and and exports a csv
    :param global_params: config params
    :param output_format: info about file to be exported
    :param rc: registered events
    :param ts: time series generated
    :param reconstruction_price: price for reconstruction
    :return:
    """
    logging.info('Reconstructing prices and adding noise...')
    rc['ret_ts'] = ts

    # 5.1 noise over returns
    ts_gn, ts_snr = gutils.add_noise(global_params['white_noise_level'], list(rc['ret_ts']))

    # 5.2 reconstruction
    rc['ts'] = gutils.reconstruct(ts, init_val=reconstruction_price)
    # rc['ts_mult'] = gutils.reconstruct(ts * 5, init_val=reconstruction_price)
    # Gaussian noise & reconstruct
    rc['ts_n1_pre'] = gutils.reconstruct(ts_gn, init_val=reconstruction_price)
    # SNR and White Gaussian Noise & reconstruct
    rc['ts_n2_pre'] = gutils.reconstruct(ts_snr, init_val=reconstruction_price)

    # 5.3 noise post-reconstruction (over prices)
    ts_gn, ts_snr = gutils.add_noise(global_params['white_noise_level'], list(rc['ts']))
    rc['ts_n1_post'] = ts_gn  # Gaussian noise
    rc['ts_n2_post'] = ts_snr  # SNR and White Gaussian Noise

    # 6 Final simulation (TS created) and a log of the regime changes (RC) to CSV files
    rc[output_format['cols']].to_csv(os.sep.join([output_format['path'],
                                                  output_format['ts_name'] + str(int(time.time())) + '.csv']),
                                     index=False)


def compute():
    """
    This function coordinates the whole process.
    1. It loads examples series and pre-train ad many models as series received.
    2. It triggers the switching and generation process and plots the resulting series.
    3. It adds white and gaussian noise.
    4. It exports the resulting time series without and with noise, and the events/switches to a CSV.
    """
    # 0 Read from YAML file
    input_data_config, global_params, output_format, armagarch_lib, plt_flag = parse_yaml()

    # 1 Get dict of series and their probabilities calling instantiate_models.
    #   The objects in this dictionary contain series of returns on log scale.
    # 2 Then, pre-train GARCH models by looking at different series
    models_dict = fit_models(series_dict=instantiate_models(config=input_data_config, show_plt=plt_flag),
                             input_data_conf=input_data_config,
                             params=global_params, armagarch_lib=armagarch_lib, show_plt=plt_flag)

    # 3 Once the models are pre-train, these are used for simulating the final series.
    # At every switch, the model that generates the final time series will be different.
    ts, rc = switching_process(tool_params=global_params, models=models_dict,
                               data_config=input_data_config, armagarch_lib=armagarch_lib, show_plt=plt_flag)

    # 4 Plot simulations
    if plt_flag:
        gutils.plot_results(ts)

    # 5 Add noise (gaussian noise and SNR) pre-reconstruction, reconstruct prices and add noise post-reconstruction
    # 6 and export
    prepare_and_export(global_params, output_format, rc, ts,
                       reconstruction_price=models_dict['fitted_1'].rec_price)


if __name__ == '__main__':
    compute()
