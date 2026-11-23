import { useCallback, useRef, useState } from "react";
import { dragPin } from "../lib/drag.js";
import Sparkline from "./Sparkline.jsx";

// The chart primitives the sheets share. Hand-rolled SVG, no chart library —
// same bargain `Sparkline` already made, and for the same reason: every one of
// these is a bar and a label, and a dependency that renders bars would also
// bring its own idea of color, tooltip and font.
//
// Three shapes cover every sheet: `Bars` (a value per day or per hour, stacked
// when a day is made of parts), `RankBars` (a sorted list where the comparison
// is the point), and `SpanBars` (a stretch of clock time, one row per day).
// Everything drag-pins the row it draws, so a bar is a thing you can send to
// the console rather than a picture of one.

const VB_W = 340;

// The width a chart is drawn at: its box's own, measured. A fixed viewBox
// stretched to `width: 100%` scales the text with it — 9px labels came out at
// 16px on a full-width sheet — so the charts lay out in real pixels instead,
// and a wider box gets more room between bars rather than bigger letters.
// `fallback` covers the first render and a box with no width yet.
export function useChartWidth(fallback = VB_W) {
  const [w, setW] = useState(fallback);
  const ro = useRef(null);
  const ref = useCallback((el) => {
    ro.current?.disconnect();
    ro.current = null;
    if (!el || typeof ResizeObserver === "undefined") return;
    const fit = () => { const cw = Math.round(el.clientWidth); if (cw > 0) setW(cw); };
    fit();
    ro.current = new ResizeObserver(fit);
    ro.current.observe(el);
  }, []);
  return [ref, w];
}

// One headline number with its name under it, and an optional line of context
// below that. `tone` is "up"/"down" only where the direction *means* something
// — the color is a judgement, so a sheet that can't judge leaves it off.
export function Stat({ value, label, sub, tone, pin }) {
  return (
    <div className="hero" {...(pin ? dragPin(...pin) : {})}>
      <span className="hero-value">{value}</span>
      <span className="hero-label">{label}</span>
      {sub != null && sub !== "" && <span className={`hero-sub ${tone || ""}`}>{sub}</span>}
    </div>
  );
}

