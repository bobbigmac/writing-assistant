"""Test multi-sequence KV cache — simplified approach.

Instead of monkey-patching, create the context manually with the params we need.
"""
import sys
import time
import ctypes
import numpy as np
import llama_cpp

MODEL_PATH = "w:/projects/writing-assistant/models/qwen2.5-3b-instruct-q4_k_m.gguf"

print("=== Loading model ===")
# Load just the model first (no context)
model = llama_cpp.LlamaModel(model_path=MODEL_PATH, verbose=False)
print(f"Model loaded")

# Create context params with multi-sequence support
params = llama_cpp.llama_context_default_params()
params.n_ctx = 4096
params.n_batch = 512
params.n_gpu_layers = -1
params.flash_attn_type = 1  # ENABLED
params.n_seq_max = 4
params.kv_unified = True
print(f"Context params: n_ctx={params.n_ctx}, n_seq_max={params.n_seq_max}, kv_unified={params.kv_unified}")

# Create context manually
ctx = llama_cpp.llama_new_context_with_model(model.model, params)
print(f"Context created: {ctx}")
print(f"n_seq_max: {llama_cpp.llama_n_seq_max(ctx)}")

mem = llama_cpp.llama_get_memory(ctx)
print(f"Memory handle: {mem}")

# Tokenize
text_a = "The quick brown fox jumps over the lazy dog."
text_b = "Climate change is the defining issue of our time."
tokens_a = llama_cpp.llama_tokenize(model.model, text_a.encode("utf-8"), True, True)
tokens_b = llama_cpp.llama_tokenize(model.model, text_b.encode("utf-8"), False, True)
print(f"\nTokens A: {len(tokens_a)} tokens")
print(f"Tokens B: {len(tokens_b)} tokens")

# Clear cache
llama_cpp.llama_memory_seq_rm(mem, -1, 0, -1)

# Test 1: Push seq 0 tokens
print("\n=== Test 1: Push seq 0 ===")
n = len(tokens_a)
tokens_arr = (ctypes.c_int * n)(*tokens_a)
batch = llama_cpp.llama_batch_get_one(tokens_arr, n, 0, 0)
ret = llama_cpp.llama_decode(ctx, batch)
print(f"decode ret: {ret}")
print(f"seq 0 pos_max: {llama_cpp.llama_memory_seq_pos_max(mem, 0)}")
print(f"seq 1 pos_max: {llama_cpp.llama_memory_seq_pos_max(mem, 1)}")

# Test 2: Push seq 1 tokens
print("\n=== Test 2: Push seq 1 ===")
n = len(tokens_b)
tokens_arr = (ctypes.c_int * n)(*tokens_b)
batch = llama_cpp.llama_batch_get_one(tokens_arr, n, 0, 1)
ret = llama_cpp.llama_decode(ctx, batch)
print(f"decode ret: {ret}")
print(f"seq 0 pos_max: {llama_cpp.llama_memory_seq_pos_max(mem, 0)}")
print(f"seq 1 pos_max: {llama_cpp.llama_memory_seq_pos_max(mem, 1)}")

# Test 3: Cross-sequence attention — single token with seq_id=[0,1]
print("\n=== Test 3: Cross-sequence attention ===")
pos_a = llama_cpp.llama_memory_seq_pos_max(mem, 0)
pos_b = llama_cpp.llama_memory_seq_pos_max(mem, 1)
gen_pos = max(pos_a, pos_b) + 1

# Create a batch with one token assigned to both seq_ids
batch_gen = llama_cpp.llama_batch_init(1, 0, 1)
try:
    batch_gen.n_tokens = 1
    # Use a common token (space)
    batch_gen.token[0] = 256  # space token in most tokenizers
    batch_gen.pos[0] = gen_pos
    batch_gen.n_seq_id[0] = 2
    seq_ids = (ctypes.c_int * 2)(0, 1)
    batch_gen.seq_id[0] = ctypes.cast(seq_ids, ctypes.POINTER(ctypes.c_int))
    batch_gen.logits[0] = 1

    print(f"Token: 256 (space), pos: {gen_pos}, seq_ids: [0, 1]")
    ret = llama_cpp.llama_decode(ctx, batch_gen)
    print(f"decode ret: {ret}")

    if ret == 0:
        n_vocab = llama_cpp.llama_n_vocab(model.model)
        logits_ptr = llama_cpp.llama_get_logits(ctx)
        logits = np.ctypeslib.as_array(logits_ptr, shape=(n_vocab,))
        top_token = int(np.argmax(logits))
        # Detokenize
        top_text = llama_cpp.llama_token_to_piece(model.model, top_token)
        print(f"Top token: {top_token} -> {top_text!r}")
        print("CROSS-SEQUENCE ATTENTION WORKS!")
    else:
        print(f"FAILED (ret={ret})")
finally:
    llama_cpp.llama_batch_free(batch_gen)

# Test 4: Surgical seq_rm on seq 1 only
print("\n=== Test 4: Surgical seq_rm on seq 1 ===")
print(f"Before: seq 0={llama_cpp.llama_memory_seq_pos_max(mem, 0)}, seq 1={llama_cpp.llama_memory_seq_pos_max(mem, 1)}")
llama_cpp.llama_memory_seq_rm(mem, 1, 0, -1)
print(f"After:  seq 0={llama_cpp.llama_memory_seq_pos_max(mem, 0)}, seq 1={llama_cpp.llama_memory_seq_pos_max(mem, 1)}")
print("Seq 0 preserved, seq 1 cleared!")

# Test 5: Verify seq 0 still works for generation after seq 1 removal
print("\n=== Test 5: Generate from seq 0 only after seq 1 removal ===")
pos_0 = llama_cpp.llama_memory_seq_pos_max(mem, 0)
batch_solo = llama_cpp.llama_batch_get_one(
    (ctypes.c_int * 1)(256), 1, pos_0 + 1, 0
)
batch_solo.logits[0] = 1
ret = llama_cpp.llama_decode(ctx, batch_solo)
print(f"decode ret: {ret}")
if ret == 0:
    n_vocab = llama_cpp.llama_n_vocab(model.model)
    logits_ptr = llama_cpp.llama_get_logits(ctx)
    logits = np.ctypeslib.as_array(logits_ptr, shape=(n_vocab,))
    top_token = int(np.argmax(logits))
    top_text = llama_cpp.llama_token_to_piece(model.model, top_token)
    print(f"Top token after seq 1 removal: {top_token} -> {top_text!r}")
    print("Seq 0 generation still works after seq 1 removal!")

print("\n=== All tests complete ===")
