"""Session manager — three cache strategies for a live KV-cache session.

Cache layout (single sequence, seq_id=0):
  [pinned prefix: system prompt + personalities]  ← pushed once, never touched
  [document snapshot]                              ← pushed once, stays cached
  [edit log entries]                               ← appended one at a time
  [reaction prompt + generation]                   ← appended per reaction

Three strategies for handling a new edit event:

1. APPEND-ONLY (audit log): Just append the diff as a compact edit entry.
   The document snapshot stays as-is. The model sees the original doc + a
   running log of changes. Fast (only the diff is computed). Best for
   small edits (typos, small additions).

2. COMMON-PREFIX TRUNCATION: When the document changes in a way we want
   accurate state for, find where old and new document diverge, seq_rm
   from that point, re-eval the new tail. The prefix KV (system prompt +
   unchanged doc prefix) stays hot. Best for structural edits.

3. FULL REBUILD (compaction): When the cache fills past the threshold,
   reset and re-push: pinned prefix + current document snapshot + recent
   edit summaries. Amortized cost is low since it's infrequent.

Decision logic:
  - If cache > rebuild_threshold full: FULL REBUILD
  - If edit is append-only (new doc starts with old doc): APPEND-ONLY
  - If edit diff < append_only_threshold_chars: APPEND-ONLY
  - Otherwise: COMMON-PREFIX TRUNCATION
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .engine import LlmEngine, GenerationResult
from .personalities import PERSONALITY_PROMPT

logger = logging.getLogger(__name__)


@dataclass
class SessionEvent:
    """A single edit event in the session's edit log."""
    event_id: int
    kind: str
    summary: str
    diff_text: str


@dataclass
class SessionState:
    """Logical state of the writing session."""

    # Pinned prefix (personalities + system prompt) — pushed once, never evicted
    pinned_text: str = ""
    pinned_tokens: int = 0
    pinned_pushed: bool = False

    # Document snapshot — the document state as it exists in the cache.
    # This may be stale (append-only mode keeps the original snapshot)
    # or current (truncation mode updates it).
    doc_text: str = ""
    doc_tokens: int = 0  # token count of the doc block in the cache
    doc_pushed: bool = False

    # Edit log — appended entries describing each edit
    edit_log: list[SessionEvent] = field(default_factory=list)
    edit_log_tokens: int = 0  # total tokens of all edit entries in cache

    # Track the total cache position breakdown for truncation
    # Position after pinned prefix
    pinned_end_pos: int = 0
    # Position after document snapshot
    doc_end_pos: int = 0
    # Position after edit log (current end of cache before generation)
    edit_log_end_pos: int = 0


