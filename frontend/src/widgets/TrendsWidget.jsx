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

// Trends card, on the lede form, narrowed to **energy**.
//
// Sleep and steps are the health card's own headline metrics, and with both
// cards leading with a conclusion instead of a chart, drawing them here would
// announce the same short night twice in two different voices. DESIGN.md's rule about not
// answering a question twice settles it: health owns the body's raw metrics,
// and this card keeps the one signal nothing else on the desk derives —
// energy, the 0-100 readiness score built from sleep, resting HR and HRV.
//
// Sleep and steps are still here as the muted line under the
// lede, where they read as the inputs to the score rather than as claims of
// their own. The full metric picker, every range and the charts are in the
// sheet.
const METRIC = "energy";

// Energy is a 0-100 score, so a point is a point across the whole range and a
// flat floor is meaningful. Twelve is about a night's worth of sleep debt.
const CARD_METRICS = [
  { key: METRIC, label: "Energy", worse: "down", floor: 12,
    fmt: (v) => `${Math.round(v)}` },
];

export function useTrendsCard() {
  // A calendar month is the only anchored window the range vocabulary offers
  // that's long enough, and on the 3rd it's three days long. So: ask for this
  // month, and if that can't reach MIN_BASELINE, prepend last month. The
  // second call only happens in the first week or so of a month, which is
  // exactly when it's needed and the rest of the time costs nothing.
  //
  // Both spans are resolved server-side from db.day_key(), which is the point
  // — the frontend must never compute a day key off the wall clock, because
  // between midnight and 4am it would be a day out (CLAUDE.md).
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

  // The last point with a value is today's reading; anything after it is a
  // day the score couldn't be computed for, which is a gap and not a zero.
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
