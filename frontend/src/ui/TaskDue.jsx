import { useSyncExternalStore } from "react";
import { dueCountdown, fmtTime, fmtDay } from "../lib/time.js";

// The trailing due column on a task row. Inside a day of the deadline it
// becomes a live countdown — "23m", "4h 20m", "2h late" — and outside it goes
// back to the date, because "37h" is a worse answer to "when" than Thursday is.
//
// Only tasks whose due date names an hour can count down at all; an all-day
// task is due by end of day and has no instant to count to (see the v-next
// due_time migration in db.py).

// One interval for every countdown on the screen, not one per row. Rows mount
// and unmount as the widget reloads, and a per-row timer meant a board with ten
// dated tasks woke the tab ten times a minute out of step with itself — the
// same minute boundary rendering as "23m" on one row and "22m" on the next.
const TICK_MS = 30_000;
const listeners = new Set();
let timer = null;
let current = Date.now();

function subscribe(fn) {
  // The first countdown on screen re-reads the clock: a tab left open
  // overnight resumes with a stale time from whenever it last ticked.
  if (listeners.size === 0) current = Date.now();
  listeners.add(fn);
  if (timer === null) {
    timer = setInterval(() => {
      current = Date.now();
      listeners.forEach((l) => l());
    }, TICK_MS);
  }
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}
const unsubscribed = () => () => {};
const snapshot = () => current;

// A clock that only exists while something is counting down.
function useNow(active) {
  return useSyncExternalStore(active ? subscribe : unsubscribed, snapshot);
}

export default function TaskDue({ t }) {
  // Subscribing is keyed on there being an hour at all rather than on the
  // countdown being live right now, so a task 25 hours out switches over on
  // its own instead of waiting for the next widget refresh.
  const now = useNow(Boolean(t.due_at));
  if (!t.due) return null;

  const left = dueCountdown(t.due_at, now);
  if (!left) {
    return (
      <span className="task-due" title={t.due_time ? `due ${fmtDay(t.due)} at ${fmtTime(t.due_at)}` : `due ${fmtDay(t.due)}`}>
        {t.due}{t.due_time ? ` ${fmtTime(t.due_at)}` : ""}
      </span>
    );
  }
  return (
    <span className={`task-due countdown${left.late ? " late" : ""}`}
      title={`due ${fmtDay(t.due)} at ${fmtTime(t.due_at)}`}>
      {left.label}
    </span>
  );
}
