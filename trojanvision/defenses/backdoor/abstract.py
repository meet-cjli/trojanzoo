#!/usr/bin/env python3

from trojanvision.attacks import TrojanNet
from trojanvision.environ import env
from trojanzoo.defenses import Defense
from trojanzoo.utils.logger import AverageMeter
from trojanzoo.utils.metric import mask_jaccard, normalize_mad
from trojanzoo.utils.output import prints, ansi, output_iter
from trojanzoo.utils.tensor import to_tensor, to_numpy, tanh_func
from trojanzoo.utils.data import TensorListDataset, sample_batch

import torch
import torch.optim as optim
import numpy as np
from sklearn import metrics

import os
import time
import datetime
from abc import abstractmethod
from tqdm import tqdm

from typing import TYPE_CHECKING
from trojanvision.datasets import ImageSet
from trojanvision.models import ImageModel
from trojanvision.attacks.backdoor import BadNet
import argparse
if TYPE_CHECKING:
    import torch.utils.data    # TODO: python 3.10


class BackdoorDefense(Defense):

    name: str = 'backdoor_defense'

    @classmethod
    def add_argument(cls, group: argparse._ArgumentGroup):
        super().add_argument(group)
        group.add_argument('--original', action='store_true',
                           help='load original clean model (default: False)')
        return group

    def __init__(self, original: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.dataset: ImageSet
        self.model: ImageModel
        self.attack: BadNet  # for linting purpose
        self.original: bool = original
        self.target_class = self.attack.target_class

    @abstractmethod
    def detect(self, **kwargs):
        if not self.original:
            self.attack.load(**kwargs)
        if isinstance(self.attack, TrojanNet):
            self.model = self.attack.combined_model
        self.attack.validate_fn()
        self.real_mark = self.attack.mark.mark.clone()
        self.real_mask = self.attack.mark.get_mask()

    def get_filename(self, **kwargs):
        return self.attack.name + '_' + self.attack.get_filename(**kwargs)


class InputFiltering(BackdoorDefense):

    name: str = 'input_filtering'

    @classmethod
    def add_argument(cls, group: argparse._ArgumentGroup):
        super().add_argument(group)
        group.add_argument('--defense_input_num', type=int,
                           help='the number of inputs to test (default: 100)')
        return group

    def __init__(self, defense_input_num: int = 100, **kwargs):
        super().__init__(**kwargs)
        self.defense_input_num = defense_input_num

    def detect(self, **kwargs):
        super().detect(**kwargs)
        y_pred = self.get_pred_labels()
        y_true = self.get_true_labels()
        print('f1_score:', metrics.f1_score(y_true, y_pred))
        print('precision_score:', metrics.precision_score(y_true, y_pred))
        print('recall_score:', metrics.recall_score(y_true, y_pred))
        print('accuracy_score:', metrics.accuracy_score(y_true, y_pred))

    def get_true_labels(self) -> torch.Tensor:
        y_true = torch.zeros(self.defense_input_num, dtype=torch.bool)
        y_true[len(y_true) // 2:] = True
        return y_true

    def get_pred_labels(self) -> torch.Tensor:
        clean_scores = []
        poison_scores = []
        loader = self.dataset.loader['valid']
        if env['tqdm']:
            loader = tqdm(loader, leave=False)
        remain_counter = self.defense_input_num
        for data in loader:
            if remain_counter == 0:
                break
            _input, _label = self.model.remove_misclassify(data)
            if len(_label) == 0:
                continue
            if len(_input) < remain_counter:
                remain_counter -= len(_input)
            else:
                _input = _input[:remain_counter]
            poison_input = self.attack.add_mark(_input)
            clean_scores.append(self.check(_input, poison=False))
            poison_scores.append(self.check(poison_input, poison=True))
        clean_scores = torch.cat(clean_scores).flatten().sort()[0]
        poison_scores = torch.cat(poison_scores).flatten().sort()[0]
        return self.score2label(clean_scores, poison_scores)

    @abstractmethod
    def check(self, _input: torch.Tensor, poison: bool = False):
        ...

    def score2label(self, clean_scores: torch.Tensor, poison_scores: torch.Tensor) -> torch.Tensor:
        return torch.cat([clean_scores, poison_scores]).bool()


class TrainingFiltering(BackdoorDefense):

    name: str = 'training_filtering'

    @classmethod
    def add_argument(cls, group: argparse._ArgumentGroup):
        super().add_argument(group)
        group.add_argument('--defense_input_num', type=int,
                           help='the number of inputs to test (default: None)')
        return group

    def __init__(self, defense_input_num: int = None, **kwargs):
        super().__init__(**kwargs)
        self.defense_input_num = defense_input_num
        if self.attack.train_mode != 'dataset':
            self.attack.poison_dataset = self.attack.get_poison_dataset(
                poison_num=len(self.dataset.loader['train'].dataset))
        self.clean_dataset, self.poison_dataset = self.get_mix_dataset()

    def get_mix_dataset(self) -> tuple[torch.utils.data.Dataset, torch.utils.data.Dataset]:
        if not self.defense_input_num:
            return self.dataset.loader['train'].dataset, self.attack.poison_dataset
        if self.attack.train_mode != 'dataset':
            poison_num = int(self.defense_input_num * self.attack.poison_percent)
            clean_num = self.defense_input_num - poison_num
            clean_input, clean_label = sample_batch(self.dataset.loader['train'].dataset,
                                                    batch_size=clean_num)
            poison_input, poison_label = sample_batch(self.attack.poison_dataset,
                                                      batch_size=poison_num)
            clean_dataset = TensorListDataset(torch.stack(clean_input), clean_label)
            poison_dataset = TensorListDataset(torch.stack(poison_input), poison_label)
        return clean_dataset, poison_dataset

    def detect(self, **kwargs):
        super().detect(**kwargs)
        y_pred = self.get_pred_labels()
        y_true = self.get_true_labels()
        print('f1_score:', metrics.f1_score(y_true, y_pred))
        print('precision_score:', metrics.precision_score(y_true, y_pred))
        print('recall_score:', metrics.recall_score(y_true, y_pred))
        print('accuracy_score:', metrics.accuracy_score(y_true, y_pred))

    def get_true_labels(self) -> torch.Tensor:
        return torch.cat([torch.zeros(len(self.clean_dataset), dtype=torch.bool),
                          torch.ones(len(self.poison_dataset), dtype=torch.bool)])

    @abstractmethod
    def get_pred_labels(self) -> torch.Tensor:
        ...


class ModelInspection(BackdoorDefense):
    name: str = 'model_inspection'

    @classmethod
    def add_argument(cls, group: argparse._ArgumentGroup):
        super().add_argument(group)
        group.add_argument('--defense_remask_epoch', type=int,
                           help='defense watermark optimizing epochs '
                           '(default: 10)')
        group.add_argument('--defense_remask_lr', type=int,
                           help='defense watermark optimizing learning rate '
                           '(default: 0.1)')
        return group

    def __init__(self, defense_remask_epoch: int = 10,
                 defense_remask_lr: float = 0.1,
                 init_cost: float = 1e-3, **kwargs):
        super().__init__(**kwargs)
        self.param_list['model_inspection'] = ['defense_remask_epoch',
                                               'defense_remask_lr',
                                               'init_cost']

        self.defense_remask_epoch = defense_remask_epoch
        self.defense_remask_lr = defense_remask_lr
        self.init_cost = init_cost

        self.cost = init_cost

    def detect(self, **kwargs):
        super().detect(**kwargs)

        self.mark_random_pos = self.attack.mark.mark_random_pos
        mark_keys = ['mark', 'mark_height', 'mark_width',
                     'mark_height_offset', 'mark_width_offset',
                     'mark_random_pos', ]
        self.mark_dict = {key: getattr(self.attack.mark, key) for key in mark_keys}
        self.new_dict = {'mark': torch.zeros(self.attack.mark.mark.size(0),
                                             self.attack.mark.data_shape[-2],
                                             self.attack.mark.data_shape[-1],
                                             device=self.attack.mark.mark.device),
                         'mark_height': self.attack.mark.data_shape[-2],
                         'mark_width': self.attack.mark.data_shape[-1],
                         'mark_height_offset': 0,
                         'mark_width_offset': 0,
                         'mark_random_pos': False,
                         }
        self.real_mask = self.attack.mark.get_mask()

        for k, v in self.new_dict.items():
            setattr(self.attack.mark, k, v)
        self.attack.mark.mark.zero_()

        mark_list, loss_list = self.get_mark_loss_list()
        mask_norms = mark_list[:, -1].flatten(start_dim=1).norm(p=1, dim=1)
        print('mask norms: ', mask_norms)
        print('mask MAD: ', normalize_mad(mask_norms))
        print('loss: ', loss_list)
        print('loss MAD: ', normalize_mad(loss_list))

        if not self.mark_random_pos:
            self.attack.mark.mark = mark_list[self.attack.target_class]
            select_num = self.attack.mark.mark_height * self.attack.mark.mark_width
            overlap = mask_jaccard(self.attack.mark.get_mask(),
                                   self.real_mask,
                                   select_num=select_num)
            print(f'Jaccard index: {overlap:.3f}')

    def get_mark_loss_list(self) -> tuple[torch.Tensor, torch.Tensor]:
        mark_list, loss_list = [], []
        # todo: parallel to avoid for loop
        file_path = os.path.normpath(os.path.join(
            self.folder_path, self.get_filename(target_class=self.target_class) + '.npz'))
        for label in range(self.model.num_classes):
            print('Class: ', output_iter(label, self.model.num_classes))
            mark, loss = self.optimize_mark(label)
            mark_list.append(mark)
            loss_list.append(loss)
            if not self.mark_random_pos:
                select_num = self.attack.mark.mark_height * self.attack.mark.mark_width
                overlap = mask_jaccard(self.attack.mark.get_mask(),
                                       self.real_mask,
                                       select_num=select_num)
                print(f'Jaccard index: {overlap:.3f}')
            np.savez(file_path, mark_list=np.stack([to_numpy(mark) for mark in mark_list]),
                     loss_list=np.array(loss_list))
            print('Defense results saved at: ' + file_path)
        mark_list = torch.stack(mark_list)
        loss_list = torch.as_tensor(loss_list)
        return mark_list, loss_list

    def loss_fn(self, _input: torch.Tensor, _label: torch.Tensor,
                target: int, trigger_output: torch.Tensor = None) -> torch.Tensor:
        if trigger_output is None:
            trigger_output = self.model(self.attack.add_mark(_input))
        return self.model.criterion(trigger_output, target * torch.ones_like(_label))

    def optimize_mark(self, label: int) -> tuple[torch.Tensor, float]:
        r"""
        Args:
            label (int): The class label to optimize.

        Returns:
            (torch.Tensor, torch.Tensor):
                Optimized mark tensor with shape ``(C + 1, H, W)``
                and loss tensor.
        """
        atanh_mark = torch.randn_like(self.attack.mark.mark, requires_grad=True)
        optimizer = optim.Adam([atanh_mark], lr=self.defense_remask_lr, betas=(0.5, 0.9))
        optimizer.zero_grad()

        # best optimization results
        norm_best: float = float('inf')
        mark_best: torch.Tensor = None
        entropy_best: float = None

        self.before_loop_fn()

        losses = AverageMeter('Loss', ':.4e')
        entropy = AverageMeter('Entropy', ':.4e')
        norm = AverageMeter('Norm', ':.4e')
        acc = AverageMeter('Acc', ':6.2f')

        self.attack.mark.mark = tanh_func(atanh_mark)    # (c+1, h, w)
        for _epoch in range(self.defense_remask_epoch):
            losses.reset()
            entropy.reset()
            norm.reset()
            acc.reset()
            epoch_start = time.perf_counter()
            loader = self.dataset.loader['train']
            if env['tqdm']:
                loader = tqdm(loader, leave=False)
            for data in loader:
                _input, _label = self.model.get_data(data)
                trigger_input = self.attack.add_mark(_input)
                trigger_label = label * torch.ones_like(_label)
                trigger_output = self.model(trigger_input)

                batch_acc = trigger_label.eq(trigger_output.argmax(1)).float().mean()
                batch_entropy = self.loss_fn(_input, _label,
                                             target=label,
                                             trigger_output=trigger_output)
                batch_norm: torch.Tensor = self.attack.mark.mark[-1].norm(p=1)
                batch_loss = batch_entropy + self.cost * batch_norm

                batch_size = _label.size(0)
                acc.update(batch_acc.item(), batch_size)
                entropy.update(batch_entropy.item(), batch_size)
                norm.update(batch_norm.item(), batch_size)
                losses.update(batch_loss.item(), batch_size)

                batch_loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                self.attack.mark.mark = tanh_func(atanh_mark)    # (c+1, h, w)

            epoch_time = str(datetime.timedelta(seconds=int(
                time.perf_counter() - epoch_start)))
            pre_str: str = '{blue_light}Epoch: {0}{reset}'.format(
                output_iter(_epoch + 1, self.defense_remask_epoch), **ansi)
            pre_str = pre_str.ljust(64 if env['color'] else 35)
            _str = ' '.join([
                f'Loss: {losses.avg:.4f},'.ljust(20),
                f'Acc: {acc.avg:.2f}, '.ljust(20),
                f'Norm: {norm.avg:.4f},'.ljust(20),
                f'Entropy: {entropy.avg:.4f},'.ljust(20),
                f'Time: {epoch_time},'.ljust(20),
            ])
            prints(pre_str, _str, indent=4)

            # check to save best mask or not
            if norm.avg < norm_best:
                mark_best = self.attack.mark.mark.detach()
                norm_best = norm.avg
                entropy_best = entropy.avg
            if mark_best is None:
                mark_best = self.attack.mark.mark.detach()
                norm_best = norm.avg
                entropy_best = entropy.avg

            if self.check_early_stop(loss=losses.avg, acc=acc.avg, norm=norm.avg,
                                     entropy=entropy.avg):
                print('early stop')
                break
        atanh_mark.requires_grad_(False)
        self.attack.mark.mark = mark_best
        self.attack.validate_fn()
        return mark_best, entropy_best

    def before_loop_fn(self, *args, **kwargs):
        pass

    def check_early_stop(self, *args, **kwargs) -> bool:
        return False

    def load(self, path: str = None):
        if path is None:
            path = os.path.join(self.folder_path, self.get_filename() + '.npz')
        _dict = np.load(path)
        for k, v in self.new_dict.items():
            setattr(self.attack.mark, k, v)
        self.attack.mark.mark = to_tensor(_dict['mark_list'][self.target_class])
        print('defense results loaded from: ', path)