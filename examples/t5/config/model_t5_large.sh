MODEL_TYPE="t5-large"
MODEL_ARGS="--t5-model \
            --vocab-size 32128 \
            --num-layers 24 \
            --hidden-size 1024 \
            --inner-hidden-size 4096 \
            --num-attention-heads 16 \
            --hidden-size-per-attention-head 64 \
            --relative-attention-num-buckets 32 \
            --layernorm-epsilon 1e-6 \
            --tokenizer-type hf_T5Tokenizer \
            --tokenizer-model-type t5-large \
            --load /dataset/fd5061f6/yanan/huggingface_models/t5-large"