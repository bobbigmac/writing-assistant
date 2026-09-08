---
name: writing-assistant
description: Co-author workflow for markdown files. Watches files on disk via a local service, reacts to saves with multi-lens feedback (corrector, enquirer, what-next, orchestrator). The LLM is an assistant to the author, not the author.
---

# Writing Assistant — Live Co-Author Pattern

## What this is

A human authors markdown files in their editor. A local Node service watches those files on disk. The LLM (Devin) connects to the service and reacts to saves (Ctrl+S) by providing feedback in chat. The LLM never writes prose the human authored — it may complete explicit TODOs the human leaves in the document. The human is the author.

This works as a live co-pilot because the IDE surfaces the LLM's chat output between responses, side-by-side with the editor. The human writes, saves, and sees the LLM's reaction appear in the chat panel. It makes the LLM less grabby and more focused than typical agentic workflows — the LLM reacts to what the human does, rather than driving.

## Architecture

```
Editor (human writes .md) ──save──> File on disk
                                          │
                          writing-assistant (Node, port 3848)
                                          │
                          /next-event?since=N (long-poll, blocks until save)
                                          │
                                    Devin (LLM)
                                          │
                                   reacts in chat
```

The service runs as `writing-assistant` (installed globally via `npm install -g .`). It watches files, computes line diffs, and exposes:

- `GET /next-event?since=N` — **long-poll**: blocks until an event with id > N exists, returns it as JSON. This is the primary endpoint the LLM calls.
- `GET /events?since=N` — returns all events since id N as JSON array. Used to catch up on missed events after a wait resolves.
- `GET /state` — current watched files + recent events.
- `GET /health` — liveness check.
- `POST /watch` `{ path }` — add a file to the watch list.
- `POST /unwatch` `{ path }` — remove a file.
- `POST /cursor` `{ path, line, character, selection }` — cursor context (from extension).

## The watch loop — CRITICAL

This is the core pattern. Get this right or the whole thing falls apart.

### The two-phase blocking trick

Devin's `exec` tool backgrounds any process that produces no output for 10 seconds. This is NOT a timeout — the process keeps running. But `exec` returns early with a `shell_id` instead of waiting for the process to finish.

The solution is a two-phase wait:

**Phase 1 — Launch the blocking call with `exec`:**
```
exec: curl -s "http://127.0.0.1:3848/next-event?since=LAST_RESPONDED_ID"
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
- `get_output` returns that output immediately. This is the actual "wait for event" — `get_output` IS the blocking call.
- If `get_output` returns "No output yet (still running)", the process is still waiting. Call `get_output` again. Each call re-blocks until output arrives or you interrupt.

**Why this works:** `get_output` resolves the instant the process produces output. It does not wait for a timeout. The 10s backgrounding in phase 1 is just exec giving up on synchronous waiting — it does not kill the process. The process stays alive, and `get_output` will catch its output the moment it arrives.

### The full wait-react cycle

```
1. Start the service (once):
   exec: writing-assistant   (timeout=0, background it)

2. Watch the file(s) the human is editing:
   exec: curl -s -X POST http://127.0.0.1:3848/watch -H 'Content-Type: application/json' -d '{"path": "/absolute/path/to/file.md"}'

3. Track LAST_RESPONDED_ID. Start at the startup event id (usually 1).

