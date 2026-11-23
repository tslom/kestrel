import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "./api";
import { batch } from "./timers.js";

// One place that answers "is this card actually reloading, and when should it
// reload on its own". Widgets each own their fetch; this owns the *when*:
//   - on mount
//   - when a sync finishes (broadcast)
//   - when the server's day_key rolls over while the window was asleep
//   - when you come back to the window after being away a while
//   - when Kestrel changes something a card shows (console `refresh` frames)
// so nothing here knows what any widget's data is, only that it's stale.

const subscribers = new Set();   // { fn, topic }

/**
 * Tell mounted widgets to re-fetch.
 *
 * With no `topic`, everything reloads — that's the sync/rollover case, where
 * anything on the desk may have moved. With one, only the widgets that asked
 * for that topic reload: closing the Tasks sheet after ticking things off has
 * to put the Tasks card straight, but /api/health, /api/calendar and
 * /api/email run provider syncs on read, so making it reload the whole desk
 * would charge a round of network for a change to one table.
 */
export function broadcastRefresh(topic) {
  subscribers.forEach((s) => { if (!topic || s.topics.includes(topic)) s.fn(); });
}

// Parallel calls and a turn's closing pass arrive within moments of each other;
// each card should read once for the lot of them.
const COALESCE_MS = 250;
const pending = new Set();
const flushSoon = batch(() => {
  const due = [...pending];
  pending.clear();
  due.forEach(broadcastRefresh);
}, COALESCE_MS);

/**
 * Reload these topics shortly, once each however many times they're asked for.
 * For a stream of changes (the console's); a click wants broadcastRefresh.
 */
export function refreshSoon(topics) {
  topics.forEach((t) => pending.add(t));
  flushSoon();
}

/**
 * The bus for code outside React (lib/look.js reloading the page's colours).
 * Installed once at load and never removed, like a window listener.
 */
export function onTopic(topic, fn) {
  subscribers.add({ fn, topics: [topic] });
}

/**
 * Call `fn` on the bus: for a view that loads itself and has no useLoad, like a
 * full sheet with a range or a filter of its own. Same names as useLoad's.
 */
export function useTopic(topic, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  // Keyed on the joined names, so an inline array literal doesn't resubscribe
  // on every render.
  const topics = [].concat(topic ?? []).join(" ");
  useEffect(() => {
    const sub = { fn: () => ref.current(), topics: topics ? topics.split(" ") : [] };
    subscribers.add(sub);
    return () => subscribers.delete(sub);
  }, [topics]);
}

/**
 * Wire a widget's loader to the bus.
 * @param topic optional name (or array of names) this widget also answers to,
 *   for the targeted broadcasts above. A widget without one still reloads on a
 *   plain broadcastRefresh() — the topic only ever adds a way to be called.
 * @returns [reload, busy] — `busy` is true while a load is in flight, so the
 *   refresh button can say so instead of looking like nothing happened.
 */
export function useLoad(loader, topic) {
  const [busy, setBusy] = useState(false);
  // The loader is usually redefined every render; keep the subscription
  // stable and read the latest one through a ref.
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
 * The read half of nearly every card: one payload, its error, and useLoad's
 * reload/busy. The rule for what an error does lives here so it can't drift —
 * a successful load clears it, and it's shown as the error's message.
 *
 * @param source a URL to GET, or a function returning a promise of the data
 *   (for a card that assembles several reads). Read through useLoad's ref, so
 *   a URL that depends on state is picked up by the next reload.
 * @param topic passed through to useLoad.
 * @returns {{data, setData, error, setError, reload, busy}} — `setData` for
 *   optimistic edits, `setError` for a failed mutation, and `reload` for a
 *   mutation to call afterwards (it returns the load's promise).
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

// The window can sit open across a rollover (laptop shut at 1am, opened at
// 9am): every cache is keyed by day_key, so the page would keep showing
// yesterday until something forced a fetch. Ask the server for its day_key —
// the 4am rollover is a backend rule, so the client must not compute it —
// whenever the window comes back to the foreground, and refresh on a change.

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

// Come back after lunch and the desk is still showing what was true when you
// left. This is the shape of refresh-on-hover that survived review: one fire
// on return to the window, instead of one per card the cursor happens to
// cross on its way to the console.
//
// Gated on how long you were away, because a refresh is not free — /api/health,
// /api/calendar and /api/email run provider syncs on read, /api/leave makes
// billable Google Routes calls, /api/status probes aw-server with a 2s
// timeout. Flicking to the editor and back is not "coming back", and firing
// on every alt-tab would be the same needless traffic hover was rejected for.
const AWAY_MS = 5 * 60 * 1000;

// Tracked from blur/focus as well as visibilitychange: switching apps (the
// normal case for a localhost dashboard sitting behind an editor) leaves the
// tab visibilityState "visible", so visibilitychange alone never fires.
let awaySince = null;

function onLeave() {
  if (awaySince === null) awaySince = Date.now();
}

async function onReturn() {
  const away = awaySince === null ? 0 : Date.now() - awaySince;
  awaySince = null;
  // The rollover check broadcasts on its own when the date moved; letting
  // the away-refresh fire too would reload every widget twice.
  const rolled = await checkDay();
  if (!rolled && away >= AWAY_MS) broadcastRefresh();
}

/**
 * Start the (single) window watcher: day rollover + refresh on return.
 * Safe to call more than once.
 */
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
