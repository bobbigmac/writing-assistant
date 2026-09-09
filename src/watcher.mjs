/**
 * File watcher — watches a set of files on disk and emits change events.
 * Uses fs.watch with debouncing (300ms) to coalesce rapid saves.
 * Keeps the last-known content of each file to compute diffs.
 */
import fs from "node:fs";
import path from "node:path";
import { appendEvent, getCursorContext, clearCursorContext } from "./store.mjs";
import { lineDiff, summarizeDiff } from "./diff.mjs";

const watched = new Map(); // filePath -> { watcher, lastContent, lastMtime }

const DEBOUNCE_MS = 300;

export function watchFile(filePath) {
  const abs = path.resolve(filePath);
  if (watched.has(abs)) return { ok: true, already: true, path: abs };

  let lastContent = null;
  let lastMtime = 0;
  let debounceTimer = null;

  // Read initial content
  try {
    lastContent = fs.readFileSync(abs, "utf8");
    lastMtime = fs.statSync(abs).mtimeMs;
  } catch {
    // File may not exist yet — that's ok, we'll pick it up when it appears
  }

  let watcher;
  try {
    watcher = fs.watch(abs, { persistent: false }, (eventType) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => handleChange(abs, eventType), DEBOUNCE_MS);
    });
  } catch {
    return { ok: false, error: `Cannot watch ${abs}` };
  }

  watcher.on("error", () => {
    // File may have been deleted or renamed — clean up
    watched.delete(abs);
  });

  watched.set(abs, { watcher, lastContent, lastMtime, debounceTimer });
  return { ok: true, path: abs };
}

export function unwatchFile(filePath) {
  const abs = path.resolve(filePath);
  const entry = watched.get(abs);
  if (!entry) return { ok: true, already: false, path: abs };
  entry.watcher.close();
  if (entry.debounceTimer) clearTimeout(entry.debounceTimer);
  watched.delete(abs);
  clearCursorContext(abs);
  return { ok: true, path: abs };
}

export function getWatchedFiles() {
  return Array.from(watched.keys());
}

/**
 * Get the current content of a watched file.
 */
export function getFileContent(filePath) {
  const abs = path.resolve(filePath);
  const entry = watched.get(abs);
  if (entry) return entry.lastContent;
  try {
    return fs.readFileSync(abs, "utf8");
  } catch {
    return null;
  }
}

/**
 * Replace text in a watched file on disk.
 * Returns { ok, path, eventId? } or { ok: false, error }.
 * The watcher will fire a change event for the edit.
 */
export function replaceInFile(filePath, find, replace, replaceAll = false) {
  const abs = path.resolve(filePath);
  let content;
  try {
    content = fs.readFileSync(abs, "utf8");
  } catch {
    return { ok: false, error: `Cannot read ${abs}` };
  }
  if (!content.includes(find)) {
    return { ok: false, error: `Text not found in ${abs}` };
  }
  let newContent;
  if (replaceAll) {
    newContent = content.split(find).join(replace);
  } else {
    newContent = content.replace(find, replace);
  }
  try {
    fs.writeFileSync(abs, newContent, "utf8");
  } catch {
    return { ok: false, error: `Cannot write ${abs}` };
  }
  return { ok: true, path: abs, replaced: replaceAll ? content.split(find).length - 1 : 1 };
}

export function closeAllWatchers() {
  for (const [abs, entry] of watched) {
    entry.watcher.close();
    if (entry.debounceTimer) clearTimeout(entry.debounceTimer);
  }
  watched.clear();
}

function handleChange(abs, eventType) {
  const entry = watched.get(abs);
  if (!entry) return;

  let newContent, newMtime;
  try {
    newContent = fs.readFileSync(abs, "utf8");
    newMtime = fs.statSync(abs).mtimeMs;
  } catch {
    // File deleted
    appendEvent({
      kind: "deleted",
      path: abs,
    });
    entry.watcher.close();
    watched.delete(abs);
    return;
  }

  // Skip if content unchanged (mtime may change without content change)
  if (newContent === entry.lastContent) return;

  const diff = lineDiff(entry.lastContent || "", newContent);
  const summary = summarizeDiff(diff);
  const cursorCtx = getCursorContext(abs);

  appendEvent({
    kind: "changed",
    path: abs,
    eventType,
    mtime: newMtime,
    summary,
    diff: diff.filter((d) => d.type !== "context").slice(0, 50), // Only changed lines, capped
    cursor: cursorCtx,
    snapshot: newContent,
  });

  entry.lastContent = newContent;
  entry.lastMtime = newMtime;
}
