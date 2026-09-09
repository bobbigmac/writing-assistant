"""HTTP server exposing the live LLM session API.

Endpoints:
  GET  /health            — engine status, cache size, VRAM
  POST /push              — body: {text} — push tokens onto the cache
  POST /generate          — body: {max_tokens, temperature, ...} — generate from cache
  POST /react             — body: {event_id, kind, summary, diff, document} — full reaction
  POST /apply-edit        — body: {path, search, replace} — search/replace in a file
  POST /reset             — clear the cache and session
  GET  /state             — session state (cache tokens, document, events)

This is a thin layer over the SessionManager. The adapter (or any client)
can use /react for the full flow, or /push + /generate for manual control.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import Config
from .engine import LlmEngine
from .session import SessionManager

logger = logging.getLogger(__name__)


class LlmServer:
    """HTTP server wrapping a SessionManager."""

    def __init__(self, config: Config):
        self._config = config
        self._engine = LlmEngine(config.llm)
        self._session = SessionManager(
            self._engine,
            max_events=config.adapter.max_events_in_context,
            max_doc_lines=config.adapter.max_doc_lines,
            target_words=config.llm.target_words,
            append_only_threshold_chars=config.adapter.append_only_threshold_chars,
            rebuild_threshold=config.adapter.rebuild_threshold,
        )
        self._httpd: Optional[ThreadingHTTPServer] = None

    @property
    def engine(self) -> LlmEngine:
        return self._engine

    @property
    def session(self) -> SessionManager:
        return self._session

    def start(self) -> None:
        """Load the model, initialize the session, and start serving."""
        self._engine.load()
        self._session.initialize()

        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(
            (self._config.server.host, self._config.server.port),
            handler,
        )
        logger.info(
            "LLM server listening on http://%s:%d",
            self._config.server.host,
            self._config.server.port,
        )
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug("HTTP %s: %s", self.address_string(), format % args)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                if length == 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {}

            def _send_json(self, status: int, data: dict) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send_json(204, {})

            def do_GET(self):
                from urllib.parse import urlparse
                path = urlparse(self.path).path

                if path == "/health":
                    self._send_json(200, {
                        "ok": server.engine.is_loaded,
                        "cache_tokens": server.engine.cache_token_count,
                        "n_ctx": server.engine.n_ctx,
                        "remaining": server.engine.remaining_context,
                        "model": server._config.llm.model_path,
                    })
                    return

                if path == "/state":
                    state = server.session.state
                    self._send_json(200, {
                        "pinned_pushed": state.pinned_pushed,
                        "pinned_tokens": state.pinned_tokens,
                        "document_pushed": state.document_pushed,
                        "document_tokens": state.document.cached_tokens,
                        "document_lines": len(state.document.text.split("\n")) if state.document.text else 0,
                        "events_count": len(state.events),
                        "events_cached_tokens": state.events_cached_tokens,
                        "cache_tokens": server.engine.cache_token_count,
                        "remaining": server.engine.remaining_context,
                    })
                    return

                self._send_json(404, {"error": "not found", "path": path})

            def do_POST(self):
                from urllib.parse import urlparse
                path = urlparse(self.path).path

                if path == "/push":
                    body = self._read_body()
                    text = body.get("text", "")
                    if not text:
                        self._send_json(400, {"error": "missing 'text'"})
                        return
                    try:
                        elapsed = server.engine.push_text(text)
                        self._send_json(200, {
                            "ok": True,
                            "pushed_tokens": True,
                            "elapsed_ms": elapsed,
                            "cache_tokens": server.engine.cache_token_count,
                        })
                    except OverflowError as e:
                        self._send_json(413, {"error": str(e)})
                    return

                if path == "/generate":
                    body = self._read_body()
                    try:
                        result = server.engine.generate(
                            max_tokens=body.get("max_tokens"),
                            temperature=body.get("temperature"),
                            top_p=body.get("top_p"),
                            top_k=body.get("top_k"),
                            repeat_penalty=body.get("repeat_penalty"),
                            stop=body.get("stop"),
                        )
                        self._send_json(200, {
                            "ok": True,
                            "text": result.text,
                            "tokens": len(result.tokens),
                            "tokens_per_second": result.tokens_per_second,
                            "elapsed_ms": result.elapsed_ms,
                            "finish_reason": result.finish_reason,
                            "cache_tokens": server.engine.cache_token_count,
                        })
                    except OverflowError as e:
                        self._send_json(413, {"error": str(e)})
                    return

                if path == "/react":
                    body = self._read_body()
                    required = ["event_id", "kind", "diff", "document"]
                    for key in required:
                        if key not in body:
                            self._send_json(400, {"error": f"missing '{key}'"})
                            return
                    try:
                        result = server.session.react_to_event(
                            event_id=body["event_id"],
                            kind=body["kind"],
                            summary=body.get("summary", ""),
                            diff_text=body["diff"],
                            full_document=body["document"],
                            max_tokens=body.get("max_tokens"),
                        )
                        self._send_json(200, {
                            "ok": True,
                            "text": result.text,
                            "tokens": len(result.tokens),
                            "tokens_per_second": result.tokens_per_second,
                            "elapsed_ms": result.elapsed_ms,
                            "finish_reason": result.finish_reason,
                            "cache_tokens": server.engine.cache_token_count,
                            "remaining": server.engine.remaining_context,
                        })
                    except OverflowError as e:
                        self._send_json(413, {"error": str(e)})
                    except Exception as e:
                        logger.exception("react failed")
                        self._send_json(500, {"error": str(e)})
                    return

                if path == "/apply-edit":
                    body = self._read_body()
                    file_path = body.get("path")
                    search = body.get("search", "")
                    replace = body.get("replace", "")
                    if not file_path or not search:
                        self._send_json(400, {"error": "missing 'path' or 'search'"})
                        return
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        if search not in content:
                            self._send_json(404, {"error": "search text not found in file"})
                            return
                        new_content = content.replace(search, replace, 1)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        logger.info("Applied edit to %s (%d -> %d chars)", file_path, len(content), len(new_content))
                        self._send_json(200, {
                            "ok": True,
                            "path": file_path,
                            "old_length": len(content),
                            "new_length": len(new_content),
                        })
                    except FileNotFoundError:
                        self._send_json(404, {"error": f"file not found: {file_path}"})
                    except Exception as e:
                        logger.exception("apply-edit failed")
                        self._send_json(500, {"error": str(e)})
                    return

                if path == "/reset":
                    server.session.reset()
                    self._send_json(200, {
                        "ok": True,
                        "cache_tokens": server.engine.cache_token_count,
                    })
                    return

                self._send_json(404, {"error": "not found", "path": path})

        return Handler
