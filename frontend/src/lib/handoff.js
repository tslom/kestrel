// When a handed-off tab's work is over, and what goes back to the tab that
// opened it. Plain functions over the console log (Console.jsx's entries), so
// node can test them (tests/handoff.test.mjs, run by pytest).
//
// A turn that ends is not always work that ends. An API error (a network
// outage, say) ends a turn mid-work; reporting then sends its last line of
// narration ("error. Now wiring it into the console…") as the outcome, lists
// it as a finished handoff, and the summary after the user's "continue" goes
// nowhere, because a brief reports once. So the report waits
// through any turn that didn't finish the work, and fires on the first one
// that does.

// Room for a real summary. The backend bounds the whole report again
// (console._bounded, 20k) — this is the cut that keeps it readable.
export const REPORT_LIMIT = 12000;

// The status word a result entry opens with ("done · 12.0s · 3k in").
const statusOf = (result) => (result ? result.text.split(" · ")[0] : "cut off");
// What in the log is the model at work (a turn), as opposed to the window's
// own lines (status, report, help, btw…), which may follow a result.
const WORK = new Set(["text", "tool", "perm", "ask", "shell", "changes", "diff"]);

// "report" when this turn ends the brief's work, else why it doesn't:
// - "stopped": the user cut in; what they type next continues it.
// - "error" / "cut off": an API error, a usage limit, a dropped socket. The
//   tab stopped mid-work, and the work resumes in a later turn.
// - "queued": a message still waiting to run is the rest of the work.
// - "agents": a background subagent is out; its finish wakes the next turn.
export function briefVerdict(turn, { queued = false, agents = false } = {}) {
  // A turn nobody typed (a subagent finishing woke it) opens with no user
  // line, so the slice can hold an earlier turn's result. Only a result that
  // closes the log counts; work after it with none of its own was cut off.
  const at = turn.findLastIndex((e) => e.kind === "result");
  const closes = at >= 0 && !turn.slice(at + 1).some((e) => WORK.has(e.kind));
  const status = closes ? statusOf(turn[at]) : "cut off";
  if (status !== "done") return status;
  if (queued) return "queued";
  if (agents) return "agents";
  return "report";
}

// The tabs tool's words for each wait (PlanApp's tab list).
export const HELD = {
  error: "its last turn ended on an error; it needs a resume",
  "cut off": "its connection dropped mid-turn; it needs a resume",
  stopped: "stopped by the user",
  queued: "a queued message is still to run",
  agents: "a background subagent is still out",
};

// The log since the brief, split into turns at each user message (or a
// wake: a turn a child's report started, Console.jsx).
export function lastTurn(entries) {
  return entries.slice(entries.findLastIndex((e) => e.kind === "user" || e.kind === "wake") + 1);
}

// The files every turn since the brief touched, lines summed per path.
export function changedFiles(entries) {
  const byPath = new Map();
  for (const e of entries)
    for (const f of e.kind === "diff" ? e.files : []) {
      const had = byPath.get(f.path);
      byPath.set(f.path, had ? { ...f, additions: had.additions + f.additions,
                                 deletions: had.deletions + f.deletions } : f);
    }
  return [...byPath.values()];
}

// The last break (paragraph, else line, else space) at or before `at` and
// after `floor`; `at` itself when there is none.
function breakBefore(text, at, floor) {
  for (const sep of ["\n\n", "\n", " "]) {
    const i = text.lastIndexOf(sep, at);
    if (i > floor) return i;
  }
  return at;
}

// The first break at or after `at` and before `ceil`.
function breakAfter(text, at, ceil) {
  for (const sep of ["\n\n", "\n", " "]) {
    const i = text.indexOf(sep, at);
    if (i >= 0 && i < ceil) return i + sep.length;
  }
  return at;
}

// Long text, cut from the middle at breaks, never inside a word, with a line
// saying how much and where the whole is. The middle because a summary's ends
// are what matter: what was done opens it, what's left to decide closes it.
// A head-only cut lands on "## For you to decide\n- **…".
export function clip(text, limit, where) {
  if (text.length <= limit) return text;
  const headEnd = breakBefore(text, Math.floor(limit * 0.7), Math.floor(limit * 0.5));
  const tailStart = breakAfter(text, text.length - Math.floor(limit * 0.25), text.length - Math.floor(limit * 0.1));
  const cut = tailStart - headEnd;
  return `${text.slice(0, headEnd).trimEnd()}\n\n[… ${cut} characters cut from the middle `
    + `of a ${text.length}-character message. The full text is ${where}.]\n\n`
    + text.slice(tailStart).trimStart();
}

// The report itself: the turn's last words (the summary; the narration before
// it isn't), and the files changed since the brief.
export function buildReport(entries, briefAt, { from, label, sessionId = null, limit = REPORT_LIMIT }) {
  const turn = lastTurn(entries);
  const said = turn.findLast((e) => e.kind === "text")?.text?.trim() || "";
  const files = changedFiles(entries.slice(briefAt));
  const where = `the last message in tab ${from}${sessionId ? ` (session ${sessionId})` : ""}`;
  const changed = files.length
    ? `\n\nFiles changed: ${files.map((f) => `${f.path} (+${f.additions} −${f.deletions})`).join(", ")}`
    : "";
  return { from, label, status: statusOf(turn.findLast((e) => e.kind === "result")),
           text: clip(said, limit, where) + changed };
}
