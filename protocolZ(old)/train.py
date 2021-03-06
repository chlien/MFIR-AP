'''
1. Prepare dataset to read in directly for training.
2. Get data generators and loss function.
3. Get setup model and compile.
4. Train and save + Generate report data..
5. Report plots.
'''
from MFIRAP.d04_modelling.models import SETUP_DIC, SETUP_RGB_FLAGS, RGB_FEATURES_LAYER_NAME
from MFIRAP.d00_utils.project import MODEL_CONFIG_KEYS, TRAIN_LOG_FP
from MFIRAP.d04_modelling.metrics import Metrics_Keras, AUC_AP, Precision_AP, Recall_AP, PrecisionAtRecall_AP, Accuracy_AP
from MFIRAP.d04_modelling.training import Train_Validation_Generators, Timestamper_counted_down
from MFIRAP.d04_modelling.losses import Losses_Keras, zero_loss
from MFIRAP.d03_processing.batch_processing import intermediate2processed
import datetime
from tensorflow.keras.applications.resnet50 import ResNet50
import tensorflow as tf
import shutil
import os
import pickle
import argparse
import MFIRAP
import MFIRAP.d00_utils.io as io
import MFIRAP.d00_utils.dataset as ds
import MFIRAP.d00_utils.verbosity as vb
vb.VERBOSITY = vb.SPECIFIC

import MFIRAP.d05_model_evaluation.plots as plots
import matplotlib.pyplot as plt
import itertools
import numpy as np

