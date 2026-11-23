import { useEffect, useState } from "react";

// What the console is doing, for anything outside it that wants to show that
// (the plan view's map). Console.jsx emits; nothing here talks to the socket.
//
// A module-level bus rather than props, because the console is memoised on
// stable props (console/Console.jsx, ConsolePane) and a turn changes state a
// dozen times a second.
//
//   { type: "start" }                          you sent a message
//   { type: "call", id, name, detail, raw, parent }
//                                               a tool call went out; `parent`
//                                               is the Agent call it runs under
//   { type: "done", id, error, image? }         it came back, `image` being the
//                                               picture it gave the model
//   { type: "computer", id, show }              a computer-use job is running;
//                                               `show` when it needs an answer
//   { type: "computer_end" }                    …and it's over
//   { type: "background", items }               `!` commands running on their own
//   { type: "agents", items }                   subagents (console_agents.py rows)
//   { type: "limited", on }                     the usage limit cut its last turn off
//   { type: "plan", text }                      the plan Claude last proposed
//   { type: "waiting", name, requestId, detail, question? }
//                                               a card is up asking you
//   { type: "answered" }                        …and was answered
//   { type: "text" }                            reply text is streaming
//   { type: "end", error, duration_ms, tokens_in, tokens_out }
//   { type: "rate", msg }                       a rate_limit event off the socket
//   { type: "runs", items, url }                a dev tab's project.json script runs, and
//                                               its tree's previewUrl (console/ProjectBar.jsx)
//   { type: "preview", url }                    open the view pane's Preview on `url`
//
// Every event carries `tab`: the console has tabs, each its own socket and
// session, all mounted, so a background tab's turn keeps running and keeps
// reporting. "rate" is account-wide and is the one event no tab owns.

const subs = new Set();

// The tabs whose last "limited" said on. A tab mounts and says so before the
// plan view has subscribed (a child's effects run before its parent's), so
// a subscriber is caught up on these when it joins rather than missing them.
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

// The other direction: what the rest of the window may ask of the console.
// Each tab registers these (answering a permission card) and clears them on
// unmount; nothing here holds a socket. An answer names its tab, because a
// background tab's card is answerable from the "needs you" list; a draft or a
// pin goes to whichever tab is showing.
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
// A context pill, from somewhere with no onPin handed down (the email reader
// opened off a citation deep in the log).
export function pinConsole(label, data) { actions.get(activeTab)?.pin?.(label, data); }
// A handed-off tab's outcome, back to the tab that handed it off. Dropped if
// that tab has since closed.
export function reportConsole(tab, report) { actions.get(tab)?.report?.(report); }

// Which node on the map a tool call touched. Custom tools are
// mcp__vault__<name>; the provider tool and a provider CLI in Bash name their
// source; everything else lands on the machine it ran on.
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

// The agent's own bookkeeping: tools that touch nothing on the map (loading a
// tool's schema, a todo list, a subagent, a question to you). They're in the
// timeline but draw no line.
const NO_NODE = new Set(["ToolSearch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Task", "Agent", "ExitPlanMode",
                         "EnterPlanMode", "AskUserQuestion", "Skill"]);

// A past session's transcript keeps a call's one-line detail, not its raw
// input; this rebuilds as much input as nodeFor reads from it (the provider's
// source, a Bash command, a path), for the agent visualizer's replays.
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

// Per-node visit counts across turns, for the map's key (the lines weighted by
// how often the console goes there). Browser-local: it's a picture of your
// habits, not data anything else reads.
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

// The console as the rest of the window sees it.
//   live:     { node: "live" | "wait" | "err" }  during a turn (the map's lines)
//   touched:  Set of nodes the last finished turn went to
//   running:  a turn is in flight;  writing: its reply is streaming
//   waiting:  { requestId, name, detail, question } while a card asks you
//   calls:    this turn's calls, in order: { id, name, detail, node, state,
//             t0, t1, waitMs } — ms since the turn started (the timeline)
//   startedAt, last: { duration_ms, tokens_in, tokens_out, error } of the last turn
//   rate:     rate_limit_type -> latest event (the usage chip)
const now = () => performance.now();

