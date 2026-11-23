import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import { lead, restLine } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// Health card, on the lede form.
//
// Four metrics in a grid every day are real numbers on a useless card:
// nothing on it says whether 71 bpm is your ordinary 71 or the start of
// something, so reading it means remembering last week — which is exactly the
// work a dashboard is supposed to have already done.
//
// So one call to /api/health/summary brings today plus the 28 days to read it
// against, `lib/notable.js` decides whether any of it is unusual, and the card
// leads with that or says nothing. Ranges, sleep stages, the nightly wellness
// trio and every other metric stay in the sheet, which is where you go
// when you want to look rather than be told.
//
// `floor` is the honest half of the notability rule and it has to be declared
// here, because this is the file that knows 3 bpm is a lot and 3 steps is not.
// Each one is "the smallest change I'd want a sentence about":
const CARD_METRICS = [
  { key: "steps", label: "Steps", worse: null, floor: 3000,
    fmt: (v) => Math.round(v).toLocaleString() },
  // Sleep is attributed to the wake-up date, so "today" is last night.
  { key: "sleep_hours", label: "Sleep", worse: "down", floor: 1,
    fmt: (v) => `${v.toFixed(1)} h` },
  // The tightest band on the card (68–76 over a month) and the most diagnostic
  // — a few bpm over your own median is the classic early-illness tell, which
  // is why the floor is low enough to catch it and `worse: "up"` colours it.
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

  // HAS is *earned*, not granted for having a pulse. "Any metric non-null"
  // is true every day the watch syncs, so the card would sit in the "today"
  // band 365 days a year and mean nothing by being there. A body
  // doing what it always does is the definition of QUIET, and the fold line
  // still carries the numbers for the morning you want them anyway.
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
          {/* The pin still carries all four metrics and all 28 days. The card
              shows one reading; the console gets the lot, which is the whole
              trade this redesign makes. */}
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
