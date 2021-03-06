"""
Implementation of the STORN model from the paper.
"""
import logging
import numpy as np
import time
import os
import json

import keras.backend as K
from keras.callbacks import ModelCheckpoint, EarlyStopping, RemoteMonitor, TensorBoard
from keras.models import Model
from keras.optimizers import Adam
from keras.layers import Input, TimeDistributed, Dense, Dropout, GRU, SimpleRNN, Concatenate, LSTM
from keras.layers import deserialize
from greenarm.models.keras_fix.lambdawithmasking import LambdaWithMasking
from greenarm.models.loss.variational import keras_variational_func
from greenarm.models.sampling.sampling import sample_gauss, sample_bernoulli
from greenarm.util import add_samples_until_divisible, get_logger

logger = get_logger(__name__)

RecurrentLayer = LSTM


# enum for different phases
class Phases:
    def __init__(self):
        pass

    predict = 1
    train = 2


class STORNModel(object):
    def __init__(self, latent_dim=7, data_dim=7, n_hidden_dense=50, n_hidden_recurrent=128, rec="gauss",
                 n_deep=6, dropout=0, activation='tanh', with_trending_prior=False, monitor=False, 
                 output_folder=None, prefix=None, embedding=None, learning_rate=0.001):
        # Tensor shapes
        self.data_dim = data_dim
        self.latent_dim = latent_dim

        # Network complexity
        self.n_hidden_dense = n_hidden_dense
        self.n_hidden_recurrent = n_hidden_recurrent
        self.n_deep = n_deep
        self.dropout = dropout
        self.activation = activation
        self.embedding = embedding
        self.rec = rec
        self.learning_rate = learning_rate

        # STORN options
        self.with_trending_prior = with_trending_prior

        # Model states
        self.z_prior_model = None
        self.z_recognition_model = None
        self.train_model = None
        self.predict_model = None

        # Misc
        self.monitor = monitor
        self.output_folder = output_folder or ""
        self.prefix = prefix or ""

    @classmethod
    def from_files(cls, model_file, weights_file, custom_objects=None, **kwargs):

        instance = cls(**kwargs)

        with open(model_file, "r") as f:
            params = json.load(f)
            instance.set_params(params, custom_objects=custom_objects)

        instance.build(batch_size=params.get("batch_size", 32))
        instance.load_predict_weights(weights_file)

        return instance

    def get_params(self):
        return {
            "data_dim": self.data_dim,
            "latent_dim": self.latent_dim,
            "n_hidden_dense": self.n_hidden_dense,
            "n_hidden_recurrent": self.n_hidden_recurrent,
            "n_deep": self.n_deep,
            "dropout": self.dropout,
            "activation": self.activation,
            "with_trending_prior": self.with_trending_prior,
            "output_folder": self.output_folder,
            "prefix": self.prefix,
            "embedding": {'class_name': self.embedding.__class__.__name__,
                          'config': self.embedding.get_config()} if self.embedding else None
        }

    def set_params(self, params, custom_objects=None):
        for param_name, param in params.items():

            if param_name == "embedding":
                if not param:
                    continue
                param = deserialize(param, custom_objects=custom_objects)
            setattr(self, param_name, param)

        return self

    def _build(self, phase, seq_shape=None, batch_size=None):
        # Recognition model

        with K.name_scope("recognition_model"):
            self.z_recognition_model = STORNRecognitionModel(self.data_dim, self.latent_dim, self.n_hidden_dense,
                                                            self.n_hidden_recurrent, self.n_deep, self.dropout,
                                                            self.activation, rec=self.rec)

            self.z_recognition_model.build(phase=phase, seq_shape=seq_shape, batch_size=batch_size, embedding=self.embedding)

            if phase == Phases.train:
                x_tm1 = Input(shape=(seq_shape, self.data_dim), name="storn_input_train", dtype="float32")
                z_t = self.z_recognition_model.train_z_t
                x_t = self.z_recognition_model.train_input
                z_post_stats = self.z_recognition_model.train_recogn_stats
            else:
                x_tm1 = Input(batch_shape=(batch_size, 1, self.data_dim), name="storn_input_predict", dtype="float32")
                z_t = self.z_recognition_model.predict_z_t
                x_t = self.z_recognition_model.predict_input
                z_post_stats = self.z_recognition_model.predict_recogn_stats

        with K.name_scope("prior_model"):
            # Prior model
            if self.with_trending_prior:
                z_tm1 = LambdaWithMasking(STORNModel.shift_z, output_shape=self.shift_z_output_shape)(z_t)
                self.z_prior_model = STORNPriorModel(self.latent_dim, self.with_trending_prior,
                                                    n_hidden_recurrent=self.n_hidden_recurrent, x_tm1=x_tm1, z_tm1=z_tm1)
            else:
                self.z_prior_model = STORNPriorModel(self.latent_dim, self.with_trending_prior)

            self.z_prior_model.build(phase=phase, seq_shape=seq_shape, batch_size=batch_size)

            if phase == Phases.train:
                z_prior_stats = self.z_prior_model.train_prior_stats
            else:
                z_prior_stats = self.z_prior_model.predict_prior_stats

        with K.name_scope("generative_model"):
            # Generative model

            # Fix of keras/engine/topology.py required for masked layer!
            # Otherwise concat with masked and non masked layer returns an error!
            # masked = Masking()(x_tm1)
            # gen_input = merge(inputs=[masked, z_t], mode='concat')

            # Unmasked Layer
            gen_input = Concatenate(axis=-1, name="generative_input")([x_tm1, z_t])

            for i in range(self.n_deep):
                gen_input = TimeDistributed(Dense(self.n_hidden_dense, activation=self.activation))(gen_input)
                if self.dropout != 0:
                    gen_input = Dropout(self.dropout)(gen_input)

            rnn_gen = RecurrentLayer(self.n_hidden_recurrent, return_sequences=True, stateful=(phase == Phases.predict))(gen_input)
            gen_map = rnn_gen
            for i in range(self.n_deep):
                gen_map = TimeDistributed(Dense(self.n_hidden_dense, activation=self.activation))(gen_map)
                if self.dropout != 0:
                    gen_map = Dropout(self.dropout)(gen_map)

            # Output statistics for the generative model
            gen_mu = TimeDistributed(Dense(self.data_dim, activation='linear' if self.rec == "gauss" else "sigmoid"))(gen_map)
            gen_sigma = TimeDistributed(Dense(self.data_dim, activation="softplus"))(gen_map)

        # Combined model
        output = Concatenate(axis=-1)([gen_mu, gen_sigma, z_post_stats, z_prior_stats])
        inputs = [x_t, x_tm1] if self.with_trending_prior else [x_t, x_tm1, z_prior_stats]
        model = Model(inputs=inputs, outputs=output)
        adam = Adam(lr=self.learning_rate)
        model.compile(optimizer=adam, loss=keras_variational_func(self.data_dim, self.latent_dim, rec=self.rec))
        # metrics=[keras_gauss, keras_divergence, mu_minus_x, mean_sigma]

        return model

    def build(self, seq_shape=None, batch_size=None):
        self.train_model = self._build(Phases.train, seq_shape=seq_shape)
        self.predict_model = self._build(Phases.predict, batch_size=batch_size)

    def load_predict_weights(self, weights_file):
        # self.train_model.save_weights("storn_weights.h5", overwrite=True)
        self.predict_model.load_weights(weights_file)
        self.train_model.load_weights(weights_file)
        return True

    def reset_predict_model(self):
        self.predict_model.reset_states()

    def fit(self, inputs, target, max_epochs=10, validation_split=0.1):
        n_sequences = target.shape[0]
        seq_len = target.shape[1]
        data_dim = target.shape[2]
        assert self.data_dim == data_dim

        # Build the train model
        list_in = inputs[:]
        if not self.with_trending_prior:
            list_in.append(STORNPriorModel.standard_input(n_sequences, seq_len, self.latent_dim, mode=self.rec))
        self.train_model = self._build(Phases.train, seq_shape=seq_len)
        # self.train_model.load_weights("start_weights.h5")

        # Do a validation split of all the inputs
        split_idx = int((1. - validation_split) * n_sequences)
        train_input, valid_input = [list(t) for t in zip(*[(X[:split_idx], X[split_idx:]) for X in list_in])]
        train_target, valid_target = target[:split_idx], target[split_idx:]

        # Save the parameters
        with open(os.path.join(self.output_folder, self.prefix + "parameters.json"), "w") as f:
            json.dump(self.get_params(), f, indent=4)

        weights_path = os.path.join(self.output_folder, self.prefix + "weights.h5")
        tensor_board = TensorBoard(log_dir=os.path.join(self.output_folder, "logs"), histogram_freq=1, write_images=True)
        checkpoint = ModelCheckpoint(weights_path, save_best_only=True, verbose=1)
        early_stop = EarlyStopping(patience=25, verbose=1)
        try:
            callbacks = [checkpoint, early_stop, tensor_board]
            if self.monitor:
                monitor = RemoteMonitor(root='http://localhost:9000')
                callbacks = callbacks + [monitor]
            self.train_model.fit(train_input, train_target, epochs=max_epochs, validation_data=(valid_input, [valid_target]), callbacks=callbacks)
        except KeyboardInterrupt:
            logger.info("Training interrupted! Restoring best weights and saving..")

        # Reload the best weights
        self.train_model.load_weights(weights_path)

    def predict_one_step(self, inputs):
        n_sequences = inputs[0].shape[0]
        seq_len = inputs[0].shape[1]
        data_dim = inputs[0].shape[2]
        assert self.data_dim == data_dim

        list_in = inputs[:]
        if not self.with_trending_prior:
            list_in.append(STORNPriorModel.standard_input(n_sequences, seq_len, self.latent_dim, mode=self.rec))

        _batch_size = 32
        pred_inputs = [add_samples_until_divisible(input_x, _batch_size) for input_x in list_in]

        # Build the predict model if necessary
        if self.predict_model is None:
            self.predict_model = self._build(Phases.predict, batch_size=_batch_size)
            self.load_predict_weights()

        return self.predict_model.predict(pred_inputs, batch_size=_batch_size)[:n_sequences, :, :]

    def evaluate_offline(self, inputs, target):
        n_sequences = target.shape[0]
        seq_len = target.shape[1]
        data_dim = target.shape[2]
        assert data_dim == self.data_dim, "Data dimensions do not match! Model is expecting {} features. Input has {}".format(self.data_dim, data_dim)

        # prepare inputs
        list_in = inputs[:]
        if not self.with_trending_prior:
            list_in.append(STORNPriorModel.standard_input(n_sequences, seq_len, self.latent_dim, mode=self.rec))

        # prepare target
        padded_target = np.concatenate(
            (target, np.zeros((n_sequences, seq_len, 4 * self.latent_dim + data_dim))),
            axis=-1)

        # get predictions
        predictions = self.train_model.predict(list_in)

        # compute loss based on predictions
        x = K.placeholder(ndim=3, dtype="float32")
        stats = K.placeholder(ndim=3, dtype="float32")
        get_loss = K.function(inputs=[x, stats], outputs=[keras_variational_func(self.data_dim, self.latent_dim, rec=self.rec)(x, stats)])
        loss = get_loss([padded_target, predictions])
        return predictions[:, :, :data_dim], loss

    def evaluate_online(self, inputs, ground_truth):
        """
        :param inputs: a list of inputs for the model. In this case, it's a
                       two element list.
        :param ground_truth: the expected value to compare to
        :return: plotting artifacts: input, prediction, and error
        """
        pred = self.predict_one_step(inputs)[:, :, :self.data_dim]
        return pred, np.mean((ground_truth - pred) ** 2, axis=-1)

    def reset_predict_model_states(self):
        self.predict_model.reset_states()

    @staticmethod
    def shift_z(rec_z):
        return K.concatenate((K.random_normal(shape=(K.shape(rec_z)[0], 1, K.shape(rec_z)[2])),
                              rec_z[:, :-1, :]), axis=1)

    @staticmethod
    def shift_z_output_shape(input_shape):
        return input_shape


