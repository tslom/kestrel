import { useEffect, useState } from "react";
import Band from "../ui/Band.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import { Bars, SeriesRow, Stat, useChartWidth } from "../ui/charts.jsx";
import { fetchJSON } from "../lib/api";
import { fmtAgo, fmtDay, fmtHM } from "../lib/time";

// One day, at the resolution the daily rows threw away (IDEAS, 09-13).
//
// Every other health surface in the app answers "how have I been" — a week of
// bars, a 28-day baseline. This one answers "what happened on Tuesday", which
// the desk could not answer at all: `health_days` kept per-night stage totals
// and `health_hours` kept only heart rate, so a night was a stack of four
// numbers and a day was one.
//
// It opens by clicking a bar on the Health sheet, and it is deliberately one
// sheet rather than two. A night is keyed by the morning it ended on, so the
// night and the day share a key and read as one thing: the sleep you started
// the day with, then the day itself.
//
// Both halves fill from the API on demand the first time a day is opened and
// cache after — see providers/health.py, which is also where the reason a cold
// day's heart rate can be missing lives (the rollUp endpoint caps heart-rate
// spans at a fortnight; steps have no such cap).

// Top → bottom, the conventional hypnogram order: awake rides above the sleep
// it interrupts, and depth increases downward.
const LANES = [
  { key: "awake", label: "Awake" },
  { key: "rem", label: "REM" },
  { key: "light", label: "Light" },
  { key: "deep", label: "Deep" },
];

const fmtDur = fmtHM;

// The clock in the room, read straight off the stored offset. Re-deriving it
// through the browser's zone is the one thing this data is stored with an
// offset to avoid.
const clock = (ts) => (ts ? ts.slice(11, 16) : "—");

const ms = (ts) => new Date(ts).getTime();

// The offset the night was recorded at, in minutes: "…T23:38:00-07:00" -> -420.
const offsetMinutes = (ts) => {
  const m = /([+-])(\d\d):(\d\d)$/.exec(ts);
  if (!m) return 0;
  return (m[1] === "-" ? -1 : 1) * (+m[2] * 60 + +m[3]);
};

// A night drawn against its own clock: one lane per stage, each segment a bar
// in its lane, the whole thing spanning lights-out to wake-up. Hour ticks are
// laid on real elapsed time rather than per-segment, so a long light stretch
// looks long.
function Hypnogram({ segments }) {
  const [box, W] = useChartWidth();
  if (!segments.length) return <p className="widget-empty">No stages recorded for this night.</p>;
  const laneH = 26, padL = 40, padR = 8, padTop = 6, padBot = 18;
  const H = padTop + LANES.length * laneH + padBot;
  const t0 = ms(segments[0].start);
  const t1 = ms(segments[segments.length - 1].end);
  const span = Math.max(1, t1 - t0);
  const plotW = W - padL - padR;
  const x = (t) => padL + ((t - t0) / span) * plotW;
  const laneY = (stage) => padTop + LANES.findIndex((l) => l.key === stage) * laneH;

  // Ticks on real hour boundaries, not on whichever segment happened to start
  // a new hour — segment starts are unevenly spaced and collide into
  // "02:0003:00". The hour is read through the night's own stored offset
  // rather than through the browser's zone, which is the whole reason the
  // offset is stored.
  const offMin = offsetMinutes(segments[0].start);
  const HOUR = 3600000;
  const wallHour = (t) =>
    String(new Date(t + offMin * 60000).getUTCHours()).padStart(2, "0");
  const first = Math.ceil((t0 + offMin * 60000) / HOUR) * HOUR - offMin * 60000;
  const every = Math.max(1, Math.ceil((span / HOUR) / Math.floor(plotW / 42)));
  const ticks = [];
  for (let t = first, i = 0; t <= t1; t += HOUR, i++) {
    if (i % every === 0) ticks.push({ t, label: `${wallHour(t)}:00` });
  }

  return (
    <figure className="hypnogram" ref={box}>
      <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" role="img"
        aria-label={`Sleep stages from ${clock(segments[0].start)} to ${clock(segments[segments.length - 1].end)}`}>
        {LANES.map((l, i) => (
          <g key={l.key}>
            <line className="hyp-lane" x1={padL} x2={W - padR}
              y1={padTop + i * laneH + laneH / 2} y2={padTop + i * laneH + laneH / 2} />
            <text className="hyp-lane-label" x={padL - 6} y={padTop + i * laneH + laneH / 2 + 3}
              textAnchor="end">{l.label}</text>
          </g>
        ))}
        {segments.map((s, i) => (
          <rect key={i} className={`hyp-seg ${s.stage}`}
            x={x(ms(s.start))} y={laneY(s.stage) + 5}
            width={Math.max(1.2, x(ms(s.end)) - x(ms(s.start)))} height={laneH - 10} rx="1.5">
            <title>{`${LANES.find((l) => l.key === s.stage).label} · ${clock(s.start)}–${clock(s.end)} · ${fmtDur(s.minutes)}`}</title>
          </rect>
        ))}
        {ticks.map((t, i) => (
          <text key={i} className="bar-label" x={x(t.t)} y={H - 5} textAnchor="middle">{t.label}</text>
        ))}
      </svg>
    </figure>
  );
}

