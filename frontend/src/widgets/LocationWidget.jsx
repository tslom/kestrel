import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import { lead, restLine } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { fmtDur, dayKeyLabel, fmtTime } from "../lib/time.js";
import { fmtKm } from "../lib/units.js";

// Location card, on the lede form.
//
// The card's standing problem shapes everything here:
// OwnTracks reports *movement*, so every number is a lower bound over whatever
// slice of the day got recorded, and **silence is not a failure** — a phone
// that sits at home all day produces no fixes at all, which looks exactly like
// OwnTracks having died. The last fix's location breaks the tie.
//
// On the lede form distance/places/at-home/pattern don't get a four-cell
// grid every day. They're a muted line unless one of them
// actually broke pattern, and the out-and-back clock times, the entropy label
// and the 28-day trend live in the sheet.
//
// Two things still print unconditionally, because neither is a measurement:
// the coverage line (what the numbers are computed from — the honesty guard
// that stops a lower bound reading as a fact) and "still home", which is a
// live state rather than a fact about the day the card is centred on.
const CARD_METRICS = [
  { key: "km_traveled", label: "Distance", worse: null, floor: 15, fmt: fmtKm },
  { key: "places", label: "Places", worse: null, floor: 3,
    fmt: (v) => `${Math.round(v)}` },
  { key: "time_at_home_min", label: "At home", worse: null, floor: 240, fmt: fmtDur },
];

const dayLabel = (sum) => dayKeyLabel(sum.day, sum.is_today);

// A stretch that can run into days. fmtDur's "40h 0m" is the wrong shape at
// this scale — the minutes are noise.
const fmtSpan = (min) =>
  min == null ? "" : min < 120 ? `${Math.round(min)}m` : `${Math.round(min / 60)}h`;

const fmtSince = (iso) =>
  `${new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString([], { weekday: "short" })}`
  + ` ${fmtTime(iso)}`;

export function useLocationCard() {
  const { data: sum, error, reload, busy: refreshing } = useWidgetData("/api/location/summary");

  const readings = sum?.day
    ? CARD_METRICS.map((m) => ({
        ...m, day: sum.day,
        value: sum[m.key] ?? null,
        series: sum.series?.[m.key] || [],
      }))
    : [];
  const hit = sum?.day ? lead(readings) : null;
  const rest = restLine(readings);

  // An ongoing stay at home wins the fold line over any of the day's numbers:
  // on a card centred on a past day, "still home · 40h" is the live fact where
  // Monday's mileage is history.
  const level = !sum ? null : hit ? HAS : QUIET;
  const summary = !sum?.day ? "no location fixes"
    : sum.home_since ? `still home · ${fmtSpan(sum.home_for_min)}`
      : rest + (sum.is_today ? "" : ` · ${dayLabel(sum)}`);

  return { sum, error, reload, refreshing, readings, hit, level, summary };
}

export default function LocationWidget({ card, onPin }) {
  const { sum, error, reload, refreshing, readings, hit } = card;

  const pin = sum?.day
    ? [`location · ${dayLabel(sum)}`, { source: "location", summary: sum }] : null;

  return (
    <section className="widget" {...(pin ? dragPin(...pin) : {})}>
      <header>
        <h2>Location {sum?.day && <span className="widget-sub">{dayLabel(sum)}</span>}</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!pin}
            onClick={() => onPin(...pin)}
            title="Attach the day's location shape and 28-day baselines to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!sum && !error && <><div className="skel" /><div className="skel w60" /></>}
      {sum && !sum.day && (
        <p className="widget-empty">
          No location fixes recorded — check that OwnTracks is running and
          posting to the listener.
        </p>
      )}
      {sum?.day && (
        <>
          <Lede hit={hit} readings={readings} label="location" />
          {sum.home_since && (
            <p className="loc-line"
              title={`Home since ${fmtSince(sum.home_since)} — no fixes since,`
                + " and OwnTracks only reports movement, so sitting at home produces none"}>
              Still home · <b>{fmtSpan(sum.home_for_min)}</b>
            </p>
          )}
          <p className="loc-coverage"
            title="Every number above is computed from these fixes only, so all of them are lower bounds">
            {sum.fixes} fixes · {fmtDur(sum.coverage_min)} of the day recorded
            {sum.fix_stale && (
              <span className="loc-stale"
                title="The last fix was away from home, so this silence isn't a stay in — check that OwnTracks is still running">
                {" · "}no fix in {fmtSpan(sum.fix_age_min)}, last seen out
              </span>
            )}
          </p>
        </>
      )}
    </section>
  );
}
