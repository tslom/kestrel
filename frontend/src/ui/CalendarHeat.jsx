import { useEffect, useMemo, useState } from "react";
import { fetchJSON } from "../lib/api";
import { dayDate, fmtDay } from "../lib/time";
import Band from "./Band.jsx";

// A year of one daily number, a square per day, fixed to the year whatever the sheet's range.
// One hue in quartile steps (a huge day can't wash out the rest). No reading draws nothing; zero draws
// a faint square: an unwatching sensor isn't a zero.

const CELL = 11, GAP = 2, PITCH = CELL + GAP;
const LABEL_W = 0, TOP = 14;
const DAY_MS = 864e5;

const quantile = (sorted, q) => sorted[Math.floor((sorted.length - 1) * q)];
const utc = (key) => Date.parse(`${key}T00:00:00Z`);
const keyOf = (ms) => new Date(ms).toISOString().slice(0, 10);

function layout(rows, start) {
  const seen = rows.filter((r) => r.value != null);
  if (!seen.length) return null;
  const byDay = new Map(seen.map((r) => [r.day, r.value]));
  const last = seen.reduce((a, r) => (r.day > a ? r.day : a), seen[0].day);
  const up = seen.map((r) => r.value).filter((v) => v > 0).sort((a, b) => a - b);
  const cuts = up.length ? [quantile(up, 0.25), quantile(up, 0.5), quantile(up, 0.75)] : [];
  const level = (v) => (v <= 0 ? 0 : 1 + cuts.filter((c) => v > c).length);

  const first = utc(start);
  const monday = (new Date(first).getUTCDay() + 6) % 7;   // weeks start Monday, as ranges.py's do
  const cells = [], months = [];
  for (let ms = first, i = monday; ms <= utc(last); ms += DAY_MS, i++) {
    const day = keyOf(ms);
    const week = Math.floor(i / 7), weekday = i % 7;
    // A month is named over its first full column, so the name sits on it.
    if (day.endsWith("-01")) {
      months.push({ week: week + (weekday ? 1 : 0),
                    name: dayDate(day).toLocaleDateString([], { month: "short" }) });
    }
    if (byDay.has(day)) cells.push({ day, week, weekday, value: byDay.get(day), lvl: level(byDay.get(day)) });
  }
  const best = seen.reduce((a, r) => (r.value > a.value ? r : a), seen[0]);
  // The grid runs to the last reading, so later months have no column to name.
  const weeks = cells.at(-1).week + 1;
  return { cells, months: months.filter((m) => m.week < weeks), weeks, best };
}

export function CalendarHeat({ rows, start, fmt, label }) {
  const [hover, setHover] = useState(null);
  const grid = useMemo(() => layout(rows, start), [rows, start]);
  if (!grid) return <p className="widget-empty">Nothing recorded this year.</p>;
  const shown = hover || grid.best;
  const w = LABEL_W + grid.weeks * PITCH, h = TOP + 7 * PITCH;
  return (
    <div className="heat" onPointerLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${w} ${h}`} role="img"
        aria-label={`${label}, ${grid.cells.length} days. Highest ${fmt(grid.best.value)} on ${fmtDay(grid.best.day)}.`}
        onPointerOver={(e) => {
          const day = e.target.dataset?.day;
          if (day) setHover(grid.cells.find((c) => c.day === day));
        }}>
        {grid.months.map((m) => (
          <text key={`${m.name}${m.week}`} x={LABEL_W + m.week * PITCH} y={9} className="heat-month">{m.name}</text>
        ))}
        {grid.cells.map((c) => (
          <rect key={c.day} data-day={c.day} x={LABEL_W + c.week * PITCH} y={TOP + c.weekday * PITCH}
            width={CELL} height={CELL} rx={2}
            className={`heat-${c.lvl}${hover?.day === c.day ? " on" : ""}`}>
            <title>{`${fmtDay(c.day)}: ${fmt(c.value)}`}</title>
          </rect>
        ))}
      </svg>
      <div className="heat-foot">
        <span>{hover ? "" : "Highest: "}{fmtDay(shown.day)}, {fmt(shown.value)}</span>
        <span className="heat-key" aria-hidden="true">less
          {[0, 1, 2, 3, 4].map((l) => <i key={l} className={`heat-${l}`} />)}more</span>
      </div>
    </div>
  );
}

// The band every sheet uses: reads its own year, independent of the sheet's range.
export default function YearHeat({ source, metric, fmt, label, title = "This year" }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    fetchJSON(`/api/${source}?${new URLSearchParams({ range: "this-year", metric })}`)
      .then((p) => { if (live) setData(p); })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [source, metric]);
  return (
    <Band title={title} pin={data ? [`${label} · this year`, data.data] : undefined}>
      {error ? <p className="widget-error">{error}</p>
        : data ? <CalendarHeat rows={data.data} start={data.start} fmt={fmt} label={label} />
        : <p className="widget-empty">Loading…</p>}
    </Band>
  );
}