export default function DaySheet({ day, onPin }) {
  const [night, setNight] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  // Bumped by the head's refresh button; the effect below is the only loader,
  // so a reload is just asking it to run again. Another day is another
  // sheet (views.jsx keys it), so it starts loading.
  const [nonce, setNonce] = useState(0);
  const reload = () => {
    setLoading(true);
    setError(null);
    setNonce((n) => n + 1);
  };

  useEffect(() => {
    let live = true;
    Promise.all([
      fetchJSON(`/api/health/night/${day}`).catch((e) => ({ error: e.message })),
      fetchJSON(`/api/health/day/${day}`).catch((e) => ({ error: e.message })),
    ]).then(([n, d]) => {
      if (!live) return;
      // Both read the same provider, so when both fail they failed for one
      // reason — the same rule useSheetData settled for the range sheets.
      setError(n.error && d.error ? n.error : null);
      setNight(n.error ? null : n);
      setDetail(d.error ? null : d);
      setLoading(false);
    });
    return () => { live = false; };
  }, [day, nonce]);

  const segments = night?.segments || [];
  const hours = detail?.hours || [];
  const stepHours = hours.filter((h) => h.steps != null);
  const hrRows = hours.map((h) => ({ day: `${h.hour}:00`, value: h.hr ?? null }));
  // All 24 slots, because a day has 24 hours and an axis that skips the ones
  // the watch missed would quietly restate the day as shorter. An hour with
  // no reading draws like an hour with no steps, so the count of missing ones
  // is said out loud in the band note rather than left to the eye.
  const stepItems = hours.map((h) => ({
    key: String(h.hour), label: `${h.hour}`, v: h.steps || 0,
  }));
  // Gaps are counted only up to the last hour anything was recorded in. On a
  // day still in progress the hours after now aren't gaps in the data, they're
  // hours that haven't happened — counting them called a normal Monday
  // afternoon "16h unmeasured".
  const lastSeen = hours.reduce(
    (last, h) => (h.steps != null || h.hr != null ? h.hour : last), -1);
  const unmeasured = hours
    .filter((h) => h.hour <= lastSeen && h.steps == null).length;
  const stepsNote = [
    detail?.totals?.steps ? `${Math.round(detail.totals.steps).toLocaleString()} total` : "",
    stepHours.length && unmeasured ? `${unmeasured}h unmeasured` : "",
  ].filter(Boolean).join(" · ");

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(night?.refreshed_at || detail?.refreshed_at)}
        label={`day · ${day}`}
        data={night || detail ? { day, night, hours } : null} onPin={onPin}
        onRefresh={reload} />

      {error && <p className="widget-error">{error}</p>}
      {loading && <p className="widget-empty">Loading…</p>}

      {!loading && !error && (
        <>
          <Band title="Night"
            note={night?.start ? `${clock(night.start)} → ${clock(night.end)}` : ""}
            pin={[`night · ${day}`, night]}>
            {/* Asleep and in bed, side by side. They were never shown apart
                before — `sleep_hours` is time asleep, so a night spent half
                awake in bed rendered as a short night with no sign of the
                difference. */}
            <div className="hero-row">
              <Stat value={fmtDur(night?.asleep_min || 0)} label="Asleep" />
              <Stat value={fmtDur(night?.in_bed_min || 0)} label="In bed"
                sub={night?.awake_min ? `${fmtDur(night.awake_min)} awake` : ""} />
              <Stat value={clock(night?.start)} label="Asleep at" />
              <Stat value={clock(night?.end)} label="Up at" />
            </div>
            <Hypnogram segments={segments} />
          </Band>

          <Band title="Steps by hour" note={stepsNote}
            pin={[`steps · ${day}`, stepHours]}>
            {/* No average line: the question a step curve answers is *when*
                you walked, and a mean hour over a day that includes the eight
                you slept through — or, before evening, the hours that haven't
                happened yet — is a reference line to nothing. */}
            <Bars items={stepItems} avgLine={false}
              series={[{ key: "v", label: "steps", cls: "seg-steps" }]}
              fmt={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : Math.round(v))}
              height={140}
              pin={(it) => [`${day} ${it.key}:00`, { day, hour: +it.key, steps: it.v }]}
              empty="No hourly steps recorded for this day." />
          </Band>

          <Band title="Heart rate by hour" pin={[`hr · ${day}`, hrRows]}>
            {hours.some((h) => h.hr != null)
              ? <SeriesRow label="bpm" rows={hrRows} fmt={(v) => Math.round(v)} />
              : <p className="widget-empty">
                  No hourly heart rate kept for this day — the rollup endpoint
                  only serves the last fortnight.
                </p>}
          </Band>
        </>
      )}
    </>
  );
}
