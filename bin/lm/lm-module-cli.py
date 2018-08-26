#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import os
import pickle
import sys
import json

import numpy as np
import tensorflow as tf

from nnli.lm.loader2 import SNLILoader
from nnli.lm.model import LanguageModel
from nnli import tfutil

logger = logging.getLogger(os.path.basename(sys.argv[0]))

# PYTHONPATH=. ./bin/lm/lm-module-cli.py


def main(argv):
    logger.info('Command line: {}'.format(' '.join(arg for arg in argv)))

    parser = argparse.ArgumentParser()
    parser.add_argument('--train', type=str, default='data/snli/snli_1.0_train.jsonl.gz')
    parser.add_argument('--valid', type=str, default='data/snli/snli_1.0_dev.jsonl.gz')

    parser.add_argument('--vocabulary', type=str, default='saved/snli/dam/2/dam_index_to_token.p')
    parser.add_argument('--checkpoint', type=str, default='saved/snli/dam/2/dam')

    parser.add_argument('--save', type=str, default='./models/lm/', help='directory to store checkpointed models')

    parser.add_argument('--embedding-size', type=int, default=300, help='embedding size')
    parser.add_argument('--rnn-size', type=int, default=512, help='size of RNN hidden state')
    parser.add_argument('--num-layers', type=int, default=1, help='number of layers in the RNN')

    parser.add_argument('--model', type=str, default='lstm', help='rnn, gru, or lstm')

    parser.add_argument('--batch-size', type=int, default=32, help='minibatch size')
    parser.add_argument('--seq-length', type=int, default=8, help='RNN sequence length')
    parser.add_argument('--num-epochs', type=int, default=100, help='number of epochs')

    parser.add_argument('--report-every', '-r', type=int, default=10, help='report loss frequency')
    parser.add_argument('--save-every', '-s', type=int, default=1000, help='save frequency')

    parser.add_argument('--learning-rate', '--lr', type=float, default=0.1, help='learning rate')

    args = parser.parse_args(argv)
    train(args)


def stats(values):
    return '{0:.4f} ± {1:.4f}'.format(round(np.mean(values), 4), round(np.std(values), 4))


