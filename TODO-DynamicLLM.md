# TODO: Dynamic LLM — persistent KV cache sessions for writing-assistant

## The goal

A self-hosted LLM inference layer that maintains a **live KV cache session** for the duration of a writing session (hours, not days). New events push tokens onto the cache; generation appends to it. The prefix is never recomputed. When the session ends, the cache dies — that's fine.

This eliminates the fundamental bottleneck of commercial API approaches: rebuilding the full context prompt on every request. Instead, compute happens only for new tokens.

## Why commercial APIs can't do this

Every commercial API (OpenAI, Anthropic, etc.) is request/response. You send the full prompt; they hash the prefix; if it matches a cached KV, they skip the forward pass for those tokens (real compute savings, not just transport). But:

- You still **send the full text every time** as a cache key
- The cache may evict at any time (LRU, not your policy)
- You can't "push tokens onto a live session" — each request is independent
- You pay per token processed, not per token newly computed

The economics: holding a 5GB KV cache per active session means a GPU serves ~10 users instead of ~1000. No commercial provider accepts that. So they don't offer live sessions.

## Why our use case is tractable

We don't need cross-session memory. We don't need infinite context. We need:

- One session, alive for hours
- Context = personality prompts (fixed) + current document (sliding window) + recent events (last N) + current event
- Total: 4-16K tokens typically, 32K max
- KV cache for 32K tokens on a 1.5-3B model: ~0.5-1.5GB VRAM
- Fits comfortably on an 8GB RTX 4060

No eviction needed at these sizes. The cache fits entirely in VRAM. Eviction only matters if you're pushing the context window limit, and at 16K on a small model, you won't be.

## The technical foundation: how KV cache persistence works

During autoregressive generation, the model doesn't recompute the whole sequence for each new token. It maintains a **KV cache** — the key and value tensors from every attention layer, for every token processed so far. When token N+1 arrives:

1. Compute attention for token N+1 **only**
2. Against the existing KV cache (tokens 1..N)
3. Append token N+1's K and V to the cache
4. Generate token N+2 the same way

The prefix's attention weights are **never recomputed**. They sit in GPU memory. This is O(new tokens) compute, not O(total context) compute.

This IS "moving the weights in response to information pushed onto the stack." It's literally how the model works internally. The problem is the API boundary — no commercial provider exposes it as a live session.

## Inference engine options

### llama.cpp (simplest, best for single-session testing)

- **Slots**: each slot holds one conversation's KV cache. The cache lives in VRAM for the session lifetime.
- **`--prompt-cache-all`**: caches the full prompt's KV state. Subsequent requests with the same prefix skip the forward pass for cached tokens.
- **`cache_prompt: true`** in the API request: tells the server to reuse the cached prefix.
- **No surgical eviction**: you either keep the whole cache (fits → great) or reset the slot (loses everything). No "evict token range X-Y."
- **Save/restore**: `--prompt-cache` can snapshot the full cache to disk (not surgical, but useful for session pause/resume).

Best for: proving the concept on a 4060. Small models (1.5-3B), single session, no eviction needed.

```bash
# Start the server with prompt caching:
llama-server \
  --model Qwen2.5-1.5B-Instruct-Q4_K_M.gguf \
  --ctx-size 8192 \
  --prompt-cache-all \
  --port 8080
```

### vLLM (production-grade, block-granular cache)

- **PagedAttention**: breaks the KV cache into **blocks** (like memory pages, typically 16 tokens each). Each block can be evicted and restored independently.
- **Block manager**: evicts based on LRU when memory pressure increases. You can't say "these tokens are priority, those expire."
- **API is still request/response**: you send the full prompt, vLLM hashes the prefix, reuses cached blocks, computes new ones.
- **No "push with priority" API**: the block manager is internal, not exposed.

Best for: if you scale to multiple concurrent writing sessions, or want block-granular eviction. Overkill for a single-session test on a 4060.

### TGI (Text Generation Inference, HuggingFace)

- Similar to vLLM in capability. Request/response API with prefix caching.
- Less granular control than vLLM's PagedAttention.

## The adapter we'd build

A thin layer between writing-assistant and the inference engine:

```
writing-assistant (file watcher, events)
        │
        ▼
adapter (session manager)
  - Creates a session on first event
  - Constructs prompt: PERSONALITIES (fixed) + DOCUMENT (sliding window) + RECENT_EVENTS (last N) + CURRENT_EVENT
  - POSTs to llama-server with cache_prompt: true
  - Prefix matches cache → only new tokens computed → fast
  - Returns response to writing-assistant
  - Appends response to session context
  - Next event: same prefix + new event tokens → cache hit → fast
        │
        ▼
llama-server (KV cache in VRAM)
  - Slot holds the session's KV cache
  - Prefix tokens: cached, skipped
  - New tokens: computed, appended
  - Generation: autoregressive, appended
```

## The cache eviction question (when you do need it)

If context grows beyond the window (long doc, many events), you'd manually manage what's in the prompt rather than rely on automatic eviction. Since you control the prompt construction:

```
PROMPT = PERSONALITIES (fixed, pinned)
       + DOCUMENT (sliding window of recent N paragraphs)
       + RECENT_EVENTS (last 5)
       + CURRENT_EVENT
```

You rebuild the prompt each time, but the **prefix** (personalities + early document) stays stable, so the KV cache hits on that prefix and only the tail is recomputed. You're manually doing what an eviction policy would do, but with full control over what stays and what goes.

This is better than automatic LRU eviction because you know what matters:
- Personality prompts are always relevant → pinned, never evicted
- Document context has a natural sliding window (recent paragraphs)
- Old events can be dropped
- An LRU cache doesn't know that personality prompts are more important than event 47

