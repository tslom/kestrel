import { useCallback, useEffect, useRef, useState } from "react";
import { emitTurn, setActiveTab } from "./turn.js";
import { DONE_KEEP, isLive } from "./children.js";
import { SETTLED_KEEP, isResting, moveTab } from "./tabRest.js";

// The console's tabs: which exist, which is showing, what each is called.
// Browser-local, like the session each one resumes — a tab is a view onto a
// Claude Code session, and the session itself lives in Claude Code's store,
// so closing a tab loses nothing that "resume" can't bring back.
//
// Each tab's session id is under `console_session:<tab id>` (Console.jsx).
// Before tabs there was one console and one key, `console_session`; the first
// load takes it over as the first tab's, so the conversation you had open is
// the one you still have open.

//
// A tab is { id, title, profile, project, spawned, parent, brief, reported, held,
// engaged }. `profile`
// is its mode (general / study / dev; null while the new-tab picker is up),
// `project` a dev tab's repo path. `spawned` marks a tab another tab opened
// through open_tab, `parent` is that tab (where the brief's outcome is
// reported), and `brief` is the first message it was handed — cleared once
// sent, so a reload doesn't send it twice. `reported`: that brief's outcome
// has reached the parent (the tab is a finished handoff). `held`: why it
// hasn't yet — the brief's turn ended without finishing the work ("error",
// "stopped", "cut off", "queued", "agents"; lib/handoff.js). `engaged`: the user
// has typed in it, so it's their conversation and never closed for room.
//
// A child (lib/children.js) also has `queued` (+ `queuedAt`): asked for
// past the working cap, waiting unmounted for a slot; `done` (+ `doneAt`):
// its brief finished and landed, so it's closed into its parent's group,
// unmounted unless on screen, resumable by a click; `needs`: why a finished
// one can't close (its work didn't land); `worktree`: its tree's path, so
// the left-behind panel doesn't list a tab that's open but not yet connected.
// `model` / `effort`: the tab's own pick, when open_tab gave it one (or the
// user changed it there); Console starts it with them instead of the
// profile's remembered pick, queued or not.
//
// The user's arrangement (#102, #91): `pinned` (first in the bar, no ×),
// `name` (a regenerated title, which the first message no longer
// overrides), `briefPills` (a ⌘Enter brief's own pills), `lastActive` (ms,
// a send or a look), `merged` (ms its worktree landed), `snoozedUntil` (ms),
// `settled` + `settledAt` + `settledWhy` ("merged" / "inactive" / null for
// your own). Resting tabs are unmounted (lib/tabRest.js).

const KEY = "console_tabs";
// Mounted tabs (not queued, not done).
export const MAX_TABS = 10;
// How many tabs may be mid-turn before a handoff is refused: each one draws
// on the same 5-hour window.
export const MAX_WORKING = 5;

