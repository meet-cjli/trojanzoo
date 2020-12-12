# -*- coding: utf-8 -*-


import trojanzoo.environ
import trojanzoo.dataset
import trojanzoo.model
import trojanzoo.train
import trojanzoo.attack
from trojanzoo.train import Train
from trojanzoo.attack import PGD

from trojanzoo.environ import env
from trojanzoo.utils import summary
import argparse

import warnings
warnings.filterwarnings("ignore")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    trojanzoo.environ.add_argument(parser)
    trojanzoo.dataset.add_argument(parser)
    trojanzoo.model.add_argument(parser)
    trojanzoo.train.add_argument(parser)
    trojanzoo.attack.add_argument(parser)

    args, _ = parser.parse_known_args()

    trojanzoo.environ.create(**args.__dict__)
    dataset = trojanzoo.dataset.create(**args.__dict__)
    model = trojanzoo.model.create(dataset=dataset, **args.__dict__)
    optimizer, lr_scheduler, train_args = trojanzoo.train.create(dataset=dataset, model=model, **args.__dict__)
    attack: PGD = trojanzoo.attack.create(dataset=dataset, model=model, **args.__dict__)

    if env['verbose']:
        summary(dataset=dataset, model=model, train=Train, attack=attack)
    attack.attack(optimizer=optimizer, lr_scheduler=lr_scheduler, **train_args)