import { useCallback, useEffect, useRef, useState } from "react";
import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Sparkline from "../ui/Sparkline.jsx";
import Patterns from "./Patterns.jsx";
import SleepStages from "./SleepStages.jsx";
import { trendUrl } from "./TrendsWidget.jsx";
import { fetchJSON } from "../lib/api";
import { dragPin } from "../lib/drag.js";

// Trends slide-over: not a useSheetData sheet — trends multi-selects metrics
// across two endpoints (/api/trends for the derived energy score + habits,
// /api/health for health series), which doesn't fit the one-source shape.
// Same shell, chips and keyboard behavior.

const DEFAULT_SELECTED = ["health:sleep_hours", "health:steps", "energy"];

export default function TrendsExpand({ onPin }) {
  const [mode, setMode] = useState("series"); // "series" | "patterns"
  const [meta, setMeta] = useState(null); // { trends, health, ranges, custom }
  const [range, setRange] = useState("this-week");
  const [selected, setSelected] = useState(new Set(DEFAULT_SELECTED));
  // series: [{ metric, data }] or [{ metric, error }] — one row either way.
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(false);
  const [allShown, setAllShown] = useState(false);   // the metric picker, unfolded
  // Only the newest request may write — the same guard useSheetData keeps.
  // /api/health reads sync the provider first, so clicking through ranges or
  // metrics can land a slow earlier answer on top of the one asked for last.
  const seq = useRef(0);

  const load = useCallback((rangeToken, metricSet) => {
    const mine = ++seq.current;
    const metrics = [...metricSet];
    if (!metrics.length) { setSeries([]); setLoading(false); return; }
    setLoading(true);
    Promise.all(
      metrics.map((m) =>
        fetchJSON(trendUrl(m, rangeToken))
          .then((p) => ({ metric: m, data: p.data }))
          .catch((e) => ({ metric: m, error: e.message }))
      )
    ).then((rows) => {
      if (mine !== seq.current) return;
      setSeries(rows);
      setLoading(false);
    });
  }, []);

  useEffect(() => {
    fetchJSON("/api/trends/metrics")
      .then((m) => { setMeta(m); load("this-week", new Set(DEFAULT_SELECTED)); })
      .catch((e) => setSeries([{ metric: "metrics", error: e.message }]));
  }, [load]);


  const pickRange = (token) => { setRange(token); load(token, selected); };
  const toggleMetric = (m) => {
    const next = new Set(selected);
    next.has(m) ? next.delete(m) : next.add(m);
    setSelected(next);
    load(range, next);
  };

  const allMetrics = meta
    ? [...meta.trends, ...meta.health.map((m) => `health:${m}`)]
    : [];
  const ok = series && series.filter((s) => !s.error);
  const label = (m) => m.replace(/^health:/, "").replace(/^habit:/, "habit · ").replace(/_/g, " ");
  const inSeries = mode === "series";

  return (
    <>
    {/* Patterns and sleep carry their own pins, so in those modes the head
        is bare. No age slot: these are several reads with no one age. */}
    <SheetHead refreshedAt={false} label={`trends · ${range}`}
      data={inSeries && ok?.length
        ? { source: "trends", range,
            series: Object.fromEntries(ok.map((s) => [s.metric, s.data])) }
        : undefined}
      onPin={inSeries ? onPin : undefined}
      pinTitle="Attach exactly this slice to your next console message"
      onRefresh={inSeries ? () => load(range, selected) : undefined} />

    <div className="chip-row mode-row">
      <button className={`chip${mode === "series" ? " active" : ""}`}
        onClick={() => setMode("series")}>series</button>
      <button className={`chip${mode === "patterns" ? " active" : ""}`}
        onClick={() => setMode("patterns")}>patterns</button>
      <button className={`chip${mode === "sleep" ? " active" : ""}`}
        onClick={() => setMode("sleep")}>sleep</button>
    </div>

    {mode === "patterns" ? <Patterns onPin={onPin} />
    : mode === "sleep" ? <SleepStages onPin={onPin} />
    : <>
      <RangePicker ranges={meta ? (meta.ranges || []) : null}
        range={range} onPick={pickRange} />

      <div className="chip-row metrics">
        {allMetrics.filter((m) => allShown || selected.has(m)).map((m) => (
          <button key={m}
            className={`chip${selected.has(m) ? " active" : ""}`}
            onClick={() => toggleMetric(m)}>
            {label(m)}
          </button>
        ))}
        <button className="chip chip-more" aria-expanded={allShown}
          onClick={() => setAllShown((v) => !v)}>
          {allShown ? "fewer" : `+ ${allMetrics.filter((m) => !selected.has(m)).length} more`}
        </button>
      </div>

      {loading && <p className="widget-empty">Loading…</p>}
      {series && !loading && !series.length && (
        <p className="widget-empty">Pick a metric above.</p>
      )}
      {series && !loading && series.map((s) => (
        <div key={s.metric} className="trend-row wide"
          {...dragPin(`trend · ${label(s.metric)} · ${range}`,
                      s.error ? undefined
                        : { source: "trends", range, metric: s.metric, data: s.data })}>
          <span className="trend-label">{label(s.metric)}</span>
          {s.error
            ? <span className="widget-error">{s.error}</span>
            : <Sparkline data={s.data} width={300} height={48} />}
        </div>
      ))}
    </>}
    </>
  );
}
