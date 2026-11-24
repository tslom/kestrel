import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "./api";
import { batch } from "./timers.js";

// Owns the *when* of card reloads (mount, sync, day rollover, return to window, console
// `refresh` frames); widgets own their fetch. Design: DESIGN.md → When cards reload (`lib/refresh.js`).

const subscribers = new Set();   // { fn, topic }

/** Re-fetch mounted widgets: all with no `topic` (sync/rollover), else only subscribers of that topic (a bare broadcast costs provider syncs). */
export function broadcastRefresh(topic) {
  subscribers.forEach((s) => { if (!topic || s.topics.includes(topic)) s.fn(); });
}

// Parallel calls and a turn's closing pass land together; each card reads once.
const COALESCE_MS = 250;
const pending = new Set();
const flushSoon = batch(() => {
  const due = [...pending];
  pending.clear();
  due.forEach(broadcastRefresh);
}, COALESCE_MS);

/** Reload these topics shortly, once each. For streams (the console's); a click wants broadcastRefresh. */
export function refreshSoon(topics) {
  topics.forEach((t) => pending.add(t));
  flushSoon();
}

/** Bus subscription for code outside React; installed once, never removed. */
export function onTopic(topic, fn) {
  subscribers.add({ fn, topics: [topic] });
}

/** Call `fn` on the bus: for a view that loads itself and has no useLoad. */
export function useTopic(topic, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  // Keyed on the joined names so an inline array literal doesn't resubscribe.
  const topics = [].concat(topic ?? []).join(" ");
  useEffect(() => {
    const sub = { fn: () => ref.current(), topics: topics ? topics.split(" ") : [] };
    subscribers.add(sub);
    return () => subscribers.delete(sub);
  }, [topics]);
}

/**
 * Wire a widget's loader to the bus; `topic` (name or array) only adds a way to be called.
 * @returns [reload, busy], busy while a load is in flight.
 */
export function useLoad(loader, topic) {
  const [busy, setBusy] = useState(false);
  // Loader is redefined every render: keep the subscription stable via a ref.
  const ref = useRef(loader);
  ref.current = loader;

  const reload = useCallback(() => {
    setBusy(true);
    return Promise.resolve(ref.current()).finally(() => setBusy(false));
  }, []);

  useEffect(() => { reload(); }, [reload]);
  useTopic(topic, reload);

  return [reload, busy];
}

/**
 * One payload, its error, and useLoad's reload/busy; a successful load clears the error.
 * @param source a URL to GET, or a function returning a promise (read through a ref, so state-dependent URLs update).
 * @returns {{data, setData, error, setError, reload, busy}}; `reload` returns the load's promise.
 */
export function useWidgetData(source, topic) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [reload, busy] = useLoad(() =>
    (typeof source === "function" ? source() : fetchJSON(source))
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message)), topic);
  return { data, setData, error, setError, reload, busy };
}

// A window left open across 4am would show yesterday: ask the server for its day_key (the
// client must not compute the rollover) whenever the window returns, and refresh on change.

let lastDay = null;
let watching = false;

/** @returns true if the day changed and a refresh was already broadcast. */
async function checkDay() {
  try {
    const { day } = await fetchJSON("/api/healthz");
    if (!day) return false;
    const rolled = Boolean(lastDay) && day !== lastDay;
    lastDay = day;
    if (rolled) broadcastRefresh();
    return rolled;
  } catch { return false; /* offline: try again on the next wake */ }
}

// One refresh on return after being away AWAY_MS (not per alt-tab: syncs, billable Routes calls, aw probes).
const AWAY_MS = 5 * 60 * 1000;

// blur/focus too: switching apps leaves visibilityState "visible".
let awaySince = null;

function onLeave() {
  if (awaySince === null) awaySince = Date.now();
}

async function onReturn() {
  const away = awaySince === null ? 0 : Date.now() - awaySince;
  awaySince = null;
  // The rollover check broadcasts itself; don't reload twice.
  const rolled = await checkDay();
  if (!rolled && away >= AWAY_MS) broadcastRefresh();
}

/** Start the single window watcher (rollover + refresh on return); idempotent. */
export function watchDayRollover() {
  if (watching) return;
  watching = true;
  checkDay();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") onReturn(); else onLeave();
  });
  window.addEventListener("focus", onReturn);
  window.addEventListener("blur", onLeave);
  // Backstop for a window that stays focused straight through 4am.
  setInterval(checkDay, 5 * 60 * 1000);
}
