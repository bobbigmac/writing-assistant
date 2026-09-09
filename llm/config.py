"""Configuration for the live LLM backend."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LlmConfig:
    """Model and inference configuration."""

    # Model file path (GGUF). Defaults to models/ subdir.
    model_path: str = _env(
        "WAS_LLM_MODEL",
        str(Path(__file__).resolve().parent.parent / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf"),
    )

    # Context window size in tokens. 8K is comfortable for 3B on 8GB.
    n_ctx: int = _env_int("WAS_LLM_CTX", 8192)

    # GPU layers to offload. -1 = all layers (full GPU offload).
    n_gpu_layers: int = _env_int("WAS_LLM_GPU_LAYERS", -1)

    # Generation defaults
    n_threads: int = _env_int("WAS_LLM_THREADS", 4)
    temperature: float = float(_env("WAS_LLM_TEMP", "0.7"))
    top_p: float = float(_env("WAS_LLM_TOP_P", "0.8"))
    top_k: int = _env_int("WAS_LLM_TOP_K", 40)
    # Qwen2.5 docs recommend repetition_penalty=1.05 for general chat.
    # We need stronger anti-repetition for short reactive feedback.
    repeat_penalty: float = float(_env("WAS_LLM_REPEAT_PENALTY", "1.1"))
    # frequency_penalty: penalizes tokens proportional to how often they appeared.
    # presence_penalty: penalizes any token that has appeared at all.
    # Together these break repetition loops that repeat_penalty alone can't.
    frequency_penalty: float = float(_env("WAS_LLM_FREQ_PENALTY", "0.2"))
    presence_penalty: float = float(_env("WAS_LLM_PRESENCE_PENALTY", "0.2"))
    # No artificial token cap — length is guided via the prompt (target_words).
    # This is a safety ceiling only, set high enough to never interfere.
    max_tokens: int = _env_int("WAS_LLM_MAX_TOKENS", 1024)

    # Target word count for reactions. Injected into the prompt as
    # "Keep your response under N words." This is the primary length
    # control — the model stops via EOS, not a hard token cutoff.
    # Different use cases (live feedback vs TODO help vs long-form)
    # can override via env var.
    target_words: int = _env_int("WAS_LLM_TARGET_WORDS", 150)

    # Flash attention (faster, less VRAM for long context)
    flash_attn: bool = _env_bool("WAS_LLM_FLASH_ATTN", True)


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server configuration for the LLM backend."""

    host: str = _env("WAS_LLM_HOST", "127.0.0.1")
    port: int = _env_int("WAS_LLM_PORT", 3849)


@dataclass(frozen=True)
class AdapterConfig:
    """Configuration for the writing-assistant event adapter."""

    # writing-assistant service URL
    was_url: str = _env("WAS_URL", "http://127.0.0.1:3848")

    # How many recent events to include in context
    max_events_in_context: int = _env_int("WAS_ADAPTER_MAX_EVENTS", 5)

    # How many lines of document to include (sliding window from the end)
    max_doc_lines: int = _env_int("WAS_ADAPTER_MAX_DOC_LINES", 80)


@dataclass
class Config:
    llm: LlmConfig = field(default_factory=LlmConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
