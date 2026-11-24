import { useEffect, useState } from "react";

// What the console is doing, for the plan view's map. Console.jsx emits; nothing here talks to the socket.
// A module-level bus, not props: the console is memoised on stable props and a turn changes state a dozen times a second.
//
//   { type: "start" }                          you sent a message
//   { type: "call", id, name, detail, raw, parent }   `parent` = the Agent call it runs under
//   { type: "done", id, error, image? }         `image` = the picture it gave the model
//   { type: "computer", id, show }              computer-use job running; `show` when it needs an answer
//   { type: "computer_end" }
//   { type: "background", items }               `!` commands running on their own
//   { type: "agents", items }                   subagents (console_agents.py rows)
//   { type: "limited", on }                     the usage limit cut its last turn off
//   { type: "plan", text }                      the plan Claude last proposed
//   { type: "waiting", name, requestId, detail, question? }   a card is asking you
//   { type: "answered" }
//   { type: "text" }                            reply text is streaming
//   { type: "end", error, duration_ms, tokens_in, tokens_out }
//   { type: "rate", msg }                       a rate_limit event off the socket
//   { type: "runs", items, url }                a dev tab's project.json script runs + its previewUrl
//   { type: "preview", url }                    open the view pane's Preview on `url`
//
// Every event carries `tab` (each tab has its own socket and session, all mounted); "rate" is account-wide and owned by no tab.

const subs = new Set();

// Tabs whose last "limited" said on: a tab mounts before the plan view subscribes, so late subscribers are caught up.
const limitedNow = new Set();

export function emitTurn(ev) {
  if (ev.type === "limited") { if (ev.on) limitedNow.add(ev.tab); else limitedNow.delete(ev.tab); }
  if (ev.type === "closed") limitedNow.delete(ev.tab);
  subs.forEach((fn) => fn(ev));
}

function subscribe(fn) {
  subs.add(fn);
  limitedNow.forEach((tab) => fn({ type: "limited", on: true, tab }));
  return () => subs.delete(fn);
}

// What the rest of the window may ask of the console; each tab registers these and clears them on unmount.
// An answer names its tab (a background tab's card is answerable from "needs you"); a draft or pin goes to the active tab.
const actions = new Map();
let activeTab = null;
export function setActiveTab(tab) { activeTab = tab; }
export function setConsoleActions(tab, a) {
  if (a) actions.set(tab, a); else actions.delete(tab);
}
export function resumeConsole(tab) { actions.get(tab)?.resume?.(); }
// After a quit (lib/interrupted.js): a "continue" saying what was cut off.
export function resumeInterruptedConsole(tab, text) { actions.get(tab)?.resumeWith?.(text); }
export function stopShell(id, tab = activeTab) { actions.get(tab)?.stopShell?.(id); }
export function answerConsole(requestId, allow, tab = activeTab) {
  actions.get(tab)?.answer?.(requestId, allow);
}
export function draftConsole(text) { actions.get(activeTab)?.draft?.(text); }
// A context pill from somewhere with no onPin handed down.
export function pinConsole(label, data) { actions.get(activeTab)?.pin?.(label, data); }
// A handed-off tab's outcome, back to its parent tab; dropped if that tab has closed.
export function reportConsole(tab, report) { actions.get(tab)?.report?.(report); }

// Which map node a tool call touched: custom tools are mcp__vault__<name>; provider calls name their source; the rest land on the machine.
const PROVIDER_CARD = {
  calendar: "calendar", email: "email", messages: "people", health: "health",
  spotify: "music", lastfm: "music", location: "location", finance: "finance",
  features: "trends",
};

const TOOL_CARD = [
  [/^email_/, "email"], [/^(calendar_ids|event_|free_time)/, "calendar"],
  [/^(chat_search|contact_|message_files)/, "people"], [/^task_/, "tasks"],
  [/^list_/, "lists"], [/^(debt|beer)_/, "debts"], [/^watchlist_/, "watchlist"],
  [/^expense_/, "expenses"], [/^habit_/, "habits"],
  [/^(package_|trip_)/, "travel"], [/^course_/, "courses"], [/^brain_/, "brain"],
  [/^file_read$/, "vault"], [/^db_query$/, "db"], [/^reddit_search$/, "web"],
];

function parse(raw) {
  try { return JSON.parse(raw || "{}"); } catch { return {}; }
}

// Agent bookkeeping (schemas, todos, subagents, questions): in the timeline, draws no line.
const NO_NODE = new Set(["ToolSearch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Task", "Agent", "ExitPlanMode",
                         "EnterPlanMode", "AskUserQuestion", "Skill"]);

// A transcript keeps a call's one-line detail, not its raw input: rebuild what nodeFor reads, for replays.
export function nodeForDetail(name, detail = "") {
  const d = String(detail);
  return nodeFor(name, JSON.stringify({ source: /source=(\w+)/.exec(d)?.[1], command: d,
                                        file_path: d.split(" ")[0] }));
}

