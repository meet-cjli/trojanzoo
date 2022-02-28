#!/usr/bin/env python3

# CUDA_VISIBLE_DEVICES=0 python examples/backdoor_attack.py --color --verbose 1 --pretrained --validate_interval 1 --epochs 10 --lr 0.01 --attack trojannn --mark_random_init

from ..abstract import BackdoorAttack

from trojanvision.environ import env
from trojanzoo.utils.tensor import tanh_func

import torch
import torch.optim as optim
import argparse


class TrojanNN(BackdoorAttack):
    r"""TrojanNN proposed by Yingqi Liu from Purdue University in NDSS 2018.
    It inherits :class:`trojanvision.attacks.BackdoorAttack`.
    Based on :class:`trojanvision.attacks.BadNet`,
    it further preprocesses watermark pixel values to maximize
    activations of neurons which are rarely used in normal images.

    See Also:
        * paper: `Trojaning Attack on Neural Networks`_
        * code: https://github.com/PurduePAML/TrojanNN

    Args:
        preprocess_layer (str): The chosen layer to maximize neuron activation.
            Defaults to ``'flatten'``.
        preprocess_next_layer (str): The next layer after preprocess_layer to find neuron index.
            Defaults to ``'classifier.fc'``.
        target_value (float): TrojanNN neuron activation target value.
            Defaults to ``100.0``.
        neuron_num (int): TrojanNN neuron number to maximize activation.
            Defaults to ``2``.
        neuron_lr (float): TrojanNN neuron optimization learning rate.
            Defaults to ``0.1``.
        neuron_epoch (int): TrojanNN neuron optimization epoch.
            Defaults to ``1000``.

    .. _Trojaning Attack on Neural Networks:
        https://github.com/PurduePAML/TrojanNN/blob/master/trojan_nn.pdf
    """

    name: str = 'trojannn'

    @classmethod
    def add_argument(cls, group: argparse._ArgumentGroup):
        super().add_argument(group)
        group.add_argument('--preprocess_layer',
                           help='the chosen layer to maximize neuron activation '
                           '(default: "flatten")')
        group.add_argument('--preprocess_next_layer',
                           help='the next layer after preprocess_layer to find neuron index '
                           '(default: "classifier.fc")')
        group.add_argument('--target_value', type=float,
                           help='trojannn neuron activation target value (default: 100)')
        group.add_argument('--neuron_num', type=int,
                           help='Trojan Net neuron numbers in neuron preprocessing '
                           '(default: 2)')
        group.add_argument('--neuron_lr', type=float,
                           help='trojann neuron optimization learning rate (default: 0.1)')
        group.add_argument('--neuron_epoch', type=int,
                           help='trojann neuron optimization epoch (default: 1000)')
        return group

    def __init__(self, preprocess_layer: str = 'features', preprocess_next_layer: str = 'classifier.fc',
                 target_value: float = 100.0, neuron_num: int = 2,
                 neuron_lr: float = 0.1, neuron_epoch: int = 1000,
                 **kwargs):
        super().__init__(**kwargs)
        if not self.mark.mark_random_init:
            raise Exception('TrojanNN requires "mark_random_init" to be True to initialize watermark.')
        if self.mark.mark_random_pos:
            raise Exception('TrojanNN requires "mark_random_pos" to be False to max activate neurons.')

        self.param_list['trojannn'] = ['preprocess_layer', 'preprocess_next_layer',
                                       'target_value', 'neuron_num',
                                       'neuron_lr', 'neuron_epoch']
        self.preprocess_layer = preprocess_layer
        self.preprocess_next_layer = preprocess_next_layer
        self.target_value = target_value

        self.neuron_lr = neuron_lr
        self.neuron_epoch = neuron_epoch
        self.neuron_num = neuron_num

        self.neuron_idx: torch.Tensor = None

    def attack(self, *args, **kwargs):
        self.neuron_idx = self.get_neuron_idx()
        self.preprocess_mark(neuron_idx=self.neuron_idx)
        super().attack(*args, **kwargs)

    # get the neuron idx for preprocess.
    def get_neuron_idx(self) -> torch.Tensor:
        weight = self.model.state_dict()[self.preprocess_next_layer + '.weight'].abs()
        if weight.dim() > 2:
            weight = weight.flatten(2).mean(2)
        weight = weight.mean(0)
        return weight.argsort(descending=False)[:self.neuron_num]

    def get_neuron_value(self, trigger_input: torch.Tensor, neuron_idx: torch.Tensor) -> float:
        trigger_feats = self.model.get_layer(trigger_input, layer_output=self.preprocess_layer)[:, neuron_idx].abs()
        if trigger_feats.dim() > 2:
            trigger_feats = trigger_feats.flatten(2).mean(2)
        return trigger_feats.mean().item()

    # train the mark to activate the least-used neurons.
    def preprocess_mark(self, neuron_idx: torch.Tensor, **kwargs):
        zeros = torch.zeros(self.dataset.data_shape, device=env['device']).unsqueeze(0)
        with torch.no_grad():
            trigger_input = self.add_mark(zeros, mark_alpha=1.0)
            print('Neuron Value Before Preprocessing:',
                  f'{self.get_neuron_value(trigger_input, neuron_idx):.5f}')

        atanh_mark = torch.randn_like(self.mark.mark[:-1], requires_grad=True)
        optimizer = optim.Adam([atanh_mark], lr=self.neuron_lr)
        optimizer.zero_grad()

        for _ in range(self.neuron_epoch):
            self.mark.mark[:-1] = tanh_func(atanh_mark)
            trigger_input = self.add_mark(zeros, mark_alpha=1.0)
            trigger_feats = self.model.get_layer(trigger_input, layer_output=self.preprocess_layer).abs()
            if trigger_feats.dim() > 2:
                trigger_feats = trigger_feats.flatten(2).mean(2)
            loss = (trigger_feats[0] - self.target_value).square().sum()
            loss.backward(inputs=[atanh_mark])
            optimizer.step()
            optimizer.zero_grad()
            self.mark.mark.detach_()
        self.mark.mark[:-1] = tanh_func(atanh_mark)
        atanh_mark.requires_grad_(False)
        self.mark.mark.detach_()

    def validate_fn(self, **kwargs) -> tuple[float, float]:
        if self.neuron_idx is not None:
            with torch.no_grad():
                zeros = torch.zeros(self.dataset.data_shape, device=env['device']).unsqueeze(0)
                trigger_input = self.add_mark(zeros, mark_alpha=1.0)
                print(f'Neuron Value: {self.get_neuron_value(trigger_input, self.neuron_idx):.5f}')
        return super().validate_fn(**kwargs)