class STORNRecognitionModel(object):
    def __init__(self, data_dim, latent_dim, n_hidden_dense,
                 n_hidden_recurrent, n_deep, dropout, activation, rec="gauss"):
        # Tensor shapes
        self.data_dim = data_dim
        self.latent_dim = latent_dim

        # Network complexity
        self.n_hidden_dense = n_hidden_dense
        self.n_hidden_recurrent = n_hidden_recurrent
        self.n_deep = n_deep
        self.dropout = dropout
        self.activation = activation
        self.rec = rec

        # Model states
        self.train_recogn_stats = None
        self.train_input = None
        self.train_z_t = None
        self.predict_recogn_stats = None
        self.predict_input = None
        self.predict_z_t = None
        self.rec_input = None

    def _build(self, phase, seq_shape=None, batch_size=None, embedding=None):
        if phase == Phases.train:
            x_t = Input(shape=(seq_shape, self.data_dim), name="stornREC_input_train", dtype="float32")
        else:
            x_t = Input(batch_shape=(batch_size, 1, self.data_dim), name="stornREC_input_predict", dtype="float32")

        # Unmasked Layer
        recogn_input = x_t

        if embedding:
            recogn_input = embedding(recogn_input)

        for i in range(self.n_deep):
            layer = TimeDistributed(Dense(self.n_hidden_dense, activation=self.activation))(recogn_input)
            if self.dropout != 0.0:
                recogn_input = Dropout(self.dropout)(recogn_input)
        self.rec_input = recogn_input

        recogn_rnn = RecurrentLayer(self.n_hidden_recurrent, return_sequences=True, stateful=(phase == Phases.predict))(recogn_input)

        recogn_map = recogn_rnn
        for i in range(self.n_deep):
            recogn_map = TimeDistributed(Dense(self.n_hidden_dense, activation=self.activation))(recogn_map)
            if self.dropout != 0:
                recogn_map = Dropout(self.dropout)(recogn_map)

        recogn_mu = TimeDistributed(Dense(self.latent_dim, activation='linear' if self.rec == "gauss" else "sigmoid"), name="recognition_mu")(recogn_map)
        recogn_sigma = TimeDistributed(Dense(self.latent_dim, activation="softplus", name="recognition_sigma"))(recogn_map)
        recogn_stats = Concatenate(axis=-1, name="recognition_stats")([recogn_mu, recogn_sigma])

        # sample z from the distribution in X
        z_t = TimeDistributed(LambdaWithMasking(STORNRecognitionModel.do_sample,
                                                output_shape=STORNRecognitionModel.sample_output_shape,
                                                arguments={
                                                    'batch_size': (None if (phase == Phases.train) else batch_size),
                                                    'dim_size': self.latent_dim,
                                                    'mode': self.rec},
                                                ))(recogn_stats)

        return recogn_stats, x_t, z_t

    def build(self, phase=Phases.train, seq_shape=None, batch_size=None, embedding=None):
        if phase == Phases.train:
            self.train_recogn_stats, self.train_input, self.train_z_t = self._build(Phases.train, 
                                                                                    seq_shape=seq_shape, embedding=embedding)
        else:
            self.predict_recogn_stats, self.predict_input, self.predict_z_t = self._build(Phases.predict,
                                                                                          batch_size=batch_size,
                                                                                          embedding=embedding)

    @staticmethod
    def do_sample(statistics, batch_size, dim_size, mode):
        # split in half
        mu = statistics[:, :dim_size]
        sigma = statistics[:, dim_size:]

        if batch_size is None:
            batch_size = K.shape(mu)[0]

        # sample with this mean and variance
        if mode == "gauss":
            return sample_gauss(mu, sigma, batch_size, dim_size)
        elif mode == "bernoulli":
            return sample_bernoulli(mu, batch_size, dim_size)

    @staticmethod
    def sample_output_shape(input_shape):
        shape = list(input_shape)
        shape[-1] /= 2
        return tuple(int(s) if s is not None else None for s in shape )


