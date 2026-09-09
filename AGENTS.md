# writing-assistant — Agent Guide

A live co-author service for markdown files. The human writes; the LLM reacts. This is not an autonomous agent — it's a reactive assistant that watches the human work and comments in real time.

## Components

### `src/server.mjs` — HTTP service (port 3848)

Node HTTP server. The core of the system. Exposes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | `{ ok, service, version, watched, sseClients, events }` |
| `/state` | GET | Watched files + recent events (last 20) |
| `/next-event` | GET | **Long-poll**: blocks until the next save after the request arrives, returns the event as JSON (includes `id` and `actions`). No params. |
| `/events?since=N` | GET | All events after id N as JSON array. Use the `id` from `/next-event` as N. |
| `/context?since=N` | GET | Current file content around changed regions since event N. Use when you need surrounding context, not just the diff lines. |
| `/replace` | POST | Fast find-and-replace in a watched file. Body: `{"path": "...", "find": "...", "replace": "...", "all": false}`. Use for TODO completions instead of read+edit. |
| `/watch` | POST | Add file to watch list. Body: `{"path": "/abs/path.md"}` |
| `/unwatch` | POST | Remove file. Body: `{"path": "/abs/path.md"}` |
| `/cursor` | POST | Update cursor context. Body: `{"path": "...", "line": N, "character": N, "selection": {...}}` |
| `/shutdown` | POST | Graceful shutdown |

Env vars: `WAS_HOST` (default `127.0.0.1`), `WAS_PORT` (default `3848`).

### `src/store.mjs` — event log + waiters

In-memory append-only event log with monotonic IDs. Keeps the last 200 events. Maintains a set of `/next-event` long-poll resolvers — when `appendEvent` fires, it resolves all waiting long-polls. `waitForNextEvent()` always blocks (never resolves immediately with existing events). Also holds the cursor context registry (keyed by file path, populated by the extension).

### `src/watcher.mjs` — file watcher

Uses `fs.watch` with 300ms debounce. Keeps last-known content per file. On change, reads the new content, computes a line diff against the previous snapshot, attaches cursor context if available, and calls `appendEvent`. Handles file deletion (emits a `deleted` event and cleans up). Also provides `getFileContent` (read current content) and `replaceInFile` (find-and-replace on disk, used by `POST /replace`).

### `src/diff.mjs` — line diff

Classic LCS algorithm. Returns `{ type: "added" | "removed" | "context", line, oldLine, newLine }` entries. `summarizeDiff` produces added/removed counts and the first/last changed line numbers. No dependencies.

### `extension/` — VS Code context provider (optional)

Minimal VS Code/Windsurf extension. No MCP, no panel, no inline suggest. Just:
- On markdown file open → `POST /watch`
- On markdown file close → `POST /unwatch`
- On cursor change (debounced 500ms) → `POST /cursor`

Gives events cursor/selection context. The core loop works without it.

### `skills/writing-assistant/SKILL.md` — Devin skill

Installed globally to `~/.config/devin/skills/writing-assistant/SKILL.md` by `scripts/install-skill.mjs` (runs on `npm install -g`). Tells Devin how to orchestrate the watch loop using `exec` + `get_output`.

### `scripts/install-skill.mjs` — postinstall hook

Copies the skill to `~/.config/devin/skills/writing-assistant/SKILL.md` so it's available in every Devin session, every project. Runs automatically on global install.

## Install

```bash
cd /home/bobbigmac/projects/writing-assistant
npm install -g .
# Installs:
#   - `writing-assistant` command (the service)
#   - skill to ~/.config/devin/skills/writing-assistant/SKILL.md
```

For development:
```bash
npm link
```

## Starting the service

```bash
writing-assistant
# listens on http://127.0.0.1:3848
```

Or from the source:
```bash
node src/server.mjs
```

## The engagement pattern — how to actually use this

This is the part that matters. The service is simple; the engagement pattern is what makes it work as a live co-author.

### The problem this solves

Normal agentic workflows are grabby: the agent drives, the human reviews. For authorial work, that's backwards — the human is the author, the agent should react. MCP-based approaches (the earlier `vscode-mdlive-extension/` attempt) required a full agent turn per edit through a polling loop, which was too slow and blocked the agent from doing anything else.

