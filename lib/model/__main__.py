import os
import socket
from copy import deepcopy
import multiprocessing

import time

from keras.backend.tensorflow_backend import set_session
import tensorflow as tf

from elephas.spark_model import SparkModel
from elephas.utils.rdd_utils import to_simple_rdd

from contextlib import contextmanager
from pyspark import SparkConf, SparkContext
from pyspark.sql import SparkSession

from keras.callbacks import ModelCheckpoint

import numpy as np

from lib.data import fetch
from lib.data.generator import WMTSequence
from lib.model.util import embedding_matrix, lr_scheduler
from lib.model import metrics
from lib.model.args import get_args
from lib.model.seq2seq import Seq2Seq
from lib.model.ensemble import Ensemble

if __name__ == '__main__':
    # Select GPU based on args
    args = get_args()
    root_dir = os.getcwd()

    # Set GPU usage
    if not args.cpu:
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        set_session(sess)
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = args.devices

    if args.dataset == 'en_de':
        encoder_train_input, decoder_train_input, decoder_train_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, dataset_size=args.dataset_size, source_vocab_size=args.source_vocab_size,
                        target_vocab_size=args.target_vocab_size)
        encoder_dev_input, decoder_dev_input, decoder_dev_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, source_vocab, target_vocab, splits='dev')
        encoder_test_input, decoder_test_input, decoder_test_target, raw_test_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, source_vocab, target_vocab, one_hot=True, splits='test')

        source_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.en.vec'), source_vocab)
        target_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.de.vec'), target_vocab)

    elif args.dataset == 'de_en':
        encoder_train_input, decoder_train_input, decoder_train_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, dataset_size=args.dataset_size, source_vocab_size=args.source_vocab_size,
                        target_vocab_size=args.target_vocab_size, reverse_lang=True)
        encoder_dev_input, decoder_dev_input, decoder_dev_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, source_vocab, target_vocab, reverse_lang=True, splits='dev')
        encoder_test_input, decoder_test_input, decoder_test_target, raw_test_target, source_vocab, target_vocab = \
            fetch.en_de(args.dataset_path, source_vocab, target_vocab, one_hot=True, reverse_lang=True, splits='test')

        source_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.de.vec'), source_vocab)
        target_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.en.vec'), target_vocab)

    elif args.dataset == 'en_vi':
        encoder_train_input, decoder_train_input, decoder_train_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, dataset_size=args.dataset_size, source_vocab_size=args.source_vocab_size,
                        target_vocab_size=args.target_vocab_size)
        encoder_dev_input, decoder_dev_input, decoder_dev_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, source_vocab, target_vocab, splits='dev')
        encoder_test_input, decoder_test_input, decoder_test_target, raw_test_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, source_vocab, target_vocab, one_hot=True, splits='test')

        source_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.en.vec'), source_vocab)
        target_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.vi.vec'), target_vocab)

    elif args.dataset == 'vi_en':
        encoder_train_input, decoder_train_input, decoder_train_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, dataset_size=args.dataset_size, source_vocab_size=args.source_vocab_size,
                        target_vocab_size=args.target_vocab_size, reverse=True)
        encoder_dev_input, decoder_dev_input, decoder_dev_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, source_vocab, target_vocab, reverse=True, splits='dev')
        encoder_test_input, decoder_test_input, decoder_test_target, raw_test_target, source_vocab, target_vocab = \
            fetch.en_vi(args.dataset_path, source_vocab, target_vocab, one_hot=True, reverse=True, splits='test')

        source_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.vi.vec'), source_vocab)
        target_embedding_map = embedding_matrix(os.path.join(args.embedding_path, 'wiki.en.vec'), target_vocab)

    else:
        raise Exception("Unsupported dataset")

    model = None
    metrics.DATASET = args.dataset
    metrics.TARGET_VOCAB = target_vocab

    model_config = deepcopy(args)
    source_vocab_size = len(source_vocab)
    target_vocab_size = len(target_vocab)
    if ',' in args.devices:
        model_config.devices = args.devices.split(',')
    else:
        model_config.devices = (args.devices, args.devices)
    model_config.source_vocab = source_vocab
    model_config.target_vocab = target_vocab
    model_config.source_vocab_size = source_vocab_size
    model_config.target_vocab_size = target_vocab_size
    model_config.source_embedding_map = source_embedding_map
    model_config.target_embedding_map = target_embedding_map

    training_generator = WMTSequence(encoder_train_input, decoder_train_input, decoder_train_target, model_config)
    validation_generator = WMTSequence(encoder_dev_input, decoder_dev_input, decoder_dev_target, model_config)

    model = Seq2Seq(model_config)

    if args.ensemble:
        conf = SparkConf().setAppName('Tardis').setMaster('local[*]').set('spark.executor.instances', '4') #.set('spark.driver.allowMultipleContexts', 'true')
        # sc = SparkContext.getOrCreate(conf=conf)
        sc = SparkContext(conf=conf)

        model = SparkModel(model.model, frequency='epoch', mode='asynchronous')  # Distributed ensemble

        # train_pairs = [(x, y) for x, y in zip([encoder_train_input, decoder_train_input], decoder_train_target)]
        # train_rdd = sc.parallelize(train_pairs, model_config.num_workers)

        train_rdd = to_simple_rdd(sc, [encoder_train_input, decoder_train_input], decoder_train_target)

        # test_pairs = [(x, y) for x, y in zip([encoder_test_input, decoder_test_input], raw_test_target)]
        # test_rdd = sc.parallelize(test_pairs, model_config.num_workers)

        # TODO: fix - multiple context!
        model.fit(train_rdd,
                batch_size=model_config.batch_size,
                epochs=model_config.epochs,
                validation_split=0.20,
                verbose=1)

        sc.stop()

    else:
        model.train_generator(training_generator, validation_generator)
        model.evaluate(encoder_test_input, raw_test_target)