// Vertical bars, stacked in `series` order (bottom → top). `items` are
// `{ key, label, v }` where `v` maps a series key to its value; a plain number
// is allowed for the single-series case. Direct labels only appear when the
// bars are wide enough to hold them — past that they'd be a smear, and the
// hover title carries the exact numbers anyway.
export function Bars({
  items, series, fmt = (n) => Math.round(n), height = 150,
  avgLine = true, pin, onPick, empty = "No data in this range.",
}) {
  const [box, W] = useChartWidth();
  if (!items || items.length === 0) return <p className="widget-empty">{empty}</p>;
  const padX = 8, padTop = 16, padBot = 20;
  const plotW = W - padX * 2;
  const plotH = height - padTop - padBot;
  const val = (it, k) => (typeof it.v === "number" ? it.v : (it.v?.[k] ?? 0)) || 0;
  const total = (it) => series.reduce((s, sr) => s + val(it, sr.key), 0);
  const max = Math.max(1, ...items.map(total));
  const step = plotW / items.length;
  const bw = Math.max(2, Math.min(38, step - Math.max(2, step * 0.22)));
  const baseY = padTop + plotH;
  const avg = items.reduce((s, it) => s + total(it), 0) / items.length;
  const wide = step >= 26;
  // Twenty-four hours of bars can't carry twenty-four labels — they'd overlap
  // into a smudge. Thin them to what fits and let the hover title carry the
  // rest; the first and last always survive so the axis still says where it
  // starts and ends.
  const every = Math.max(1, Math.ceil(items.length / Math.floor(W / 38)));
  const labelled = (i) => i % every === 0 || i === items.length - 1;

  return (
    <div className="chart" ref={box}>
      <svg className="chart-svg" viewBox={`0 0 ${W} ${height}`} height={height}
        role="img" aria-label={`bar chart, ${items.length} bars, max ${fmt(max)}`}>
        <line className="chart-axis" x1={padX} y1={baseY} x2={W - padX} y2={baseY} />
        {avgLine && items.length > 2 && (
          <>
            <line className="chart-avg" x1={padX} y1={baseY - (avg / max) * plotH}
              x2={W - padX} y2={baseY - (avg / max) * plotH} />
            <text className="chart-avg-label" x={W - padX} y={baseY - (avg / max) * plotH - 3}
              textAnchor="end">avg {fmt(avg)}</text>
          </>
        )}
        {items.map((it, i) => {
          const x = padX + i * step + (step - bw) / 2;
          let y = baseY;
          const t = total(it);
          return (
            // A bar is already a thing you can drag; `onPick` makes it a thing
            // you can open. Keyboard gets the same door as the mouse — the
            // group takes focus and Enter/Space picks it — because a
            // drill-down reachable only by clicking a 20px rectangle is a
            // drill-down half the desk can't reach.
            <g key={it.key} className={`bar${onPick ? " pickable" : ""}`}
              {...(pin ? dragPin(...pin(it)) : {})}
              {...(onPick ? {
                role: "button", tabIndex: 0,
                "aria-label": `Open ${it.label}`,
                onClick: () => onPick(it),
                onKeyDown: (e) => {
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(it); }
                },
              } : {})}>
              <title>{[it.label, ...series.map((sr) =>
                `${sr.label} ${fmt(val(it, sr.key))}`)].join(" · ")}</title>
              {series.map((sr) => {
                const v = val(it, sr.key);
                const h = (v / max) * plotH;
                y -= h;
                return h <= 0 ? null : (
                  <rect key={sr.key} className={`bar-seg ${sr.cls || ""}`}
                    x={x} y={y} width={bw} height={h} />
                );
              })}
              {/* a bar with nothing in it still has to be visibly a day */}
              {t === 0 && <rect className="bar-zero" x={x} y={baseY - 1} width={bw} height={1} />}
              {wide && t > 0 && (
                <text className="bar-value" x={x + bw / 2} y={y - 4} textAnchor="middle">{fmt(t)}</text>
              )}
              {labelled(i) && (
                <text className="bar-label" x={x + bw / 2} y={height - 6} textAnchor="middle">{it.label}</text>
              )}
            </g>
          );
        })}
      </svg>
      {series.length > 1 && (
        <ul className="chart-legend">
          {series.map((sr) => (
            // Drawn with the bar's own classes, so the key can't disagree
            // with the bars: the seg-* rules set `fill`, and a CSS-background
            // swatch fell back to --data for any series without its own
            // .swatch rule (Email's read/unread both showed blue).
            <li key={sr.key}>
              <svg className="swatch" viewBox="0 0 8 8" aria-hidden="true">
                <rect className={`bar-seg ${sr.cls || ""}`} width="8" height="8" rx="1" />
              </svg>
              {sr.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// The rows a chart can actually draw. Providers report a missing day as an
// absent or null value rather than a zero (gap, not zero), and a null that
// reaches the arithmetic turns "we didn't measure" into "it was nothing".
export const finite = (rows) =>
  (rows || []).filter((r) => r.value != null && Number.isFinite(r.value));
export const mean = (rows) =>
  (rows.length ? rows.reduce((s, r) => s + r.value, 0) / rows.length : null);

// A series that shouldn't start at zero. Resting HR lives in a 20-beat band
// and app switches in the dozens, so bars from a zero baseline would be five
// identical columns; a sparkline scales to what actually varied, and the two
// numbers beside it say where the line ends up and what it usually is.
export function SeriesRow({ label, rows, fmt = (n) => n, pin }) {
  const good = finite(rows);
  const avg = mean(good);
  return (
    <div className="series-row" {...(pin ? dragPin(...pin) : {})}>
      <span className="series-name">{label}</span>
      <Sparkline data={rows} last={false} />
      <span className="series-now">{good.length ? fmt(good[good.length - 1].value) : "—"}</span>
      <span className="series-avg">{avg == null ? "" : `avg ${fmt(avg)}`}</span>
    </div>
  );
}

// A sorted list where the length of the bar is the comparison and the number
// beside it is the fact — which is why the track never carries a label of its
// own. `rows` are `{ label, value, data }`, already ordered by the caller.
export function RankBars({ rows, fmt = (n) => n, pinLabel, empty = "Nothing here." }) {
  if (!rows || rows.length === 0) return <p className="widget-empty">{empty}</p>;
  const max = Math.max(...rows.map((r) => r.value)) || 1;
  return (
    <ul className="rank-bars">
      {rows.map((r) => (
        // `key` where the visible label isn't unique — two different artists
        // can put out a track with the same name.
        <li key={r.key ?? r.label}
          {...(pinLabel ? dragPin(pinLabel(r), r.data ?? { label: r.label, value: r.value }) : {})}>
          <span className="rank-label" title={r.title || r.label}>{r.label}</span>
          <span className="rank-track">
            <span className="rank-fill" style={{ width: `${Math.max(1, (r.value / max) * 100)}%` }} />
          </span>
          <span className="rank-value">{fmt(r.value)}</span>
        </li>
      ))}
    </ul>
  );
}

// The band note for a truncated ranking. A count on its own over a list of
// ten reads as "these are the ten"; saying which ten they are out of keeps the
// number and the rows from disagreeing.
export const rankNote = (shown, total) =>
  (total > shown ? `top ${shown} of ${total}` : `${total || ""}`);

// One row per day, drawn against a fixed 24-hour axis: where the stretch sits
// carries as much as how long it is, which is exactly what a duration in a
// table throws away. `from`/`to` are minutes since the day's start.
//
// `originHour` is what minute zero means. The providers that produce these
// spans measure from the 4am rollover, not from midnight — a day-key runs 4am
// to 4am — so drawing them on a midnight axis would put every morning four
// hours early and hide the small hours off the left edge entirely.
export function SpanBars({ rows, originHour = 0, empty = "No days with coverage." }) {
  if (!rows || rows.length === 0) return <p className="widget-empty">{empty}</p>;
  const clock = (m) => {
    const t = m + originHour * 60;
    // truncate, don't round: the backend formats the same minutes with
    // strftime, and a card reading 2:19 beside a sheet reading 2:20 is one
    // number disagreeing with itself.
    const h = Math.floor(t / 60) % 24, mm = Math.floor(t % 60);
    return `${h % 12 || 12}:${String(mm).padStart(2, "0")}${h < 12 ? "am" : "pm"}`;
  };
  return (
    <div className="span-bars">
      <div className="span-axis">
        {[0, 6, 12, 18, 24].map((h) => {
          const t = (h + originHour) % 24;
          return (
            <span key={h} style={{ left: `${(h / 24) * 100}%` }}>
              {h === 24 ? "" : `${t % 12 || 12}${t < 12 ? "a" : "p"}`}
            </span>
          );
        })}
      </div>
      <ul>
        {rows.map((r) => (
          <li key={r.key} {...(r.pin ? dragPin(...r.pin) : {})}>
            <span className="span-day">{r.label}</span>
            <span className="span-track">
              {r.from != null && r.to != null && r.to > r.from ? (
                <span className="span-fill"
                  title={`${clock(r.from)} – ${clock(r.to)}`}
                  style={{
                    left: `${(Math.max(0, r.from) / 1440) * 100}%`,
                    width: `${(Math.min(1440, r.to - r.from) / 1440) * 100}%`,
                  }} />
              ) : null}
            </span>
            <span className="span-clock">
              {r.from != null && r.to != null && r.to > r.from
                ? `${clock(r.from)} – ${clock(r.to)}` : "—"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
