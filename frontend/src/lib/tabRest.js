// Tabs at rest: snoozed (hidden until a time) and settled (on a Settled list). Plain functions (tests/tabRest.test.mjs).
// A resting tab is unmounted like a done child (session stays in Claude Code's store; waking resumes it), so a
// tab mid-turn or waiting on you can't rest. Self-settling: merged and looked away from, or idle INACTIVE_DAYS;
// never pinned, on screen, a child, or one still to send its first message.

export const INACTIVE_DAYS = 7;
const DAY_MS = 24 * 3600 * 1000;
export const INACTIVE_MS = INACTIVE_DAYS * DAY_MS;
export const SETTLED_KEEP = 30;
// db.day_key's 4am rollover: "tomorrow" at 2am is this morning.
const ROLLOVER_HOUR = 4;

export const isResting = (t) => !!(t.settled || t.snoozedUntil);

const busy = (turn) => !!(turn?.running || turn?.waiting);

// Whether the tab may rest now, and if not why (the menu's title).
export function canRest(t, turn) {
  if (busy(turn)) return { ok: false, why: "Stop the turn first" };
  if (t.queued) return { ok: false, why: "It hasn't started yet" };
  return { ok: true, why: null };
}

const at = (d, h, m = 0) => { const x = new Date(d); x.setHours(h, m, 0, 0); return x; };
const fmt = (d) => d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });

// The Later choices ({ label, until, hint }): an hour, tonight (before 8pm), tomorrow morning, next Monday.
export function snoozeChoices(now = new Date()) {
  const n = new Date(now);
  const out = [{ label: "1 hour", until: new Date(n.getTime() + 3600 * 1000) }];
  if (n.getHours() >= ROLLOVER_HOUR && n.getHours() < 20) out.push({ label: "Tonight", until: at(n, 20) });
  const morning = at(n, 8);
  if (n.getHours() >= ROLLOVER_HOUR) morning.setDate(morning.getDate() + 1);
  out.push({ label: "Tomorrow", until: morning });
  const monday = at(n, 8);
  monday.setDate(monday.getDate() + (((8 - monday.getDay()) % 7) || 7));
  if (monday.getTime() - morning.getTime() > DAY_MS / 2) out.push({ label: "Next week", until: monday });
  return out.map((o) => ({ ...o, until: o.until.getTime(), hint: `until ${fmt(o.until)}` }));
}

// Snoozed tabs whose time has come.
export function dueToWake(tabs, now = Date.now()) {
  return tabs.filter((t) => t.snoozedUntil && t.snoozedUntil <= now).map((t) => t.id);
}

// Tabs that settle now: [{ id, why }]. `unread` (tab ids): a merged tab waits for you to read it; an inactive one doesn't.
export function toSettle(tabs, turns, { active, now = Date.now(), unread = new Set() } = {}) {
  const byId = new Set(tabs.map((t) => t.id));
  const out = [];
  for (const t of tabs) {
    if (t.queued || t.done || isResting(t) || t.pinned || t.id === active) continue;
    if (t.brief || !t.profile || (t.parent && byId.has(t.parent))) continue;
    if (busy(turns[t.id])) continue;
    if (t.merged && !unread.has(t.id)) out.push({ id: t.id, why: "merged" });
    else if (t.lastActive && now - t.lastActive > INACTIVE_MS) out.push({ id: t.id, why: "inactive" });
  }
  return out;
}

// Pinned tabs first, the rest after, each in the order they're in.
export function pinnedFirst(tabs) {
  return [...tabs.filter((t) => t.pinned), ...tabs.filter((t) => !t.pinned)];
}

// `id` moved before `before` (null: end), clamped so it stays on its side of the pinned line.
export function moveTab(tabs, id, before) {
  const t = tabs.find((x) => x.id === id);
  if (!t || id === before) return tabs;
  const rest = tabs.filter((x) => x.id !== id);
  let i = before ? rest.findIndex((x) => x.id === before) : rest.length;
  if (i < 0) i = rest.length;
  const lastPinned = rest.reduce((n, x, k) => (x.pinned ? k + 1 : n), 0);
  const firstUnpinned = rest.findIndex((x) => !x.pinned);
  if (t.pinned) i = Math.min(i, lastPinned);
  else if (firstUnpinned >= 0) i = Math.max(i, firstUnpinned);
  return [...rest.slice(0, i), t, ...rest.slice(i)];
}

// One step among the bar's tabs (`order`), as `before` for moveTab.
export function stepTarget(order, id, step) {
  const i = order.findIndex((t) => t.id === id);
  if (i < 0) return undefined;
  if (step < 0) return i > 0 ? order[i - 1].id : undefined;
  if (i >= order.length - 1) return undefined;
  return order[i + 2]?.id ?? null;
}

export function restLine(t, now = Date.now()) {
  if (t.snoozedUntil) {
    const d = new Date(t.snoozedUntil);
    const sameDay = new Date(now).toDateString() === d.toDateString();
    return `back ${sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : fmt(d)}`;
  }
  const why = t.settledWhy === "merged" ? "merged" : t.settledWhy === "inactive" ? `idle ${INACTIVE_DAYS}d` : "settled";
  const days = t.settledAt ? Math.floor((now - t.settledAt) / DAY_MS) : 0;
  return days > 0 ? `${why} · ${days}d ago` : why;
}
