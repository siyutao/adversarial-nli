# -*- coding: utf-8 -*-

from abc import abstractmethod

import numpy as np
import tensorflow as tf

from nnli import activations, tfutil
from nnli.models.base import BaseRTEModel

import logging

logger = logging.getLogger(__name__)


class BaseDecomposableAttentionModel(BaseRTEModel):
    @abstractmethod
    def _transform_input(self, sequence, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_attend(self, sequence, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_compare(self, sequence, reuse=False):
        raise NotImplementedError

    @abstractmethod
    def _transform_aggregate(self, v1_v2, reuse=False):
        raise NotImplementedError

    def __init__(self, use_masking=True, init_std_dev=0.01, *args, **kwargs):
        self.init_std_dev = init_std_dev
        super().__init__(*args, **kwargs)

        embedding1_size = self.sequence1.get_shape()[-1].value
        embedding2_size = self.sequence2.get_shape()[-1].value

        assert embedding1_size == embedding2_size

        # [batch_size, time_steps, embedding_size] -> [batch_size, time_steps, representation_size]
        self.transformed_sequence1 = self._transform_input(self.sequence1, reuse=self.reuse)

        # [batch_size, time_steps, embedding_size] -> [batch_size, time_steps, representation_size]
        self.transformed_sequence2 = self._transform_input(self.sequence2, reuse=True)

        self.transformed_sequence1_length = self.sequence1_length
        self.transformed_sequence2_length = self.sequence2_length

        logger.info('Building the Attend graph ..')

        self.raw_attentions = None
        self.attention_sentence1 = self.attention_sentence2 = None

        # tensors with shape (batch_size, time_steps, num_units)
        self.alpha, self.beta = self.attend(sequence1=self.transformed_sequence1,
                                            sequence2=self.transformed_sequence2,
                                            sequence1_lengths=self.transformed_sequence1_length,
                                            sequence2_lengths=self.transformed_sequence2_length,
                                            use_masking=use_masking, reuse=self.reuse)

        logger.info('Building the Compare graph ..')

        # tensor with shape (batch_size, time_steps, num_units)
        self.v1 = self.compare(self.transformed_sequence1, self.beta, reuse=self.reuse)

        # tensor with shape (batch_size, time_steps, num_units)
        self.v2 = self.compare(self.transformed_sequence2, self.alpha, reuse=True)

        logger.info('Building the Aggregate graph ..')
        self.logits = self.aggregate(v1=self.v1, v2=self.v2,
                                     num_classes=self.nb_classes,
                                     v1_lengths=self.transformed_sequence1_length,
                                     v2_lengths=self.transformed_sequence2_length,
                                     use_masking=use_masking, reuse=self.reuse)

    def __call__(self):
            return self.logits

    def attend(self, sequence1, sequence2,
               sequence1_lengths=None, sequence2_lengths=None,
               use_masking=True, reuse=False):
        """
        Attend phase.

        :param sequence1: tensor with shape (batch_size, time_steps, num_units)
        :param sequence2: tensor with shape (batch_size, time_steps, num_units)
        :param sequence1_lengths: time_steps in sequence1
        :param sequence2_lengths: time_steps in sequence2
        :param use_masking: use masking
        :param reuse: reuse variables
        :return: two tensors with shape (batch_size, time_steps, num_units)
        """
        with tf.variable_scope('attend') as _:
            # tensor with shape (batch_size, time_steps, num_units)
            self.attend_transformed_sequence1 = self._transform_attend(sequence1, reuse)

            # tensor with shape (batch_size, time_steps, num_units)
            self.attend_transformed_sequence2 = self._transform_attend(sequence2, True)

            # tensor with shape (batch_size, time_steps, time_steps)
            self.raw_attentions = tf.matmul(self.attend_transformed_sequence1,
                                            tf.transpose(self.attend_transformed_sequence2, [0, 2, 1]))

            masked_raw_attentions = self.raw_attentions
            if use_masking:
                masked_raw_attentions = tfutil.mask_3d(sequences=masked_raw_attentions,
                                                       sequence_lengths=sequence2_lengths,
                                                       mask_value=- np.inf, dimension=2)
            self.attention_sentence1 = tfutil.attention_softmax3d(masked_raw_attentions)

            # tensor with shape (batch_size, time_steps, time_steps)
            attention_transposed = tf.transpose(self.raw_attentions, [0, 2, 1])
            masked_attention_transposed = attention_transposed
            if use_masking:
                masked_attention_transposed = tfutil.mask_3d(sequences=masked_attention_transposed,
                                                             sequence_lengths=sequence1_lengths,
                                                             mask_value=- np.inf, dimension=2)
            self.attention_sentence2 = tfutil.attention_softmax3d(masked_attention_transposed)

            # tensors with shape (batch_size, time_steps, num_units)
            alpha = tf.matmul(self.attention_sentence2, sequence1, name='alpha')
            beta = tf.matmul(self.attention_sentence1, sequence2, name='beta')
            return alpha, beta

    def compare(self, sentence, soft_alignment, reuse=False):
        """
        Compare phase.

        :param sentence: tensor with shape (batch_size, time_steps, num_units)
        :param soft_alignment: tensor with shape (batch_size, time_steps, num_units)
        :param reuse: reuse variables
        :return: tensor with shape (batch_size, time_steps, num_units)
        """
        # tensor with shape (batch, time_steps, num_units)
        sentence_and_alignment = tf.concat(axis=2, values=[sentence, soft_alignment])
        transformed_sentence_and_alignment = self._transform_compare(sentence_and_alignment, reuse=reuse)
        return transformed_sentence_and_alignment

    def aggregate(self, v1, v2, num_classes,
                  v1_lengths=None, v2_lengths=None, use_masking=True, reuse=False):
        """
        Aggregate phase.

        :param v1: tensor with shape (batch_size, time_steps, num_units)
        :param v2: tensor with shape (batch_size, time_steps, num_units)
        :param num_classes: number of output units
        :param v1_lengths: time_steps in v1
        :param v2_lengths: time_steps in v2
        :param use_masking: use masking
        :param reuse: reuse variables
        :return: 
        """
        with tf.variable_scope('aggregate', reuse=reuse) as _:
            if use_masking:
                v1 = tfutil.mask_3d(sequences=v1, sequence_lengths=v1_lengths, mask_value=0, dimension=1)
                v2 = tfutil.mask_3d(sequences=v2, sequence_lengths=v2_lengths, mask_value=0, dimension=1)

            v1_sum = tf.reduce_sum(v1, [1])
            v2_sum = tf.reduce_sum(v2, [1])

            v1_v2 = tf.concat(axis=1, values=[v1_sum, v2_sum])

            transformed_v1_v2 = self._transform_aggregate(v1_v2, reuse=reuse)

            logits = tf.contrib.layers.fully_connected(inputs=transformed_v1_v2,
                                                       num_outputs=num_classes,
                                                       weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                       biases_initializer=tf.zeros_initializer(),
                                                       activation_fn=None)
        return logits


class FeedForwardDAM(BaseDecomposableAttentionModel):
    def __init__(self, representation_size=200, dropout_keep_prob=1.0, *args, **kwargs):
        self.representation_size = representation_size
        self.dropout_keep_prob = dropout_keep_prob
        super().__init__(*args, **kwargs)

    def _transform_input(self, sequence, reuse=False):
        with tf.variable_scope('transform_embeddings', reuse=reuse) as _:
            projection = tf.contrib.layers.fully_connected(inputs=sequence, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=None, activation_fn=None)
        return projection

    def _transform_attend(self, sequence, reuse=False):
        with tf.variable_scope('transform_attend', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
        return projection

    def _transform_compare(self, sequence, reuse=False):
        with tf.variable_scope('transform_compare', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
        return projection

    def _transform_aggregate(self, v1_v2, reuse=False):
        with tf.variable_scope('transform_aggregate', reuse=reuse) as _:
            projection = tf.nn.dropout(v1_v2, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer(),
                                                           activation_fn=tf.nn.relu)
        return projection


class FeedForwardDAMP(BaseDecomposableAttentionModel):
    def __init__(self, representation_size=200, dropout_keep_prob=1.0, *args, **kwargs):
        self.representation_size = representation_size
        self.dropout_keep_prob = dropout_keep_prob
        super().__init__(*args, **kwargs)

    def _transform_input(self, sequence, reuse=False):
        with tf.variable_scope('transform_embeddings', reuse=reuse) as _:
            projection = tf.contrib.layers.fully_connected(inputs=sequence, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=None, activation_fn=None)
        return projection

    def _transform_attend(self, sequence, reuse=False):
        with tf.variable_scope('transform_attend', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='1')
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='2')
        return projection

    def _transform_compare(self, sequence, reuse=False):
        with tf.variable_scope('transform_compare', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='1')
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='2')
        return projection

    def _transform_aggregate(self, v1_v2, reuse=False):
        with tf.variable_scope('transform_aggregate', reuse=reuse) as _:
            projection = tf.nn.dropout(v1_v2, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='1')
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.prelu(projection, name='2')
        return projection


class FeedForwardDAMS(BaseDecomposableAttentionModel):
    def __init__(self, representation_size=200, dropout_keep_prob=1.0, *args, **kwargs):
        self.representation_size = representation_size
        self.dropout_keep_prob = dropout_keep_prob
        super().__init__(*args, **kwargs)

    def _transform_input(self, sequence, reuse=False):
        with tf.variable_scope('transform_embeddings', reuse=reuse) as _:
            projection = tf.contrib.layers.fully_connected(inputs=sequence, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=None, activation_fn=None)
        return projection

    def _transform_attend(self, sequence, reuse=False):
        with tf.variable_scope('transform_attend', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
        return projection

    def _transform_compare(self, sequence, reuse=False):
        with tf.variable_scope('transform_compare', reuse=reuse) as _:
            projection = tf.nn.dropout(sequence, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
        return projection

    def _transform_aggregate(self, v1_v2, reuse=False):
        with tf.variable_scope('transform_aggregate', reuse=reuse) as _:
            projection = tf.nn.dropout(v1_v2, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
            projection = tf.nn.dropout(projection, keep_prob=self.dropout_keep_prob)
            projection = tf.contrib.layers.fully_connected(inputs=projection, num_outputs=self.representation_size,
                                                           weights_initializer=tf.random_normal_initializer(0.0, self.init_std_dev),
                                                           biases_initializer=tf.zeros_initializer())
            projection = activations.selu(projection)
        return projection
