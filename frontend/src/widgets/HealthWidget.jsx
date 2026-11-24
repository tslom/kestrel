import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import { lead, restLine } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// Health card, lede form: /api/health/summary brings today plus 28 baseline days, `lib/notable.js`
// decides whether anything is unusual, and the card leads with that or says nothing (a grid of
// numbers can't say whether 71 bpm is ordinary). Everything else lives in the sheet.
// `floor` is declared here (this file knows 3 bpm is a lot, 3 steps is not): the smallest change worth a sentence.
const CARD_METRICS = [
  { key: "steps", label: "Steps", worse: null, floor: 3000,
    fmt: (v) => Math.round(v).toLocaleString() },
  // Sleep is attributed to the wake-up date, so "today" is last night.
  { key: "sleep_hours", label: "Sleep", worse: "down", floor: 1,
    fmt: (v) => `${v.toFixed(1)} h` },
  // Tightest band (68–76 over a month); a few bpm over your median is the early-illness tell, hence the low floor.
  { key: "resting_hr", label: "Resting HR", worse: "up", floor: 3,
    fmt: (v) => `${Math.round(v)} bpm` },
  { key: "active_zone_minutes", label: "Zone min", worse: null, floor: 20,
    fmt: (v) => `${Math.round(v)} min` },
];

export function useHealthCard() {
  const { data: sum, error, reload, busy: refreshing } = useWidgetData("/api/health/summary");

  const readings = sum
    ? CARD_METRICS.map((m) => ({
        ...m, day: sum.day,
        value: sum.metrics[m.key]?.value ?? null,
        series: sum.metrics[m.key]?.series || [],
      }))
    : [];
  const hit = sum ? lead(readings) : null;
  const rest = restLine(readings);

  // HAS is earned: "any metric non-null" is true every synced day (a body doing its usual is QUIET).
  const level = !sum ? null : hit ? HAS : QUIET;
  const summary = rest || "nothing synced today";

  return { sum, error, reload, refreshing, readings, hit, rest, level, summary };
}

export default function HealthWidget({ card, onPin }) {
  const { sum, error, reload, refreshing, readings, hit, rest } = card;

  const pin = ["health · today", { source: "health", day: sum?.day, summary: sum }];

  return (
    <section className="widget" {...(sum ? dragPin(...pin) : {})}>
      <header>
        <h2>Health <span className="widget-sub">today</span></h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          {/* The pin carries all four metrics and 28 days; the card shows one reading. */}
          <button className="pin" disabled={!sum}
            onClick={() => onPin(...pin)}
            title="Attach today's readings and their 28-day baselines to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!sum && !error && <><div className="skel" /><div className="skel w60" /></>}
      {sum && !rest && (
        <p className="widget-empty">
          Nothing synced today — the watch may not have been worn or uploaded yet.
        </p>
      )}
      {sum && rest && <Lede hit={hit} readings={readings} label="health" />}
    </section>
  );
}
