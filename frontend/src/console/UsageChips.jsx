import { useEffect, useState } from "react";
import { timer } from "../lib/timers.js";
import { fetchJSON } from "../lib/api.js";
import { useVisiblePoll } from "../lib/poll.js";

// The console bar's usage chips: one per /usage-limit window, cached percentages merged with the
// socket's live rate_limit events (Console owns those).

// display order + short labels for subscription /usage-limit windows
const RATE_LIMIT_LABELS = {
  five_hour: "5h",
  seven_day: "7d",
  seven_day_opus: "7d opus",
  seven_day_sonnet: "7d sonnet",
  overage: "credits",
};
const RATE_LIMIT_ORDER = Object.keys(RATE_LIMIT_LABELS);

// Fired on window by Console.jsx when a turn's result lands.
export const TURN_END_EVENT = "kestrel:turn-end";
const TURN_END_DELAY_MS = 4000;

const money = (amount, currency) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);

// Age of the cached percentages: only interactive Claude Code refreshes that cache, and an unlabelled stale percentage is worse than none.
function formatAge(seconds) {
  if (seconds == null) return "never fetched";
  const mins = Math.round(seconds / 60);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m old`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h${mins % 60 ? ` ${mins % 60}m` : ""} old`;
}

// One chip per window from two sources, newer wins (#52): the CLI sends an event only when status
// changes, so an event winning outright kept a stale percent or a passed resets_at over a fresh
// reading. An unstamped event, or a window the reading lacks, still counts.
function mergeUsage(windows, events, readAt = -Infinity) {
  const byKey = new Map(windows.map((w) => [w.key, { ...w, live: false }]));
  for (const [key, ev] of Object.entries(events)) {
    if (byKey.has(key) && ev.at != null && ev.at < readAt) continue;
    const cached = byKey.get(key) || {
      key, label: RATE_LIMIT_LABELS[key] || key.replace(/_/g, " "), percent: null,
    };
    const pct = ev.utilization != null ? ev.utilization * 100 : null;
    byKey.set(key, {
      ...cached,
      status: ev.status || cached.status,
      resets_at: ev.resets_at ?? cached.resets_at,
      percent: pct ?? cached.percent,
      live: pct != null,   // only a percent from the event escapes the stale mark
    });
  }
  const rank = (k) => (RATE_LIMIT_ORDER.indexOf(k) + 1 || 99);
  return [...byKey.values()]
    .map((w) => (isExpired(w) ? { ...w, percent: null, expired: true,
                                  // The window rolled over: no longer rejecting.
                                  status: "allowed" } : w))
    .sort((a, b) => rank(a.key) - rank(b.key));
}

// Past its reset a window's percentage is void, not merely old (the 5h chip sat at a confident 100%
// for hours after the limit lifted); the week's number survives staleness. Checked at render so it
// flips on the clock, not the next poll.
const isExpired = (w) => w.resets_at != null && w.resets_at * 1000 <= Date.now();

export function formatResetIn(unixTs) {
  if (unixTs == null) return null;
  const ms = unixTs * 1000 - Date.now();
  if (ms <= 0) return "resets soon";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `resets in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hrs < 24) return `resets in ${hrs}h${rem ? ` ${rem}m` : ""}`;
  return `resets in ${Math.round(hrs / 24)}d`;
}

// Countdown for the chip: "37m", "2h13m", "4d6h"; minute precision (re-renders once a minute).
function formatCountdown(unixTs) {
  if (unixTs == null) return null;
  const mins = Math.ceil((unixTs * 1000 - Date.now()) / 60000);
  if (mins <= 0) return null;
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h${String(mins % 60).padStart(2, "0")}m`;
  return `${Math.floor(hrs / 24)}d${hrs % 24}h`;
}

// "5h 87% ↻2h13m": the countdown is in the chip, not only the title. An expired window has none (next reset unknown).
function ChipText({ w }) {
  const left = w.expired ? null : formatCountdown(w.resets_at);
  return (
    <>
      {w.label} {w.percent != null ? `${Math.round(w.percent)}%` : "—"}
      {left && <span className="usage-reset" data-cut="reset"> ↻{left}</span>}
    </>
  );
}