K = tf.keras.backend


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('config_json_name', type=str)
    args = parser.parse_args()
    config_json_name = args.config_json_name.split(".json")[0]
    if not os.path.exists(TRAIN_LOG_FP):
        with open(TRAIN_LOG_FP, "w") as f:
            dt = datetime.datetime.now()
            f.write("Log started by {} on {} at {}. \n".format(
                config_json_name, dt.date(), dt.time()))
    # 1.
    # Read model
    model_config = io.read_json(os.path.join(
        "settings", config_json_name+".json"))
    for key in MODEL_CONFIG_KEYS:
        try:
            model_config[key]
        except KeyError:
            raise Exception(
                "Key {} not found in model configuration.".format(key))
    try:
        Setup = SETUP_DIC[model_config["setup"]]
    except KeyError:
        raise ValueError("Specified setup {} doesn't exist. Implemented setups: {}".format(
            model_config["setup"], SETUP_DIC))
    dataset_path, destination_parent_path = model_config[
        'dataset_intermediate_path'], model_config['dataset_processed_parent_path']
    processed_develop_path = os.path.join(
        destination_parent_path, os.path.split(dataset_path)[1])
    if os.path.exists(processed_develop_path):
        print("Dataset exists! Remove manually if needed and restart.")
    else:
        # Initialize feature extractor
        base_model = ResNet50(weights='imagenet',
                              pooling=max, include_top=False)
        intermediate2processed(dataset_path, destination_parent_path,
                               ds.read_development_subjects(), ds.read_test_subjects(), base_model)
    # 2.
    vb.print_specific("Creating training and validation data generators...")    
    plt.clf()
    generators = Train_Validation_Generators(dataset_path=processed_develop_path, view_IDs=model_config["view_IDs"], train_size=model_config[
                                             'train_size'], batch_size=model_config['batch_size'], RGB=SETUP_RGB_FLAGS[model_config["setup"]])
    train_generator = generators.get_train()
    valid_generator = generators.get_valid()
    vb.print_specific("Created training generator of lenght {} and validation generator of length {}".format(
        len(train_generator), len(valid_generator)))
    if not len(valid_generator):
        valid_generator = None
    vb.print_specific("Loss function: {}".format(
        model_config['loss_function']))
    # 3.
    # set up atta
    #never forget that! cd = counted_down
    train_timestamps_cd = K.variable(train_generator._get_batch_timestamps(0))
    train_timestamper = Timestamper_counted_down(
        train_timestamps_cd, trainining_generator=train_generator)
    timestampers = [train_timestamper]
    # settting up atta and other metrics
    metrics = Metrics_Keras(
        model_config["frames"], model_config["frame_shift"], train_timestamps_cd)
    atta_fnc = metrics.get_average_time_to_accident()
    ap_metrics = [AUC_AP(), Accuracy_AP(), Precision_AP(),
                  Recall_AP(), PrecisionAtRecall_AP(0.8)]
    losses = Losses_Keras(
        frames=model_config['frames'], frame_shift=model_config['frame_shift'])
    ##################
    # FINAL SETTINGS #
    ##################
    metrics = ap_metrics+[atta_fnc]
    callbacks = [] + timestampers
    optimizer = "adam"
    loss_fnc = losses.get_by_name(
        model_config['loss_function'], from_logits=False)
    epochs = model_config['epochs']
    if SETUP_RGB_FLAGS[model_config["setup"]]:
        try:
            model_config["pretraining_epochs"]
        except KeyError:
            raise Exception(
                "Key {} not found in model configuration.".format("pretraining_epochs"))
        pretraining_epochs = model_config['pretraining_epochs']
    # if RGB pretraining
    if SETUP_RGB_FLAGS[model_config["setup"]]:
        precompile_kwargs = {"loss": [zero_loss, loss_fnc], "loss_weights": [
            0, 1], "optimizer": optimizer}
        prefit_kwargs = {"x": train_generator, "epochs": epochs,
                         "validation_data": valid_generator, "callbacks": callbacks}
    else:
        precompile_kwargs = None
        prefit_kwargs = None
    compile_kwargs = {"loss": loss_fnc,
                      "optimizer": optimizer, "metrics": metrics}
    fit_kwargs = {"x": train_generator, "epochs": epochs,
                  "validation_data": valid_generator, "callbacks": callbacks}
    setup = Setup(name=config_json_name, compile_kwargs=compile_kwargs, fit_kwargs=fit_kwargs,
                  TPA_view_IDs=model_config['view_IDs'], pretraining=SETUP_RGB_FLAGS[model_config["setup"]], precompile_kwargs=precompile_kwargs, prefit_kwargs=prefit_kwargs)
    vb.print_specific(setup.model.summary())
    vb.print_specific("Compiling...")
    # 4. (Pretrain, ) train and save.
    setup.delete_existing_model_data_and_output()
    setup.train()
    setup.save()
    with open(TRAIN_LOG_FP, "a") as f:
        dt = datetime.datetime.now()
        f.write("Model {} trained on {} at {}\n".format(
            config_json_name, dt.date(), dt.time()))
    # 4A. Copy mu, sigma for scaling during testing
    shutil.copy2(os.path.join(processed_develop_path, "scaling.pkl"),
                 os.path.join(setup.data_models_model_path, "scaling.pkl"))
    # 5. Generate plots.
    setup.plot_metrics(plot_val_metrics=valid_generator)
    # Get optimal threshold.
    preds_list, trues_list = [], []
    generators = [train_generator, valid_generator] if valid_generator else [train_generator]
    for generator in generators:
        for i in range(len(generator)):
            x, y = generator[i]
            preds_list.append(setup.model.predict(x))
            trues_list.append(y)
    preds = np.vstack(preds_list)
    trues = np.vstack(trues_list)
    labels_dict, predictions_dict = {}, {}
    for idx, l in enumerate(zip(preds, trues)):
        pred, true = l
        predictions_dict[idx] = pred[:, 1]
        sample_class = true[-1][-1]
        labels_dict[idx] = model_config["frames"]-model_config["frame_shift"] if sample_class else -1
    prc_pre_fpr, prc_pre_tpr, prc_pre_thresholds = plots.prediction_pr_curve(labels_dict, predictions_dict)

    # get optimal threshold
    fpr, tpr, thresh = prc_pre_fpr[:-1], prc_pre_tpr[:-1], prc_pre_thresholds
    xy = np.stack([fpr, tpr]).T
    ideal = np.array([1,1])
    d = ideal-xy
    D = (d*d).sum(axis=-1)
    optimal_threshold= thresh[D.argmin()]
    with open(os.path.join(setup.data_models_model_path, "threshold.pkl"), "wb") as f:
        pickle.dump(optimal_threshold, f)