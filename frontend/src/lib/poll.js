import { useEffect, useRef } from "react";

/**
 * A setInterval that sits out ticks while the tab is hidden — a minimized
 * window or a background tab has nobody reading the number it would fetch.
 *
 * `catchUp`: whether a tick skipped while hidden runs the moment the tab is
 * visible again. Pass true for a poll the refresh bus doesn't reach (nothing
 * else would refresh it before the next tick). A `useLoad` widget passes false:
 * lib/refresh.js already broadcasts on return from a hidden tab once it was
 * away 5 minutes or the day rolled, and a catch-up on top would fetch twice.
 *
 * Only the interval is gated. The first load stays with the caller, and a
 * window merely behind another app is still `visible`, so it keeps polling.
 */
export function useVisiblePoll(fn, ms, { catchUp = false } = {}) {
  useTicker(fn, ms, { catchUp });
}

/**
 * The interval under useVisiblePoll. `whenHidden`: tick while the page is
 * hidden too, for a clock that has to fire whether or not anyone's looking
 * (an automatic resume after the usage limit, console/LimitBanner.jsx). A
 * hidden page's timers may be throttled, so the tick runs again the moment
 * it's visible. `ms` null: no ticking at all.
 */
export function useTicker(fn, ms, { catchUp = false, whenHidden = false } = {}) {
  const ref = useRef(fn);
  ref.current = fn;

  useEffect(() => {
    if (!ms) return undefined;
    let missed = false;
    const tick = () => {
      if (document.hidden && !whenHidden) { missed = true; return; }
      ref.current();
    };
    const onVisible = () => {
      if (document.hidden) return;
      if (whenHidden) { ref.current(); return; }
      if (!missed) return;
      missed = false;
      if (catchUp) ref.current();
    };
    const id = setInterval(tick, ms);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [ms, catchUp, whenHidden]);
}