This service solves it by turning saves into a simple event stream that Devin can wait on using its native `exec` + `get_output` tools — no MCP, no extension lifecycle, no agent turn per keystroke.

### The two-phase blocking trick

Devin's `exec` tool backgrounds any process that produces no output for 10 seconds. This is NOT a timeout — the process keeps running. But `exec` returns early with a `shell_id` instead of waiting for the process to finish.

The solution is a two-phase wait:

**Phase 1 — Launch the blocking call with `exec`:**
```
exec: curl -s "http://127.0.0.1:3848/next-event"
```
- Set the `timeout` parameter high (280000ms) so exec doesn't kill it.
- After 10s of no output, exec backgrounds the process and returns a `shell_id`.
- This is expected. The curl is still running, still waiting for an event.

**Phase 2 — Wait on the backgrounded process with `get_output`:**
```
get_output(shell_id=<the id from phase 1>)
```
- `get_output` blocks until the process produces output or exits.
- When the human saves, the service resolves the long-poll, curl prints the event JSON and exits.
- `get_output` returns that output immediately. This IS the blocking call.
- If `get_output` returns "No output yet (still running)", the process is still waiting. Call `get_output` again. Each call re-blocks until output arrives.

**Why this works:** `get_output` resolves the instant the process produces output. The 10s backgrounding in phase 1 is just exec giving up on synchronous waiting — it does not kill the process. The process stays alive, and `get_output` catches its output the moment it arrives.

### The full wait-react cycle

```
1. Start the service (once):
   exec: writing-assistant   (timeout=0, background it)

2. Watch the file(s) the human is editing:
   exec: curl -s -X POST http://127.0.0.1:3848/watch \
     -H 'Content-Type: application/json' \
     -d '{"path": "/absolute/path/to/file.md"}'

3. Enter the loop. No state to track — /next-event tells you the id, /events takes it.

4. LOOP:
   a. exec: curl -s "http://127.0.0.1:3848/next-event"  (timeout=280000)
      -> backgrounds after 10s, returns shell_id
   
   b. get_output(shell_id=<from step a>)
      -> blocks until the human saves and an event arrives
      -> returns the event JSON when it does (includes id + actions)
      -> if "No output yet (still running)", call get_output again
   
   c. Take the id from the event, fetch ALL events since it:
      exec: curl -s --max-time 5 "http://127.0.0.1:3848/events?since=<id from step b>"
      -> returns JSON array of all missed events
   
   d. React to the FULL SET of new events (not just the one that unblocked the wait).
      - If the human left a TODO in the document, HELP WITH IT DIRECTLY via
        POST /replace — no need to read+edit the whole file. Do NOT run TODOs
        through the personality lenses.
      - If you need surrounding context, call GET /context?since=<id>.
      - Otherwise, apply all four personalities (below) to the combined diff
        and post feedback in chat (the human's splitscreen surface).
      - Keep it concise.
   
   e. Go to step a. ALWAYS. The loop never terminates on its own. Every
      response — personality feedback, TODO help, a clarifying question, even
      silence — ends by re-entering the wait loop (step a). The only
      reason to stop is the human explicitly ending the session.
```

### Why fetch-all-since matters

Between the moment `/next-event` resolves and the moment you actually read its output via `get_output`, the human may have saved again (or several times). If you only react to the single event that unblocked the wait, you'll lag behind and queue up stale reactions. Always fetch the full set since your last response and react to the cumulative state.

### What NOT to do

- **Do NOT set a short timeout on exec.** The curl needs to stay alive indefinitely. Use 280000ms (the max).
- **Do NOT treat "No output yet (still running)" as an error.** It means the process is still waiting. Call `get_output` again.
- **Do NOT send chat messages between get_output calls.** Every chat message you send ends your turn and the human has to prompt you again. If you need to wait silently, just call `get_output` again without outputting text.
- **Do NOT use the SSE `/events` stream.** It requires a persistent connection and doesn't resolve cleanly. Use `/next-event` (long-poll) for waiting and `/events?since=N` (JSON) for catching up.
- **Do NOT react to your own edits.** When you complete a TODO via `POST /replace`, the service fires an event for your edit. Skip it — just re-wait without reacting.
- **Do NOT end your turn without re-entering the wait loop.** Every response — feedback, TODO help, a question, even silence — ends by going back to step (a) of the loop. The loop is the session. If you stop looping, the human has to re-prompt you, which defeats the live co-pilot pattern. The only valid reason to stop is the human explicitly ending the session.
- **Do NOT run TODOs through the personalities.** A TODO is a request for help writing, not writing to critique. Help with the TODO directly; reserve personalities for the human's actual prose.

