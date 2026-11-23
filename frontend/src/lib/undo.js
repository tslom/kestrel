import { useEffect, useState, useSyncExternalStore } from "react";
import { fetchJSON, postJSON } from "./api";
import { useLoad } from "./refresh";

// Every one-click action in the plan view can be taken back for a few
// seconds: the action happens, a toast offers the inverse. The window is 8s,
// but it only counts down while you could be reading it — hovering or
// focusing the toast pauses it, and so does leaving the tab.
//
// Two shapes:
//   offerUndo({ label, undo })          the write already happened; undo() is
//                                       its inverse (reopen the task, …)
//   offerUndo({ label, commit, undo })  the write is held until the toast goes
//                                       (an unsubscribe can't be recalled);
//                                       undo() just puts things back on screen
// `askId` marks the action as finishing that ask: it counts toward "N cleared
// today" (/api/usage/cleared) and undo takes it back out. Snoozing doesn't.
//
// One toast at a time: a new offer lets the old one go, committing it.

export const UNDO_MS = 8000;

let current = null;          // { label, undo, commit, askId, id }
let seq = 0;
const subs = new Set();
const publish = () => subs.forEach((fn) => fn(current));

let cleared = { count: null };
const clearedSubs = new Set();
function setCleared(res) {
  if (res && typeof res.count === "number") {
    cleared = { count: res.count };
    clearedSubs.forEach((fn) => fn(cleared));
  }
}

function markCleared(askId, undo = false) {
  postJSON("/api/usage/cleared", { ask_id: askId, undo }).then(setCleared).catch(() => {});
}

function settle(entry) {
  // The toast went without an undo: a held write goes out now.
  if (entry?.commit) Promise.resolve(entry.commit()).catch(() => {});
}

export function offerUndo(entry) {
  settle(current);
  current = { ...entry, id: ++seq };
  if (entry.askId) markCleared(entry.askId);
  publish();
  return current.id;
}

export function dismissUndo(id) {
  if (!current || (id != null && current.id !== id)) return;
  const e = current;
  current = null;
  publish();
  settle(e);
}

export function runUndo() {
  if (!current) return false;
  const e = current;
  current = null;
  publish();
  if (e.askId) markCleared(e.askId, true);
  Promise.resolve(e.undo?.()).catch(() => {});
  return true;
}

// Deletes, held: the row leaves the screen at once and the DELETE goes out
// only when the toast does (or the next offer lets it go); Undo just puts the
// row back. One shape for every delete in the app (watchlist and list rows,
// a whole list, debts, the Sources fault log), instead of a browser confirm
// on one and nothing on the rest. A window closed inside the 8s keeps the row.
//   holdDelete({ label, key, commit })   key: what useHeld() hides ("list:3")
//   useHeld()                            the keys held right now (a Set)
let held = new Set();
const heldSubs = new Set();
const setHeld = (key, on) => {
  held = new Set(held);
  if (on) held.add(key); else held.delete(key);
  heldSubs.forEach((fn) => fn());
};
const subscribeHeld = (fn) => { heldSubs.add(fn); return () => heldSubs.delete(fn); };
const heldNow = () => held;

export function holdDelete({ label, key, commit }) {
  setHeld(key, true);
  return offerUndo({
    label,
    // released once the write (and the caller's reload) lands, so the row
    // doesn't flash back in between; on a failed write it comes back
    commit: () => Promise.resolve(commit()).finally(() => setHeld(key, false)),
    undo: () => setHeld(key, false),
  });
}

export const useHeld = () => useSyncExternalStore(subscribeHeld, heldNow);

// The toast's state, for whoever draws it.
export function useUndo() {
  const [entry, setEntry] = useState(current);
  useEffect(() => { subs.add(setEntry); return () => subs.delete(setEntry); }, []);
  return entry;
}

// Today's cleared count: read on mount and on the refresh bus (a sync, the 4am
// rollover), and kept current in between by the POSTs above.
export function useClearedToday() {
  const [c, setC] = useState(cleared);
  useEffect(() => { clearedSubs.add(setC); return () => clearedSubs.delete(setC); }, []);
  useLoad(() => fetchJSON("/api/usage/cleared").then(setCleared).catch(() => {}));
  return c.count;
}
