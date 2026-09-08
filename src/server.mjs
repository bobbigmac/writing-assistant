#!/usr/bin/env node
/**
 * HTTP server with SSE stream + REST API for the writing-assistant service.
 *
 * Endpoints:
 *   GET  /health          — { ok, watched, sseClients, events }
 *   GET  /state           — { watched, recentEvents }
 *   GET  /events          — SSE stream (text/event-stream)
 *   GET  /events?since=N  — JSON array of events since id N (poll fallback)
 *   POST /watch           — body: { path } — add a file to the watch list
 *   POST /unwatch         — body: { path } — remove a file from the watch list
 *   POST /cursor          — body: { path, line, character, selection } — update cursor context
 *   POST /shutdown        — graceful shutdown
 */
import http from "node:http";
import {
  appendEvent,
  getEventsSince,
  getRecentEvents,
  addSseClient,
  removeSseClient,
  sseClientCount,
  setCursorContext,
  waitForNextEvent,
  cancelWaiter,
} from "./store.mjs";
import { watchFile, unwatchFile, getWatchedFiles, closeAllWatchers } from "./watcher.mjs";

const HOST = process.env.WAS_HOST || "127.0.0.1";
const PORT = Number(process.env.WAS_PORT || "3848");

function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error("Invalid JSON body"));
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, status, data) {
  res.setHeader("Content-Type", "application/json");
  res.writeHead(status);
  res.end(JSON.stringify(data));
}

const server = http.createServer(async (req, res) => {
  setCors(res);
  const url = new URL(req.url || "/", `http://${req.headers.host}`);

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  // Poll: GET /events?since=N — returns all events since id N as JSON
  // Must be checked BEFORE the SSE handler to avoid matching the SSE stream.
  if (url.pathname === "/events" && req.method === "GET" && url.searchParams.has("since")) {
    const since = Number(url.searchParams.get("since")) || 0;
    sendJson(res, 200, { events: getEventsSince(since) });
    return;
  }

  // SSE stream
  if (url.pathname === "/events" && req.method === "GET") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    // Send a hello event so the client knows it's connected
    res.write(`event: hello\ndata: ${JSON.stringify({ ok: true, ts: new Date().toISOString() })}\n\n`);
    addSseClient(res);
    req.on("close", () => removeSseClient(res));
    return;
  }

  // Long-poll: GET /next-event?since=N
  // Blocks until an event with id > since exists, then returns it as JSON.
  // This is the endpoint the LLM calls — it resolves the instant a change happens.
  if (url.pathname === "/next-event" && req.method === "GET") {
    const since = Number(url.searchParams.get("since")) || 0;
    const existing = getEventsSince(since);
    if (existing.length > 0) {
      sendJson(res, 200, existing[0]);
      return;
    }
    // No event yet — block until one arrives
    req.on("close", () => {
      // Client disconnected before an event arrived
    });
    try {
      const event = await waitForNextEvent(since);
      sendJson(res, 200, event);
    } catch {
      sendJson(res, 500, { error: "wait failed" });
    }
    return;
  }

  if (url.pathname === "/health" && req.method === "GET") {
    sendJson(res, 200, {
      ok: true,
      service: "writing-assistant",
      version: "0.1.0",
      watched: getWatchedFiles().length,
      sseClients: sseClientCount(),
      events: getRecentEvents().length,
    });
    return;
  }

  if (url.pathname === "/state" && req.method === "GET") {
    sendJson(res, 200, {
      watched: getWatchedFiles(),
      recentEvents: getRecentEvents(20),
    });
    return;
  }

  if (url.pathname === "/watch" && req.method === "POST") {
    let body;
    try {
      body = await readBody(req);
    } catch {
      return sendJson(res, 400, { ok: false, error: "Invalid JSON" });
    }
    if (!body.path) return sendJson(res, 400, { ok: false, error: "Missing 'path'" });
    const result = watchFile(body.path);
    sendJson(res, result.ok ? 200 : 500, result);
    return;
  }

  if (url.pathname === "/unwatch" && req.method === "POST") {
    let body;
    try {
      body = await readBody(req);
    } catch {
      return sendJson(res, 400, { ok: false, error: "Invalid JSON" });
    }
    if (!body.path) return sendJson(res, 400, { ok: false, error: "Missing 'path'" });
    const result = unwatchFile(body.path);
    sendJson(res, 200, result);
    return;
  }

  if (url.pathname === "/cursor" && req.method === "POST") {
    let body;
    try {
      body = await readBody(req);
    } catch {
      return sendJson(res, 400, { ok: false, error: "Invalid JSON" });
    }
    if (!body.path) return sendJson(res, 400, { ok: false, error: "Missing 'path'" });
    setCursorContext(body.path, {
      line: body.line ?? null,
      character: body.character ?? null,
      selection: body.selection ?? null,
      visibleRanges: body.visibleRanges ?? null,
    });
    sendJson(res, 200, { ok: true });
    return;
  }

  if (url.pathname === "/shutdown" && req.method === "POST") {
    sendJson(res, 200, { ok: true, message: "shutting down" });
    closeAllWatchers();
    setTimeout(() => process.exit(0), 100);
    return;
  }

  sendJson(res, 404, { error: "Not found", path: url.pathname });
});

server.listen(PORT, HOST, () => {
  console.log(`[writing-assistant] listening on http://${HOST}:${PORT}`);
  console.log(`[writing-assistant] SSE stream:  http://${HOST}:${PORT}/events`);
  console.log(`[writing-assistant] health:      http://${HOST}:${PORT}/health`);
});

// Emit a startup event so SSE listeners know the service is live
appendEvent({ kind: "startup", host: HOST, port: PORT });

process.on("SIGINT", () => {
  console.log("\n[writing-assistant] shutting down...");
  closeAllWatchers();
  process.exit(0);
});
