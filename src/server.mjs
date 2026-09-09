#!/usr/bin/env node
/**
 * HTTP server with SSE stream + REST API for the writing-assistant service.
 *
 * Endpoints:
 *   GET  /health          — { ok, watched, sseClients, events }
 *   GET  /state           — { watched, recentEvents }
 *   GET  /events          — SSE stream (text/event-stream)
 *   GET  /events?since=N  — JSON array of events since id N (poll fallback)
 *   GET  /next-event       — long-poll, blocks until next save, returns event + actions
 *   GET  /context?since=N  — current file content around changed regions since event N
 *   POST /replace          — body: { path, find, replace, all? } — fast find-and-replace
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
import { watchFile, unwatchFile, getWatchedFiles, closeAllWatchers, getFileContent, replaceInFile } from "./watcher.mjs";

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

const CONTEXT_PADDING = 5; // lines before/after each changed region

function actionsFor(eventId) {
  return {
    context: `GET /context?since=${eventId}`,
    events: `GET /events?since=${eventId}`,
    replace: `POST /replace { path, find, replace, all? }`,
    next: `GET /next-event`,
  };
}

/**
 * Build context around changed regions from events since N.
 * Returns the current file content, but only the regions around changed lines
 * (plus CONTEXT_PADDING lines before/after each region).
 */
function buildContext(sinceId) {
  const events = getEventsSince(sinceId).filter((e) => e.kind === "changed" && e.summary);
  if (events.length === 0) return null;

  // Collect changed line ranges from all events, grouped by file
  const byFile = new Map();
  for (const e of events) {
    if (!e.path) continue;
    if (!byFile.has(e.path)) byFile.set(e.path, []);
    const s = e.summary;
    if (s.firstNewLine != null && s.lastNewLine != null) {
      byFile.get(e.path).push([s.firstNewLine, s.lastNewLine]);
    }
    // Also track removed lines via oldLine in diff entries
    if (e.diff) {
      for (const d of e.diff) {
        if (d.type === "removed" && d.oldLine != null) {
          byFile.get(e.path).push([d.oldLine, d.oldLine]);
        }
      }
    }
  }

  const results = [];
  for (const [filePath, ranges] of byFile) {
    const content = getFileContent(filePath);
    if (!content) continue;
    const lines = content.split("\n");

    // Merge overlapping/adjacent ranges with padding
    const merged = mergeRanges(ranges, CONTEXT_PADDING, lines.length);

    // Extract the regions
    const regions = [];
    for (const [start, end] of merged) {
      const regionLines = [];
      for (let i = start - 1; i < end && i < lines.length; i++) {
        regionLines.push({ line: i + 1, text: lines[i] });
      }
      regions.push({ startLine: start, endLine: end, lines: regionLines });
    }
    results.push({ path: filePath, regions });
  }
  return results;
}

function mergeRanges(ranges, padding, maxLine) {
  if (ranges.length === 0) return [];
  const sorted = ranges.map(([s, e]) => [Math.max(1, s - padding), Math.min(maxLine, e + padding)])
    .sort((a, b) => a[0] - b[0]);
  const merged = [sorted[0]];
  for (let i = 1; i < sorted.length; i++) {
    const last = merged[merged.length - 1];
    if (sorted[i][0] <= last[1] + 1) {
      last[1] = Math.max(last[1], sorted[i][1]);
    } else {
      merged.push(sorted[i]);
    }
  }
  return merged;
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
    const events = getEventsSince(since);
    sendJson(res, 200, { events, actions: actionsFor(events.length > 0 ? events[events.length - 1].id : since) });
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

  // Long-poll: GET /next-event
  // Blocks until the next event arrives AFTER this request, then returns it as JSON.
  // Always waits — never resolves immediately with existing events.
  // The response includes the event id, which the caller uses as `since` for /events.
  if (url.pathname === "/next-event" && req.method === "GET") {
    req.on("close", () => {
      // Client disconnected before an event arrived
    });
    try {
      const event = await waitForNextEvent();
      sendJson(res, 200, { ...event, actions: actionsFor(event.id) });
    } catch {
      sendJson(res, 500, { error: "wait failed" });
    }
    return;
  }

  // Context: GET /context?since=N — returns current file content around changed regions since event N
  if (url.pathname === "/context" && req.method === "GET") {
    const since = Number(url.searchParams.get("since")) || 0;
    const context = buildContext(since);
    if (!context) {
      sendJson(res, 200, { regions: [], actions: actionsFor(since) });
    } else {
      sendJson(res, 200, { regions: context, actions: actionsFor(since) });
    }
    return;
  }

  // Replace: POST /replace — body: { path, find, replace, all? }
  // Fast find-and-replace in a watched file. The watcher fires a change event.
  if (url.pathname === "/replace" && req.method === "POST") {
    let body;
    try {
      body = await readBody(req);
    } catch {
      return sendJson(res, 400, { ok: false, error: "Invalid JSON" });
    }
    if (!body.path) return sendJson(res, 400, { ok: false, error: "Missing 'path'" });
    if (body.find === undefined) return sendJson(res, 400, { ok: false, error: "Missing 'find'" });
    if (body.replace === undefined) return sendJson(res, 400, { ok: false, error: "Missing 'replace'" });
    const result = replaceInFile(body.path, body.find, body.replace, body.all === true);
    sendJson(res, result.ok ? 200 : 404, result);
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
