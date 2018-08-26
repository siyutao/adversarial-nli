#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import argparse

import json

import logging


def main(argv):
    def fmt(prog):
        return argparse.HelpFormatter(prog, max_help_position=100, width=200)

    argparser = argparse.ArgumentParser('Parse JSONL file containing violations', formatter_class=fmt)
    argparser.add_argument('path', action='store', type=str, default=None)

    args = argparser.parse_args(argv)

    path = args.path

    obj_lst = []

    with open(path, 'r') as f:
        for line in f:
            obj = json.loads(line)
            obj_lst += [obj]

    sort_key = 'inconsistency_loss'
    if 'inconsistency_loss_increase' in obj_lst[0]:
        sort_key = 'inconsistency_loss_increase'

    s_obj_lst = sorted(obj_lst, key=lambda k: k[sort_key])

    for obj in s_obj_lst:
        print(json.dumps(obj))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])
