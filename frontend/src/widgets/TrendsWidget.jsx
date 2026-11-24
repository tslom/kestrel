import { fetchJSON, staleReason } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import StaleBadge from "../ui/StaleBadge.jsx";
import { HAS, QUIET } from "../lib/salience";
import { lead, restLine, MIN_BASELINE } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// health:* metrics belong to the health provider; everything else is ours.
// Exported for TrendsExpand, which is still the full picker.
export const trendUrl = (m, range) =>
  m.startsWith("health:")
    ? `/api/health?range=${range}&metric=${m.slice(7)}`
    : `/api/trends?range=${range}&metric=${encodeURIComponent(m)}`;

// Trends card, lede form, narrowed to energy (0-100 readiness from sleep, resting HR, HRV): health
// owns the raw metrics (don't answer a question twice); sleep/steps stay as the muted inputs line.
// Design: DESIGN.md → Sheets are written to the subject, not the provider.
const METRIC = "energy";

// Energy is a 0-100 score, so a point is a point across the whole range and a
// flat floor is meaningful. Twelve is about a night's worth of sleep debt.
const CARD_METRICS = [
  { key: METRIC, label: "Energy", worse: "down", floor: 12,
    fmt: (v) => `${Math.round(v)}` },
];

export function useTrendsCard() {
  // This month is the only anchored window long enough, but on the 3rd it's 3 days: if it can't
  // reach MIN_BASELINE, prepend last month. Spans resolve server-side (db.day_key()), never off the wall clock.
  const { data: loaded, error, reload, busy: refreshing } = useWidgetData(async () => {
    const cur = await fetchJSON(trendUrl(METRIC, "this-month"));
    let data = cur.data;
    let stales = [cur];
    if (data.filter((p) => p.value != null).length < MIN_BASELINE + 1) {
      const prev = await fetchJSON(trendUrl(METRIC, "last-month"));
      data = [...prev.data, ...data];
      stales = [prev, cur];
    }
    return { series: data, stale: staleReason(...stales) };
  });
  const series = loaded?.series ?? null;
  const stale = loaded?.stale ?? null;

  // The last valued point is today's reading; later days are gaps, not zeros.
  const pts = (series || []).filter((p) => p.value != null);
  const latest = pts.length ? pts[pts.length - 1] : null;
  const readings = latest
    ? CARD_METRICS.map((m) => ({ ...m, day: latest.day, value: latest.value, series }))
    : [];
  const hit = latest ? lead(readings) : null;
  const rest = restLine(readings);

  const level = !series ? null : hit ? HAS : QUIET;
  const summary = rest || "no series this month";

  return { series, stale, error, reload, refreshing, latest, readings, hit, level, summary };
}

export default function TrendsWidget({ card, onPin }) {
  const { series, stale, error, reload, refreshing, latest, readings, hit } = card;

  const pin = ["trends · energy", { source: "trends", metric: METRIC, series }];

  return (
    <section className="widget" {...(series ? dragPin(...pin) : {})}>
      <header>
        <h2>Trends <span className="widget-sub">energy</span></h2>
        <StaleBadge reason={stale} />
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!series}
            onClick={() => onPin(...pin)}
            title="Attach the energy series to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!series && !error && <><div className="skel" /><div className="skel w60" /></>}
      {series && !latest && (
        <p className="widget-empty">
          No energy score yet this month — it needs sleep and resting-HR data to derive.
        </p>
      )}
      {latest && <Lede hit={hit} readings={readings} label="trends" />}
    </section>
  );
}
