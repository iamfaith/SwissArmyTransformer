from lib2to3.pgen2 import token
import os

import torch
import argparse
import numpy as np

from SwissArmyTransformer import mpu, update_args_with_file
from SwissArmyTransformer.training.deepspeed_training import training_main
from bert_model import BertModel
from SwissArmyTransformer.model.mixins import MLPHeadMixin

class ClassificationModel(BertModel):
    def __init__(self, args, transformer=None, parallel_output=True, layernorm_epsilon=1e-12, **kwargs):
        super().__init__(args, transformer=transformer, parallel_output=parallel_output, layernorm_epsilon=layernorm_epsilon, **kwargs)
        self.del_mixin('bert-final')
        self.add_mixin('classification_head', MLPHeadMixin(args.hidden_size, 2048, 1))
        # self.add_mixin('prefix-tuning', PrefixTuningMixin(args.num_layers, args.hidden_size // args.num_attention_heads, args.num_attention_heads, args.prefix_len))
    def disable_untrainable_params(self):
        self.transformer.word_embeddings.requires_grad_(False)
        # for layer_id in range(len(self.transformer.layers)):
        #     self.transformer.layers[layer_id].requires_grad_(False)
    @classmethod
    def add_model_specific_args(cls, parser):
        group = parser.add_argument_group('BERT-finetune', 'BERT finetune Configurations')
        # group.add_argument('--prefix_len', type=int, default=16)
        return super().add_model_specific_args(parser)

def get_batch(data_iterator, args, timers):
    # Items and their type.
    keys = ['input_ids', 'position_ids', 'token_type_ids', 'attention_mask', 'label']
    datatype = torch.int64

    # Broadcast data.
    timers('data loader').start()
    if data_iterator is not None:
        data = next(data_iterator)
    else:
        data = None
    timers('data loader').stop()
    data_b = mpu.broadcast_data(keys, data, datatype)
    # Unpack.
    tokens = data_b['input_ids'].long()
    labels = data_b['label'].long()
    position_ids = data_b['position_ids'].long()
    token_type_ids = data_b['token_type_ids'].long()
    attention_mask = data_b['attention_mask'][:, None, None, :].float()

    # Convert
    if args.fp16:
        attention_mask = attention_mask.half()
    
    return tokens, labels, attention_mask, position_ids, token_type_ids, (tokens!=1)


def forward_step(data_iterator, model, args, timers):
    """Forward step."""

    # Get the batch.
    timers('batch generator').start()
    tokens, labels, attention_mask, position_ids, token_type_ids, loss_mask = get_batch(
        data_iterator, args, timers)
    timers('batch generator').stop()

    logits, *mems = model(input_ids=tokens, position_ids=position_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
    # pred = ((logits.contiguous().float().squeeze(-1)) * loss_mask).sum(dim=-1) / loss_mask.sum(dim=-1)
    pred = logits.contiguous().float().squeeze(-1)[..., 0]
    loss = torch.nn.functional.binary_cross_entropy_with_logits(
        pred,
        labels.float()
    )
    acc = ((pred > 0.).long() == labels).sum() / labels.numel()
    return loss, {'acc': acc}


def _encode(text, text_pair, args):
    pretrain_path = args.pretrain_path
    from transformers import BertTokenizer
    tokenizer = BertTokenizer.from_pretrained(os.path.join(pretrain_path, 'bert-base-uncased'))
    encoded_input = tokenizer(text, text_pair, max_length=args.sample_length, padding='max_length', truncation='only_first')
    seq_len = len(encoded_input['input_ids'])
    position_ids = torch.arange(seq_len)
    return dict(input_ids=encoded_input['input_ids'], position_ids=position_ids.numpy(), token_type_ids=encoded_input['token_type_ids'], attention_mask=encoded_input['attention_mask'])

from SwissArmyTransformer.data_utils import load_hf_dataset
def create_dataset_function(path, args):
    def process_fn(row):
        pack, label = _encode(row['passage'], row['question'], args), int(row['label'])
        return {
            'input_ids': np.array(pack['input_ids'], dtype=np.int64),
            'position_ids': np.array(pack['position_ids'], dtype=np.int64),
            'attention_mask': np.array(pack['attention_mask'], dtype=np.int64),
            'token_type_ids': np.array(pack['token_type_ids'], dtype=np.int64),
            'label': label
        }
    return load_hf_dataset(path, process_fn, columns = ["input_ids", "position_ids", "token_type_ids", "attention_mask", "label"], cache_dir=args.data_root, offline=False, transformer_name="boolq_transformer")

if __name__ == '__main__':
    py_parser = argparse.ArgumentParser(add_help=False)
    py_parser.add_argument('--sample_length', type=int, default=512-16)
    py_parser.add_argument('--old_checkpoint', action="store_true")
    py_parser.add_argument('--data_root', type=str)
    py_parser.add_argument('--pretrain_path', type=str)
    py_parser = ClassificationModel.add_model_specific_args(py_parser)
    args = update_args_with_file(py_parser)
    from SwissArmyTransformer.training.deepspeed_training import initialize_distributed, set_random_seed
    initialize_distributed(args)
    set_random_seed(args.seed)
    model = ClassificationModel.from_pretrained(args)
    if args.fp16:
        model.half()
    elif args.bf16:
        model.bfloat16()
    model = model.cuda(torch.cuda.current_device())
    # from cogdata.utils.ice_tokenizer import get_tokenizer as get_ice
    # tokenizer = get_tokenizer(args=args, outer_tokenizer=get_ice())
    training_main(args, model_cls=model, forward_step_function=forward_step, create_dataset_function=create_dataset_function)