// Windows a rotating chip cycles (5h, 7d, per-model weeks); a warning or shut one holds the chip.
const rotates = (key) => key === "five_hour" || key.startsWith("seven_day");
const ROTATE_MS = 4000;
const SEVERITY = { rejected: 2, allowed_warning: 1 };
export function rotation(windows) {
  const cycle = windows.filter((w) => rotates(w.key));
  const worst = Math.max(0, ...cycle.map((w) => SEVERITY[w.status] || 0));
  return worst ? cycle.filter((w) => (SEVERITY[w.status] || 0) === worst) : cycle;
}

/**
 * @param rateLimits rate_limit_type -> latest RateLimitInfo off the socket
 * @param rotate one chip that switches between the 5-hour and 7-day windows
 *   (the plan view's status bar, which has room for one). It holds still
 *   while the tab is hidden, and its title names both.
 */
export default function UsageChips({ rateLimits, rotate = false }) {
  const [usage, setUsage] = useState(null);   // cached %s from /api/console/usage
  const [turn, setTurn] = useState(0);
  // Also the countdown's clock: re-render flips an expired window to "—" without a poll.
  useVisiblePoll(() => setTurn((n) => n + 1), rotate ? ROTATE_MS : 60_000);

  // Polled every 30s (backend asks the API at most once a minute), skipped while hidden and caught up
  // on return (the refresh bus doesn't reload this). A turn ending asks for a fresh reading a few
  // seconds late so the API has counted it (backend throttle 15s). readAt: when the API was asked, our clock.
  const load = (fresh = false) => fetchJSON(`/api/console/usage${fresh ? "?fresh=1" : ""}`)
    .then((u) => setUsage({ ...u, readAt: u.age_seconds == null ? -Infinity
                                                                 : Date.now() - u.age_seconds * 1000 }))
    .catch(() => {});   // a missing percentage is not worth a console error
  useEffect(() => { load(); }, []);
  useVisiblePoll(load, 30_000, { catchUp: true });
  useEffect(() => {
    // A wall-clock wait (nothing tells us when the API has counted the turn); tabs ending together ask once.
    const later = timer();
    const onTurnEnd = () => later.start(() => load(true), TURN_END_DELAY_MS);
    window.addEventListener(TURN_END_EVENT, onTurnEnd);
    return () => { later.clear(); window.removeEventListener(TURN_END_EVENT, onTurnEnd); };
  }, []);

  const windows = mergeUsage(usage?.windows || [], rateLimits, usage?.readAt);
  if (rotate) {
    const all = windows.filter((x) => rotates(x.key));
    const cycle = rotation(windows);
    if (!cycle.length) return null;
    const w = cycle[turn % cycle.length];
    const stale = w.expired || (usage?.stale && !w.live);
    const pct = (x) => (x.percent != null ? `${Math.round(x.percent)}%` : "—");
    // Sized to the widest window (with and without countdown): a width change every 4s would fold/unfold the status bar (lib/barHost.js).
    const short = (x) => `${x.label} ${pct(x)}`.length;
    const full = (x) => short(x) + (x.expired ? 0 : (formatCountdown(x.resets_at) || "").length + 2);
    const widths = { "--chip-full": `${Math.max(...cycle.map(full))}ch`,
                     "--chip-short": `${Math.max(...cycle.map(short))}ch` };
    return (
      <span className={`usage-chip rotating usage-${w.status || "allowed"}${stale ? " usage-stale" : ""}`}
        data-cut="usage" style={widths}
        title={all.map((x) => `${x.label} ${pct(x)}${x.expired ? " (window reset)"
          : x.resets_at ? ` · ${formatResetIn(x.resets_at)}` : ""}`).join("\n")}>
        <ChipText w={w} />
      </span>
    );
  }

  return windows.map((w) => {
    const stale = w.expired || (usage?.stale && !w.live);
    const title = [
      w.expired ? "window reset — no reading since" :
        w.status === "rejected" ? "limit reached" :
          w.status === "allowed_warning" ? "approaching limit" : "usage",
      w.spend && `${money(w.spend.used, w.spend.currency)} of `
                 + `${money(w.spend.limit, w.spend.currency)} this month`,
      w.expired ? null : formatResetIn(w.resets_at),
      stale ? formatAge(usage?.age_seconds) : null,
    ].filter(Boolean).join(" · ");
    return (
      <span
        key={w.key}
        className={`bar-item usage-chip usage-${w.status || "allowed"}${stale ? " usage-stale" : ""}`}
        title={title}
      >
        <ChipText w={w} />
      </span>
    );
  });
}
