#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import traceback

import torch
from torch.utils.data import DataLoader
from trainer.torch import NoamLR

from TTS.encoder.dataset import EncoderDataset
from TTS.encoder.losses import AngleProtoLoss, GE2ELoss, SoftmaxAngleProtoLoss
from TTS.encoder.utils.generic_utils import save_best_model, setup_speaker_encoder_model
from TTS.encoder.utils.samplers import PerfectBatchSampler
from TTS.encoder.utils.training import init_training
from TTS.encoder.utils.visual import plot_embeddings
from TTS.tts.datasets import load_tts_samples
from TTS.utils.audio import AudioProcessor
from TTS.utils.generic_utils import count_parameters, remove_experiment_folder, set_init_dict
from TTS.utils.io import load_fsspec, copy_model_files
from TTS.utils.radam import RAdam
from TTS.utils.training import check_update

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True
torch.manual_seed(54321)
use_cuda = torch.cuda.is_available()
num_gpus = torch.cuda.device_count()
print(" > Using CUDA: ", use_cuda)
print(" > Number of GPUs: ", num_gpus)


def setup_loader(ap: AudioProcessor, is_val: bool = False, verbose: bool = False):
    if is_val:
        loader = None
    else:
        dataset = EncoderDataset(
            ap,
            meta_data_eval if is_val else meta_data_train,
            voice_len=c.voice_len,
            num_utter_per_class=c.num_utter_per_class,
            num_classes_in_batch=c.num_classes_in_batch,
            verbose=verbose,
            augmentation_config=c.audio_augmentation if not is_val else None,
            use_torch_spec=c.model_params.get("use_torch_spec", False),
        )

        sampler = PerfectBatchSampler(
            dataset.items,
            dataset.get_class_list(),
            batch_size=c.num_classes_in_batch*c.num_utter_per_class, # total batch size
            num_classes_in_batch=c.num_classes_in_batch,
            num_gpus=1,
            shuffle=False if is_val else True,
            drop_last=True)

        loader = DataLoader(
            dataset,
            num_workers=c.num_loader_workers,
            batch_sampler=sampler,
            collate_fn=dataset.collate_fn,
        )     

    return loader, dataset.get_num_classes(), dataset.get_map_classid_to_classname()


