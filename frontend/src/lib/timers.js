// Every wall-clock delay in src/ goes through here (eslint.config.js lets only this file call setTimeout).
// A timeout papering over an ordering bug is what the ban stops: wait on the event, promise or response instead.
//   useDebounce(fn, ms, deps)  run once deps hold still for ms (search boxes)
//   useTimeout()               restartable timer cleared on unmount (flashes, toasts, backoff, polls)
//   timer()                    the same outside React
//   batch(fn, ms)              first call arms it, later ones ride along
//   withTimeout(promise, ms)   bound a promise that may never settle
import { useEffect, useRef } from "react";

// setTimeout treats anything past 2^31-1 ms (24.8 days) as 0 and fires at once.
const MAX_MS = 2 ** 31 - 1;
const clamp = (ms) => Math.min(Math.max(0, ms), MAX_MS);

/**
 * One pending call at a time: `start` replaces whatever was pending.
 * `pending` is true from start until it fires or is cleared.
 */
export function timer() {
  let id = null;
  return {
    start(fn, ms) {
      clearTimeout(id);
      id = setTimeout(() => { id = null; fn(); }, clamp(ms));
    },
    clear() { clearTimeout(id); id = null; },
    get pending() { return id !== null; },
  };
}

/** A timer() cleared on unmount; the same object every render, so old socket-handler closures can still use it. */
export function useTimeout() {
  const ref = useRef(null);
  ref.current ??= timer();
  const t = ref.current;
  useEffect(() => () => t.clear(), [t]);
  return t;
}

/**
 * Run `fn` once `deps` hold still for `ms`; `fn` gets `stale()` (true once replaced or unmounted).
 * `ms` 0 runs it now; null runs nothing. `ms` is read on deps change and isn't a dep (a clock-derived delay would restart every render).
 */
export function useDebounce(fn, ms, deps) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms == null) return undefined;
    let stale = false;
    const run = () => ref.current(() => stale);
    if (ms <= 0) {
      run();
      return () => { stale = true; };
    }
    const id = setTimeout(run, clamp(ms));
    return () => { stale = true; clearTimeout(id); };
  }, deps);   // eslint-disable-line react-hooks/exhaustive-deps -- the caller's deps, as useEffect's would be
}

/** Coalesce a burst: the first call arms `fn` for `ms`, later calls ride along instead of pushing it back. */
export function batch(fn, ms) {
  const t = timer();
  return () => { if (!t.pending) t.start(fn, ms); };
}

/** `promise`, or a rejection with `message` after `ms`; for APIs that can hang forever (WebKit dropping a permission request). */
export function withTimeout(promise, ms, message = "timed out") {
  let id;
  const clock = new Promise((_, reject) => { id = setTimeout(() => reject(new Error(message)), clamp(ms)); });
  return Promise.race([promise, clock]).finally(() => clearTimeout(id));
}
