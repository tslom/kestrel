// Child tabs: the ones a tab opened through open_tab, drawn under it and
// left to run themselves. Plain functions over the tab list (lib/tabs.js) and
// the turn trace (lib/turn.js), so node can test them
// (tests/children.test.mjs, run by pytest).
//
// A child is in one of five states, the dot it gets in the tab bar:
// - "queued": every working slot was taken when it was asked for; it isn't
//   mounted, and starts by itself when one frees (nextQueued).
// - "working": mid-turn, or about to start on its brief.
// - "needs": it can't go on without you: a question waiting, a turn that
//   died (an error, a dropped socket, a stop), or a finished brief that
//   didn't land (commits not merged, a `Needs you:` line, landing()).
// - "done": finished and landed. Closed into the parent's group, unmounted,
//   still resumable by a click.
// - "idle": none of those, e.g. one the user has since typed in.
//
// Before 11-18 children were peers in a flat bar, a closed one's unmerged
// branch was forgotten, and a sixth open_tab was refused outright.

// Tabs past these wait in the queue, the rest of the queue is refused.
export const MAX_QUEUED = 8;
// Done children kept in their parent's group; the oldest drops off (its
// session stays in the sessions panel).
export const DONE_KEEP = 12;

const HELD_DEAD = new Set(["error", "cut off", "stopped"]);

// The tab a child hangs under: its top ancestor that's still open. A child
// of a child (one the user unlocked by typing in it) groups with the root.
export function rootOf(tab, byId) {
  let t = tab, hops = 0;
  while (t.parent && byId.has(t.parent) && hops++ < 20) t = byId.get(t.parent);
  return t;
}

// The bar's order: each top-level tab followed by everything under it, in
// the order they were opened. ⌥1–9 count in this order too.
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

// A tab mounted in the window: not waiting in the queue, not closed into a
// group, not snoozed or settled (lib/tabRest.js). The one on screen is
// mounted whatever it is.
export const isLive = (t) => !t.queued && !t.done && !t.settled && !t.snoozedUntil;

export function childState(t, turn) {
  if (t.queued) return "queued";
  if (turn?.waiting || t.needs || HELD_DEAD.has(t.held)) return "needs";
  if (turn?.running || t.brief || t.held) return "working";
  if (t.done) return "done";
  return "idle";
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// The parent's badge: "3 running · 1 needs you". Done ones count only once
// nothing else is going on — "4 done" is the news when it's all there is.
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

// Tabs mid-turn, plus handed-off ones about to start on their brief (their
// turn hasn't begun yet, but it's coming, and it draws on the same window).
export function busyCount(tabs, turns) {
  return tabs.filter((t) => isLive(t) && (turns[t.id]?.running || t.brief)).length;
}

// What open_tab gets: { start } now, { queue: n } (its place), or { refuse }.
// A queue that has anything in it takes the newcomer behind it, so briefs
// start in the order they were asked for.
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

// The child's own word that it can't land: a line `Needs you: <why>` in its
// report (SPAWNED_APPEND asks for it), markdown dressing allowed.
export function needsLine(text) {
  const m = /^[ \t>*_-]*needs you[*_]*:[*_]*[ \t]*(.+)$/im.exec(text || "");
  return m ? m[1].replace(/[*_]+$/, "").trim() || null : null;
}

const s = (n) => (n === 1 ? "" : "s");

// Whether a finished brief landed: { done } closes the child into its group,
// else { why } is what it needs you for. `status` is its worktree's
// (/api/console/worktree): commits beyond the base or uncommitted files mean
// the work isn't on the base yet. null: no worktree (a general or study tab,
// or a dev tab in the main checkout), so only its own word counts.
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

// A parent's follow-up to its child (message_tab): { start } — it goes as
// the child's next brief, now or when its turn ends; { queue } — a done
// child reopening while every working slot is taken; or { refuse } with why.
// Only the tab's own children: the rest are the user's or another tab's.
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
