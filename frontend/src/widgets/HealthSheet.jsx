import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import { Bars, SeriesRow, Stat, finite, mean } from "../ui/charts.jsx";
import YearHeat from "../ui/CalendarHeat.jsx";
import { useSheetData } from "../lib/sheet.js";
import { fmtAgo, fmtDay, fmtHM, shortDay } from "../lib/time";

// The Health sheet — a body, not fourteen columns.
//
// Fitbit exposes fourteen metrics and the generic panel showed them one chip
// at a time, which is how `sleep_deep_min` ends up on screen as a word. They
// aren't fourteen questions: they're sleep, movement, and the two numbers that
// say how the body is handling both. So this sheet reads nine of them at once
// and arranges them as those three, with the latest day's figures on top
// because "how am I doing" is asked far more often than "what was Tuesday".
//
// Nothing here computes a score. `energy` already exists on /api/trends and is
// derived server-side from this same cache; inventing a second, differently
// derived wellness number in the browser would give the app two answers to one
// question.

const SLEEP_STAGES = [
  { metric: "sleep_deep_min", key: "deep", label: "Deep", cls: "seg-deep" },
  { metric: "sleep_rem_min", key: "rem", label: "REM", cls: "seg-rem" },
  { metric: "sleep_light_min", key: "light", label: "Light", cls: "seg-light" },
  { metric: "sleep_awake_min", key: "awake", label: "Awake", cls: "seg-awake" },
];

const VIEWS = [
  "steps", "sleep_hours", "resting_hr", "hrv", "active_zone_minutes",
  ...SLEEP_STAGES.map((s) => s.metric),
];

const fmtSteps = (v) => Math.round(v).toLocaleString();
const fmtHours = (h) => fmtHM(h * 60);
const fmtMin = (m) => `${Math.round(m)} min`;

// Whether a number moving up is a number getting better. Left null wherever
// the answer is "it depends" — a resting heart rate that fell 8% might be
// fitness or might be a bad reading, and coloring it green would be the app
// telling you something it doesn't know.
const METRICS = {
  sleep_hours: { label: "Sleep", fmt: fmtHours, better: "up" },
  steps: { label: "Steps", fmt: fmtSteps, better: "up" },
  active_zone_minutes: { label: "Active min", fmt: fmtMin, better: "up" },
  resting_hr: { label: "Resting HR", fmt: (v) => `${Math.round(v)} bpm`, better: null },
  hrv: { label: "HRV", fmt: (v) => `${Math.round(v)} ms`, better: null },
};

// The latest reading, and how it sits against the rest of the range. The
// comparison is against the *other* days, not against all of them — including
// today in its own baseline drags the baseline toward today and shrinks every
// delta, most of all in a short range.
function Hero({ metric, rows }) {
  const spec = METRICS[metric];
  const good = finite(rows);
  if (good.length === 0) return null;
  const last = good[good.length - 1];
  const rest = good.slice(0, -1);
  const base = mean(rest);
  const pct = base ? ((last.value - base) / base) * 100 : null;
  const dir = pct == null || Math.abs(pct) < 5 ? null : pct > 0 ? "up" : "down";
  const tone = dir && spec.better ? (dir === spec.better ? "up" : "down") : "";

  return (
    <Stat value={spec.fmt(last.value)} label={spec.label} tone={tone}
      pin={[`${metric} · ${last.day}`, { metric, ...last, avg: base }]}
      // the size of the move is only stated when it was big enough to call a
      // move at all — "1% vs 73 bpm" is noise wearing a number
      sub={base == null ? fmtDay(last.day)
        : `${dir === "up" ? `↑${Math.abs(pct).toFixed(0)}% ` : dir === "down" ? `↓${Math.abs(pct).toFixed(0)}% ` : ""}vs ${spec.fmt(base)}`} />
  );
}

export default function HealthSheet({ onPin, onOpen }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("health", VIEWS, "this-week");

  // Nights, assembled from the four stage series. A night is keyed by day, and
  // a day that reported no stages at all is dropped rather than drawn as a
  // zero-height night you slept through.
  const nights = new Map();
  for (const st of SLEEP_STAGES) {
    for (const r of rows(st.metric)) {
      if (r.value == null) continue;
      if (!nights.has(r.day)) nights.set(r.day, { key: r.day, label: shortDay(r.day), v: {} });
      nights.get(r.day).v[st.key] = r.value;
    }
  }
  const sleepItems = [...nights.values()].sort((a, b) => a.key.localeCompare(b.key));
  const asleepMin = (it) => (it.v.deep || 0) + (it.v.rem || 0) + (it.v.light || 0);
  const avgAsleep = sleepItems.length
    ? sleepItems.reduce((s, it) => s + asleepMin(it), 0) / sleepItems.length : null;

  const stepRows = finite(rows("steps"));
  const stepItems = stepRows.map((r) => ({ key: r.day, label: shortDay(r.day), v: r.value }));
  const stepTotal = stepRows.reduce((s, r) => s + r.value, 0);

  const pinLabel = `health · ${range}`;
  const pinData = payloads && Object.fromEntries(
    VIEWS.map((m) => [m, rows(m)]).filter(([, v]) => v.length)
  );
  const nothing = payloads && VIEWS.every((m) => finite(rows(m)).length === 0);

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (nothing
        ? <p className="widget-empty">Nothing synced from Fitbit in this range.</p>
        : <>
            <Band title="Latest">
              <div className="hero-row">
                {["sleep_hours", "steps", "resting_hr", "hrv", "active_zone_minutes"]
                  .map((m) => <Hero key={m} metric={m} rows={rows(m)} />)}
              </div>
            </Band>

            <Band title="Sleep"
              note={avgAsleep == null ? "" : `${fmtHours(avgAsleep / 60)} asleep avg`}
              pin={[`sleep · ${range}`, sleepItems.map((n) => ({ day: n.key, ...n.v }))]}>
              {/* A bar is four numbers; the night behind it is a shape. Both
                  charts open the same sheet, because the night and the day it
                  began are keyed the same way. */}
              <Bars items={sleepItems} series={SLEEP_STAGES}
                fmt={(m) => fmtHours(m / 60)} height={170}
                onPick={onOpen && ((it) => onOpen(`day:${it.key}`))}
                pin={(it) => [`sleep · ${fmtDay(it.key)}`, { day: it.key, ...it.v }]}
                empty="No nights recorded in this range." />
            </Band>

            <Band title="Steps"
              note={stepRows.length ? `${fmtSteps(stepTotal)} total` : ""}
              pin={[`steps · ${range}`, stepRows]}>
              <Bars items={stepItems} series={[{ key: "v", label: "steps", cls: "seg-steps" }]}
                fmt={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : Math.round(v))}
                height={130}
                onPick={onOpen && ((it) => onOpen(`day:${it.key}`))}
                pin={(it) => [`steps · ${fmtDay(it.key)}`, { day: it.key, steps: it.v }]}
                empty="No step counts in this range." />
            </Band>

            <YearHeat source="health" metric="steps" label="steps" title="Steps this year"
              fmt={(v) => Math.round(v).toLocaleString()} />

            <Band title="Body" pin={[`vitals · ${range}`,
              { resting_hr: rows("resting_hr"), hrv: rows("hrv"),
                active_zone_minutes: rows("active_zone_minutes") }]}>
              {["resting_hr", "hrv", "active_zone_minutes"].map((m) => (
                <SeriesRow key={m} label={METRICS[m].label} fmt={METRICS[m].fmt}
                  rows={rows(m)} pin={[m, rows(m)]} />
              ))}
            </Band>
          </>)}
    </>
  );
}