class SessionManager:
    """Manages a single writing session's context and cache lifecycle."""

    def __init__(
        self,
        engine: LlmEngine,
        max_events: int = 5,
        max_doc_lines: int = 80,
        target_words: int = 150,
        append_only_threshold_chars: int = 500,
        rebuild_threshold: float = 0.75,
    ):
        self._engine = engine
        self._max_events = max_events
        self._max_doc_lines = max_doc_lines
        self._target_words = target_words
        self._append_only_threshold = append_only_threshold_chars
        self._rebuild_threshold = rebuild_threshold
        self._state = SessionState()

    @property
    def engine(self) -> LlmEngine:
        return self._engine

    @property
    def state(self) -> SessionState:
        return self._state

    def initialize(self) -> None:
        """Push the pinned prefix onto the cache. Called once at session start."""
        if self._state.pinned_pushed:
            return

        pinned = PERSONALITY_PROMPT
        self._state.pinned_text = pinned
        tokens = self._engine.tokenize(pinned, add_bos=True)
        self._engine.push(tokens)
        self._state.pinned_tokens = len(tokens)
        self._state.pinned_pushed = True
        self._state.pinned_end_pos = self._engine.cache_token_count
        logger.info(
            "Session initialized: pinned prefix pushed (%d tokens, cache now %d/%d)",
            self._state.pinned_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

    def _window(self, full_text: str) -> str:
        """Take the last N lines as the sliding window."""
        lines = full_text.split("\n")
        if len(lines) > self._max_doc_lines:
            return "\n".join(lines[-self._max_doc_lines:])
        return full_text

    def _format_document_block(self, text: str) -> str:
        """Format the document window as a context block."""
        if not text:
            return ""
        return f"\n\n[Current document]\n{text}\n[End document]\n"

    def _format_edit_entry(self, event: SessionEvent) -> str:
        """Format a single edit as a compact audit log entry."""
        return f"\n[Edit #{event.event_id} ({event.kind})]\n{event.diff_text}\n"

    def _format_reaction_prompt(self) -> str:
        """Format the instruction to react (pushed right before generate)."""
        return (
            f"\n\nReact to the latest edit above. Keep your response under {self._target_words} words. "
            f"Write your feedback now:\n"
        )

    def _push_document(self, text: str) -> None:
        """Push the document block onto the cache."""
        block = self._format_document_block(text)
        if block:
            tokens = self._engine.tokenize(block)
            self._engine.push(tokens)
            self._state.doc_tokens = len(tokens)
            self._state.doc_end_pos = self._engine.cache_token_count
            self._state.doc_pushed = True

    def _push_edit_entry(self, event: SessionEvent) -> None:
        """Push a single edit entry onto the cache (append)."""
        entry = self._format_edit_entry(event)
        tokens = self._engine.tokenize(entry)
        self._engine.push(tokens)
        self._state.edit_log_tokens += len(tokens)
        self._state.edit_log_end_pos = self._engine.cache_token_count

    def _full_rebuild(self, current_doc_text: str) -> None:
        """Strategy 3: Full rebuild with compaction.

        Reset the entire cache and re-push:
          pinned prefix + current document snapshot + recent edit summaries
        """
        logger.info("STRATEGY: full rebuild (compaction)")
        self._engine.reset()

        # Reset state
        self._state.pinned_pushed = False
        self._state.doc_pushed = False
        self._state.doc_tokens = 0
        self._state.edit_log_tokens = 0
        self._state.pinned_end_pos = 0
        self._state.doc_end_pos = 0
        self._state.edit_log_end_pos = 0

        # Re-push pinned prefix
        self.initialize()

        # Push current document as fresh snapshot
        self._state.doc_text = current_doc_text
        self._push_document(current_doc_text)

        # Push a summary of recent edits (not the full log)
        if self._state.edit_log:
            recent = self._state.edit_log[-self._max_events:]
            summary_parts = ["[Recent edit summary]\n"]
            for ev in recent:
                # Just the summary line, not the full diff
                summary_parts.append(f"Edit #{ev.event_id}: {ev.summary}\n")
            summary_parts.append("[End summary]\n")
            summary_text = "\n".join(summary_parts)
            tokens = self._engine.tokenize(summary_text)
            self._engine.push(tokens)
            self._state.edit_log_tokens = len(tokens)
            self._state.edit_log_end_pos = self._engine.cache_token_count

        logger.info(
            "Cache rebuilt: pinned=%d, doc=%d, edits=%d (total %d/%d)",
            self._state.pinned_tokens,
            self._state.doc_tokens,
            self._state.edit_log_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

    def _append_only(self, event: SessionEvent) -> None:
        """Strategy 1: Append-only audit log.

        Just append the edit as a compact entry. The document snapshot
        stays as-is. The model sees the original doc + a running log of
        changes. Only the diff tokens are computed.
        """
        logger.info("STRATEGY: append-only (audit log)")
        # Ensure document is pushed (might not be if this is the first event)
        if not self._state.doc_pushed:
            self._push_document(self._state.doc_text)

        # Append the edit entry
        self._push_edit_entry(event)

        logger.info(
            "Appended edit #%d (%d tokens, edit log now %d tokens, cache %d/%d)",
            event.event_id,
            self._state.edit_log_tokens,
            self._state.edit_log_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
        )

    def _common_prefix_truncate(self, new_doc_text: str, event: SessionEvent) -> None:
        """Strategy 2: Common-prefix truncation.

        Find where old and new document diverge, seq_rm from that point,
        re-eval the new tail. The prefix KV (system prompt + unchanged
        doc prefix) stays hot in VRAM.
        """
        old_doc = self._state.doc_text
        new_doc = new_doc_text

        # Find common prefix at the character level
        # (token-level would be more precise but char-level is simpler and
        #  we tokenize the tail anyway)
        common_len = 0
        min_len = min(len(old_doc), len(new_doc))
        while common_len < min_len and old_doc[common_len] == new_doc[common_len]:
            common_len += 1

        # We need to find the token boundary that corresponds to the
        # common prefix. Since we pushed the document as a formatted block,
        # we need to re-tokenize the common prefix to find how many tokens
        # it corresponds to.
        old_block = self._format_document_block(old_doc)
        new_block = self._format_document_block(new_doc)

        # Tokenize both to find the common token prefix
        old_tokens = self._engine.tokenize(old_block)
        new_tokens = self._engine.tokenize(new_block)

        # Find common token prefix length
        common_tokens = 0
        min_tokens = min(len(old_tokens), len(new_tokens))
        while common_tokens < min_tokens and old_tokens[common_tokens] == new_tokens[common_tokens]:
            common_tokens += 1

        # The position in the cache where the document starts
        doc_start = self._state.pinned_end_pos
        # The position after the common prefix
        truncate_pos = doc_start + common_tokens

        logger.info(
            "STRATEGY: common-prefix truncation (common=%d tokens, truncate from pos %d, recompute %d tokens)",
            common_tokens,
            truncate_pos,
            len(new_tokens) - common_tokens,
        )

        # Truncate the cache from the divergence point
        # This removes: old doc tail + all edit log + any previous generation
        self._engine.truncate(truncate_pos)

        # Push the new document tail (from common_tokens onward)
        tail_tokens = new_tokens[common_tokens:]
        if tail_tokens:
            self._engine.push(tail_tokens)

        # Update state
        self._state.doc_text = new_doc_text
        self._state.doc_tokens = len(new_tokens)
        self._state.doc_end_pos = self._engine.cache_token_count
        self._state.doc_pushed = True

        # Reset edit log (it was truncated)
        self._state.edit_log = []
        self._state.edit_log_tokens = 0
        self._state.edit_log_end_pos = self._engine.cache_token_count

        # Push the current event as the first entry in the new edit log
        self._push_edit_entry(event)

        logger.info(
            "Truncated + re-pushed: doc=%d tokens, edit=%d tokens (cache %d/%d)",
            self._state.doc_tokens,
            self._state.edit_log_tokens,
            self._engine.cache_token_count,
            self._engine.n_ctx,
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
        """React to a new edit event using the appropriate cache strategy.

        Decision logic:
          1. If cache > rebuild_threshold full: FULL REBUILD
          2. If this is the first event (no doc pushed yet): push doc + append edit
          3. If new doc starts with old doc (append-only edit): APPEND-ONLY
          4. If edit diff < append_only_threshold_chars: APPEND-ONLY
          5. Otherwise: COMMON-PREFIX TRUNCATION
        """
        new_doc = self._window(full_document)
        event = SessionEvent(event_id=event_id, kind=kind, summary=summary, diff_text=diff_text)

        # Ensure pinned prefix is pushed
        if not self._state.pinned_pushed:
            self.initialize()

        # Strategy 3: Full rebuild if cache is too full
        cache_fraction = self._engine.cache_token_count / self._engine.n_ctx
        if cache_fraction > self._rebuild_threshold:
            logger.info(
                "Cache %.0f%% full (%d/%d), triggering full rebuild",
                cache_fraction * 100,
                self._engine.cache_token_count,
                self._engine.n_ctx,
            )
            self._state.edit_log.append(event)
            self._full_rebuild(new_doc)
        elif not self._state.doc_pushed:
            # First event: push document snapshot, then append edit
            logger.info("STRATEGY: initial (push doc + append edit)")
            self._state.doc_text = new_doc
            self._push_document(new_doc)
            self._push_edit_entry(event)
        elif new_doc.startswith(self._state.doc_text) and len(new_doc) > len(self._state.doc_text):
            # Append-only edit: new doc is old doc + new content
            # Update doc_text but don't re-push (the snapshot stays as the
            # original; the appended content goes into the edit log)
            logger.info("STRATEGY: append-only (document grew)")
            # Update doc_text to reflect current state, but keep the cached
            # snapshot. The edit log captures what changed.
            self._state.doc_text = new_doc
            self._push_edit_entry(event)
        elif len(diff_text) < self._append_only_threshold:
            # Small edit: append as audit log entry
            self._state.doc_text = new_doc
            self._append_only(event)
        else:
            # Larger edit: use common-prefix truncation for accurate doc state
            self._common_prefix_truncate(new_doc, event)

        # Track the event in the edit log
        self._state.edit_log.append(event)
        if len(self._state.edit_log) > self._max_events * 3:
            # Keep the log from growing unbounded (but allow more than
            # max_events since the log is the primary context in append-only mode)
            self._state.edit_log = self._state.edit_log[-self._max_events * 3:]

        # Push reaction prompt
        prompt = self._format_reaction_prompt()
        self._engine.push_text(prompt)
        logger.info(
            "About to generate: cache=%d/%d, remaining=%d, target_words=%d",
            self._engine.cache_token_count,
            self._engine.n_ctx,
            self._engine.remaining_context,
            self._target_words,
        )

        # Generate
        result = self._engine.generate(
            max_tokens=max_tokens,
            stop=["\n\n\n", "\n[Current document", "\n[Recent edit", "\n[End ", "\n[Edit #", "Human:", "\n\nHuman"],
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
