import { useSyncExternalStore } from "react";
import { dueCountdown, fmtTime, fmtDay } from "../lib/time.js";

// A task row's due column: a live countdown ("23m", "2h late") inside a day, else the date.
// Only tasks with a due hour count down; an all-day task has no instant (see the due_time migration in db.py).

// One interval for every countdown (per-row timers drifted out of step: "23m" beside "22m").
const TICK_MS = 30_000;
const listeners = new Set();
let timer = null;
let current = Date.now();

function subscribe(fn) {
  // The first countdown re-reads the clock (an overnight tab holds a stale time).
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
  // Subscribe whenever there's an hour (not only when live) so a task 25h out switches over by itself.
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
