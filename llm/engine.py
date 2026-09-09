"""LLM engine — wraps llama-cpp-python to expose a live KV-cache session.

This is the "closer to the metal" layer. A Llama context holds the KV cache
in VRAM for its lifetime. eval() appends tokens to the cache; generation
appends to it. The prefix is never recomputed.

The key operations:
  - push(tokens): eval tokens into the context, appending to the KV cache.
  - generate(max_tokens): autoregressive sampling loop, appends to cache.
  - kv_cache_tokens(): how many tokens are currently in the cache.
  - reset(): discard the entire context (new session).

This maps directly to the session.push() / session.generate() API from
TODO-DynamicLLM.md.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from llama_cpp import Llama

from .config import LlmConfig

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    """Result of a generate() call."""

    text: str
    tokens: list[int] = field(default_factory=list)
    tokens_per_second: float = 0.0
    elapsed_ms: float = 0.0
    prompt_eval_ms: float = 0.0
    finish_reason: str = "stop"


class LlmEngine:
    """Holds a single Llama context with a live KV cache in VRAM.

    The context is created once at startup and lives for the process lifetime.
    Tokens pushed via push() are eval'd into the context and their KV entries
    are appended to the cache. Generation appends its output tokens too.

    This is NOT request/response with prefix matching. The prefix KV is
    physically in VRAM — eval() only touches the new tokens.
    """

    def __init__(self, config: LlmConfig):
        self._config = config
        self._llm: Optional[Llama] = None
        self._n_ctx = config.n_ctx
        # Track how many tokens we've pushed (for cache management)
        self._cache_token_count = 0
        # Track the last generation's output tokens so we can rewind if needed
        self._last_generation_tokens: list[int] = []

    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    def load(self) -> None:
        """Load the model into VRAM with full GPU offload."""
        logger.info(
            "Loading model: %s (ctx=%d, gpu_layers=%d, flash_attn=%s)",
            self._config.model_path,
            self._config.n_ctx,
            self._config.n_gpu_layers,
            self._config.flash_attn,
        )
        t0 = time.perf_counter()

        kwargs = dict(
            model_path=self._config.model_path,
            n_ctx=self._config.n_ctx,
            n_gpu_layers=self._config.n_gpu_layers,
            n_threads=self._config.n_threads,
            verbose=False,
            # Don't pre-allocate a chat handler — we do raw eval/generate.
            logits_all=False,
        )
        # Flash attention reduces KV cache VRAM and speeds up long-context.
        # Supported in recent llama-cpp-python builds.
        if self._config.flash_attn:
            kwargs["flash_attn"] = True

        self._llm = Llama(**kwargs)
        elapsed = time.perf_counter() - t0
        logger.info("Model loaded in %.2fs", elapsed)

    def _ensure_loaded(self) -> Llama:
        if self._llm is None:
            raise RuntimeError("Engine not loaded. Call load() first.")
        return self._llm

    @property
    def cache_token_count(self) -> int:
        """Number of tokens currently in the KV cache."""
        return self._cache_token_count

    @property
    def n_ctx(self) -> int:
        return self._n_ctx

    @property
    def remaining_context(self) -> int:
        """Tokens remaining before we hit the context window limit."""
        return self._n_ctx - self._cache_token_count

    def tokenize(self, text: str, add_bos: bool = False) -> list[int]:
        """Tokenize text using the model's tokenizer."""
        llm = self._ensure_loaded()
        return llm.tokenize(
            text.encode("utf-8"),
            add_bos=add_bos,
            special=True,
        )

    def detokenize(self, tokens: list[int]) -> str:
        """Convert tokens back to text."""
        llm = self._ensure_loaded()
        return llm.detokenize(tokens).decode("utf-8", errors="replace")

    def push(self, tokens: list[int]) -> float:
        """Push tokens into the context, appending to the KV cache.

        This is the core operation: eval the new tokens against the existing
        cache. Only these tokens are computed; the prefix KV is already in VRAM.

        Returns the eval time in milliseconds.
        """
        llm = self._ensure_loaded()
        if not tokens:
            return 0.0

        # Check if we'd overflow the context window
        if self._cache_token_count + len(tokens) > self._n_ctx:
            raise OverflowError(
                f"Push of {len(tokens)} tokens would exceed context window "
                f"({self._cache_token_count + len(tokens)} > {self._n_ctx}). "
                f"Call reset() or manage the sliding window."
            )

        t0 = time.perf_counter()
        llm.eval(tokens)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self._cache_token_count += len(tokens)
        logger.debug(
            "pushed %d tokens (cache now %d/%d, %.1fms)",
            len(tokens),
            self._cache_token_count,
            self._n_ctx,
            elapsed_ms,
        )
        return elapsed_ms

    def push_text(self, text: str, add_bos: bool = False) -> float:
        """Tokenize and push text. Convenience wrapper around push()."""
        tokens = self.tokenize(text, add_bos=add_bos)
        return self.push(tokens)

    def generate(
        self,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repeat_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        stop: Optional[list[str]] = None,
    ) -> GenerationResult:
        """Generate tokens autoregressively from the current cache state.

        Each generated token is eval'd back into the cache, so the cache grows
        by max_tokens. The next push() or generate() starts from this new state.

        This is the "session.generate()" from the TODO.
        """
        llm = self._ensure_loaded()
        # max_tokens is a safety ceiling only. Length is guided by the prompt.
        # Default (from config) is high enough to never interfere with normal output.
        max_tokens = max_tokens if max_tokens is not None else self._config.max_tokens
        temperature = temperature if temperature is not None else self._config.temperature
        top_p = top_p if top_p is not None else self._config.top_p
        top_k = top_k if top_k is not None else self._config.top_k
        repeat_penalty = repeat_penalty if repeat_penalty is not None else self._config.repeat_penalty
        frequency_penalty = frequency_penalty if frequency_penalty is not None else self._config.frequency_penalty
        presence_penalty = presence_penalty if presence_penalty is not None else self._config.presence_penalty

        if self.remaining_context <= 0:
            raise OverflowError("Context window full. Call reset() before generating.")

        # Cap at remaining context
        max_tokens = min(max_tokens, self.remaining_context)

        t0 = time.perf_counter()
        generated_tokens: list[int] = []
        finish_reason = "stop"

        logger.info(
            "GENERATION STARTING: max_tokens=%d, temp=%.2f, top_p=%.2f, top_k=%d, "
            "repeat_penalty=%.2f, freq_penalty=%.2f, presence_penalty=%.2f, cache=%d/%d, stop=%s",
            max_tokens, temperature, top_p, top_k,
            repeat_penalty, frequency_penalty, presence_penalty,
            self._cache_token_count, self._n_ctx, stop,
        )

        for i in range(max_tokens):
            # Get logits for the next token from the current cache state.
            # sample() uses the logits of the last eval'd token.
            token_id = llm.sample(
                top_k=top_k,
                top_p=top_p,
                temp=temperature,
                repeat_penalty=repeat_penalty,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )

            # Check for EOS
            if token_id == llm.token_eos():
                finish_reason = "stop"
                logger.info("EOS token hit after %d tokens", len(generated_tokens))
                break

            generated_tokens.append(token_id)

            # Repetition detection: if the last 20 tokens contain a repeated
            # 8-token sequence, stop. This catches loops that penalties miss.
            if len(generated_tokens) >= 40:
                recent = generated_tokens[-40:]
                # Check if any 8-token subsequence appears twice in last 40 tokens
                seen = set()
                loop_detected = False
                for j in range(len(recent) - 8):
                    ngram = tuple(recent[j:j+8])
                    if ngram in seen:
                        loop_detected = True
                        break
                    seen.add(ngram)
                if loop_detected:
                    logger.warning("Repetition loop detected after %d tokens, stopping", len(generated_tokens))
                    finish_reason = "repetition"
                    break

            # Eval the generated token back into the cache
            llm.eval([token_id])
            self._cache_token_count += 1

            # Log progress every 10 tokens so we can see it's alive
            if (i + 1) % 10 == 0:
                partial_preview = self.detokenize(generated_tokens[-10:])
                logger.info(
                    "  gen progress: %d tokens, %.1fs, cache %d/%d, last: %r",
                    len(generated_tokens),
                    time.perf_counter() - t0,
                    self._cache_token_count,
                    self._n_ctx,
                    partial_preview[:80],
                )

            # Check stop strings
            if stop:
                partial = self.detokenize(generated_tokens)
                for s in stop:
                    if s in partial:
                        # Trim at the stop string
                        idx = partial.index(s)
                        partial = partial[:idx]
                        finish_reason = "stop"
                        logger.info("Stop string %r hit after %d tokens", s, len(generated_tokens))
                        result = GenerationResult(
                            text=partial,
                            tokens=generated_tokens,
                            tokens_per_second=len(generated_tokens) / max(time.perf_counter() - t0, 1e-6),
                            elapsed_ms=(time.perf_counter() - t0) * 1000,
                            finish_reason=finish_reason,
                        )
                        self._last_generation_tokens = generated_tokens
                        return result

        text = self.detokenize(generated_tokens)
        elapsed = time.perf_counter() - t0
        tps = len(generated_tokens) / max(elapsed, 1e-6)

        self._last_generation_tokens = generated_tokens
        logger.info(
            "generated %d tokens in %.2fs (%.1f tok/s), cache now %d/%d",
            len(generated_tokens),
            elapsed,
            tps,
            self._cache_token_count,
            self._n_ctx,
        )

        return GenerationResult(
            text=text,
            tokens=generated_tokens,
            tokens_per_second=tps,
            elapsed_ms=elapsed * 1000,
            finish_reason=finish_reason,
        )

    def reset(self) -> None:
        """Discard the entire KV cache and start fresh.

        llama-cpp-python's reset() clears the KV cache in-place without
        reloading the model. The context window is emptied but the model
        weights stay in VRAM.
        """
        llm = self._ensure_loaded()
        llm.reset()
        self._cache_token_count = 0
        self._last_generation_tokens = []
        logger.info("KV cache cleared (in-place reset)")

    def truncate(self, from_pos: int) -> None:
        """Surgically remove tokens from position `from_pos` to the end.

        Uses llama.cpp's seq_rm to remove KV cache entries without clearing
        the entire cache. The prefix (positions 0..from_pos-1) stays hot
        in VRAM. Only the tail is removed and must be re-eval'd.

        This is the common-prefix truncation strategy: when a document is
        edited in the middle, we keep the unchanged prefix KV and only
        recompute from the divergence point.

        After truncation, the next push() will eval tokens starting at
        from_pos, exactly where the old ones were removed.
        """
        llm = self._ensure_loaded()
        if from_pos < 0 or from_pos > self._cache_token_count:
            raise ValueError(f"from_pos {from_pos} out of range (cache has {self._cache_token_count} tokens)")

        if from_pos == self._cache_token_count:
            # Nothing to remove
            return

        if from_pos == 0:
            # Full reset
            self.reset()
            return

        # Use low-level seq_rm to remove from from_pos to end
        # llama-cpp-python's Llama.eval() internally calls kv_cache_seq_rm
        # before each batch, but we need to do it manually here.
        # The Llama wrapper stores ctx as llm._ctx.ctx (LlamaContext.ctx)
        import llama_cpp
        ctx = llm._ctx.ctx
        mem = llama_cpp.llama_get_memory(ctx)
        llama_cpp.llama_memory_seq_rm(mem, 0, from_pos, -1)

        removed = self._cache_token_count - from_pos
        self._cache_token_count = from_pos
        self._last_generation_tokens = []
        logger.info(
            "KV cache truncated: removed %d tokens from pos %d (cache now %d/%d)",
            removed,
            from_pos,
            self._cache_token_count,
            self._n_ctx,
        )

    def rewind_last_generation(self) -> None:
        """Remove the last generation's tokens from the logical cache count.

        Note: llama.cpp can't actually un-eval tokens from the KV cache.
        This only adjusts our bookkeeping so the next push/generate treats
        the cache as if the generation didn't happen. The actual VRAM KV
        entries remain but will be overwritten by subsequent evals.

        Use this when you want to generate a fresh response to the same
        prompt state without the previous generation polluting the context.
        """
        count = len(self._last_generation_tokens)
        if count > 0:
            self._cache_token_count -= count
            self._last_generation_tokens = []
            logger.debug(
                "Rewound %d generation tokens (logical cache now %d)",
                count,
                self._cache_token_count,
            )