export function nodeFor(name, raw) {
  if (NO_NODE.has(name)) return null;
  const input = parse(raw);
  const custom = /^mcp__vault__(.+)$/.exec(name);
  if (custom) {
    const t = custom[1];
    if (t === "provider") return PROVIDER_CARD[input.source] || "db";
    return TOOL_CARD.find(([re]) => re.test(t))?.[1] || "db";
  }
  if (/Gmail/.test(name)) return "email";
  if (/Google_Calendar/.test(name)) return "calendar";
  if (name === "WebFetch" || name === "WebSearch") return "web";
  if (name === "Bash") {
    const cmd = input.command || "";
    const prov = /app\.providers\s+(\w+)/.exec(cmd);
    if (prov) return PROVIDER_CARD[prov[1]] || "db";
    if (/sqlite3\b.*\bapp\.db/.test(cmd)) return "db";
    return "shell";
  }
  // Read/Edit/Write/Glob/Grep and the rest: the console's cwd is the vault.
  const path = input.file_path || input.path || input.notebook_path || "";
  if (/app\.db$/.test(path)) return "db";
  return "vault";
}

// Per-node visit counts across turns, for the map key. Browser-local: nothing else reads it.
const USE_KEY = "plan_node_use";

function readUse() {
  try { return JSON.parse(localStorage.getItem(USE_KEY) || "{}"); } catch { return {}; }
}

function bumpUse(nodes) {
  try {
    const use = readUse();
    for (const n of nodes) use[n] = (use[n] || 0) + 1;
    localStorage.setItem(USE_KEY, JSON.stringify(use));
    return use;
  } catch { return {}; }
}

// The console as the rest of the window sees it:
//   live: { node: "live"|"wait"|"err" } during a turn; touched: nodes the last turn went to
//   waiting: { requestId, name, detail, question }; calls: { id, name, detail, node, state, t0, t1, waitMs } (ms since turn start)
//   last: { duration_ms, tokens_in, tokens_out, error }; rate: rate_limit_type -> latest event
const now = () => performance.now();

const IDLE = Object.freeze({
  calls: [], waiting: null, waitSince: null, running: false, writing: false,
  startedAt: null, last: null, touched: new Set(), views: [], computer: null,
  background: [], plan: null, edited: [], agents: [], worktree: null,
  runs: [], previewUrl: null, preview: null,
});

const AGENT_DONE = new Set(["completed", "failed", "stopped", "killed"]);
export const runningAgents = (t) => (t?.agents || []).filter((a) => !AGENT_DONE.has(a.status));

function editedFile(name, raw) {
  if (!/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(name)) return null;
  try { return JSON.parse(raw)?.file_path || JSON.parse(raw)?.notebook_path || null; } catch { return null; }
}

// Plan mode writes the plan under ~/.claude/plans and may never reach ExitPlanMode.
function planIn(name, raw) {
  if (name !== "Write") return null;
  try {
    const input = JSON.parse(raw);
    return input?.file_path?.includes("/.claude/plans/") ? input.content || null : null;
  } catch { return null; }
}

// One tab's turn advanced by one event.
function step(t, ev) {
  const at = t.startedAt == null ? 0 : now() - t.startedAt;
  const patch = (id, fn) => t.calls.map((c) => (c.id === id ? fn(c) : c));
  switch (ev.type) {
    case "start":
      return { ...t, calls: [], waiting: null, waitSince: null, running: true,
               writing: false, startedAt: now(), views: [] };
    case "call": {
      // A resumed or summoned turn can start without our "start".
      const startedAt = t.running ? t.startedAt : now();
      const t0 = t.running ? at : 0;
      const call = { id: ev.id || `${ev.name}-${t.calls.length}`, name: ev.name,
                     detail: ev.detail || "", node: nodeFor(ev.name, ev.raw),
                     parent: ev.parent || null, state: "live", t0, t1: null, waitMs: 0 };
      const file = editedFile(ev.name, ev.raw);
      return { ...t, running: true, writing: false, startedAt, calls: [...t.calls, call],
               plan: planIn(ev.name, ev.raw) || t.plan,
               edited: file ? [file, ...t.edited.filter((f) => f !== file)].slice(0, 20) : t.edited };
    }
    case "done":
      if (!t.calls.some((c) => c.id === ev.id)) return t;
      return { ...t, calls: patch(ev.id, (c) => ({ ...c, state: ev.error ? "err" : "done", t1: at })),
               // Pictures Claude looked at, kept past the turn, last 8 (data URLs).
               views: ev.image
                 ? [...t.views, { src: ev.image, detail: t.calls.find((c) => c.id === ev.id).detail }].slice(-8)
                 : t.views };
    case "computer":
      return { ...t, computer: { id: ev.id, show: !!ev.show } };
    case "computer_end":
      return { ...t, computer: null };
    case "background":
      return { ...t, background: ev.items };
    case "agents":
      return { ...t, agents: ev.items };
    // A dev tab's own worktree: Files and preview read it, not the main checkout.
    case "worktree":
      return t.worktree?.path === ev.worktree?.path ? t : { ...t, worktree: ev.worktree };
    case "runs":
      return { ...t, runs: ev.items || [], previewUrl: ev.url || null };
    // `n` counts asks so asking for the same address again still opens it.
    case "preview":
      return { ...t, preview: { url: ev.url, n: (t.preview?.n || 0) + 1 } };
    case "limited":
      return !!t.limited === ev.on ? t : { ...t, limited: ev.on };
    case "plan":
      return { ...t, plan: ev.text };
    case "waiting":
      return { ...t, writing: false, waitSince: at,
               waiting: { requestId: ev.requestId, name: ev.name, detail: ev.detail,
                          question: !!ev.question } };
    case "answered": {
      if (!t.waiting) return t;
      // Charge the wait to the latest live call of that tool (the timeline draws time spent on you).
      const asked = [...t.calls].reverse().find((c) => c.state === "live" && c.name === t.waiting.name);
      const spent = at - (t.waitSince ?? at);
      return { ...t, waiting: null, waitSince: null,
               calls: asked ? patch(asked.id, (c) => ({ ...c, waitMs: c.waitMs + spent })) : t.calls };
    }
    case "text":
      return t.writing || !t.running ? t : { ...t, writing: true };
    case "end": {
      // A socket close emits an end too: with no turn in flight, "error" would be a lie.
      if (!t.running) return t;
      const nodes = new Set(t.calls.map((c) => c.node).filter(Boolean));
      // Calls that never reported back close with the turn.
      const calls = t.calls.map((c) => (c.t1 == null ? { ...c, t1: at } : c));
      return { ...t, calls, waiting: null, waitSince: null, running: false, writing: false,
               touched: nodes,
               last: { duration_ms: ev.duration_ms ?? at, tokens_in: ev.tokens_in,
                       tokens_out: ev.tokens_out, error: !!ev.error,
                       failed: calls.filter((c) => c.state === "err").length } };
    }
    default:
      return t;
  }
}

