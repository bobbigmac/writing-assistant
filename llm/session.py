"""Session manager — constructs prompts, manages the sliding window,
and drives the engine's push/generate cycle.

The session holds the logical conversation state:
  - Pinned prefix: personality prompts + system instructions (never evicted)
  - Document window: sliding window of recent document content
  - Event history: ring buffer of recent events
  - Current event: the event being reacted to

The session translates this logical structure into token sequences and
pushes them onto the engine's KV cache. When the context window fills,
it resets the cache and re-pushes the pinned prefix + current window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .engine import LlmEngine, GenerationResult
from .personalities import PERSONALITY_PROMPT, get_personality_block

logger = logging.getLogger(__name__)


@dataclass
class DocumentWindow:
    """Sliding window of document content."""

    text: str = ""
    # Token count of the current document text in the cache
    cached_tokens: int = 0


@dataclass
class SessionEvent:
    """A single event in the session's event history."""

    event_id: int
    kind: str
    summary: str
    diff_text: str


@dataclass
class SessionState:
    """Logical state of the writing session."""

    # The pinned prefix (personalities + system prompt) — pushed once, never evicted
    pinned_text: str = ""
    pinned_tokens: int = 0

    # Document sliding window
    document: DocumentWindow = field(default_factory=DocumentWindow)

    # Event ring buffer (most recent first)
    events: list[SessionEvent] = field(default_factory=list)
    events_cached_tokens: int = 0

    # Whether the pinned prefix has been pushed to the cache
    pinned_pushed: bool = False
    # Whether the document window has been pushed to the cache
    document_pushed: bool = False
    # Whether the event history has been pushed to the cache
    events_pushed: bool = False