### How Devin's tools work together

The pattern uses three of Devin's built-in tools in concert:

1. **`exec`** — launches the `curl` long-poll call. With `timeout=280000`, it won't kill the process. After 10s of no output, it backgrounds the process and returns a `shell_id`. This is the "launch the waiter" step.

2. **`get_output`** — the actual blocking wait. Called on the `shell_id` from `exec`, it returns the moment the process produces output (i.e., when the service resolves the long-poll). This is the "wait for event" step.

3. **`read` / `edit`** — used when you need to read the current document state directly. For TODO completions, prefer `POST /replace` instead — it's faster and doesn't require reading the whole file.

The skill (installed globally) tells Devin how to orchestrate these tools. The service is the adapter between the filesystem and Devin's HTTP-based tool interface. The extension optionally enriches events with cursor context.

## TODOs vs personalities — CRITICAL distinction

These are two different modes. Do not mix them.

- **Personalities** (corrector, enquirer, what-next, orchestrator) are for
  reacting to the human's *writing*. They comment on what the human just wrote:
  typos, claims that need sources, what could come next, structural issues.
  Output goes in chat.
- **TODOs** are the human asking for *help writing*. When the human leaves a
  `-TODO: ...` line in the document, they want assistance producing the thing
  the TODO describes — a clarifying question, a set of options, a stub in the
  doc. Do NOT run a TODO through the personality lenses. Do NOT give
  "corrector/enquirer/what-next/orchestrator" feedback on the TODO line
  itself. Just help with the TODO.

A single save may contain both: the human wrote a paragraph (apply
personalities to the paragraph) AND left a TODO (help with the TODO). Treat
them separately.

## Personalities (apply ALL active ones per response)

### corrector (word/sentence level)
- Fix typos, spacing, capitalization, malformed sentences.
- Finish obviously incomplete sentences without changing meaning.
- If the author typed something messy (e.g. "tyPPE like This"), assume they want it cleaned up.
- Give exact before/after. When clean, say nothing.

### enquirer (narrative/research level)
- Relevance questions, structural placement, fact-check suggestions with links.
- Flag claims that need sources: `[VERIFY: specific claim]`.
- Challenge vague statements — ask for one concrete example.

### what-next (local continuation)
- Suggest what could come next: rest of line, paragraph, chapter.
- Brief talking points or stubs, never full prose.
- Frame as "good, and now: ..."

### orchestrator (structural level)
- Section moves, splits, new sections, duplicated ground, off-topic material.
- Flag structural issues: ordering, flow, missing sections.
- Suggest reorders with reasoning.

## Two surfaces, peers in information delivery

- **Chat** — running commentary, personality feedback, questions, structural suggestions. The human has this side-by-side with their editor (splitscreen: chat | editor).
- **Document** — the human's file. The LLM may complete explicit TODOs left in the document, but never edits prose or structure the human wrote.

Feedback on edits goes in chat. TODOs the human leaves in the doc may be completed in the doc.

## Rules

- The LLM is an assistant, NOT the author. The human is the author.
- Keep reactions concise — the human reads them alongside their editor.
- One consolidated response per wait-cycle, covering all active personalities.
- **Always return to the wait loop.** The loop is the session. See "What NOT to do" above.
- **TODOs are not personality input.** Service TODOs directly; apply personalities only to the human's prose. See "TODOs vs personalities" above.
- Non-fiction only. Do not steer toward fiction.
- Do not use remote provider keys (Gemini, Grok, OpenAI, Featherless) for ad hoc calls.

## Future: inline suggestions via extension

The earlier `vscode-mdlive-extension/` (in the InfoBookGenerator repo) attempted inline ghost-text suggestions via MCP. That approach was too slow — it required a full LLM agent turn per suggestion. A future version could use this service as a backend for an `InlineCompletionItemProvider` that calls a lightweight model directly, bypassing the agent loop entirely. This is a future direction, not implemented.