const IDLE = Object.freeze({
  calls: [], waiting: null, waitSince: null, running: false, writing: false,
  startedAt: null, last: null, touched: new Set(), views: [], computer: null,
  background: [], plan: null, edited: [], agents: [], worktree: null,
  runs: [], previewUrl: null, preview: null,
});

const AGENT_DONE = new Set(["completed", "failed", "stopped", "killed"]);
export const runningAgents = (t) => (t?.agents || []).filter((a) => !AGENT_DONE.has(a.status));

// The file a call wrote, off its raw input.
function editedFile(name, raw) {
  if (!/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(name)) return null;
  try { return JSON.parse(raw)?.file_path || JSON.parse(raw)?.notebook_path || null; } catch { return null; }
}

// Plan mode writes the plan to a file under ~/.claude/plans, and may never reach
// ExitPlanMode, whose card is the other place a plan turns up.
function planIn(name, raw) {
  if (name !== "Write") return null;
  try {
    const input = JSON.parse(raw);
    return input?.file_path?.includes("/.claude/plans/") ? input.content || null : null;
  } catch { return null; }
}

// One tab's turn, advanced by one event. `nodes` comes back set when a turn
// ended having touched the map, so the caller can count the visits.
function step(t, ev) {
  const at = t.startedAt == null ? 0 : now() - t.startedAt;
  const patch = (id, fn) => t.calls.map((c) => (c.id === id ? fn(c) : c));
  switch (ev.type) {
    case "start":
      return { ...t, calls: [], waiting: null, waitSince: null, running: true,
               writing: false, startedAt: now(), views: [] };
    case "call": {
      // A resumed or summoned turn can start without a "start" of ours.
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
               // The pictures Claude looked at this turn, for the view pane.
               // Held past the turn's end, and a handful: they're data URLs.
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
    // A dev tab's own git worktree (console_worktree), for the view pane's
    // Files and preview: they read the tab's tree, not the main checkout.
    case "worktree":
      return t.worktree?.path === ev.worktree?.path ? t : { ...t, worktree: ev.worktree };
    case "runs":
      return { ...t, runs: ev.items || [], previewUrl: ev.url || null };
    // `n` counts the asks, so asking again for the same address still opens it.
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
      // The wait is charged to the call that asked: the latest still-live
      // call of that tool, so the timeline can draw time spent on you.
      const asked = [...t.calls].reverse().find((c) => c.state === "live" && c.name === t.waiting.name);
      const spent = at - (t.waitSince ?? at);
      return { ...t, waiting: null, waitSince: null,
               calls: asked ? patch(asked.id, (c) => ({ ...c, waitMs: c.waitMs + spent })) : t.calls };
    }
    case "text":
      return t.writing || !t.running ? t : { ...t, writing: true };
    case "end": {
      // The socket closing emits an end too (a backend restart): with no
      // turn in flight there's nothing to end, and "error" would be a lie.
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

// `active`: the tab whose turn the result describes (the map, the bar, the
// timeline). `tabs` is every tab's turn, for what has to see past the one on
// screen — a background tab's card in the "needs you" list, its dot on the tab.
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

  // The other tabs' lines, so two consoles at work both show on the map.
  const liveBg = {};
  for (const [id, t] of Object.entries(state.tabs)) {
    if (id === active) continue;
    for (const node of Object.keys(liveOf(t))) liveBg[node] = "live";
  }
  return { ...trace, live: liveOf(trace), liveBg };
}

// Collapse a tab's calls to one state per node: an error anywhere wins, then a
// call waiting on your answer, then one still running.
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

// Every tab with a turn in flight, for the map's Kestrel dots: where it is
// (the node of its newest unfinished call of its own, else null — thinking, at
// Kestrel), whether it's waiting on you, and the subagents it has out, each
// with the node its own newest call is on. A subagent's calls are its own: the
// tab is sitting on the Agent call meanwhile, not visiting them. A background
// subagent's Agent call has already come back, so its calls are what say it's
// there.
export function workingTabs(tabs, turns) {
  return tabs.flatMap((tab, i) => {
    const t = turns[tab.id];
    const bg = runningAgents(t);
    // A background subagent outlives its turn: the tab is still at work.
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