const newId = () => Math.random().toString(36).slice(2, 8);

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    if (saved?.tabs?.length) {
      // Tabs from before modes were all the vault console.
      // Terminal tabs were retired for a tool any tab can use.
      // A tab from before lastActive counts from now, or the first load
      // would settle every tab as a week idle.
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

// Where the view goes when the tab on screen leaves it (closed, snoozed,
// settled): the neighbour to the left that's still live, as in every tabbed
// thing, else any live one.
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

  // No spec: a tab showing the mode picker, focused. A spec (a handoff):
  // a tab already in its mode, opened *behind* the one you're in — the
  // point of handing work off is not to be pulled away from this one.
  const open = useCallback((spec = null) => setState((s) => {
    if (!spec?.queued && s.tabs.filter(isLive).length >= MAX_TABS) return s;
    const id = newId();
    const tab = spec ? { id, title: null, ...spec } : { id, title: null, profile: null };
    return { tabs: [...s.tabs, tab], active: spec ? s.active : id };
  }), []);

  // A child whose brief finished and landed: closed into its parent's group.
  // The oldest done ones past DONE_KEEP drop off (sessions panel has them).
  const finish = useCallback((id) => setState((s) => {
    if (!s.tabs.some((t) => t.id === id && !t.done)) return s;
    let tabs = s.tabs.map((t) => (t.id === id ? { ...t, done: true, doneAt: Date.now(), needs: null } : t));
    const done = tabs.filter((t) => t.done && t.id !== s.active).sort((a, b) => a.doneAt - b.doneAt);
    const dropped = done.slice(0, Math.max(0, done.length - DONE_KEEP)).map((t) => t.id);
    if (dropped.length) tabs = tabs.filter((t) => !dropped.includes(t.id));
    // In the updater (idempotent), since it may run after this call returns.
    for (const gone of dropped) {
      try { localStorage.removeItem(`console_session:${gone}`); } catch { /* fine */ }
    }
    return { ...s, tabs };
  }), []);

  // A queued child's turn (or a click on it: the user starting it now).
  const start = useCallback((id) => setState((s) => (
    s.tabs.some((t) => t.id === id && t.queued)
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, queued: false, queuedAt: null } : t)) }
      : s)), []);

  // A parent's follow-up (message_tab, lib/children.js followUp): the
  // child's next brief, so it goes when the tab is connected and idle and
  // reports back as the first did. A done child reopens for it (queued if
  // every working slot is taken).
  const followUp = useCallback((id, text, queued = false) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id ? {
      ...t, brief: text, done: false, doneAt: null, needs: null, reported: false, held: null,
      queued, queuedAt: queued ? Date.now() : null,
    } : t)),
  })), []);

  // The picker's answer: what this tab is for.
  const configure = useCallback((id, profile, project = null) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, profile, project } : t)),
  })), []);

  const briefSent = useCallback((id) => setState((s) => ({
    ...s, tabs: s.tabs.map((t) => (t.id === id && t.brief ? { ...t, brief: null, briefPills: null } : t)),
  })), []);

  // Set a boolean on a tab once (`reported`, `engaged`); a no-op after that,
  // so calling it on every send doesn't churn the tab list.
  // Typing in a tab makes it the user's: no longer done or waiting on them,
  // active as of now, and no longer a finished merge (there's more work).
  // `lastActive` moves at most once a minute, for the same churn.
  const flag = useCallback((id, key) => setState((s) => {
    const now = Date.now();
    const stale = (t) => key === "engaged" && (t.done || t.needs || t.merged || !(now - (t.lastActive || 0) < 60000));
    return s.tabs.some((t) => t.id === id && (!t[key] || stale(t)))
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id
        ? { ...t, [key]: true, ...(key === "engaged" ? { done: false, needs: null, merged: null, lastActive: now } : {}) }
        : t)) }
      : s;
  }), []);

  // Set a value on a tab (`held`: why a handoff's report is waiting, null
  // once it isn't).
  const set = useCallback((id, key, value) => setState((s) => (
    s.tabs.some((t) => t.id === id && (t[key] ?? null) !== value)
      ? { ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, [key]: value } : t)) }
      : s)), []);

  // The neighbour to the left takes over, as in every tabbed thing. The last
  // tab can't close — the console is never absent, only empty. A parent
  // takes its done children with it; the rest move up a level (a queued or
  // working child still has its brief to do, and nowhere to report it).
  const close = useCallback((id) => {
    // Read off the latest state, not in the updater: the updater may run
    // after this returns, and the side effects below need the list now.
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

  // Going to a queued child starts it now, past the cap: the user asked.
  // Going to a resting one wakes or unsettles it: same thing, you want it.
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

  // A name you asked for (regenerated, #102): the first message no longer
  // names the tab. null goes back to it.
  const rename = useCallback((id, name) => setState((s) => ({
    ...s, tabs: s.tabs.map((x) => (x.id === id ? { ...x, name: name || null } : x)),
  })), []);

  const pin = useCallback((id, on) => setState((s) => {
    if (!s.tabs.some((x) => x.id === id && !!x.pinned !== !!on)) return s;
    const tabs = s.tabs.map((x) => (x.id === id ? { ...x, pinned: !!on } : x));
    // Either way it lands at the pin line: pinning puts it last of the
    // pinned, unpinning first of the rest.
    const firstRest = tabs.find((x) => !x.pinned && x.id !== id);
    return { ...s, tabs: moveTab(tabs, id, firstRest?.id ?? null) };
  }), []);

  // Drag or step a tab to sit before `before` (null: last).
  const move = useCallback((id, before) => setState((s) => {
    const tabs = moveTab(s.tabs, id, before);
    return tabs === s.tabs ? s : { ...s, tabs };
  }), []);

  // Rest a tab: snoozed until `until` (ms), or settled (`until` null) with
  // why ("merged" / "inactive", null when you did it). The view leaves it
  // if it was showing. The oldest settled past SETTLED_KEEP drop off.
  const rest = useCallback((ids, { until = null, why = null } = {}) => setState((s) => {
    const list = [].concat(ids).filter((id) => s.tabs.some((t) => t.id === id && !isResting(t)));
    if (!list.length) return s;
    const now = Date.now();
    let tabs = s.tabs.map((t) => (list.includes(t.id)
      ? (until ? { ...t, snoozedUntil: until } : { ...t, settled: true, settledAt: now, settledWhy: why, snoozedUntil: null })
      : t));
    // Never the last live tab: the console is never absent.
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

  // Back from rest without going to it: a snooze whose time came. Unsettle
  // goes through show(), since you asked for that one.
  const wake = useCallback((ids) => setState((s) => (
    s.tabs.some((t) => ids.includes(t.id) && isResting(t))
      ? { ...s, tabs: s.tabs.map((t) => (ids.includes(t.id) ? { ...t, ...unrest, lastActive: Date.now() } : t)) }
      : s)), []);

  return { ...state, open, close, show, title, rename, pin, move, rest, wake,
           configure, briefSent, flag, set, finish, start, followUp };
}
