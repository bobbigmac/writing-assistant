"""Live KV-cache LLM backend for writing-assistant.

Holds a single llama.cpp context in VRAM for the duration of a writing session.
Tokens are pushed onto the cache via eval(); generation appends to it. The
prefix is never recomputed. This is the "session.push() / session.generate()"
API described in TODO-DynamicLLM.md.
"""
