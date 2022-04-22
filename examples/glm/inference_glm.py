# -*- encoding: utf-8 -*-
'''
@File    :   inference_glm.py
@Time    :   2021/10/22 19:41:58
@Author  :   Ming Ding
@Contact :   dm18@mails.tsinghua.edu.cn
'''

# here put the import lib
from functools import partial
import os
import sys
import random
import time
from datetime import datetime
import torch
import torch.nn.functional as F
import argparse
import stat
from functools import partial

from SwissArmyTransformer import mpu, get_args, get_tokenizer, load_checkpoint, initialize_distributed, set_random_seed

from SwissArmyTransformer.model import GLMModel
from SwissArmyTransformer.model.mixins import CachedAutoregressiveMixin
from SwissArmyTransformer.generation.autoregressive_sampling import filling_sequence, filling_two_sequences, \
    evaluate_perplexity
from SwissArmyTransformer.generation.sampling_strategies import BeamSearchStrategy, BaseStrategy
from SwissArmyTransformer.generation.utils import timed_name, generate_continually


def get_masks_and_position_ids_glm(seq, mask_position, context_length):
    tokens = seq.unsqueeze(0)

    attention_mask = torch.ones((1, len(seq), len(seq)), device=tokens.device)
    attention_mask.tril_()
    attention_mask[..., :context_length] = 1
    attention_mask.unsqueeze_(1)

    position_ids = torch.zeros(2, len(seq), device=tokens.device, dtype=torch.long)
    torch.arange(0, context_length, out=position_ids[0, :context_length])
    position_ids[0, context_length:] = mask_position
    torch.arange(1, len(seq) - context_length + 1, out=position_ids[1, context_length:])

    position_ids = position_ids.unsqueeze(0)
    return tokens, attention_mask, position_ids


def main(args):
    args.do_train = False
    initialize_distributed(args)
    tokenizer = get_tokenizer(args)
    # build model 
    model = GLMModel(args)
    model.add_mixin('auto-regressive', CachedAutoregressiveMixin())
    if args.fp16:
        model = model.half()
    model = model.to(args.device)
    load_checkpoint(model, args)
    set_random_seed(args.seed)
    model.eval()

    end_tokens = [tokenizer.get_command('eop').Id, tokenizer.get_command('eos').Id]
    # define function for each query
    if args.sampling_strategy == 'BaseStrategy':
        strategy = BaseStrategy(temperature=args.temperature, top_k=args.top_k, end_tokens=end_tokens)
    elif args.sampling_strategy == 'BeamSearchStrategy':
        strategy = BeamSearchStrategy(args.batch_size, length_penalty=args.length_penalty, consider_end=True, end_tokens=end_tokens, no_repeat_ngram_size=args.no_repeat_ngram_size, min_tgt_length=args.min_tgt_length)
    else:
        raise ValueError(f'unknown strategy {args.sampling_strategy}')
    
    def process(raw_text):
        if args.with_id:
            query_id, raw_text = raw_text.split('\t')
        # add MASK
        if "|||" in raw_text:
            texts = raw_text.split("|||")
            uncond_text, cond_text = texts
        else:
            uncond_text, cond_text = None, raw_text
        generation_mask = '[gMASK]' if args.task_mask else '[MASK]'

        def process_text(text):
            if 'MASK]' not in text:
                text += ' ' + generation_mask
            if 'MASK]' not in text:
                text += ' ' + generation_mask
            seq = tokenizer.EncodeAsIds(text).tokenization
            seq = [tokenizer.get_command('ENC').Id] + seq
            if not text.endswith('MASK]'):
                seq = seq + [tokenizer.get_command('eos').Id]
            if len(seq) > args.max_sequence_length:
                raise ValueError('text too long.')
            print('text: {}\n'.format(text))
            return seq

        cond_seq = process_text(cond_text)
        if uncond_text is not None:
            uncond_seq = process_text(uncond_text)

        # generation
        mbz = args.max_inference_batch_size # 12
        assert args.batch_size < mbz or args.batch_size % mbz == 0
        # continually detect the first mark position
        # detect
        mask_tokens = ['MASK', 'sMASK', 'gMASK'] if args.task_mask else ['MASK']
        mask_tokens = [tokenizer.get_command(token).Id for token in mask_tokens]
        cond_mask_position = len(cond_seq)
        for token in mask_tokens:
            try:
                cond_mask_position = min(cond_mask_position, cond_seq.index(token))
            except ValueError:
                pass
        cond_get_func = partial(get_masks_and_position_ids_glm, mask_position=cond_mask_position, context_length=len(cond_seq))

        if uncond_text is not None:
            uncond_mask_position = len(uncond_seq)
            for token in mask_tokens:
                try:
                    uncond_mask_position = min(uncond_mask_position, uncond_seq.index(token))
                except ValueError:
                    pass
            uncond_get_func = partial(get_masks_and_position_ids_glm, mask_position=uncond_mask_position,
                                      context_length=len(uncond_seq))

        cond_seq = torch.cuda.LongTensor(
            cond_seq + [tokenizer.get_command('sop').Id] + [-1] * (args.out_seq_length - len(cond_seq) - 1),
            device=args.device)
        if uncond_text is not None:
            uncond_seq = torch.cuda.LongTensor(
                uncond_seq + [tokenizer.get_command('sop').Id] + [-1] * (args.out_seq_length - len(uncond_seq) - 1),
                device=args.device)
            output_list = filling_two_sequences(model, cond_seq, uncond_seq,
                                                alpha=5,
                                                batch_size=min(args.batch_size, mbz),
                                                strategy=strategy,
                                                get_masks_and_position_ids1=cond_get_func,
                                                get_masks_and_position_ids2=uncond_get_func,
                                                )[0]  # we don't use mems, fill back
        else:
            output_list = filling_sequence(model, cond_seq, batch_size=min(args.batch_size, mbz), strategy=strategy,
                                           get_masks_and_position_ids=cond_get_func)[0]
        if isinstance(output_list, torch.Tensor): # different strategies
            output_list = list(output_list)

        # decoding
        txts = []
        for seq in output_list:
            decode_tokens = tokenizer.DecodeIds(seq.tolist())
            txts.append(decode_tokens)

        # save
        if args.with_id:
            full_path = os.path.join(args.output_path, query_id + '.txt')
        else:
            prefix = raw_text.replace('/', '')[:20]
            full_path = timed_name(prefix, '.txt', args.output_path)
            print(txts[0]) # print the first.
        with open(full_path, 'w', encoding='utf-8') as fout:
            for txt in txts:
                fout.write(txt + '\n')
        os.chmod(full_path, stat.S_IRWXO + stat.S_IRWXG + stat.S_IRWXU)

    os.makedirs(args.output_path, exist_ok=True)
    generate_continually(process, args.input_source)


if __name__ == "__main__":
    py_parser = argparse.ArgumentParser(add_help=False)
    py_parser.add_argument('--sampling-strategy', type=str, default='BaseStrategy', help='type name of sampling strategy')
    GLMModel.add_model_specific_args(py_parser)
    known, args_list = py_parser.parse_known_args()
    args = get_args(args_list)
    args = argparse.Namespace(**vars(args), **vars(known))
    
    with torch.no_grad():
        main(args)
