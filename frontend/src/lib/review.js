// The diff pane's line comments (console/DiffView.jsx): where each one sits
// in a file's diff, and the message they go back to the tab as. Pure, so the
// wording and the anchoring can be checked without a browser.
//
// A comment: { id, path, side, line, start, quote, body }. `side` is "new"
// (an added or unchanged line, numbered as the file is now) or "old" (a
// removed one, numbered as it was); `start`..`line` is the range; `quote` is
// the rows it was written against, [{ kind, text }], which is both what the
// tab is shown and how the comment finds its line again after the diff moves.

/** A file's diff lines as rows, numbered off each hunk header. */
export function numbered(lines) {
  let o = 0, n = 0;
  return lines.map((ln) => {
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(ln);
    if (h) { o = +h[1]; n = +h[2]; return { kind: "hunk", text: ln }; }
    if (ln.startsWith("\\")) return { kind: "meta", text: ln };
    const c = ln[0];
    if (c === "+") return { kind: "add", text: ln.slice(1), n: n++ };
    if (c === "-") return { kind: "del", text: ln.slice(1), o: o++ };
    return { kind: "ctx", text: ln.slice(1), o: o++, n: n++ };
  });
}

export const isCode = (row) => row && (row.kind === "add" || row.kind === "del" || row.kind === "ctx");
const number = (row, side) => (side === "old" ? row.o : row.n);

/** A comment's anchor from rows[from..to] (either order). */
export function anchor(rows, from, to) {
  const picked = rows.slice(Math.min(from, to), Math.max(from, to) + 1).filter(isCode);
  if (!picked.length) return null;
  const last = picked[picked.length - 1];
  const side = last.kind === "del" ? "old" : "new";
  const first = picked.find((r) => number(r, side) != null) || last;
  return { side, start: number(first, side), line: number(last, side),
           quote: picked.map((r) => ({ kind: r.kind, text: r.text })) };
}

/** Where `comments` go among `rows`: under the row each was written on, if
 *  that row still reads the same; the rest are `outdated` (their line moved
 *  or went), and still go back to the tab, quoting what they were about. */
export function place(rows, comments) {
  const at = new Map();
  const outdated = [];
  for (const c of comments) {
    const want = c.quote[c.quote.length - 1];
    const i = rows.findIndex((r) => isCode(r) && (r.kind === "del") === (c.side === "old")
      && number(r, c.side) === c.line && r.text === want?.text);
    if (i < 0) outdated.push(c);
    else at.set(i, [...(at.get(i) || []), c]);
  }
  return { at, outdated };
}

const span = (c) => (c.start != null && c.start !== c.line ? `${c.start}-${c.line}` : `${c.line}`);

/** `path:12`, `path:12-14`, or for removed lines `path (removed, was line 12)`. */
export function where(c) {
  if (c.side === "old") return `${c.path} (removed, was line${c.start !== c.line ? "s" : ""} ${span(c)})`;
  return `${c.path}:${span(c)}`;
}

/** The same without the path, for a comment shown in its file. */
export function lines(c) {
  const many = c.start != null && c.start !== c.line;
  return `${c.side === "old" ? "removed " : ""}line${many ? "s" : ""} ${span(c)}`;
}

const QUOTE_MAX = 6;
const SIGN = { add: "+", del: "-", ctx: " " };

function quoted(c) {
  const rows = c.quote.slice(0, QUOTE_MAX).map((r) => `> ${SIGN[r.kind] || " "}${r.text}`);
  if (c.quote.length > QUOTE_MAX) rows.push(`> … ${c.quote.length - QUOTE_MAX} more`);
  return rows.join("\n");
}

/** The tab's next message: every comment, in file order, with the lines it
 *  was about, and the overall note if there is one. */
export function reviewMessage(comments, { note = "", branch } = {}) {
  // In reading order: by file, then where each starts (a range before a
  // comment on its last line), removed lines after the file's others.
  const sorted = [...comments].sort((a, b) => a.path.localeCompare(b.path)
    || (a.side === b.side ? (a.start ?? a.line) - (b.start ?? b.line) || a.line - b.line
      : a.side === "new" ? -1 : 1));
  const n = sorted.length;
  const what = `${n} comment${n === 1 ? "" : "s"}`;
  // Comments from any of the pane's views travel together, so the head
  // names the branch, not "against base": one may be on uncommitted lines.
  const parts = [`${branch ? `Review comments on ${branch}: ${what}.` : `Review comments on the diff: ${what}.`}`
    + " Each quotes the lines it's about."];
  for (const c of sorted) parts.push(`${where(c)}\n${quoted(c)}\n${c.body.trim()}`);
  if (note.trim()) parts.push(`Overall: ${note.trim()}`);
  return parts.join("\n\n");
}

/** The message as it goes out: what was typed, then the comments riding
 *  with it. `/` and `!` lines are commands, not messages, so they go alone
 *  and the comments wait for the next real one. */
export function withComments(text, comments, branch) {
  if (!comments.length || /^[!/]/.test(text)) return { text, tags: [], used: false };
  const review = reviewMessage(comments, { branch });
  return { text: text ? `${text}\n\n${review}` : review, tags: comments.map(where), used: true };
}

/** The composer's chip for a comment: `name.py:12`, the file without its folder. */
export function chip(c) {
  const name = c.path.slice(c.path.lastIndexOf("/") + 1);
  return where({ ...c, path: name });
}

/** Every turn in the log that changed the repo, oldest first, each with the
 *  message that started it: the pane's turn picker. */
export function turnDiffs(entries) {
  const out = [];
  let asked = "";
  for (const e of entries) {
    if (e.kind === "user") asked = e.text || "";
    else if (e.kind === "diff") out.push({ entry: e, files: e.files || [], asked });
  }
  return out;
}

const names = (list, max = 8) =>
  list.slice(0, max).join(", ") + (list.length > max ? ` and ${list.length - max} more` : "");

/** Asking the tab to resolve what a merge would stop on (or did). */
export function conflictMessage(conflicts, { branch, base }) {
  const files = conflicts.map((c) => (c.kind ? `${c.path} (${c.kind})` : c.path));
  return `${base} has moved on and conflicts with ${branch} in ${names(files)}. `
    + `In your worktree run \`git rebase ${base}\`, resolve each file, \`git add\` it and `
    + "`git rebase --continue`, re-run the checks, then say when it's ready to merge.";
}

/** Asking the tab to commit what the review can't see and the merge refuses. */
export function commitMessage(dirty) {
  return `Your worktree has uncommitted changes (${names(dirty)}), which the review doesn't `
    + "show and the merge refuses. Commit them, or tell me which ones shouldn't be kept.";
}

// Comments outlive the pane and a reload, per tab folder, until they're sent.
const key = (folder) => `diff-comments:${folder}`;

export function loadComments(folder) {
  try {
    const list = JSON.parse(localStorage.getItem(key(folder)) || "[]");
    return Array.isArray(list) ? list.filter((c) => c?.path && c?.body && Array.isArray(c.quote)) : [];
  } catch {
    return [];
  }
}

export function saveComments(folder, list) {
  try {
    if (list.length) localStorage.setItem(key(folder), JSON.stringify(list));
    else localStorage.removeItem(key(folder));
  } catch { /* a full or blocked store loses drafts, not the review */ }
}