4. LOOP:
   a. exec: curl -s "http://127.0.0.1:3848/next-event?since=LAST_RESPONDED_ID"  (timeout=280000)
      -> backgrounds after 10s, returns shell_id
   
   b. get_output(shell_id=<from step a>)
      -> blocks until the human saves and an event arrives
      -> returns the event JSON when it does
      -> if "No output yet (still running)", call get_output again
   
   c. When event arrives, IMMEDIATELY fetch ALL events since LAST_RESPONDED_ID:
      exec: curl -s --max-time 5 "http://127.0.0.1:3848/events?since=LAST_RESPONDED_ID"
      -> returns JSON array of all missed events
   
   d. React to the FULL SET of new events (not just the one that unblocked the wait).
      - Apply all four personalities (below) to the combined diff.
      - Post feedback in chat (the human's splitscreen surface).
      - If the human left a TODO in the document, complete it in the document.
      - Keep it concise.
   
   e. Update LAST_RESPONDED_ID to the highest event id you responded to.
   
   f. Go to step a.
```

### Why fetch-all-since matters

Between the moment `/next-event` resolves and the moment you actually read its output via `get_output`, the human may have saved again (or several times). If you only react to the single event that unblocked the wait, you'll lag behind and queue up stale reactions. Always fetch the full set since your last response and react to the cumulative state.

### What NOT to do

- **Do NOT set a short timeout on exec.** The curl needs to stay alive indefinitely. Use 280000ms (the max).
- **Do NOT treat "No output yet (still running)" as an error.** It means the process is still waiting. Call `get_output` again.
- **Do NOT send chat messages between get_output calls.** Every chat message you send ends your turn and the human has to prompt you again. If you need to wait silently, just call `get_output` again without outputting text.
- **Do NOT use the SSE `/events` stream.** It requires a persistent connection and doesn't resolve cleanly. Use `/next-event` (long-poll) for waiting and `/events?since=N` (JSON) for catching up.
- **Do NOT react to your own edits.** When you complete a TODO in the document, the service will fire an event for your edit. Skip events you generated by checking if the diff matches your own edit.

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

## Rules

- The LLM is an assistant, NOT the author. The human is the author.
- **Two surfaces, peers in information delivery:**
  - **Chat** — running commentary, personality feedback, questions, structural suggestions. The human has this side-by-side with their editor (splitscreen: chat | editor).
  - **Document** — the human's file. The LLM may complete explicit TODOs left in the document, but never edits prose or structure the human wrote.
- Feedback on edits goes in chat. TODOs the human leaves in the doc may be completed in the doc.
- Keep reactions concise — the human reads them alongside their editor.
- One consolidated response per wait-cycle, covering all active personalities.
- Non-fiction only. Do not steer toward fiction.
- Do not use remote provider keys (Gemini, Grok, OpenAI, Featherless) for ad hoc calls.

## How Devin's tools work together

The pattern uses three of Devin's built-in tools in concert:

1. **`exec`** — launches the `curl` long-poll call. With `timeout=280000`, it won't kill the process. After 10s of no output, it backgrounds the process and returns a `shell_id`. This is the "launch the waiter" step.

2. **`get_output`** — the actual blocking wait. Called on the `shell_id` from `exec`, it returns the moment the process produces output (i.e., when the service resolves the long-poll). This is the "wait for event" step. It does NOT have a meaningful timeout in the sense that it returns immediately when output arrives — you just call it and it blocks until there's output or the process exits.

3. **`read` / `edit`** — used to read the current document state when completing TODOs, and to write TODO completions into the document.

The skill (this file) tells Devin how to orchestrate these tools. The service is the adapter between the filesystem and Devin's HTTP-based tool interface. The extension (`extension/`) optionally enriches events with cursor context.

## Optional: extension for cursor context

`extension/` is a minimal VS Code extension that reports open files and cursor positions to the service via `POST /watch`, `POST /unwatch`, `POST /cursor`. This gives events cursor/selection context. The core loop works without it — the extension is optional enrichment.

## Starting the service

```bash
cd /path/to/writing-assistant
node src/server.mjs
# listens on http://127.0.0.1:3848
```

Env vars: `WAS_HOST` (default 127.0.0.1), `WAS_PORT` (default 3848).

## Future: inline suggestions via extension

The current VS Code extension (`vscode-mdlive-extension/`) attempted inline ghost-text suggestions via MCP. That approach was too slow — it required a full LLM agent turn per suggestion. A future version could use the writing-assistant service as a backend for an `InlineCompletionItemProvider` that calls a lightweight model directly, bypassing the agent loop entirely. This is documented as a future direction, not implemented.
