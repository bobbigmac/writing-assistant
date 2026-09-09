#!/usr/bin/env python3
"""Entry point for the live LLM backend.

Usage:
  # Start the HTTP server (exposes /push, /generate, /react, /health)
  python -m llm.run serve

  # Run the adapter (polls writing-assistant, prints reactions)
  python -m llm.run adapter

  # Both (server in background thread, adapter in foreground)
  python -m llm.run both

  # Smoke test (load model, push some text, generate, print stats)
  python -m llm.run smoke

Environment variables:
  WAS_LLM_MODEL       Path to GGUF model (default: models/qwen2.5-3b-instruct-q4_k_m.gguf)
  WAS_LLM_CTX         Context window size (default: 8192)
  WAS_LLM_GPU_LAYERS  GPU layers to offload, -1 = all (default: -1)
  WAS_LLM_HOST        Server host (default: 127.0.0.1)
  WAS_LLM_PORT        Server port (default: 3849)
  WAS_URL             writing-assistant URL (default: http://127.0.0.1:3848)
"""
from __future__ import annotations

import logging
import sys
import time

from .config import Config
from .engine import LlmEngine
from .session import SessionManager


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_smoke() -> int:
    """Smoke test: load model, push text, generate, print stats."""
    config = Config()
    engine = LlmEngine(config.llm)

    print(f"Loading model: {config.llm.model_path}")
    print(f"  ctx={config.llm.n_ctx}, gpu_layers={config.llm.n_gpu_layers}, flash_attn={config.llm.flash_attn}")

    t0 = time.perf_counter()
    engine.load()
    load_time = time.perf_counter() - t0
    print(f"  loaded in {load_time:.2f}s")

    # Push a system prompt
    prompt = "You are a helpful writing assistant. Respond concisely.\n\n"
    print(f"\nPushing prompt ({len(prompt)} chars)...")
    t0 = time.perf_counter()
    engine.push_text(prompt, add_bos=True)
    push_time = time.perf_counter() - t0
    print(f"  pushed in {push_time*1000:.1f}ms, cache now {engine.cache_token_count} tokens")

    # Generate
    print("\nGenerating (max 128 tokens)...")
    t0 = time.perf_counter()
    result = engine.generate(max_tokens=128, stop=["\n\n\n"])
    gen_time = time.perf_counter() - t0
    print(f"  generated {len(result.tokens)} tokens in {gen_time:.2f}s ({result.tokens_per_second:.1f} tok/s)")
    print(f"  finish_reason: {result.finish_reason}")
    print(f"  cache now {engine.cache_token_count} tokens")
    print(f"\n--- Output ---\n{result.text}\n--- End ---\n")

    # Push more text (incremental — prefix is cached)
    more = "Now tell me about cats. "
    print(f"Pushing more text ({len(more)} chars) — prefix should be cached...")
    t0 = time.perf_counter()
    engine.push_text(more)
    push_time2 = time.perf_counter() - t0
    print(f"  pushed in {push_time2*1000:.1f}ms, cache now {engine.cache_token_count} tokens")

    # Generate again
    print("\nGenerating (max 128 tokens)...")
    t0 = time.perf_counter()
    result2 = engine.generate(max_tokens=128, stop=["\n\n\n"])
    gen_time2 = time.perf_counter() - t0
    print(f"  generated {len(result2.tokens)} tokens in {gen_time2:.2f}s ({result2.tokens_per_second:.1f} tok/s)")
    print(f"  cache now {engine.cache_token_count} tokens")
    print(f"\n--- Output ---\n{result2.text}\n--- End ---\n")

    print("\nSmoke test passed.")
    return 0


def run_serve() -> int:
    """Run the HTTP server."""
    from .server import LlmServer

    config = Config()
    server = LlmServer(config)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()
    return 0


def run_adapter() -> int:
    """Run the event adapter (polls writing-assistant, drives reactions)."""
    from .adapter import WritingAssistantAdapter

    config = Config()
    engine = LlmEngine(config.llm)
    engine.load()
    session = SessionManager(
        engine,
        max_events=config.adapter.max_events_in_context,
        max_doc_lines=config.adapter.max_doc_lines,
        target_words=config.llm.target_words,
    )
    session.initialize()

    adapter = WritingAssistantAdapter(config.adapter, session)
    try:
        adapter.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        adapter.stop()
    return 0


def run_both() -> int:
    """Run server in a background thread, adapter in foreground."""
    import threading

    from .adapter import WritingAssistantAdapter
    from .server import LlmServer

    config = Config()
    server = LlmServer(config)

    # Load model and init session before starting server thread
    server.engine.load()
    server.session.initialize()

    # Start server in background thread
    import http.server
    handler = server._make_handler()
    httpd = http.server.ThreadingHTTPServer(
        (config.server.host, config.server.port),
        handler,
    )
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print(f"Server on http://{config.server.host}:{config.server.port}")

    # Run adapter in foreground
    adapter = WritingAssistantAdapter(config.adapter, server.session)
    try:
        adapter.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        adapter.stop()
        httpd.shutdown()
    return 0


def main() -> int:
    setup_logging()

    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1].lower()
    if cmd == "smoke":
        return run_smoke()
    elif cmd == "serve":
        return run_serve()
    elif cmd == "adapter":
        return run_adapter()
    elif cmd == "both":
        return run_both()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
