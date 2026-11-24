import { useCallback, useEffect, useRef, useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { fetchJSON } from "../lib/api";
import { broadcastRefresh, useLoad } from "../lib/refresh";
import StatusChip from "../ui/StatusChip.jsx";
import Icon from "../ui/icons.jsx";
import Mark from "../ui/Mark.jsx";
import { dayDate, fmtTime } from "../lib/time";
import { dragPin } from "../lib/drag.js";

// Glance strip: date, weather, next event, leave-by, sync, sources dot, navigation. Same endpoints
// as the widgets; re-read on mount, sync and day-key rollover. No task/habit counts: the plan
// view's "needs you" list is that summary (on a narrow window the strip carries its chip).

// Glance chips degrade silently — a failed fetch just drops its chip.
const getJson = (url) => fetchJSON(url).catch(() => null);

// WMO weather_code -> icon name, day/night aware for clear codes; bucketed, not exhaustive.
export function weatherIcon({ weather_code, is_day }) {
  const c = weather_code;
  if (c === 0 || c === 1) return is_day ? "sun" : "moon";
  if (c === 2) return is_day ? "cloudSun" : "cloudMoon";
  if (c === 3) return "cloud";
  if (c === 45 || c === 48) return "fog";
  if ((c >= 51 && c <= 67) || (c >= 80 && c <= 82)) return "rain";
  if ((c >= 71 && c <= 77) || c === 85 || c === 86) return "snow";
  if (c >= 95) return "storm";
  return "thermometer";
}

export default function GlanceStrip({ onExpandCalendar, onOpenStatus, onPin,
                                      needCount, onNeeds, onPalette, onSettings, onWeather }) {
  const [next, setNext] = useState(null);   // next upcoming event today
  const [leave, setLeave] = useState(null); // soonest imminent leave_by
  const [weather, setWeather] = useState(null);
  const [dayKey, setDayKey] = useState(null); // server day_key (4am rollover)
  const [syncing, setSyncing] = useState(false);
  const [progress, setProgress] = useState(null); // live per-provider results
  const esRef = useRef(null);
  const linger = useTimeout();

  // Force every provider to re-pull now. SSE so the strip can name the source it waits on (a bare
  // "syncing…" reads as a hang); on finish widgets re-read, with no page reload (console transcript survives).
  function syncNow() {
    if (syncing) return;
    linger.clear();   // the last sync's list, still lingering, mustn't clear this one's
    setSyncing(true);
    setProgress({ pending: [], results: [] });
    const es = new EventSource("/api/sync/stream");
    esRef.current = es;
    es.onmessage = (m) => {
      const evt = JSON.parse(m.data);
      if (evt.event === "start") {
        setProgress({ pending: evt.providers, results: [] });
      } else if (evt.event === "provider") {
        setProgress((p) => p && {
          pending: p.pending.filter((n) => n !== evt.provider),
          results: [...p.results, evt],
        });
      } else if (evt.event === "done") {
        es.close();
        setSyncing(false);
        broadcastRefresh();
        linger.start(() => setProgress(null), 4000); // let the result list linger
      }
    };
    es.onerror = () => {
      es.close();
      setSyncing(false);
      setProgress((p) => p && { ...p, failed: true });
    };
  }

  useEffect(() => () => esRef.current?.close(), []);

  const load = useCallback(() => Promise.all([
    getJson("/api/healthz").then((h) => setDayKey(h?.day || null)),
    getJson("/api/calendar?range=today").then((p) => {
      const now = Date.now();
      const ev = (p?.data || []).find(
        (e) => !e.all_day && new Date(e.start).getTime() >= now
      );
      setNext(ev || null);
    }),
    getJson("/api/weather/current").then(setWeather),
    // Soonest departure only, and only inside 90 min: a nudge, not a list.
    getJson("/api/leave").then((p) => {
      const now = Date.now();
      const soon = (p?.data || [])
        .filter((r) => r.leave_by && new Date(r.leave_by).getTime() - now < 90 * 60000)
        .sort((a, b) => new Date(a.leave_by) - new Date(b.leave_by))[0];
      setLeave(soon || null);
    }),
  ]), []);

  const [, loading] = useLoad(load, "calendar");   // next event and leave-by

  // The day key, not the wall clock: at 1am everything below is still yesterday's (4am rollover).
  const day = (dayKey ? dayDate(dayKey) : new Date())
    .toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

  // Shared by pin and drag.
  const frame = {
    source: "glance", day: dayKey,
    weather: weather && { temp: weather.temp, high: weather.high,
      low: weather.low, conditions: weather.weather_text },
    next_event: next && { at: next.start, summary: next.summary },
    leave_by: leave && { at: leave.leave_by, event: leave.event,
      drive_min: leave.drive_min },
    needs_you: needCount,
  };

  return (
    <div className="glance" {...dragPin(`today · ${day}`, frame)}>
      {/* Masthead: the only display size in an 11-13px app. */}
      <span className="glance-mast">
        <Mark size={20} title="Kestrel" />
        <span className={`glance-day${loading ? " loading" : ""}`}
          title={loading ? "Refreshing…" : undefined}>{day}</span>
      </span>
      {weather && weather.temp != null && (
        <button className="glance-chip weather" onClick={onWeather}
          title={`${weather.weather_text}: open the forecast`}
          {...dragPin(`weather · ${day}`, frame.weather)}>
          <span className="n">{Math.round(weather.temp)}°</span>
          <span className="ico"><Icon name={weatherIcon(weather)} size={13} /></span>
          {weather.high != null && weather.low != null && (
            <span className="k hilo">
              ↑{Math.round(weather.high)}° ↓{Math.round(weather.low)}°
            </span>
          )}
        </button>
      )}
      {next && (
        <button className="glance-chip next" onClick={onExpandCalendar}
          {...dragPin(`next · ${next.summary}`, frame.next_event)}
          title="Next on the calendar — click to expand">
          <span className="n">{fmtTime(next.start)}</span> <span className="k">{next.summary}</span>
        </button>
      )}
      {leave && (
        <button className="glance-chip leave" onClick={onExpandCalendar}
          {...dragPin(`leave by ${fmtTime(leave.leave_by)}`, frame.leave_by)}
          title={`Leave by ${fmtTime(leave.leave_by)} for ${leave.event}${leave.drive_min != null ? ` — ${leave.drive_min} min drive` : ""}`}>
          <span className="ico"><Icon name="car" size={13} /></span> <span className="n">leave {fmtTime(leave.leave_by)}</span>
          {leave.event && <span className="k">→ {leave.event}</span>}
        </button>
      )}
      {/* One roll-up for every source's auth/freshness. */}
      <StatusChip onOpen={onOpenStatus} />
      <span className="glance-spacer" />
      {/* The only place the day's frame sits together: one pin instead of five cards. */}
      <button className="glance-chip pin" title="Attach today's frame to your next console message"
        aria-label="Attach today to the console"
        onClick={() => onPin(`today · ${day}`, frame)}><Icon name="pin" /></button>
      <div className="sync-wrap">
        <button className={`glance-chip sync${syncing ? " on" : ""}`} onClick={syncNow}
          disabled={syncing} title="Pull fresh data from every source now">
          <span className="ico"><Icon name="refresh" size={12} /></span>{" "}
          <span className="k">
            {syncing
              ? progress?.pending?.length
                ? `${progress.pending[0]}…`
                : "syncing…"
              : "sync"}
          </span>
        </button>
        {/* Source-by-source progress. */}
        {progress && (
          <ul className="sync-log">
            {progress.results.map((r) => (
              <li key={r.provider} className={r.ok ? "ok" : "err"}
                title={r.error || JSON.stringify(r.info)}>
                <span className="mark">{<Icon name={r.ok ? "check" : "close"} size={9} />}</span>
                <span className="name">{r.provider}</span>
                <span className="ms">{r.elapsed_ms}ms</span>
              </li>
            ))}
            {progress.pending?.map((n) => (
              <li key={n} className="pending">
                <span className="mark">·</span><span className="name">{n}</span>
              </li>
            ))}
            {progress.failed && <li className="err">sync stream dropped</li>}
          </ul>
        )}
      </div>
      <button className="glance-chip needs-toggle" onClick={onNeeds}
        title="Show what needs you">
        <span className="n">{needCount}</span> <span className="k">need you</span>
      </button>
      <button className="glance-chip settings" onClick={onSettings} title="Settings"
        aria-label="Settings"><Icon name="sliders" size={13} /></button>
      <button className="glance-chip" onClick={onPalette}
        title="Jump anywhere, open any view, refresh, focus the console">
        <span className="k">⌘K</span>
      </button>
    </div>
  );
}
