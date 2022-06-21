from transformers import T5Model, T5ForConditionalGeneration, T5Tokenizer
device = 'cuda:1'
tokenizer = T5Tokenizer.from_pretrained("t5-large")
model = T5ForConditionalGeneration.from_pretrained("/sharefs/english/yanan/huggingface_models/t5-large-lm-adapt").bfloat16()
model = model.to(device)
model.eval()
input_ids = tokenizer('The <extra_id_0> walks in <extra_id_1> park', return_tensors='pt').input_ids.to(device)
decoder_input_ids = tokenizer('<extra_id_0> cute dog <extra_id_1> the <extra_id_2>', return_tensors='pt').input_ids.to(device)
output = model(input_ids=input_ids, labels=decoder_input_ids)
breakpoint()
output.loss.backward()
a = 1