def train(args):
    vocabulary_path = args.vocabulary
    checkpoint_path = args.checkpoint

    with open(vocabulary_path, 'rb') as f:
        index_to_token = pickle.load(f)

    # Enumeration of tokens start at index=3:
    #   index=0 PADDING
    #   index=1 START_OF_SENTENCE
    #   index=2 END_OF_SENTENCE
    #   index=3 UNKNOWN_WORD
    bos_idx, eos_idx, unk_idx = 1, 2, 3

    index_to_token.update({0: '<PAD>', bos_idx: '<BOS>', eos_idx: '<EOS>', unk_idx: '<UNK>'})

    token_to_index = {token: index for index, token in index_to_token.items()}

    logger.info('Loading the dataset ..')

    loader = SNLILoader(path=args.train,
                        token_to_index=token_to_index,
                        batch_size=args.batch_size,
                        seq_length=args.seq_length,
                        shuffle=True)

    valid_loader = SNLILoader(path=args.valid,
                              token_to_index=token_to_index,
                              batch_size=args.batch_size,
                              seq_length=args.seq_length,
                              shuffle=False)

    vocab_size = len(token_to_index)

    config = {
        'model': args.model,
        'seq_length': args.seq_length,
        'batch_size': args.batch_size,
        'vocab_size': vocab_size,
        'embedding_size': args.embedding_size,
        'rnn_size': args.rnn_size,
        'num_layers': args.num_layers
    }

    config_path = os.path.join(args.save, 'config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f)

    logger.info('Generating the computational graph ..')

    print(max(index_to_token.keys()), vocab_size)
    assert max(index_to_token.keys()) + 1 == vocab_size

    discriminator_scope_name = 'discriminator'
    with tf.variable_scope(discriminator_scope_name):
        embedding_layer = tf.get_variable('embeddings',
                                          shape=[vocab_size, args.embedding_size],
                                          initializer=tf.contrib.layers.xavier_initializer(),
                                          trainable=False)

    lm_scope_name = 'language_model'
    with tf.variable_scope(lm_scope_name) as scope:
        model = LanguageModel(model=config['model'],
                              seq_length=config['seq_length'],
                              batch_size=config['batch_size'],
                              rnn_size=config['rnn_size'],
                              num_layers=config['num_layers'],
                              vocab_size=config['vocab_size'],
                              embedding_layer=embedding_layer,
                              infer=False)

        scope.reuse_variables()
        imodel = LanguageModel(model=config['model'],
                               seq_length=config['seq_length'],
                               batch_size=config['batch_size'],
                               rnn_size=config['rnn_size'],
                               num_layers=config['num_layers'],
                               vocab_size=config['vocab_size'],
                               embedding_layer=embedding_layer,
                               infer=True)

    optimizer = tf.train.AdagradOptimizer(args.learning_rate)

    tvars = tf.trainable_variables()
    train_op = optimizer.minimize(model.cost, var_list=[var for var in tvars])

    session_config = tf.ConfigProto()
    session_config.gpu_options.allow_growth = True

    init_op = tf.global_variables_initializer()

    saver = tf.train.Saver(tf.global_variables())
    emb_saver = tf.train.Saver([embedding_layer], max_to_keep=1)

    logger.info('Creating the session ..')

    with tf.Session(config=session_config) as session:
        logger.info('Trainable Parameters: {}'.format(
            tfutil.count_trainable_parameters(var_list=[var for var in tvars])))
        session.run(init_op)

        emb_saver.restore(session, checkpoint_path)

        loss_values = []
        best_valid_log_perplexity = None

        for epoch_id in range(0, args.num_epochs):
            logger.debug('Epoch: {}'.format(epoch_id))

            loader.reset_batch_pointer()
            state = session.run(model.initial_state)

            for batch_id in range(loader.pointer, loader.num_batches):
                x, y = loader.next_batch()

                feed_dict = {
                    model.input_data: x,
                    model.targets: y,
                    model.initial_state: state
                }

                loss_value, state, _ = session.run([model.cost, model.final_state, train_op], feed_dict=feed_dict)
                loss_values += [loss_value]

                if (epoch_id * loader.num_batches + batch_id) % args.report_every == 0:
                    a = epoch_id * loader.num_batches + batch_id
                    b = args.num_epochs * loader.num_batches
                    logger.info("{}/{} (epoch {}), loss = {}".format(a, b, epoch_id, stats(loss_values)))
                    loss_values = []

                    sample_value = imodel.sample(session, index_to_token, token_to_index, 10, 'A', 0, 1, 4)
                    logger.info('Sample: {}'.format(sample_value))

                if (epoch_id * loader.num_batches + batch_id) % args.save_every == 0:
                    valid_loader.reset_batch_pointer()
                    state = session.run(model.initial_state)

                    valid_log_perplexity = 0.0
                    valid_log_perplexities = []

                    for batch_id in range(valid_loader.pointer, valid_loader.num_batches):
                        x, y = valid_loader.next_batch()

                        feed_dict = {
                            model.input_data: x,
                            model.targets: y,
                            model.initial_state: state
                        }

                        batch_valid_log_perplexity, state = session.run([model.cost, model.final_state], feed_dict=feed_dict)
                        valid_log_perplexity += batch_valid_log_perplexity
                        valid_log_perplexities += [batch_valid_log_perplexity]

                    if best_valid_log_perplexity is None or valid_log_perplexity < best_valid_log_perplexity:
                        checkpoint_path = os.path.join(args.save, 'lm.ckpt')
                        saver.save(session, checkpoint_path, global_step=epoch_id * loader.num_batches + batch_id)
                        logger.info("Language model saved to {}".format(checkpoint_path))

                        logger.info('Validation Log-Perplexity: {0:.4f}'.format(valid_log_perplexity))
                        logger.info('Validation Log-Perplexities: {0}'.format(stats(valid_log_perplexities)))

                        best_valid_log_perplexity = valid_log_perplexity
                        config['valid_log_perplexity'] = best_valid_log_perplexity
                        with open(config_path, 'w') as f:
                            json.dump(config, f)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    main(sys.argv[1:])
