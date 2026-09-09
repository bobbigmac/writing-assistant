"""Adapter — connects writing-assistant's event stream to the LLM session.

Polls writing-assistant's /next-event long-poll endpoint, formats events
for the session manager, and prints reactions to stdout (or posts them back).

This implements the wait-react cycle from AGENTS.md:
  1. Long-poll /next-event?since=LAST_ID
  2. Fetch all events since LAST_ID (catch up on missed saves)
  3. React to the combined diff
  4. Update LAST_ID
  5. Loop forever
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Optional

from .config import AdapterConfig
from .session import SessionManager

logger = logging.getLogger(__name__)


class WritingAssistantAdapter:
    """Polls writing-assistant events and drives LLM reactions."""

    def __init__(self, config: AdapterConfig, session: SessionManager):
        self._config = config
        self._session = session
        self._last_id = 0
        self._running = False

    def _fetch(self, path: str, timeout: float = 300.0) -> dict:
        """Fetch JSON from writing-assistant."""
        url = f"{self._config.was_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _format_diff(self, event: dict) -> str:
        """Format an event's diff into text for the LLM context."""
        diff_entries = event.get("diff", [])
        if not diff_entries:
            return event.get("summary", {}).get("kind", "changed")

        parts = []
        for entry in diff_entries:
            line_type = entry.get("type", "context")
            line = entry.get("line", "")
            prefix = {"added": "+", "removed": "-", "context": " "}.get(line_type, " ")
            parts.append(f"{prefix} {line}")
        return "\n".join(parts)

    def _react_to_event(self, event: dict) -> Optional[str]:
        """Send an event to the session and get a reaction."""
        try:
            result = self._session.react_to_event(
                event_id=event["id"],
                kind=event.get("kind", "changed"),
                summary=json.dumps(event.get("summary", {})),
                diff_text=self._format_diff(event),
                full_document=event.get("snapshot", ""),
            )
            return result.text
        except Exception:
            logger.exception("Reaction failed for event %s", event.get("id"))
            return None

    def run(self) -> None:
        """Main wait-react loop. Runs forever."""
        self._running = True
        logger.info("Adapter started, polling %s", self._config.was_url)

        # Get the startup event ID as our starting point
        try:
            state = self._fetch("/state", timeout=5)
            events = state.get("recentEvents", [])
            if events:
                self._last_id = max(e["id"] for e in events)
                logger.info("Starting from event ID %d", self._last_id)
        except Exception:
            logger.warning("Could not fetch initial state, starting from 0")

        while self._running:
            try:
                # Phase 1: Long-poll for the next event
                event = self._fetch(
                    f"/next-event?since={self._last_id}",
                    timeout=300,
                )

                # Phase 2: Fetch ALL events since last ID (catch up)
                all_events_resp = self._fetch(
                    f"/events?since={self._last_id}",
                    timeout=10,
                )
                new_events = all_events_resp.get("events", [])

                if not new_events:
                    new_events = [event]

                logger.info("Processing %d new event(s)", len(new_events))

                # Phase 3: React to each event
                for ev in new_events:
                    if ev.get("kind") == "startup":
                        self._last_id = max(self._last_id, ev["id"])
                        continue

                    if ev.get("kind") == "deleted":
                        logger.info("File deleted: %s", ev.get("path"))
                        self._last_id = max(self._last_id, ev["id"])
                        continue

                    reaction = self._react_to_event(ev)
                    if reaction:
                        logger.info("=== REACTION TO EDIT #%d ===\n%s\n=== END REACTION ===", ev["id"], reaction)
                        print(f"\n{'='*60}", flush=True)
                        print(f"Reaction to edit #{ev['id']}:", flush=True)
                        print(f"{'='*60}", flush=True)
                        print(reaction, flush=True)
                        print(f"{'='*60}\n", flush=True)

                    self._last_id = max(self._last_id, ev["id"])

            except Exception:
                if self._running:
                    logger.exception("Adapter loop error, retrying in 2s")
                    time.sleep(2)

    def stop(self) -> None:
        self._running = False
