// Every wall-clock delay in src/ goes through here; this is the one file
// eslint.config.js lets call setTimeout. The ban is there because agents
// reach for a timeout to paper over an ordering bug (wait 300ms and hope the
// state landed). Each one that was doing that on 11-22 was really waiting on
// something it could name: a re-render, a subscriber, a backend job. So
// before you add a delay, find the event, promise or response it would be
// guessing at, and wait on that.
//
// What's left is time itself:
//   useDebounce(fn, ms, deps)  run once deps hold still for ms (search boxes)
//   useTimeout()               a restartable timer the component owns, cleared
//                              on unmount (flash messages, toasts, backoff, polls)
//   timer()                    the same outside React (a module, an effect)
//   batch(fn, ms)              first call arms it, later ones ride along
//   withTimeout(promise, ms)   a promise that may never settle, bounded
import { useEffect, useRef } from "react";

// setTimeout treats anything past 2^31-1 ms (24.8 days) as 0 and fires at
// once. A far-off snooze shouldn't mean a re-render loop.
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

/**
 * A timer() that lives as long as the component and is cleared when it
 * unmounts, so a late flash or retry never lands on an unmounted tree. The
 * same object every render, so socket handlers holding an old closure can
 * still start and clear it.
 */
export function useTimeout() {
  const ref = useRef(null);
  ref.current ??= timer();
  const t = ref.current;
  useEffect(() => () => t.clear(), [t]);
  return t;
}

/**
 * Run `fn` once `deps` have held still for `ms`; a change or unmount first
 * cancels the pending run. `fn` gets `stale()`, true once a newer run (or
 * unmount) has replaced this one, for a slow answer to drop itself.
 * `ms` 0 runs it now, in the effect (an empty box clears at once); `ms`
 * null runs nothing. `ms` is read when deps change and isn't one of them: a
 * delay computed off the clock would otherwise restart on every render.
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

/**
 * Coalesce a burst: the first call arms `fn` for `ms` from now, and calls
 * before it fires ride along instead of pushing it back. Returns the
 * scheduling function; `fn` itself can still be called directly to flush
 * early (the timer then fires on whatever's left).
 */
export function batch(fn, ms) {
  const t = timer();
  return () => { if (!t.pending) t.start(fn, ms); };
}

/**
 * `promise`, or a rejection with `message` if it hasn't settled within `ms`.
 * For an API that can leave a promise hanging forever (WebKit dropping a
 * permission request), where silence has to become an answer.
 */
export function withTimeout(promise, ms, message = "timed out") {
  let id;
  const clock = new Promise((_, reject) => { id = setTimeout(() => reject(new Error(message)), clamp(ms)); });
  return Promise.race([promise, clock]).finally(() => clearTimeout(id));
}