def train(model, optimizer, scheduler, criterion, data_loader, global_step):
    model.train()
    epoch_time = 0
    best_loss = float("inf")
    avg_loss = 0
    avg_loss_all = 0
    avg_loader_time = 0
    end_time = time.time()
    print(len(data_loader))
    for _, data in enumerate(data_loader):
        start_time = time.time()

        # setup input data
        inputs, labels = data
        # agroup samples of each class in the batch. perfect sampler produces [3,2,1,3,2,1] we need [3,3,2,2,1,1]
        labels = torch.transpose(labels.view(c.num_utter_per_class, c.num_classes_in_batch), 0, 1).reshape(labels.shape)
        inputs = torch.transpose(inputs.view(c.num_utter_per_class, c.num_classes_in_batch, -1), 0, 1).reshape(inputs.shape)
        """
        labels_converted = torch.transpose(labels.view(c.num_utter_per_class, c.num_classes_in_batch), 0, 1).reshape(labels.shape)
        inputs_converted = torch.transpose(inputs.view(c.num_utter_per_class, c.num_classes_in_batch, -1), 0, 1).reshape(inputs.shape)
        idx = 0
        for j in range(0, c.num_classes_in_batch, 1):
            for i in range(j, len(labels), c.num_classes_in_batch):
                if not torch.all(labels[i].eq(labels_converted[idx])) or not torch.all(inputs[i].eq(inputs_converted[idx])):
                    print("Invalid")
                    print(labels)
                    exit()
                idx += 1
        labels = labels_converted
        inputs = inputs_converted
        print(labels)
        print(inputs.shape)"""

        loader_time = time.time() - end_time
        global_step += 1

        # setup lr
        if c.lr_decay:
            scheduler.step()
        optimizer.zero_grad()

        # dispatch data to GPU
        if use_cuda:
            inputs = inputs.cuda(non_blocking=True)
            labels = labels.cuda(non_blocking=True)

        # forward pass model
        outputs = model(inputs)

        # loss computation
        loss = criterion(outputs.view(c.num_classes_in_batch, outputs.shape[0] // c.num_classes_in_batch, -1), labels)
        loss.backward()
        grad_norm, _ = check_update(model, c.grad_clip)
        optimizer.step()

        step_time = time.time() - start_time
        epoch_time += step_time

        # Averaged Loss and Averaged Loader Time
        avg_loss = 0.01 * loss.item() + 0.99 * avg_loss if avg_loss != 0 else loss.item()
        num_loader_workers = c.num_loader_workers if c.num_loader_workers > 0 else 1
        avg_loader_time = (
            1 / num_loader_workers * loader_time + (num_loader_workers - 1) / num_loader_workers * avg_loader_time
            if avg_loader_time != 0
            else loader_time
        )
        current_lr = optimizer.param_groups[0]["lr"]

        if global_step % c.steps_plot_stats == 0:
            # Plot Training Epoch Stats
            train_stats = {
                "loss": avg_loss,
                "lr": current_lr,
                "grad_norm": grad_norm,
                "step_time": step_time,
                "avg_loader_time": avg_loader_time,
            }
            dashboard_logger.train_epoch_stats(global_step, train_stats)
            figures = {
                "UMAP Plot": plot_embeddings(outputs.detach().cpu().numpy(), c.num_classes_in_batch),
            }
            dashboard_logger.train_figures(global_step, figures)

        if global_step % c.print_step == 0:
            print(
                "   | > Step:{}  Loss:{:.5f}  AvgLoss:{:.5f}  GradNorm:{:.5f}  "
                "StepTime:{:.2f}  LoaderTime:{:.2f}  AvGLoaderTime:{:.2f}  LR:{:.6f}".format(
                    global_step, loss.item(), avg_loss, grad_norm, step_time, loader_time, avg_loader_time, current_lr
                ),
                flush=True,
            )
        avg_loss_all += avg_loss

        if global_step >= c.max_train_step or global_step % c.save_step == 0:
            # save best model only
            best_loss = save_best_model(model, optimizer, criterion, avg_loss, best_loss, OUT_PATH, global_step)
            avg_loss_all = 0
            if global_step >= c.max_train_step:
                break

        end_time = time.time()

    return avg_loss, global_step


def main(args):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global meta_data_train
    global meta_data_eval

    ap = AudioProcessor(**c.audio)
    model = setup_speaker_encoder_model(c)

    optimizer = RAdam(model.parameters(), lr=c.lr, weight_decay=c.wd)

    # pylint: disable=redefined-outer-name
    meta_data_train, meta_data_eval = load_tts_samples(c.datasets, eval_split=True)

    train_data_loader, num_classes, map_classid_to_classname = setup_loader(ap, is_val=False, verbose=True)
    # eval_data_loader, _, _ = setup_loader(ap, is_val=True, verbose=True)

    if c.loss == "ge2e":
        criterion = GE2ELoss(loss_method="softmax")
    elif c.loss == "angleproto":
        criterion = AngleProtoLoss()
    elif c.loss == "softmaxproto":
        criterion = SoftmaxAngleProtoLoss(c.model_params["proj_dim"], num_classes)
        if c.model == "emotion_encoder":
            # update config with the class map
            c.map_classid_to_classname = map_classid_to_classname
            copy_model_files(c, OUT_PATH)
    else:
        raise Exception("The %s  not is a loss supported" % c.loss)

    if args.restore_path:
        checkpoint = load_fsspec(args.restore_path)
        try:
            model.load_state_dict(checkpoint["model"])

            if "criterion" in checkpoint:
                criterion.load_state_dict(checkpoint["criterion"])

        except (KeyError, RuntimeError):
            print(" > Partial model initialization.")
            model_dict = model.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint["model"], c)
            model.load_state_dict(model_dict)
            del model_dict
        for group in optimizer.param_groups:
            group["lr"] = c.lr

        print(" > Model restored from step %d" % checkpoint["step"], flush=True)
        args.restore_step = checkpoint["step"]
    else:
        args.restore_step = 0

    if c.lr_decay:
        scheduler = NoamLR(optimizer, warmup_steps=c.warmup_steps, last_epoch=args.restore_step - 1)
    else:
        scheduler = None

    num_params = count_parameters(model)
    print("\n > Model has {} parameters".format(num_params), flush=True)

    if use_cuda:
        model = model.cuda()
        criterion.cuda()

    global_step = args.restore_step
    _, global_step = train(model, optimizer, scheduler, criterion, train_data_loader, global_step)


if __name__ == "__main__":
    args, c, OUT_PATH, AUDIO_PATH, c_logger, dashboard_logger = init_training()

    try:
        main(args)
    except KeyboardInterrupt:
        remove_experiment_folder(OUT_PATH)
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        remove_experiment_folder(OUT_PATH)
        traceback.print_exc()
        sys.exit(1)
