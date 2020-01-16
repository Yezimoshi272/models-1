#  Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os


def set_paddle_flags(flags):
    for key, value in flags.items():
        if os.environ.get(key, None) is None:
            os.environ[key] = str(value)


set_paddle_flags({
    'FLAGS_conv_workspace_size_limit': 500,
    'FLAGS_eager_delete_tensor_gb': 0,  # enable gc
    'FLAGS_memory_fraction_of_eager_deletion': 1,
    'FLAGS_fraction_of_gpu_memory_to_use': 0.98
})

import sys
import numpy as np
import time
import shutil
import collections
import paddle
import paddle.fluid as fluid
import reader
import models.model_builder as model_builder
import models.resnet as resnet
import checkpoint as checkpoint
from config import cfg
from utility import parse_args, print_arguments, SmoothedValue, TrainingStats, now_time, check_gpu
num_trainers = int(os.environ.get('PADDLE_TRAINERS_NUM', 1))


def get_device_num():
    # NOTE(zcd): for multi-processe training, each process use one GPU card.
    if num_trainers > 1:
        return 1
    return fluid.core.get_cuda_device_count()


def train():
    learning_rate = cfg.learning_rate
    image_shape = [3, cfg.TRAIN.max_size, cfg.TRAIN.max_size]

    devices_num = get_device_num()
    total_batch_size = devices_num * cfg.TRAIN.im_per_batch

    use_random = True
    startup_prog = fluid.Program()
    train_prog = fluid.Program()
    with fluid.program_guard(train_prog, startup_prog):
        with fluid.unique_name.guard():
            model = model_builder.RRPN(
                add_conv_body_func=resnet.ResNet(),
                add_roi_box_head_func=resnet.ResNetC5(),
                use_pyreader=cfg.use_pyreader,
                use_random=use_random)
            model.build_model(image_shape)
            losses, keys, rpn_rois = model.loss()
            loss = losses[0]
            fetch_list = losses

            boundaries = cfg.lr_steps
            gamma = cfg.lr_gamma
            step_num = len(cfg.lr_steps)
            values = [learning_rate * (gamma**i) for i in range(step_num + 1)]
            start_lr = learning_rate * cfg.start_factor
            lr = fluid.layers.piecewise_decay(boundaries, values)
            lr = fluid.layers.linear_lr_warmup(lr, cfg.warm_up_iter, start_lr,
                                               learning_rate)
            optimizer = fluid.optimizer.Momentum(
                learning_rate=lr,
                regularization=fluid.regularizer.L2Decay(cfg.weight_decay),
                momentum=cfg.momentum)
            optimizer.minimize(loss)
            fetch_list = fetch_list + [lr]

            for var in fetch_list:
                var.persistable = True
    gpu_id = int(os.environ.get('FLAGS_selected_gpus', 0))
    place = fluid.CUDAPlace(gpu_id) if cfg.use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)

    build_strategy = fluid.BuildStrategy()
    build_strategy.fuse_all_optimizer_ops = False
    build_strategy.fuse_elewise_add_act_ops = True
    exec_strategy = fluid.ExecutionStrategy()
    exec_strategy.num_iteration_per_drop_scope = 1
    exe.run(startup_prog)

    if cfg.pretrained_model:
        checkpoint.load_and_fusebn(exe, train_prog, cfg.pretrained_model)
    compiled_train_prog = fluid.CompiledProgram(train_prog).with_data_parallel(
        loss_name=loss.name,
        build_strategy=build_strategy,
        exec_strategy=exec_strategy)

    shuffle = True
    shuffle_seed = None
    if num_trainers > 1:
        shuffle_seed = 1
    if cfg.use_pyreader:
        train_reader = reader.train(
            batch_size=cfg.TRAIN.im_per_batch,
            total_batch_size=total_batch_size,
            padding_total=cfg.TRAIN.padding_minibatch,
            shuffle=shuffle,
            shuffle_seed=shuffle_seed)
        if num_trainers > 1:
            assert shuffle_seed is not None, \
                "If num_trainers > 1, the shuffle_seed must be set, because " \
                "the order of batch data generated by reader " \
                "must be the same in the respective processes."
            # NOTE: the order of batch data generated by batch_reader
            # must be the same in the respective processes.
            if num_trainers > 1:
                train_reader = fluid.contrib.reader.distributed_batch_reader(
                    train_reader)
        py_reader = model.py_reader
        py_reader.decorate_paddle_reader(train_reader)
    else:
        if num_trainers > 1: shuffle = False
        train_reader = reader.train(
            batch_size=total_batch_size, shuffle=shuffle)
        feeder = fluid.DataFeeder(place=place, feed_list=model.feeds())

    def train_loop_pyreader():
        py_reader.start()
        train_stats = TrainingStats(cfg.log_window, keys)
        try:
            start_time = time.time()
            prev_start_time = start_time
            for iter_id in range(cfg.max_iter):
                prev_start_time = start_time
                start_time = time.time()
                outs = exe.run(compiled_train_prog,
                               fetch_list=[v.name for v in fetch_list])
                stats = {k: np.array(v).mean() for k, v in zip(keys, outs[:-1])}
                train_stats.update(stats)
                logs = train_stats.log()
                if iter_id % 10 == 0:
                    strs = '{}, iter: {}, lr: {:.5f}, {}, time: {:.3f}'.format(
                        now_time(), iter_id,
                        np.mean(outs[-1]), logs, start_time - prev_start_time)
                    print(strs)
                sys.stdout.flush()
                if (iter_id) % cfg.TRAIN.snapshot_iter == 0 and iter_id != 0:
                    save_name = "{}".format(iter_id)
                    checkpoint.save(exe, train_prog,
                                    os.path.join(cfg.model_save_dir, save_name))
                if (iter_id) == cfg.max_iter:
                    checkpoint.save(
                        exe, train_prog,
                        os.path.join(cfg.model_save_dir, "model_final"))
                    break
            end_time = time.time()
            total_time = end_time - start_time
            last_loss = np.array(outs[0]).mean()
        except (StopIteration, fluid.core.EOFException):
            py_reader.reset()

    def train_loop():
        start_time = time.time()
        prev_start_time = start_time
        start = start_time
        train_stats = TrainingStats(cfg.log_window, keys)
        for iter_id, data in enumerate(train_reader()):
            prev_start_time = start_time
            start_time = time.time()
            if data[0][1].shape[0] == 0:
                continue

            outs = exe.run(compiled_train_prog,
                           fetch_list=[v.name for v in fetch_list],
                           feed=feeder.feed(data))
            stats = {k: np.array(v).mean() for k, v in zip(keys, outs[:-1])}
            train_stats.update(stats)
            logs = train_stats.log()
            if iter_id % 10 == 0:
                strs = '{}, iter: {}, lr: {:.5f}, {}, time: {:.3f}'.format(
                    now_time(), iter_id,
                    np.mean(outs[-1]), logs, start_time - prev_start_time)
                print(strs)
            sys.stdout.flush()
            if (iter_id + 1) % cfg.TRAIN.snapshot_iter == 0 and iter_id != 0:
                save_name = "{}".format(iter_id + 1)
                checkpoint.save(exe, train_prog,
                                os.path.join(cfg.model_save_dir, save_name))
            if (iter_id + 1) == cfg.max_iter:
                checkpoint.save(exe, train_prog,
                                os.path.join(cfg.model_save_dir, "model_final"))
                break

        end_time = time.time()
        total_time = end_time - start_time
        last_loss = np.array(outs[0]).mean()

    if cfg.use_pyreader:
        train_loop_pyreader()
    else:
        train_loop()


if __name__ == '__main__':
    args = parse_args()
    print_arguments(args)
    check_gpu(args.use_gpu)
    train()