#! /bin/bash

# Change for multinode config
CHECKPOINT_PATH=/dataset/fd5061f6/sat_pretrained/roberta


NUM_WORKERS=1
NUM_GPUS_PER_WORKER=1
MP_SIZE=1

script_path=$(realpath $0)
script_dir=$(dirname $script_path)
main_dir=$(dirname $script_dir)
source $main_dir/config/model_roberta_large.sh
echo $MODEL_TYPE

task_name=$1

OPTIONS_NCCL="NCCL_DEBUG=info NCCL_IB_DISABLE=0 NCCL_NET_GDR_LEVEL=2"
HOST_FILE_PATH="hostfile"
HOST_FILE_PATH="hostfile_single"

dataset_name="$task_name"
if [[ "$task_name" == "wsc" ]]; then
  dataset_name="wsc.fixed"
fi

en_data="hf://super_glue/${dataset_name}/train"
eval_data="hf://super_glue/${dataset_name}/validation"

if [[ "$task_name" == "sst2" ]]; then
  en_data="hf://glue/${dataset_name}/train"
  eval_data="hf://glue/${dataset_name}/validation"
fi

config_json="$script_dir/ds_config_ft.json"
gpt_options=" \
       --experiment-name finetune-$MODEL_TYPE-${dataset_name}-D-0.3-1e-5-ptahead-\
       --model-parallel-size ${MP_SIZE} \
       --mode finetune \
       --train-iters 16000 \
       --resume-dataloader \
       $MODEL_ARGS \
       --train-data ${en_data} \
       --distributed-backend nccl \
       --lr-decay-style linear \
       --fp16 \
       --eval-interval 100 \
       --save checkpoints/ \
       --split 1 \
       --eval-batch-size 2 \
       --warmup 0.1 \
       --valid-data ${eval_data} \
       --strict-eval \
       --save-interval 15000 \
       --child-type ChildTuning-D \
       --reserve-p 0.3 \
       --max-grad-norm 1.0 \
"
# finetune-roberta-large-boolq-lora-1e-4-03-18-12-27
#       --child-load /workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-boolq-pt-7e-3-nowarmup-03-08-10-58
#       --child-load /workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-boolq-bitfit-1e-3-03-08-13-15
#       --child-load /workspace/yzy/ST_develop/SwissArmyTransformer/examples/roberta_test/checkpoints/finetune-roberta-large-sst2-onehead-1e-3-03-09-10-58 \

gpt_options="${gpt_options}
       --deepspeed \
       --deepspeed_config ${config_json} \
"

((port=$RANDOM+10000))

if [ "$FINETUNE_GPU" ]; then
  echo "use gpu $FINETUNE_GPU"
else
  export FINETUNE_GPU=0
  echo "use gpu $FINETUNE_GPU"
fi

run_cmd="${OPTIONS_NCCL} deepspeed --include=localhost:$FINETUNE_GPU --master_port ${port} --hostfile ${HOST_FILE_PATH} child_ex/finetune_roberta_${task_name}.py ${gpt_options}"
echo ${run_cmd}
eval ${run_cmd}

set +x