class SessionManager:
    """Manages a single writing session's context and cache lifecycle.

    The flow for each event:
      1. Ensure pinned prefix is in the cache (push once at session start).
      2. Update the document window (if changed, reset and re-push).
      3. Append the new event to the event ring buffer.
      4. Push the event tokens onto the cache.
      5. Generate a reaction.
      6. The generation output stays in the cache (becomes part of context).

    When the context window fills, we reset the cache and re-push:
      pinned_prefix + current_document_window + recent_events
    """

    def __init__(self, engine: LlmEngine, max_events: int = 5, max_doc_lines: int = 80, target_words: int = 150):
        self._engine = engine
        self._max_events = max_events
        self._max_doc_lines = max_doc_lines
        self._target_words = target_words
        self._state = SessionState()

    @property
    def engine(self) -> LlmEngine:
        return self._engine

    @property
    def state(self) -> SessionState:
        return self._state

    def initialize(self) -> None:
        """Push the pinned prefix (personalities + system prompt) onto the cache.

        This is called once at session start. The prefix stays in the cache
        for the entire session — it's never recomputed.
        """
        if self._state.pinned_pushed:
            return

        pinned = PERSONALITY_PROMPT
        self._state.pinned_text = pinned
        tokens = self._engine.tokenize(pinned, add_bos=True)
        self._engine.push(tokens)
        self._state.pinned_tokens = len(tokens)
        self._state.pinned_pushed = True
        logger.info(
            "Session initialized: pinned prefix pushed (%d tokens, cache now %d/%d)",
            self._state.pinned_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

    def update_document(self, full_text: str) -> bool:
        """Update the document sliding window.

        If the document content has changed since the last push, we need to
        reset the cache and re-push everything (pinned + document + events).
        This is the "manual eviction" approach from the TODO — we control
        what stays and what goes, not an LRU policy.

        Returns True if the document changed (requiring a cache rebuild).
        """
        # Take the last N lines as the sliding window
        lines = full_text.split("\n")
        if len(lines) > self._max_doc_lines:
            window_text = "\n".join(lines[-self._max_doc_lines:])
        else:
            window_text = full_text

        if window_text == self._state.document.text:
            return False

        self._state.document.text = window_text
        self._state.document.cached_tokens = 0
        self._state.document_pushed = False
        return True

    def add_event(self, event_id: int, kind: str, summary: str, diff_text: str) -> None:
        """Add an event to the ring buffer."""
        event = SessionEvent(
            event_id=event_id,
            kind=kind,
            summary=summary,
            diff_text=diff_text,
        )
        self._state.events.append(event)
        # Trim to max events (ring buffer)
        if len(self._state.events) > self._max_events:
            self._state.events = self._state.events[-self._max_events:]
        self._state.events_pushed = False
        self._state.events_cached_tokens = 0

    def _rebuild_cache(self) -> None:
        """Reset the engine cache and re-push everything.

        Called when the document changes (can't just append — the old
        document tokens are stale). We reset and re-push:
          pinned_prefix + document_window + recent_events
        """
        logger.info("Rebuilding cache (document changed)")
        self._engine.reset()

        # Re-push pinned prefix
        self._state.pinned_pushed = False
        self.initialize()

        # Push document window
        doc_block = self._format_document_block()
        if doc_block:
            tokens = self._engine.tokenize(doc_block)
            self._engine.push(tokens)
            self._state.document.cached_tokens = len(tokens)
            self._state.document_pushed = True

        # Push event history
        events_block = self._format_events_block()
        if events_block:
            tokens = self._engine.tokenize(events_block)
            self._engine.push(tokens)
            self._state.events_cached_tokens = len(tokens)
            self._state.events_pushed = True

        logger.info(
            "Cache rebuilt: pinned=%d, doc=%d, events=%d (total %d/%d)",
            self._state.pinned_tokens,
            self._state.document.cached_tokens,
            self._state.events_cached_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

    def _push_new_event(self) -> None:
        """Push only the latest event onto the cache (incremental).

        Called when the document hasn't changed — we just append the new
        event tokens. The prefix and document are already cached.
        """
        # Ensure document is pushed (might not be if this is the first event)
        if not self._state.document_pushed:
            doc_block = self._format_document_block()
            if doc_block:
                tokens = self._engine.tokenize(doc_block)
                self._engine.push(tokens)
                self._state.document.cached_tokens = len(tokens)
            self._state.document_pushed = True

        # Push the latest event
        latest = self._state.events[-1] if self._state.events else None
        if latest:
            event_text = self._format_single_event(latest)
            tokens = self._engine.tokenize(event_text)
            self._engine.push(tokens)
            self._state.events_cached_tokens += len(tokens)
            self._state.events_pushed = True

    def _format_document_block(self) -> str:
        """Format the document window as a context block."""
        if not self._state.document.text:
            return ""
        return f"\n\n[Current document tail]\n{self._state.document.text}\n[End document]\n\n"

    def _format_events_block(self) -> str:
        """Format all events in the ring buffer as a context block."""
        if not self._state.events:
            return ""
        parts = ["[Recent edits]"]
        for event in self._state.events:
            parts.append(self._format_single_event(event))
        parts.append("[End recent edits]\n")
        return "\n".join(parts)

    def _format_single_event(self, event: SessionEvent) -> str:
        """Format a single event for the context."""
        return f"[Edit #{event.event_id} ({event.kind})]\n{event.diff_text}"

    def _format_reaction_prompt(self) -> str:
        """Format the instruction to react (pushed right before generate).

        The word count target is injected here, not as a token cap. The model
        is told to keep under N words and stops via EOS. Different use cases
        can change target_words via config.
        """
        return (
            f"\n\nReact to the latest edit above. Keep your response under {self._target_words} words. "
            f"Write your feedback now:\n"
        )

    def react_to_event(
        self,
        event_id: int,
        kind: str,
        summary: str,
        diff_text: str,
        full_document: str,
        max_tokens: Optional[int] = None,
    ) -> GenerationResult:
        """React to a new event.

        This is the main entry point. It:
          1. Updates the document window (rebuilds cache if changed)
          2. Adds the event to the ring buffer
          3. Pushes the event tokens (incrementally or via rebuild)
          4. Pushes the reaction prompt
          5. Generates a reaction

        Returns the generation result.
        """
        # Step 1: Update document window
        doc_changed = self.update_document(full_document)

        # Step 2: Add event to ring buffer
        self.add_event(event_id, kind, summary, diff_text)

        # Step 3: Push tokens
        if doc_changed or not self._state.pinned_pushed:
            # Document changed — need full cache rebuild
            self._rebuild_cache()
        else:
            # Document unchanged — just push the new event incrementally
            self._push_new_event()

        # Step 4: Check context budget
        if self._engine.remaining_context < (max_tokens or 64):
            logger.warning(
                "Context window nearly full (%d remaining), rebuilding",
                self._engine.remaining_context,
            )
            self._rebuild_cache()

        # Step 5: Push reaction prompt
        prompt = self._format_reaction_prompt()
        self._engine.push_text(prompt)
        logger.info(
            "About to generate: cache=%d/%d, remaining=%d, target_words=%d",
            self._engine.cache_token_count,
            self._engine.n_ctx,
            self._engine.remaining_context,
            self._target_words,
        )

        # Step 6: Generate
        result = self._engine.generate(
            max_tokens=max_tokens,
            stop=["\n\n\n", "\n[Current document", "\n[Recent edits", "\n[End "],
        )

        logger.info(
            "Reaction to event #%d: %d tokens, %.1f tok/s, cache %d/%d",
            event_id,
            len(result.tokens),
            result.tokens_per_second,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

        return result

    def reset(self) -> None:
        """Full session reset — clear everything."""
        self._engine.reset()
        self._state = SessionState()
        self.initialize()
