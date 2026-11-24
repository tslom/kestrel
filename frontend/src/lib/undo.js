import { useEffect, useState, useSyncExternalStore } from "react";
import { fetchJSON, postJSON } from "./api";
import { useLoad } from "./refresh";

// Every one-click action in the plan view can be taken back: the action happens and a toast offers the inverse
// for 8s (paused while hovered, focused or the tab is hidden). Two shapes:
//   offerUndo({ label, undo })          the write already happened; undo() is its inverse
//   offerUndo({ label, commit, undo })  the write is held until the toast goes (an unsubscribe can't be recalled)
// `askId` marks finishing that ask: counts toward "N cleared today" (/api/usage/cleared); undo takes it back out.
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

// Deletes, held: the row leaves at once and the DELETE goes out when the toast does (or the next offer lets it go);
// Undo puts the row back; a window closed inside the 8s keeps it. One shape for every delete in the app.
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
    // Released once the write and the caller's reload land (no flash back in); a failed write brings the row back.
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