## The "slots fed by a dirty tester" idea

llama.cpp slots could be fed by a process that fragments the context deliberately:

- **Slot 1**: personality prompts + system prompt (pinned, never evicted)
- **Slot 2**: document context (sliding window, managed by the adapter)
- **Slot 3**: recent events ring buffer (fixed size, FIFO eviction)

The adapter would manage which tokens go to which slot and how they're composed for generation. This is speculative — llama.cpp doesn't natively support multi-slot composition for a single generation. But the concept maps to how you'd want to control cache lifecycle:

- Priority context (personalities) → pinned
- Semi-stable context (document) → evictable but low priority
- Ephemeral context (events) → evict first, FIFO

This would need either:
- A custom fork of llama.cpp that exposes slot-level cache management
- A wrapper that reconstructs the prompt from fragments and relies on prefix matching to avoid recompute (works today, less elegant)

## What doesn't exist but should

A cache manager API that exposes:

```python
session = create_session(max_tokens=16000)
session.push(priority="pinned", tokens=personality_prompts)    # never evicted
session.push(priority="normal", tokens=document_context)       # evictable
session.push(priority="low", tokens=event_1)                   # evict first
session.generate()                                              # appends to cache
session.push(priority="low", tokens=event_2)                    # evicts event_1 if needed
session.generate()
session.evict(older_than="event", count=5)                     # manual control
session.push(priority="low", tokens=event_3)
session.generate()
session.close()                                                 # cache dies
```

The pieces exist in research (PagedAttention, RingAttention, cache-aware scheduling) but no inference engine exposes a "push tokens with priority, evict by policy" API. This is the missed opportunity.

## Test plan for the 4060

1. **Install llama.cpp**
   ```bash
   # Build from source or use prebuilt binary
   git clone https://github.com/ggerganov/llama.cpp
   cd llama.cpp && make GGML_CUDA=1
   ```

2. **Download a small GGUF model**
   ```bash
   # Qwen2.5-1.5B-Instruct (~1GB in Q4_K_M)
   # or Phi-3-mini-4k-instruct (~2.5GB in Q4_K_M)
   huggingface-cli download Qwen/Qwen2.5-1.5B-Instruct-GGUF qwen2.5-1.5b-instruct-q4_k_m.gguf
   ```

3. **Start the server with prompt caching**
   ```bash
   ./llama-server \
     --model qwen2.5-1.5b-instruct-q4_k_m.gguf \
     --ctx-size 8192 \
     --prompt-cache-all \
     --port 8080
   ```

4. **Test the latency difference**
   ```bash
   # Cold request (full forward pass, slow):
   time curl -s http://localhost:8080/completion \
     -H 'Content-Type: application/json' \
     -d '{"prompt": "You are a writing assistant. [long context here] React to this edit: ...", "cache_prompt": true}'

   # Warm request (prefix matches cache, only new tokens computed, fast):
   time curl -s http://localhost:8080/completion \
     -H 'Content-Type: application/json' \
     -d '{"prompt": "You are a writing assistant. [long context here] React to this edit: ... [previous response] React to this new edit: ...", "cache_prompt": true}'
   ```

   You should see the warm request is significantly faster — the prefix's KV cache is reused, only the new tokens are computed.

5. **Write the adapter**
   - Node or Python script
   - Connects to writing-assistant's `/next-event` long-poll
   - Maintains session state (accumulated context)
   - Constructs prompt: personalities + document + events
   - POSTs to llama-server with `cache_prompt: true`
   - Returns response to chat or writes to document
   - ~200 lines

6. **Test end-to-end**
   - Start writing-assistant + llama-server
   - Edit a markdown file, save
   - Adapter receives event, sends to llama-server
   - First reaction: slow (cold cache)
   - Second reaction: fast (warm cache)
   - Measure the latency difference

## Models that fit on 8GB VRAM (4-bit quant)

| Model | Size (Q4) | Context | KV cache (8K) | Fits? |
|-------|-----------|---------|---------------|-------|
| Qwen2.5-1.5B-Instruct | ~1GB | 32K | ~0.5GB | Yes, lots of headroom |
| Phi-3-mini-4k-instruct | ~2.5GB | 4K | ~0.3GB | Yes, limited context |
| Qwen2.5-3B-Instruct | ~2GB | 32K | ~1GB | Yes |
| Llama-3.2-3B-Instruct | ~2GB | 8K | ~0.5GB | Yes |
| Qwen2.5-7B-Instruct | ~4.5GB | 8K | ~2GB | Tight but possible |

Recommend starting with Qwen2.5-1.5B or 3B — good instruction following, decent context, lots of VRAM headroom for the KV cache.

## The industry take (for context)

The industry is choosing not to expose live KV cache sessions because:
- Holding warm VRAM per session reduces GPU multiplexing 10-100x
- Commercial APIs optimize for throughput (serve many users), not latency (serve one user well)
- Prefix caching (what OpenAI/Anthropic offer) is the compromise — compute savings without holding state

This is a real limitation, not just economics. Even with infinite VRAM, LLMs have no mechanism for consolidating experience into persistent state — the weights don't change during inference. But for our use case (single session, hours, controlled context), that doesn't matter. We control what fits in the context window. We just need the compute to be incremental, not full-rebuild.

The "10x data centers" take is valid for making live KV sessions commercially viable. It's not needed for our case — we self-host, one session, one GPU, small model, small context. The tech exists today. It's just not packaged as a product.

## Next steps

1. Set up llama.cpp on the 4060 system
2. Download Qwen2.5-1.5B or 3B
3. Test cold vs warm latency
4. Write the adapter
5. Connect to writing-assistant
6. If it works, consider scaling up (bigger model, vLLM, cloud GPU)
