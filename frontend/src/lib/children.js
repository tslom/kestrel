// Child tabs (opened through open_tab): plain functions over lib/tabs.js and lib/turn.js (tests/children.test.mjs).
// States, the tab-bar dot: "queued" (no working slot free; unmounted, starts when one frees), "working",
// "needs" (a question, a dead turn, or a finished brief that didn't land: see landing()), "done" (landed; closed
// into its parent's group, resumable by a click), "idle" (e.g. the user typed in it).

export const MAX_QUEUED = 8;
// Done children kept per parent group; the oldest drops (its session stays in the sessions panel).
export const DONE_KEEP = 12;

const HELD_DEAD = new Set(["error", "cut off", "stopped"]);

// The top ancestor still open: a child of a child groups with the root.
export function rootOf(tab, byId) {
  let t = tab, hops = 0;
  while (t.parent && byId.has(t.parent) && hops++ < 20) t = byId.get(t.parent);
  return t;
}

// The bar's order (⌥1-9 count in it): each top-level tab, then everything under it in open order.
export function tabOrder(tabs) {
  const byId = new Map(tabs.map((t) => [t.id, t]));
  const kids = new Map();
  const top = [];
  for (const t of tabs) {
    const root = rootOf(t, byId);
    if (root === t) top.push(t);
    else kids.set(root.id, [...(kids.get(root.id) || []), t]);
  }
  return top.map((t) => ({ tab: t, children: kids.get(t.id) || [] }));
}

// Mounted: not queued, done, snoozed or settled (the one on screen is mounted regardless).
export const isLive = (t) => !t.queued && !t.done && !t.settled && !t.snoozedUntil;

export function childState(t, turn) {
  if (t.queued) return "queued";
  if (turn?.waiting || t.needs || HELD_DEAD.has(t.held)) return "needs";
  if (turn?.running || t.brief || t.held) return "working";
  if (t.done) return "done";
  return "idle";
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// The parent's badge, "3 running · 1 needs you"; done counts only when nothing else is going on.
export function summary(children, turns) {
  const n = { working: 0, needs: 0, queued: 0, done: 0, idle: 0 };
  for (const c of children) n[childState(c, turns[c.id])] += 1;
  const parts = [];
  if (n.working) parts.push({ state: "working", text: `${n.working} running` });
  if (n.needs) parts.push({ state: "needs", text: `${n.needs} need${n.needs === 1 ? "s" : ""} you` });
  if (n.queued) parts.push({ state: "queued", text: `${n.queued} queued` });
  if (!parts.length && n.done) parts.push({ state: "done", text: `${n.done} done` });
  if (!parts.length && n.idle) parts.push({ state: "idle", text: plural(n.idle, "tab") });
  return { counts: n, parts, text: parts.map((p) => p.text).join(" · ") };
}

// Mid-turn tabs plus handed-off ones about to start (same window).
export function busyCount(tabs, turns) {
  return tabs.filter((t) => isLive(t) && (turns[t.id]?.running || t.brief)).length;
}

// open_tab's answer: { start }, { queue: place }, or { refuse }. A non-empty queue takes newcomers behind it (FIFO).
export function admit(tabs, turns, { from = null, maxWorking, maxTabs, maxQueued = MAX_QUEUED }) {
  const others = busyCount(tabs.filter((t) => t.id !== from), turns);
  const live = tabs.filter(isLive).length;
  const queued = tabs.filter((t) => t.queued).length;
  if (live >= maxTabs)
    return { refuse: `${maxTabs} tabs are open — call tabs to see them and close ones that are done, then try again` };
  if (others < maxWorking && !queued) return { start: true };
  if (queued >= maxQueued)
    return { refuse: `${queued} tabs are already queued behind ${maxWorking} working ones; wait for some to finish` };
  return { queue: queued + 1 };
}

// The queued tab whose turn it is, if a slot is free.
export function nextQueued(tabs, turns, { maxWorking, maxTabs }) {
  if (tabs.filter(isLive).length >= maxTabs) return null;
  if (busyCount(tabs, turns) >= maxWorking) return null;
  const q = tabs.filter((t) => t.queued).sort((a, b) => (a.queuedAt || 0) - (b.queuedAt || 0));
  return q[0]?.id || null;
}

// A queued tab's place in line, from 1.
export function queuePlace(tabs, id) {
  const q = tabs.filter((t) => t.queued).sort((a, b) => (a.queuedAt || 0) - (b.queuedAt || 0));
  return q.findIndex((t) => t.id === id) + 1;
}

// A `Needs you: <why>` line in a child's report (SPAWNED_APPEND asks for it); markdown dressing allowed.
export function needsLine(text) {
  const m = /^[ \t>*_-]*needs you[*_]*:[*_]*[ \t]*(.+)$/im.exec(text || "");
  return m ? m[1].replace(/[*_]+$/, "").trim() || null : null;
}

const s = (n) => (n === 1 ? "" : "s");

// Whether a finished brief landed: { done } or { why }. `status` is its worktree's (/api/console/worktree):
// commits ahead or dirty files mean not on base. null = no worktree, so only its own word counts.
export function landing({ text, status = null }) {
  const said = needsLine(text);
  if (said) return { done: false, why: said };
  if (status?.error) return { done: false, why: `its worktree couldn't be read (${status.error})` };
  if (status) {
    const left = [];
    if (status.ahead) left.push(`${status.ahead} commit${s(status.ahead)} on ${status.branch} not on ${status.base}`);
    const d = status.dirty?.length || 0;
    if (d) left.push(`${d} uncommitted file${s(d)} in its worktree`);
    if (left.length) return { done: false, why: `${left.join(" and ")}: it didn't land` };
  }
  return { done: true, why: null };
}

// message_tab: { start } (next brief, now or at turn end), { queue } (done child reopening, slots full) or { refuse }; own children only.
export function followUp(tabs, turns, { id, from, maxWorking, maxTabs }) {
  const t = tabs.find((x) => x.id === id);
  if (!t) return { refuse: `no tab ${id}; call tabs to see the ids` };
  if (t.parent !== from) return { refuse: `tab ${id} wasn't opened by this one, so it isn't this tab's to message` };
  if (t.queued) return { refuse: `tab ${id} is still queued and hasn't started its brief; it reports when it has` };
  if (t.brief) return { refuse: `tab ${id} hasn't taken its last message in yet; wait for its report` };
  if (isLive(t) || turns[id]?.running) return { start: true };
  if (tabs.filter(isLive).length >= maxTabs)
    return { refuse: `${maxTabs} tabs are open, so the done tab ${id} can't reopen; close ones that are done first` };
  return busyCount(tabs, turns) >= maxWorking ? { queue: true } : { start: true };
}
