# -*- coding: utf-8 -*-

import numpy as np
import tensorflow as tf

from tensorflow.contrib import rnn
from tensorflow.contrib import legacy_seq2seq

from nnli.lm.beam import BeamSearch

import logging

logger = logging.getLogger(__name__)


class LanguageModel:
    def __init__(self, model='rnn', seq_length=25, batch_size=50, rnn_size=256, num_layers=1,
                 embedding_layer=None, vocab_size=None, infer=False, seed=0):

        assert embedding_layer is not None
        assert vocab_size is not None

        if infer:
            batch_size = 1
            seq_length = 1

        cell_to_fn = {
            'rnn': rnn.BasicRNNCell,
            'gru': rnn.GRUCell,
            'lstm': rnn.BasicLSTMCell
        }

        if model not in cell_to_fn:
            raise ValueError("model type not supported: {}".format(model))

        cell_fn = cell_to_fn[model]
        cells = [cell_fn(rnn_size) for _ in range(num_layers)]

        self.cell = cell = rnn.MultiRNNCell(cells)

        self.input_data = tf.placeholder(tf.int32, [batch_size, seq_length])
        self.targets = tf.placeholder(tf.int32, [batch_size, seq_length])
        self.initial_state = cell.zero_state(batch_size, tf.float32)

        with tf.variable_scope('rnnlm'):
            W = tf.get_variable("W", [rnn_size, vocab_size], initializer=tf.contrib.layers.xavier_initializer())
            b = tf.get_variable("b", [vocab_size], initializer=tf.zeros_initializer())

            emb_lookup = tf.nn.embedding_lookup(embedding_layer, self.input_data)
            emb_projection = tf.contrib.layers.fully_connected(inputs=emb_lookup,
                                                               num_outputs=rnn_size,
                                                               weights_initializer=tf.contrib.layers.xavier_initializer(),
                                                               biases_initializer=tf.zeros_initializer())

            inputs = tf.split(emb_projection, seq_length, 1)
            inputs = [tf.squeeze(input_, [1]) for input_ in inputs]

        def loop(prev, _):
            prev = tf.matmul(prev, W) + b
            prev_symbol = tf.stop_gradient(tf.argmax(prev, 1))
            return tf.nn.embedding_lookup(embedding_layer, prev_symbol)

        outputs, last_state = legacy_seq2seq.rnn_decoder(decoder_inputs=inputs,
                                                         initial_state=self.initial_state,
                                                         cell=cell,
                                                         loop_function=loop if infer else None,
                                                         scope='rnnlm')
        output = tf.reshape(tf.concat(outputs, 1), [-1, rnn_size])

        self.logits = tf.matmul(output, W) + b
        self.probabilities = tf.nn.softmax(self.logits)

        loss = legacy_seq2seq.sequence_loss_by_example(logits=[self.logits],
                                                       targets=[tf.reshape(self.targets, [-1])],
                                                       weights=[tf.ones([batch_size * seq_length])])

        self.cost = tf.reduce_sum(loss) / batch_size / seq_length
        self.final_state = last_state

        self.random_state = np.random.RandomState(seed)

    def score_sequence(self, session, sequence):
        x = np.zeros((1, 1))
        state = session.run(self.cell.zero_state(1, tf.float32))
        res = 0.0
        for i, idx in enumerate(sequence):
            x[0, 0] = idx
            feed = {
                self.input_data: x,
                self.initial_state: state
            }
            probabilities, state = session.run([self.probabilities, self.final_state], feed)
            if i < len(sequence) - 1:
                next_idx = sequence[i + 1]
                res += np.log(probabilities[0, next_idx])
        return res

    def sample(self, session, words, vocab, num=200, prime='first all', sampling_type=1, pick=0, width=4):
        def weighted_pick(weights):
            t = np.cumsum(weights)
            s = np.sum(weights)
            return int(np.searchsorted(t, np.random.rand(1) * s))

        def beam_search_predict(sample, state):
            """Returns the updated probability distribution (`probs`) and
            `state` for a given `sample`. `sample` should be a sequence of
            vocabulary labels, with the last word to be tested against the RNN.
            """
            x = np.zeros((1, 1))
            x[0, 0] = sample[-1]

            feed_dict = {
                self.input_data: x,
                self.initial_state: state
            }
            probabilities, final_state = session.run([self.probabilities, self.final_state], feed_dict=feed_dict)
            return probabilities, final_state

        def beam_search_pick(prime, width):
            """Returns the beam search pick."""
            if not len(prime) or prime == ' ':
                prime = self.random_state.choice(list(vocab.keys()))

            prime_labels = [vocab.get(w, 0) for w in prime.split()]
            bs = BeamSearch(beam_search_predict, session.run(self.cell.zero_state(1, tf.float32)), prime_labels)
            samples, scores = bs.search(None, None, k=width, maxsample=num)
            return samples[np.argmin(scores)]

        res = ''
        if pick == 1:
            state = session.run(self.cell.zero_state(1, tf.float32))
            if not len(prime) or prime == ' ':
                prime = self.random_state.choice(list(vocab.keys()))

            logger.info('Prime: {}'.format(prime))

            for word in prime.split()[:-1]:
                logger.info('Word: {}'.format(word))
                x = np.zeros((1, 1))
                x[0, 0] = vocab.get(word, 0)
                feed = {
                    self.input_data: x,
                    self.initial_state: state
                }
                state = session.run([self.final_state], feed)

            res = prime
            word = prime.split()[-1]

            for n in range(num):
                x = np.zeros((1, 1))
                x[0, 0] = vocab.get(word, 0)
                feed = {
                    self.input_data: x,
                    self.initial_state: state
                }
                probabilities, state = session.run([self.probabilities, self.final_state], feed)
                p = probabilities[0]

                if sampling_type == 0:
                    sample = np.argmax(p)
                elif sampling_type == 2:
                    sample = weighted_pick(p) if word == '\n' else np.argmax(p)
                else:
                    sample = weighted_pick(p)

                sample = np.clip(sample, 0, max(words.keys()))

                predictions = words[sample]
                res += ' ' + predictions
                word = predictions
        elif pick == 2:
            predictions = beam_search_pick(prime, width)
            for i, label in enumerate(predictions):
                res += ' ' + words[label] if i > 0 else words[label]
        return res
