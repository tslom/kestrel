// Clock times in the zone the backend chose: read the wall clock straight out of the ISO string, never
// re-render through the browser's zone (the backend re-expresses events in the zone you'll be in on that date).
// Design: DESIGN.md → `frontend/src/lib/time.js`.

// Minutes as "<n> min"; sub-minute prints "<1 min" (rounding to "0 min" reads as none).
export function fmtDur(min) {
  if (min == null) return "—";
  if (min > 0 && min < 1) return "<1 min";
  if (min < 60) return `${Math.round(min)} min`;
  return fmtHM(min);
}

// Compact "1h 5m". Rounds to the minute first (rounding only the remainder gave "1h 60m").
export function fmtHM(min) {
  const t = Math.round(min);
  const h = Math.floor(t / 60), m = t % 60;
  if (!h) return `${m}m`;
  return m ? `${h}h ${m}m` : `${h}h`;
}

// `isToday` comes from the backend (the 4am rollover is its call); any other day prints as its date, never "yesterday".
export function dayKeyLabel(day, isToday) {
  return isToday ? "today" : fmtDay(day);
}

// A day-key as a local Date at noon: midnight doesn't exist on some zones' DST days, noon always does.
export function dayDate(key) {
  return new Date(`${key}T12:00:00`);
}

// {weekday, day} alone renders "28 Tue", so the month goes with it.
export function fmtDay(key) {
  return dayDate(key).toLocaleDateString([],
    { weekday: "short", month: "short", day: "numeric" });
}

// Without the weekday, for chart axes.
export function shortDay(key) {
  return dayDate(key).toLocaleDateString([],
    { month: "short", day: "numeric" });
}

// Providers cache: a panel must say when the cache last moved rather than claim "now".
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
  // No time part: an all-day date or a non-timestamp; let Date have it rather than inventing a midnight.
  if (!m) return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const h = Number(m[1]);
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? "AM" : "PM"}`;
}

// Weekly clock time (minutes past midnight, e.g. office hours) in fmtTime's shape.
export function fmtClock(min) {
  if (min == null) return "";
  const h = Math.floor(min / 60);
  return `${h % 12 || 12}:${String(min % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

// Start/end as the pieces a row prints: `start` (the scanned column), `end` (a quieter trailing column, so
// two times don't double the reading), `inline` (both, for single-slot places). The meridiem is written once
// when shared; `+1` marks a next-day end.
function pair(startLabel, endLabel, overnight) {
  if (!endLabel || endLabel === startLabel) {
    return { start: startLabel, end: null, inline: startLabel };
  }
  // Both labels come from fmtTime/fmtClock: meridiem is the last two chars.
  const shared = startLabel.slice(-2) === endLabel.slice(-2);
  const tail = `${endLabel}${overnight ? "+1" : ""}`;
  return {
    start: startLabel,
    end: shared ? `${endLabel.slice(0, -3)}${overnight ? "+1" : ""}` : tail,
    inline: `${shared ? startLabel.slice(0, -3) : startLabel}–${tail}`,
    endLabel, overnight,
  };
}

// Span of a dated event; compares dates as strings (wall clocks in the backend's zone; re-parsing would move them).
export function timeRange(startIso, endIso) {
  if (!endIso) return pair(fmtTime(startIso), null);
  const overnight = /T/.test(startIso || "") && /T/.test(endIso)
    && endIso.slice(0, 10) > startIso.slice(0, 10);
  return pair(fmtTime(startIso), fmtTime(endIso), overnight);
}

// Same for a weekly slot in minutes past midnight.
export function clockRange(startMin, endMin) {
  if (startMin == null) return pair("", null);
  return pair(fmtClock(startMin), endMin == null ? null : fmtClock(endMin), false);
}

// Shortest phrase for a deadline distance: "23m", "4h 20m", "2d"; minutes drop past 6h; sub-minute is "now".
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

// A task's `due_at` as a countdown; null with no hour or further off than a day (a date reads better than "37h").
// `due_at` is a bare local stamp, read in the browser zone (the dashboard only serves its own machine); the day key still comes from the server.
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
