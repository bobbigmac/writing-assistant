/**
 * In-memory event log + SSE client registry.
 * The event log is append-only with monotonic IDs.
 * SSE clients are kept as a set of response objects; on emit, we write to all.
 */

const events = [];
let nextId = 1;
const sseClients = new Set();
const waiters = new Set(); // /next-event long-poll resolvers

export function appendEvent(event) {
  const id = nextId++;
  const entry = { id, ts: new Date().toISOString(), ...event };
  events.push(entry);
  // Keep only the last 200 events to avoid unbounded growth
  if (events.length > 200) events.splice(0, events.length - 200);
  broadcast(entry);
  // Resolve any /next-event long-polls
  for (const resolve of waiters) {
    waiters.delete(resolve);
    resolve(entry);
  }
  return entry;
}

/**
 * Wait for the next event with id > sinceId.
 * Returns a promise that resolves when an event arrives.
 * Caller is responsible for adding a timeout if desired.
 */
export function waitForNextEvent(sinceId) {
  return new Promise((resolve) => {
    // Check if an event already exists
    const existing = events.filter((e) => e.id > sinceId);
    if (existing.length > 0) {
      resolve(existing[0]);
      return;
    }
    waiters.add(resolve);
  });
}

export function cancelWaiter(resolve) {
  waiters.delete(resolve);
}

export function getEventsSince(sinceId) {
  return events.filter((e) => e.id > sinceId);
}

export function getRecentEvents(limit = 20) {
  return events.slice(-limit);
}

export function addSseClient(res) {
  sseClients.add(res);
}

export function removeSseClient(res) {
  sseClients.delete(res);
}

export function sseClientCount() {
  return sseClients.size;
}

function broadcast(entry) {
  const data = `id: ${entry.id}\nevent: change\ndata: ${JSON.stringify(entry)}\n\n`;
  for (const client of sseClients) {
    try {
      client.write(data);
    } catch {
      sseClients.delete(client);
    }
  }
}

/**
 * Cursor context registry — keyed by file path.
 * The extension POSTs cursor/selection info here; the service attaches it to change events.
 */
const cursorContexts = new Map();

export function setCursorContext(filePath, context) {
  cursorContexts.set(filePath, { ts: new Date().toISOString(), ...context });
}

export function getCursorContext(filePath) {
  return cursorContexts.get(filePath) || null;
}

export function clearCursorContext(filePath) {
  cursorContexts.delete(filePath);
}
