// Files a reply names: is it real (batched to /api/files/resolve), and what
// clicking it does. The rules about what's real live backend-side
// (app/files.py); this only asks, caches, and hands out the answer.
//
// A store read through useSyncExternalStore rather than an effect per link:
// a long reply names a dozen paths, and they should cost one request, not
// twelve, and not twelve effects either. The ask is made during render,
// which is safe because it's idempotent — the first render of a key queues
// it, every later one finds it already queued.
import { createContext, useContext, useSyncExternalStore } from "react";
import { postJSON } from "./api";
import { lineless } from "./paths";
import { batch } from "./timers.js";

// `{ bases, view(hit) }` — a console tab provides it: where its relative
// paths are relative to (its worktree, its project), and its viewer pane.
// Outside a console there's none, and a click opens with the default app.
export const FileContext = createContext(null);
export const useFiles = () => useContext(FileContext);

const answers = new Map();     // key -> hit | null (resolved) ; absent = not yet
const pending = new Set();     // keys asked and not yet answered
const queued = new Map();      // bases-key -> Set<path>
const listeners = new Set();
const keyOf = (path, bases) => `${(bases || []).join("|")}\n${path}`;

function flush() {
  const batches = [...queued];
  queued.clear();
  for (const [bkey, set] of batches) {
    const bases = bkey ? bkey.split("|") : [];
    const paths = [...set];
    postJSON("/api/files/resolve", { paths, bases })
      .then((r) => { for (const p of paths) answers.set(keyOf(p, bases), r.files?.[p] ?? null); })
      // A failed ask (backend restarting) leaves them unasked, so the next
      // render of the same reply tries again.
      .catch(() => { for (const p of paths) pending.delete(keyOf(p, bases)); })
      .finally(() => listeners.forEach((f) => f()));
  }
}

// Every path a render pass names, in one request.
const flushSoon = batch(flush, 30);

function ask(path, bases) {
  const key = keyOf(path, bases);
  if (answers.has(key) || pending.has(key)) return;
  pending.add(key);
  const bkey = (bases || []).join("|");
  if (!queued.has(bkey)) queued.set(bkey, new Set());
  queued.get(bkey).add(path);
  flushSoon();
}

function subscribe(f) {
  listeners.add(f);
  return () => listeners.delete(f);
}

// The resolved file, `null` if it isn't one, `undefined` while asking.
export function useResolved(path, bases) {
  const clean = lineless(path);
  const key = keyOf(clean, bases);
  if (clean) ask(clean, bases);
  return useSyncExternalStore(subscribe, () => (answers.has(key) ? answers.get(key) : undefined));
}

export const rawUrl = (path) => `/api/files/raw?path=${encodeURIComponent(path)}`;

export function openFile(path, reveal = false) {
  return postJSON("/api/files/open", { path, reveal });
}

// The text of a markdown/text file, for the viewer; same store shape.
const texts = new Map();       // path -> { text } | { error }
export function useText(path) {
  if (path && !texts.has(path)) {
    texts.set(path, undefined);
    // eslint-disable-next-line no-restricted-syntax -- the body is the file's own text, not JSON to parse; errors are handled right here
    fetch(rawUrl(path))
      .then(async (r) => (r.ok ? { text: await r.text() } : { error: (await r.text()) || r.statusText }))
      .catch((e) => ({ error: String(e.message || e) }))
      .then((v) => { texts.set(path, v); listeners.forEach((f) => f()); });
  }
  return useSyncExternalStore(subscribe, () => texts.get(path));
}

// Called when the viewer is closed, so reopening reads the file fresh.
export function forgetText(path) {
  texts.delete(path);
}
