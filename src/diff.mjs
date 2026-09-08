/**
 * Simple line-level diff between two text snapshots.
 * Returns an array of { type: "added" | "removed" | "context", line, lineNumber } entries.
 * Uses the classic LCS algorithm — no dependencies, good enough for markdown edits.
 */
export function lineDiff(oldText, newText) {
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  const m = oldLines.length;
  const n = newLines.length;

  // Build LCS table
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (oldLines[i] === newLines[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  // Backtrack to produce diff
  const result = [];
  let i = 0, j = 0;
  let oldLine = 1, newLine = 1;
  while (i < m && j < n) {
    if (oldLines[i] === newLines[j]) {
      result.push({ type: "context", line: oldLines[i], oldLine, newLine });
      i++; j++; oldLine++; newLine++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ type: "removed", line: oldLines[i], oldLine, newLine: null });
      i++; oldLine++;
    } else {
      result.push({ type: "added", line: newLines[j], oldLine: null, newLine });
      j++; newLine++;
    }
  }
  while (i < m) {
    result.push({ type: "removed", line: oldLines[i], oldLine, newLine: null });
    i++; oldLine++;
  }
  while (j < n) {
    result.push({ type: "added", line: newLines[j], oldLine: null, newLine });
    j++; newLine++;
  }
  return result;
}

/**
 * Summarize a diff into added/removed line counts and the first changed line range.
 */
export function summarizeDiff(diff) {
  let added = 0, removed = 0;
  let firstNew = null, lastNew = null;
  for (const entry of diff) {
    if (entry.type === "added") {
      added++;
      if (firstNew === null) firstNew = entry.newLine;
      lastNew = entry.newLine;
    } else if (entry.type === "removed") {
      removed++;
    }
  }
  return { added, removed, firstNewLine: firstNew, lastNewLine: lastNew };
}
