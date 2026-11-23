// Clock times, rendered in the zone the backend already chose.
//
// Every timestamp the API hands back carries its own UTC offset, and for
// calendar events the backend has deliberately re-expressed that offset in the
// zone the user will be *in* on the event's date (`app/tz.py`) — Pacific for a
// USC block in September, Eastern for a club call in August, whichever the
// event was originally filed in. `new Date(iso).toLocaleTimeString()` discards
// that and re-renders in the browser's zone, which agrees only until the day
// you move; it is why a tomorrow-view could disagree with the same event's
// brief line. Reading the wall clock straight out of the string keeps the
// backend's choice, and `ev.tz` is the label that says which choice it was.
// A duration in minutes, as a human reads it. Sub-minute values print as
// "<1 min" rather than rounding to "0 min", which reads as "none" when it
// isn't — a six-second sliver of Instagram is still a visit.
export function fmtDur(min) {
  if (min == null) return "—";
  if (min > 0 && min < 1) return "<1 min";
  if (min < 60) return `${Math.round(min)} min`;
  return fmtHM(min);
}

// A duration in minutes, compact: "45m", "1h 5m", "2h". Rounds to the minute
// first — rounding only the remainder turned 119.7 into "1h 60m".
export function fmtHM(min) {
  const t = Math.round(min);
  const h = Math.floor(t / 60), m = t % 60;
  if (!h) return `${m}m`;
  return m ? `${h}h ${m}m` : `${h}h`;
}

// The label for a backend day-key. `isToday` comes from the backend because
// the 4am rollover is its call, not the browser's; any other day is shown as
// its own date rather than guessed at ("yesterday" would be wrong for four
// hours a night, and wrong by days if the source has been down).
export function dayKeyLabel(day, isToday) {
  return isToday ? "today" : fmtDay(day);
}

// A day-key (YYYY-MM-DD) as a local Date, at noon. Never midnight: where DST
// springs forward at 00:00 (Santiago, Beirut, ...) that local time doesn't
// exist on the transition day. Current V8 and JavaScriptCore both nudge it to
// 01:00 the same day (checked 2026-09-22), but what a nonexistent time parses
// to is the engine's call, and noon is a time every zone has on every day.
export function dayDate(key) {
  return new Date(`${key}T12:00:00`);
}

// A day-key as a date. The weekday needs the month with it — {weekday, day}
// renders "28 Tue".
export function fmtDay(key) {
  return dayDate(key).toLocaleDateString([],
    { weekday: "short", month: "short", day: "numeric" });
}

// The same day without its weekday, for chart axes where a tick has room for
// two words and no more.
export function shortDay(key) {
  return dayDate(key).toLocaleDateString([],
    { month: "short", day: "numeric" });
}

// How long ago a payload was refreshed, phrased as the sentence it appears in.
// Providers cache, so "now" is a claim a panel should never make on the
// cache's behalf without saying when the cache last moved.
export function fmtAgo(iso) {
  if (!iso) return null;
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
  if (mins < 1) return "refreshed just now";
  if (mins < 60) return `refreshed ${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `refreshed ${hours}h ago`;
  return `refreshed ${Math.round(hours / 24)}d ago`;
}

export function fmtTime(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  // No time part means an all-day date, or something that isn't a timestamp;
  // let Date have it rather than inventing a midnight.
  if (!m) return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const h = Number(m[1]);
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? "AM" : "PM"}`;
}

// A weekly clock time, as minutes past midnight. Office hours repeat every
// week, so they have no date to hang an ISO string on — but they should read
// the same as every other time on the desk, so this is fmtTime's shape.
export function fmtClock(min) {
  if (min == null) return "";
  const h = Math.floor(min / 60);
  return `${h % 12 || 12}:${String(min % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

// A start and an end as the pieces a row prints:
//
//   start   the full start time, exactly what the row printed before ends
//           existed — the column a card is scanned down.
//   end     the end alone, for a row that has somewhere to put it away from
//           that column (`.event-ends`, at the row's trailing edge).
//   inline  both as one string, for the places that have only one slot to
//           print into — a "Next ·" line, a sheet's trailing note.
//
// Splitting rather than always returning one string is the whole point. Two
// clock times side by side in the time column doubles what you read to find
// the next row and pushes every summary out of line behind the one event that
// crosses noon; the end is a second, quieter column instead.
//
// The meridiem is written once when both halves share it — "3:20" against a
// 2:00 PM start, "9:30–10:30 AM" inline. It only needs saying twice when the
// span crosses noon or midnight, which is exactly when it carries information.
function pair(startLabel, endLabel, overnight) {
  if (!endLabel || endLabel === startLabel) {
    return { start: startLabel, end: null, inline: startLabel };
  }
  // Both labels come out of fmtTime/fmtClock, so the meridiem is always the
  // last two characters and the space before it is always ours to drop.
  const shared = startLabel.slice(-2) === endLabel.slice(-2);
  // `+1` marks an end that lands on the next day.
  const tail = `${endLabel}${overnight ? "+1" : ""}`;
  return {
    start: startLabel,
    end: shared ? `${endLabel.slice(0, -3)}${overnight ? "+1" : ""}` : tail,
    inline: `${shared ? startLabel.slice(0, -3) : startLabel}–${tail}`,
    endLabel, overnight,
  };
}

// The span of a dated event. `+1` marks an end that lands on the next day (a
// 10pm–1am block is real on the day it starts, and reading its end as 1am
// *this* morning would put it in the past). The date comparison is on the
// string, not on Date, for the same reason fmtTime is: these are wall clocks
// in the zone the backend chose, and re-parsing them here would move them.
export function timeRange(startIso, endIso) {
  if (!endIso) return pair(fmtTime(startIso), null);
  const overnight = /T/.test(startIso || "") && /T/.test(endIso)
    && endIso.slice(0, 10) > startIso.slice(0, 10);
  return pair(fmtTime(startIso), fmtTime(endIso), overnight);
}

// The same, for a weekly slot measured in minutes past midnight.
export function clockRange(startMin, endMin) {
  if (startMin == null) return pair("", null);
  return pair(fmtClock(startMin), endMin == null ? null : fmtClock(endMin), false);
}

// ---------- countdowns ----------

// How far off a deadline is, as the shortest phrase that still says it: "23m",
// "4h", "4h 20m", "2d". Minutes are dropped past six hours, where they stop
// changing the decision and only make the number harder to read at a glance.
// Sub-minute reads "now" rather than "0m".
export function fmtUntil(ms) {
  const min = Math.round(Math.abs(ms) / 60000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  if (h < 6) {
    const m = min - h * 60;
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  if (h < 48) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}

// A task's `due_at`, as a countdown the row can print. Returns null when
// there's nothing to count down to — no hour on the due date, or the deadline
// is further off than a day, where a date reads better than "37h".
//
// `due_at` is a bare local ISO stamp with no offset, so `new Date` reads it in
// the browser's zone. That agrees with the backend because this dashboard only
// ever serves the machine it runs on; a countdown is arithmetic on an instant
// either way, which is why it can use the browser clock here while the *day
// key* still has to come from the server (see tasks_today).
export const COUNTDOWN_WINDOW_MS = 24 * 60 * 60 * 1000;

export function dueCountdown(dueAt, now = Date.now()) {
  if (!dueAt) return null;
  const at = new Date(dueAt).getTime();
  if (Number.isNaN(at)) return null;
  const left = at - now;
  if (left >= COUNTDOWN_WINDOW_MS) return null;
  return left < 0
    ? { late: true, label: `${fmtUntil(left)} late`, ms: left }
    : { late: false, label: fmtUntil(left), ms: left };
}
