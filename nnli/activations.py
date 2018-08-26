# -*- coding: utf-8 -*-

import tensorflow as tf


def parametric_relu(x, name=None):
    alphas = tf.get_variable('{}/alpha'.format(name) if name else 'alpha',
                             x.get_shape()[-1],
                             initializer=tf.constant_initializer(0.0),
                             dtype=tf.float32)
    return tf.nn.relu(x) + alphas * (x - abs(x)) * 0.5


def selu(x):
    alpha = 1.6732632423543772848170429916717
    scale = 1.0507009873554804934193349852946
    return scale * tf.where(x >= 0.0, x, alpha * tf.nn.elu(x))


# Aliases
relu = tf.nn.relu
prelu = parametric_relu
