import { useCallback, useEffect, useRef, useState } from "react";
import { emitTurn, setActiveTab } from "./turn.js";
import { DONE_KEEP, isLive } from "./children.js";
import { SETTLED_KEEP, isResting, moveTab } from "./tabRest.js";

// The console's tabs: which exist, which shows, what each is called. Browser-local: a tab is a view onto a
// Claude Code session (kept in Claude Code's store), so closing one loses nothing "resume" can't bring back.
// Session id lives under `console_session:<tab id>` (Console.jsx); the legacy `console_session` key is adopted by the first tab.
//
// A tab: { id, title, profile (general/study/dev; null while the picker is up), project (dev repo path),
// spawned (opened via open_tab), parent (where the outcome reports), brief (first message, cleared once sent),
// reported (outcome reached parent), held (why not: "error"/"stopped"/"cut off"/"queued"/"agents"; lib/handoff.js),
// engaged (user typed: never closed for room) }.
// Child tabs (lib/children.js): queued/queuedAt (waiting unmounted past the working cap), done/doneAt (landed,
// closed into the parent's group), needs (why a finished one can't close), worktree (its tree's path), model/effort
// (the tab's own pick, overriding the profile's remembered one).
// Arrangement: pinned, name (regenerated title; first message no longer overrides), briefPills, lastActive (ms),
// merged (ms), snoozedUntil (ms), settled/settledAt/settledWhy ("merged"/"inactive"/null = yours). Resting tabs
// are unmounted (lib/tabRest.js).

const KEY = "console_tabs";
// Mounted tabs only.
export const MAX_TABS = 10;
// Mid-turn cap before a handoff is refused: all draw on the same 5-hour window.
export const MAX_WORKING = 5;

const newId = () => Math.random().toString(36).slice(2, 8);

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved?.tabs?.length) {
      // Migrate: pre-mode and retired terminal tabs become general; no lastActive counts from now (else all settle as idle).
      const now = Date.now();
      const tabs = saved.tabs.map((t) => (t.profile === undefined || t.profile === "terminal"
        ? { ...t, profile: "general", project: null } : t))
        .map((t) => (t.lastActive ? t : { ...t, lastActive: now }));
      let active = tabs.some((t) => t.id === saved.active) ? saved.active : tabs[0].id;
      // Never reopen onto a resting tab when a live one is there.
      if (isResting(tabs.find((t) => t.id === active)) && tabs.some((t) => !isResting(t)))
        active = tabs.find((t) => !isResting(t)).id;
      return { tabs, active };
    }
  } catch { /* unreadable: start over with one tab */ }
  const id = newId();
  try {
    const legacy = localStorage.getItem("console_session");
    if (legacy) localStorage.setItem(`console_session:${id}`, legacy);
  } catch { /* no storage: the tab just starts a new session */ }
  return { tabs: [{ id, title: null, profile: "general", lastActive: Date.now() }], active: id };
}

const unrest = { snoozedUntil: null, settled: false, settledAt: null, settledWhy: null };

// Where the view goes when its tab leaves: the live neighbour to the left, else any live one.
function nextActive(before, after, id) {
  const at = before.findIndex((x) => x.id === id);
  const left = before.slice(0, Math.max(0, at)).reverse()
    .find((x) => isLive(x) && after.some((y) => y.id === x.id && isLive(y)));
  return (left || after.find(isLive) || after[0]).id;
}