class STORNPriorModel(object):
    def __init__(self, latent_dim, trending, n_hidden_recurrent=None, x_tm1=None, z_tm1=None, rec="gauss"):
        # Tensor shapes
        self.latent_dim = latent_dim

        # Network complexity
        self.n_hidden_recurrent = n_hidden_recurrent
        self.rec = rec

        # STORN options
        self.trending = trending

        # Model states
        self.x_tm1 = x_tm1
        self.z_tm1 = z_tm1
        self.train_prior_stats = None
        self.predict_prior_stats = None

    def _build_std(self, phase, seq_shape=None, batch_size=None):
        if phase == Phases.train:
            input_layer = Input(shape=(seq_shape, 2 * self.latent_dim),
                                name="storn_prior_input_train", dtype="float32")
        else:
            input_layer = Input(batch_shape=(batch_size, 1, 2 * self.latent_dim),
                                name="storn_prior_input_predict", dtype="float32")
        return input_layer

    def _build_trending(self, phase):
        prior_input = Concatenate(axis=-1, name="prior_input")([self.x_tm1, self.z_tm1])
        rnn_prior = RecurrentLayer(self.n_hidden_recurrent,
                                   return_sequences=True,
                                   stateful=(phase == Phases.predict))(
            prior_input)
        rnn_rec_mu = TimeDistributed(Dense(self.latent_dim, activation='linear' if self.rec == "gauss" else "sigmoid"), name="prior_mu")(rnn_prior)
        rnn_rec_sigma = TimeDistributed(Dense(self.latent_dim, activation="softplus"), name="prior_sigma")(rnn_prior)

        return Concatenate(axis=-1, name="prior_stats")([rnn_rec_mu, rnn_rec_sigma])

    def build(self, phase=Phases.train, seq_shape=None, batch_size=None):
        if phase == Phases.train:
            if self.trending:
                self.train_prior_stats = self._build_trending(phase)
            else:
                self.train_prior_stats = self._build_std(phase, seq_shape=seq_shape)
        else:
            if self.trending:
                self.predict_prior_stats = self._build_trending(phase)
            else:
                self.predict_prior_stats = self._build_std(phase, batch_size=batch_size)

    @staticmethod
    def standard_input(number_of_series, seq_len, latent_dim, mode="gauss"):
        if mode == "gauss":
            sigma = np.ones(
                (number_of_series, seq_len, latent_dim),
                dtype="float32"
            )
            my = np.zeros(
                (number_of_series, seq_len, latent_dim),
                dtype="float32"
            )
        elif mode == "bernoulli":
            sigma = np.zeros(
                (number_of_series, seq_len, latent_dim),
                dtype="float32"
            )
            my = np.full(
                (number_of_series, seq_len, latent_dim), 0.5,
                dtype="float32"
            )
        else:
            raise ValueError("Unknown mode!")

        return np.concatenate([my, sigma], axis=-1)


def run_storn_grid_search(inputs, target, test_inputs, test_target):
    """
    STORN is not compatible with the sklearn grid search, so we need
    to do a basic grid search ourselves.
    I'll use a standard holdout instead of cross validation, since
    cross validation is very expensive (STORN trains slowly).
    """
    hdlr = logging.FileHandler('results/grid_search/storn_grid.log')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)
    logger.setLevel(logging.INFO)

    deep = [0, 4, 8]
    latent = [16, 32, 64]
    for n_deep in deep:
        for latent_dim in latent:
            storn = STORNModel(activation='tanh', n_deep=n_deep, with_trending_prior=True, latent_dim=latent_dim,
                               n_hidden_dense=64)
            storn.fit(inputs, target, max_epochs=600)
            _, err = storn.evaluate_offline(test_inputs, test_target)
            logger.info("deep: %d, latent: %d, loss: %f" % (n_deep, latent_dim, np.mean(err)))
