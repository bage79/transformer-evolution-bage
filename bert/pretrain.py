import sys

sys.path.append("..")
import os, argparse, random
from tqdm import tqdm, trange
import numpy as np

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel

from vocab import load_vocab
import config as cfg
from bert import model as bert
from bert import data
import optimization as optim


def set_seed(args):
    """ random seed """
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def init_process_group(rank, world_size):
    """ init_process_group """
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def destroy_process_group():
    """ destroy_process_group """
    dist.destroy_process_group()


def train_epoch(config, rank, epoch, model, criterion_lm, criterion_cls, optimizer, scheduler, train_loader):
    """ 모델 epoch 학습 """
    losses = []
    model.train()

    with tqdm(total=len(train_loader), desc=f"Train({rank}) {epoch}") as pbar:
        for i, value in enumerate(train_loader):
            labels_cls, labels_lm, inputs, segments = map(lambda v: v.to(config.device), value)

            optimizer.zero_grad()
            outputs = model(inputs, segments)
            logits_cls, logits_lm = outputs[0], outputs[1]

            loss_cls = criterion_cls(logits_cls, labels_cls)
            loss_lm = criterion_lm(logits_lm.view(-1, logits_lm.size(2)), labels_lm.view(-1))
            loss = loss_cls + loss_lm

            loss_val = loss_lm.item()
            losses.append(loss_val)

            loss.backward()
            optimizer.step()
            scheduler.step()

            pbar.update(1)
            pbar.set_postfix_str(f"Loss: {loss_val:.3f} ({np.mean(losses):.3f})")
    return np.mean(losses)


def train_model(rank, world_size, args):
    """ 모델 학습 """
    if 1 < args.n_gpu:
        init_process_group(rank, world_size)
    master = (world_size == 0 or rank % world_size == 0)

    vocab = load_vocab(args.vocab)

    config = cfg.Config.load(args.config)
    config.n_enc_vocab = len(vocab)
    config.device = torch.device(f"cuda:{rank}" if torch.cuda.is_available() else "cpu")
    print(config)

    best_epoch, best_loss = 0, 0
    model = bert.BERTPretrain(config)
    if os.path.isfile(args.save):
        best_epoch, best_loss = model.bert.load(args.save)
        print(f"rank: {rank} load pretrain from: {args.save}, epoch={best_epoch}, loss={best_loss}")
        best_epoch += 1
    if 1 < args.n_gpu:
        model.to(config.device)
        model = DistributedDataParallel(model, device_ids=[rank], find_unused_parameters=True)
    else:
        model.to(config.device)

    criterion_lm = torch.nn.CrossEntropyLoss(ignore_index=-1, reduction='mean')
    criterion_cls = torch.nn.CrossEntropyLoss()

    train_loader = data.build_pretrain_loader(vocab, args, epoch=best_epoch, shuffle=True)

    t_total = len(train_loader) * args.epoch
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = optim.get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)

    offset = best_epoch
    losses = []
    for step in trange(args.epoch, desc="Epoch"):
        epoch = step + offset
        if 0 < step:
            del train_loader
            train_loader = data.build_pretrain_loader(vocab, args, epoch=epoch, shuffle=True)

        loss = train_epoch(config, rank, epoch, model, criterion_lm, criterion_cls, optimizer, scheduler, train_loader)
        losses.append(loss)

        if master:
            best_epoch, best_loss = epoch, loss
            if isinstance(model, DistributedDataParallel):
                model.module.bert.save(best_epoch, best_loss, args.save)
            else:
                model.bert.save(best_epoch, best_loss, args.save)
            print(f">>>> rank: {rank} save model to {args.save}, epoch={best_epoch}, loss={best_loss:.3f}")

    print(f">>>> rank: {rank} losses: {losses}")
    if 1 < args.n_gpu:
        destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_dir', default='../data' if not cfg.IS_MAC else '../data_sample', type=str, required=False,
                        help='a data directory which have downloaded, corpus text, vocab files.')
    parser.add_argument("--config", default="config_half.json", type=str, required=False,
                        help="config file")
    parser.add_argument("--vocab", default="kowiki.model", type=str, required=False,
                        help="vocab file")
    parser.add_argument("--input", default="kowiki_bert_{}.json", type=str, required=False,
                        help="input pretrain data file")
    parser.add_argument("--count", default=10, type=int, required=False,
                        help="count of pretrain data")
    parser.add_argument("--save", default="save_pretrain.pth", type=str, required=False,
                        help="save file")
    parser.add_argument("--epoch", default=20, type=int, required=False,
                        help="epoch")
    parser.add_argument("--batch", default=256, type=int, required=False,
                        help="batch")
    parser.add_argument("--gpu", default=None, type=int, required=False,
                        help="GPU id to use.")
    parser.add_argument('--seed', type=int, default=42, required=False,
                        help="random seed for initialization")
    parser.add_argument('--weight_decay', type=float, default=0, required=False,
                        help="weight decay")
    parser.add_argument('--learning_rate', type=float, default=5e-5, required=False,
                        help="learning rate")
    parser.add_argument('--adam_epsilon', type=float, default=1e-8, required=False,
                        help="adam epsilon")
    parser.add_argument('--warmup_steps', type=float, default=0, required=False,
                        help="warmup steps")
    args = parser.parse_args()
    args.vocab = os.path.join(os.getcwd(), args.data_dir, args.vocab)

    if torch.cuda.is_available():
        args.n_gpu = torch.cuda.device_count() if args.gpu is None else 1
    else:
        args.n_gpu = 0
    set_seed(args)

    if 1 < args.n_gpu:
        mp.spawn(train_model,
                 args=(args.n_gpu, args),
                 nprocs=args.n_gpu,
                 join=True)
    else:
        train_model(0 if args.gpu is None else args.gpu, args.n_gpu, args)