export function useConsoleTabs() {
  const [state, setState] = useState(load);
  const latest = useRef(state);
  latest.current = state;
  setActiveTab(state.active);

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* per-viewer nicety */ }
  }, [state]);

  // No spec: the mode picker, focused. A spec (handoff): opened *behind* the current tab.
  const open = useCallback((spec = null) => setState((s) => {
    if (!spec?.queued && s.tabs.filter(isLive).length >= MAX_TABS) return s;
    const id = newId();
    const tab = spec ? { id, title: null, ...spec } : { id, title: null, profile: null };
    return { tabs: [...s.tabs, tab], active: spec ? s.active : id };
  }), []);

  // A child whose brief landed closes into its parent's group; done ones past DONE_KEEP drop (sessions panel has them).
  const finish = useCallback((id) => setState((s) => {
    if (!s.tabs.some((t) => t.id === id && !t.done)) return s;
    let tabs = s.tabs.map((t) => (t.id === id ? { ...t, done: true, doneAt: Date.now(), needs: null } : t));
    const done = tabs.filter((t) => t.done && t.id !== s.active).sort((a, b) => a.doneAt - b.doneAt);
    const dropped = done.slice(0, Math.max(0, done.length - DONE_KEEP)).map((t) => t.id);
    if (dropped.length) tabs = tabs.filter((t) => !dropped.includes(t.id));
    for (const gone of dropped) {
      try { localStorage.removeItem(`console_session:${gone}`); } catch { /* fine */ }
    }
    return { ...s, tabs };
  }), []);

  // A queued child's turn (or the user clicking it).
  const start = useCallback((id) => setState((s) => (
    s.tabs.some((t) => t.id === id && t.queued)
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, queued: false, queuedAt: null } : t)) }
      : s)), []);

  // A parent's follow-up (message_tab): the child's next brief; a done child reopens for it (queued if slots are full).
  const followUp = useCallback((id, text, queued = false) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id ? {
      ...t, brief: text, done: false, doneAt: null, needs: null, reported: false, held: null,
      queued, queuedAt: queued ? Date.now() : null,
    } : t)),
  })), []);

  const configure = useCallback((id, profile, project = null) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, profile, project } : t)),
  })), []);

  const briefSent = useCallback((id) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id && t.brief ? { ...t, brief: null, briefPills: null } : t)),
  })), []);

  // Set a boolean once (`reported`, `engaged`); no-op after, so every send doesn't churn the list.
  // Engaging (typing) makes the tab the user's: not done/needs/merged, lastActive now (at most once a minute).
  const flag = useCallback((id, key) => setState((s) => {
    const now = Date.now();
    const stale = (t) => key === "engaged" && (t.done || t.needs || t.merged || !(now - (t.lastActive || 0) < 60000));
    return s.tabs.some((t) => t.id === id && (!t[key] || stale(t)))
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id
        ? { ...t, [key]: true, ...(key === "engaged" ? { done: false, needs: null, merged: null, lastActive: now } : {}) }
        : t)) }
      : s;
  }), []);

  const set = useCallback((id, key, value) => setState((s) => (
    s.tabs.some((t) => t.id === id && (t[key] ?? null) !== value)
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, [key]: value } : t)) }
      : s)), []);

  // The last live tab can't close. A parent takes its done children; the rest move up a level (they still owe a report).
  const close = useCallback((id) => {
    // Read latest state outside the updater: the side effects below need the list now.
    const now = latest.current;
    const t0 = now.tabs.find((x) => x.id === id);
    if (!t0 || (isLive(t0) && now.tabs.filter(isLive).length <= 1)) return;
    const gone = [id, ...now.tabs.filter((c) => c.parent === id && c.done).map((c) => c.id)];
    setState((s) => {
      const t = s.tabs.find((x) => x.id === id);
      if (!t) return s;
      const tabs = s.tabs.filter((x) => !gone.includes(x.id))
        .map((x) => (x.parent === id ? { ...x, parent: t.parent || null } : x));
      const active = gone.includes(s.active) ? nextActive(s.tabs, tabs, id) : s.active;
      return { tabs, active };
    });
    for (const g of gone) {
      try { localStorage.removeItem(`console_session:${g}`); } catch { /* fine */ }
      emitTurn({ type: "closed", tab: g });
    }
  }, []);

  // Going to a queued child starts it past the cap; going to a resting one wakes it: you asked.
  const show = useCallback((id) => setState((s) => {
    const t = s.tabs.find((x) => x.id === id);
    if (!t || (s.active === id && !t.queued && !isResting(t))) return s;
    const tabs = s.tabs.map((x) => (x.id === id
      ? { ...x, queued: false, queuedAt: null, ...unrest, lastActive: Date.now() } : x));
    return { tabs, active: id };
  }), []);

  const title = useCallback((id, text) => setState((s) => {
    const t = s.tabs.find((x) => x.id === id);
    if (!t || t.title === text) return s;
    return { ...s, tabs: s.tabs.map((x) => (x.id === id ? { ...x, title: text } : x)) };
  }), []);

  // A name you asked for (#102): the first message stops naming the tab; null reverts.
  const rename = useCallback((id, name) => setState((s) => ({
    ...s, tabs: s.tabs.map((x) => (x.id === id ? { ...x, name: name || null } : x)),
  })), []);

  const pin = useCallback((id, on) => setState((s) => {
    if (!s.tabs.some((x) => x.id === id && !!x.pinned !== !!on)) return s;
    const tabs = s.tabs.map((x) => (x.id === id ? { ...x, pinned: !!on } : x));
    // Lands at the pin line: last of the pinned, or first of the rest.
    const firstRest = tabs.find((x) => !x.pinned && x.id !== id);
    return { ...s, tabs: moveTab(tabs, id, firstRest?.id ?? null) };
  }), []);

  // Drag or step a tab to sit before `before` (null: last).
  const move = useCallback((id, before) => setState((s) => {
    const tabs = moveTab(s.tabs, id, before);
    return tabs === s.tabs ? s : { ...s, tabs };
  }), []);

  // Rest: snoozed until `until` (ms) or settled (until null; why "merged"/"inactive"/null); oldest settled past SETTLED_KEEP drop.
  const rest = useCallback((ids, { until = null, why = null } = {}) => setState((s) => {
    const list = [].concat(ids).filter((id) => s.tabs.some((t) => t.id === id && !isResting(t)));
    if (!list.length) return s;
    const now = Date.now();
    let tabs = s.tabs.map((t) => (list.includes(t.id)
      ? (until ? { ...t, snoozedUntil: until } : { ...t, settled: true, settledAt: now, settledWhy: why, snoozedUntil: null })
      : t));
    // Never rest the last live tab.
    if (!tabs.some(isLive)) return s;
    const settled = tabs.filter((t) => t.settled).sort((a, b) => a.settledAt - b.settledAt);
    const dropped = settled.slice(0, Math.max(0, settled.length - SETTLED_KEEP)).map((t) => t.id);
    if (dropped.length) tabs = tabs.filter((t) => !dropped.includes(t.id));
    for (const gone of dropped) {
      try { localStorage.removeItem(`console_session:${gone}`); } catch { /* fine */ }
    }
    const active = list.includes(s.active) ? nextActive(s.tabs, tabs, s.active) : s.active;
    return { tabs, active };
  }), []);

  // Back from rest without going to it (a snooze whose time came); unsettle goes through show().
  const wake = useCallback((ids) => setState((s) => (
    s.tabs.some((t) => ids.includes(t.id) && isResting(t))
      ? { ...s, tabs: s.tabs.map((t) => (ids.includes(t.id) ? { ...t, ...unrest, lastActive: Date.now() } : t)) }
      : s)), []);

  return { ...state, open, close, show, title, rename, pin, move, rest, wake,
           configure, briefSent, flag, set, finish, start, followUp };
}
