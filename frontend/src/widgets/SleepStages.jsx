import { useEffect, useState } from "react";
import { fetchJSON } from "../lib/api";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { useChartWidth } from "../ui/charts.jsx";
import { dayDate, fmtHM, shortDay } from "../lib/time";

// Sleep-stage stacked bars: one bar per night stacking
// deep / rem / light / awake minutes. The four series are already synced by
// the health provider (providers/health.py) and served on /api/health — this
// is UI only, no backend. Depth reads as a single-hue blue ramp (deep darkest
// → light lightest); awake is a neutral gray because it isn't sleep. The
// legend, the direct hour labels and the hover breakdown all carry identity,
// so color is never the only cue. Self-contained like Patterns.jsx: its own
// range chips and fetch, rendered as a third mode inside TrendsExpand.

// Bottom → top of each stack. Order is by depth so the ramp reads dark→light.
const STAGES = [
  { metric: "sleep_deep_min", key: "deep", label: "Deep" },
  { metric: "sleep_rem_min", key: "rem", label: "REM" },
  { metric: "sleep_light_min", key: "light", label: "Light" },
  { metric: "sleep_awake_min", key: "awake", label: "Awake" },
];

const RANGES = ["this-week", "last-week", "this-month"];

const fmtDur = fmtHM;

function weekday(key) {
  return dayDate(key).toLocaleDateString([], { weekday: "short" });
}

// Stacked bars off a bottom baseline, one per night. A 1.5px surface-colored
// stroke on each segment opens a clean gap between stages without distorting
// true stacked heights. A dotted line marks the average total sleep; total
// hours are direct-labeled above each bar when the range is short enough to fit.
function StackChart({ nights }) {
  const [box, W] = useChartWidth();
  const H = 192, padX = 12, padTop = 16, padBot = 26;
  const plotH = H - padTop - padBot;
  const maxTotal = Math.max(1, ...nights.map((n) => n.total));
  const avg = nights.reduce((s, n) => s + n.total, 0) / nights.length;
  const baseY = padTop + plotH;
  const scale = plotH / maxTotal;
  const slot = (W - padX * 2) / nights.length;
  const barW = Math.min(30, slot * 0.62);
  const avgY = baseY - avg * scale;
  const labelTotals = nights.length <= 10;
  const useWeekday = nights.length <= 8;
  const tickEvery = Math.ceil(nights.length / 8);

  return (
    <figure className="fin-chart" ref={box}>
      <div className="fin-hero-row">
        <span className="fin-hero-lg">{fmtDur(avg)}</span>
        <span className="fin-hero-sub">avg / night</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="fin-chart-svg" role="img"
        aria-label="Nightly sleep stages (deep, REM, light, awake) with average line">
        <line className="fin-chart-axis" x1={padX} x2={W - padX} y1={baseY} y2={baseY} />
        {nights.map((n, i) => {
          const cx = padX + slot * i + slot / 2;
          let cursor = baseY;
          const segs = STAGES.map((s) => {
            const v = n[s.key];
            if (!v) return null;
            const h = Math.max(1, v * scale);
            const y = cursor - h;
            cursor = y;
            return <rect key={s.key} className={`sleep-seg ${s.key}`}
              x={cx - barW / 2} y={y} width={barW} height={h} rx="1.5" />;
          });
          const title = `${useWeekday ? weekday(n.day) + " · " : ""}${shortDay(n.day)}\n`
            + STAGES.map((s) => `${s.label} ${fmtDur(n[s.key])}`).join("  ·  ")
            + `\ntotal ${fmtDur(n.total)}`;
          return (
            <g key={n.day} className="sleep-bar">
              <title>{title}</title>
              {segs}
              {labelTotals && (
                <text className="fin-bar-label" textAnchor="middle" x={cx} y={cursor - 4}>
                  {(n.total / 60).toFixed(1)}
                </text>
              )}
              {i % tickEvery === 0 && (
                <text className="fin-axis-label" textAnchor="middle" x={cx} y={H - 9}>
                  {useWeekday ? weekday(n.day) : shortDay(n.day)}
                </text>
              )}
            </g>
          );
        })}
        <line className="fin-avg-line" x1={padX} x2={W - padX} y1={avgY} y2={avgY} />
        <text className="fin-avg-label" textAnchor="end" x={W - padX} y={avgY - 4}>avg</text>
      </svg>
    </figure>
  );
}

export default function SleepStages({ onPin }) {
  const [range, setRange] = useState("this-week");
  const [nights, setNights] = useState(null); // [{day, deep, rem, light, awake, total}]
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    Promise.all(STAGES.map((s) =>
      fetchJSON(`/api/health?range=${range}&metric=${s.metric}`)
        .then((p) => [s.key, p.data])))
      .then((pairs) => {
        if (!live) return;
        // Align the four series by wake-up day. A night with no stage data at
        // all is dropped (a gap), never rendered as a zero-height bar.
        const byDay = new Map();
        for (const [key, data] of pairs) {
          for (const pt of data) {
            if (pt.value == null) continue;
            if (!byDay.has(pt.day)) {
              byDay.set(pt.day, { day: pt.day, deep: 0, rem: 0, light: 0, awake: 0 });
            }
            byDay.get(pt.day)[key] = pt.value;
          }
        }
        const rows = [...byDay.values()]
          .map((r) => ({ ...r, total: r.deep + r.rem + r.light + r.awake }))
          .filter((r) => r.total > 0)
          .sort((a, b) => a.day.localeCompare(b.day));
        setNights(rows);
      })
      .catch((e) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [range]);

  return (
    <div className="sleep-stages"
      {...dragPin(`sleep stages · ${range}`,
                  nights?.length ? { source: "sleep_stages", range, nights } : undefined)}>
      <p className="patterns-intro">
        How each night breaks down across sleep stages — deep and REM are the
        restorative ones; time awake in bed stacks on top.
      </p>

      <div className="chip-row">
        {RANGES.map((t) => (
          <button key={t} className={`chip${range === t ? " active" : ""}`}
            onClick={() => setRange(t)}>{t.replace(/-/g, " ")}</button>
        ))}
      </div>

      {loading && <p className="widget-empty">Loading…</p>}
      {error && <p className="widget-error">{error}</p>}
      {nights && !loading && !nights.length && (
        <p className="widget-empty">No sleep-stage data in this range.</p>
      )}

      {nights && !loading && !!nights.length && (
        <>
          <StackChart nights={nights} />
          <div className="sleep-legend">
            {STAGES.map((s) => (
              <span key={s.key} className="sleep-legend-item">
                <span className={`sleep-swatch ${s.key}`} aria-hidden="true" />
                {s.label}
              </span>
            ))}
          </div>
          <div className="patterns-foot">
            <button className="pin" onClick={() => onPin(
              `sleep stages · ${range}`, { source: "sleep_stages", range, nights })}
              title="Attach these nights to your next console message"><Icon name="pin" /> pin</button>
          </div>
        </>
      )}
    </div>
  );
}