// `active`: the tab the map/bar/timeline describe; `tabs` is every tab's turn (background cards, tab dots).
export function useTurnTrace(active) {
  const [state, setState] = useState(() => ({ tabs: {}, use: readUse(), rate: {} }));

  useEffect(() => {
    const on = (ev) => setState((s) => {
      if (ev.type === "rate")
        return { ...s, rate: { ...s.rate, [ev.msg.rate_limit_type]: ev.msg } };
      if (ev.type === "closed") {
        const { [ev.tab]: _gone, ...rest } = s.tabs;
        return { ...s, tabs: rest };
      }
      const t = s.tabs[ev.tab] || IDLE;
      const next = step(t, ev);
      if (next === t) return s;
      const ended = ev.type === "end" && next.touched.size > 0;
      return { ...s, tabs: { ...s.tabs, [ev.tab]: next },
               use: ended ? bumpUse(next.touched) : s.use };
    });
    return subscribe(on);
  }, []);

  const trace = { ...(state.tabs[active] || IDLE), use: state.use, rate: state.rate,
                  tabs: state.tabs };

  // Other tabs' lines, so two consoles at work both show.
  const liveBg = {};
  for (const [id, t] of Object.entries(state.tabs)) {
    if (id === active) continue;
    for (const node of Object.keys(liveOf(t))) liveBg[node] = "live";
  }
  return { ...trace, live: liveOf(trace), liveBg };
}

// One state per node: error beats waiting on you beats running.
const RANK = { err: 3, wait: 2, live: 1, done: 0 };
function liveOf(t) {
  const live = {};
  if (!t.running) return live;
  for (const c of t.calls) {
    if (!c.node) continue;
    const s = c.state === "live" && t.waiting?.name === c.name ? "wait" : c.state;
    if (s !== "done" && (RANK[s] || 0) > (RANK[live[c.node]] || 0)) live[c.node] = s;
  }
  return live;
}

// Every tab with a turn in flight, for the map's Kestrel dots: its node (newest unfinished own call, else null),
// whether it waits on you, and its subagents with their own nodes (the tab sits on the Agent call meanwhile).
export function workingTabs(tabs, turns) {
  return tabs.flatMap((tab, i) => {
    const t = turns[tab.id];
    const bg = runningAgents(t);
    // A background subagent outlives its turn.
    if (!t?.running && !bg.length) return [];
    const open = t.running ? t.calls.filter((c) => c.state === "live") : [];
    const subs = new Map();
    for (const c of open) {
      if (c.name === "Task" || c.name === "Agent") subs.set(c.id, subs.get(c.id) ?? null);
      if (c.parent) subs.set(c.parent, c.node ?? subs.get(c.parent) ?? null);
    }
    for (const a of bg) {
      const key = a.tool_use_id || a.id;
      if (!subs.has(key)) subs.set(key, null);
    }
    return [{ id: tab.id, n: i + 1, title: tab.title || "New conversation",
              node: [...open].reverse().find((c) => c.node && !c.parent)?.node || null,
              waiting: !!t.waiting,
              subagents: [...subs].map(([id, node]) => ({ id, node })) }];
  });
}
