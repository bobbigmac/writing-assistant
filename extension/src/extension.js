/**
 * Writing Assistant Context Provider — minimal VS Code extension.
 *
 * Job: tell the writing-assistant service which markdown files are open
 * and where the cursor is. Nothing else. No MCP, no panel, no inline suggest.
 *
 * On markdown file open  -> POST /watch
 * On markdown file close -> POST /unwatch
 * On cursor change (debounced) -> POST /cursor
 */
const vscode = require("vscode");
const http = require("http");

const openDocs = new Set(); // uri.toString -> filePath
const cursorTimers = new Map(); // filePath -> timeout

function getServiceUrl() {
  return vscode.workspace.getConfiguration("writingAssistant").get("serviceUrl", "http://127.0.0.1:3848");
}

function getDebounceMs() {
  return vscode.workspace.getConfiguration("writingAssistant").get("cursorDebounceMs", 500);
}

function post(path, body) {
  const url = getServiceUrl() + path;
  const data = JSON.stringify(body);
  const parsed = new URL(url);
  const req = http.request(
    {
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.pathname,
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
    },
    (res) => res.resume()
  );
  req.on("error", () => {}); // Silent — service may not be running yet
  req.write(data);
  req.end();
}

function watchDoc(doc) {
  if (doc.languageId !== "markdown" && !doc.fileName.match(/\.(md|markdown)$/i)) return;
  const filePath = doc.uri.fsPath;
  if (openDocs.has(doc.uri.toString())) return;
  openDocs.add(doc.uri.toString());
  post("/watch", { path: filePath });
}

function unwatchDoc(doc) {
  if (!openDocs.has(doc.uri.toString())) return;
  openDocs.delete(doc.uri.toString());
  const filePath = doc.uri.fsPath;
  post("/unwatch", { path: filePath });
  const timer = cursorTimers.get(filePath);
  if (timer) {
    clearTimeout(timer);
    cursorTimers.delete(filePath);
  }
}

function sendCursor(editor) {
  if (!editor || editor.document.languageId !== "markdown") return;
  const filePath = editor.document.uri.fsPath;
  const sel = editor.selection;
  const body = {
    path: filePath,
    line: sel.active.line,
    character: sel.active.character,
    selection:
      sel && !sel.isEmpty
        ? { startLine: sel.start.line, startChar: sel.start.character, endLine: sel.end.line, endChar: sel.end.character }
        : null,
    visibleRanges: editor.visibleRanges.map((r) => ({ startLine: r.start.line, endLine: r.end.line })),
  };
  post("/cursor", body);
}

function onCursorChange(editor) {
  if (!editor || editor.document.languageId !== "markdown") return;
  const filePath = editor.document.uri.fsPath;
  const debounce = getDebounceMs();
  const existing = cursorTimers.get(filePath);
  if (existing) clearTimeout(existing);
  cursorTimers.set(
    filePath,
    setTimeout(() => {
      cursorTimers.delete(filePath);
      sendCursor(editor);
    }, debounce)
  );
}

function activate(context) {
  // Watch already-open documents
  for (const doc of vscode.workspace.textDocuments) watchDoc(doc);

  context.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument(watchDoc),
    vscode.workspace.onDidCloseTextDocument(unwatchDoc),
    vscode.window.onDidChangeActiveTextEditor(onCursorChange),
    vscode.window.onDidChangeTextEditorSelection((e) => onCursorChange(e.textEditor)),
    vscode.window.onDidChangeVisibleTextEditors((editors) => {
      for (const ed of editors) onCursorChange(ed);
    })
  );

  // Send initial cursor for active editor
  if (vscode.window.activeTextEditor) onCursorChange(vscode.window.activeTextEditor);
}

function deactivate() {
  // Unwatch all open docs on deactivate
  for (const uriStr of openDocs) {
    const uri = vscode.Uri.parse(uriStr);
    post("/unwatch", { path: uri.fsPath });
  }
  openDocs.clear();
  for (const timer of cursorTimers.values()) clearTimeout(timer);
  cursorTimers.clear();
}

module.exports = { activate, deactivate };
