import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import { Bars, SeriesRow, SpanBars, Stat, finite, mean } from "../ui/charts.jsx";
import YearHeat from "../ui/CalendarHeat.jsx";
import { useSheetData } from "../lib/sheet.js";
import { fmtAgo, fmtDur, shortDay } from "../lib/time";
import { fmtKm } from "../lib/units.js";

// Location sheet. OwnTracks reports movement, not presence: every figure is a lower bound, so the
// sheet never draws an unobserved zero (`finite` drops nulls) and words the away window as fixes.
const VIEWS = ["km_traveled", "time_at_home_min", "places", "entropy",
  "first_leave", "last_return"];

// Minutes past 4am, which is where a day-key starts — see `SpanBars`.
const DAY_START_HOUR = 4;

// Shannon bits over dwell-per-cluster, in words. Mirrors `ENTROPY_BANDS` /
// `_entropy_label()` in `app/providers/location.py`, which is where the cuts
// live: ~1.5 is an errand and a bed, ~3 is a day with a shape to it.
const ENTROPY_BANDS = [[1.5, "routine"], [3.0, "mixed"]];
const entropyLabel = (bits) =>
  (bits == null ? "—" : (ENTROPY_BANDS.find(([cut]) => bits < cut) || [, "varied"])[1]);

export default function LocationSheet({ onPin }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("location", VIEWS, "this-week");

  const km = finite(rows("km_traveled"));
  const home = finite(rows("time_at_home_min"));
  const places = finite(rows("places"));
  const entropy = finite(rows("entropy"));

  const totalKm = km.reduce((s, r) => s + r.value, 0);
  const days = km.length;

  const leaveAt = new Map(finite(rows("first_leave")).map((r) => [r.day, r.value]));
  const returnAt = new Map(finite(rows("last_return")).map((r) => [r.day, r.value]));
  // Every day with any fix gets a row, even with no away window (a blank track is "the phone never left").
  const spanDays = [...new Set([...km.map((r) => r.day), ...leaveAt.keys()])].sort();
  const spans = spanDays.map((day) => ({
    key: day, label: shortDay(day),
    from: leaveAt.get(day) ?? null, to: returnAt.get(day) ?? null,
    pin: [`away · ${day}`,
      { day, first_leave: leaveAt.get(day) ?? null, last_return: returnAt.get(day) ?? null }],
  }));

  const pinLabel = `location · ${range}`;
  const pinData = payloads && {
    range,
    km_traveled: Math.round(totalKm * 10) / 10,
    days_with_fixes: days,
    time_at_home_min: Math.round(home.reduce((s, r) => s + r.value, 0)),
    places_per_day: places.length ? Math.round(mean(places) * 10) / 10 : null,
    variety: entropy.length ? entropyLabel(mean(entropy)) : null,
  };

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (
        <>
          <Band title="Distance" note={days ? fmtKm(totalKm) : ""}
            pin={[`distance · ${range}`, rows("km_traveled")]}>
            <div className="hero-row">
              <Stat value={fmtKm(days ? totalKm / days : null)} label="Per day"
                sub={days ? `${days} day${days === 1 ? "" : "s"} with fixes` : ""} />
              <Stat value={home.length ? fmtDur(mean(home)) : "—"} label="At home"
                sub={home.length ? "seen per day" : ""} />
              <Stat value={places.length ? Math.round(mean(places)).toLocaleString() : "—"}
                label="Places" sub={places.length ? "per day" : ""} />
              <Stat value={entropy.length ? entropyLabel(mean(entropy)) : "—"}
                label="Variety" sub={entropy.length ? "how spread out" : ""} />
            </div>
            <Bars items={km.map((r) => ({ key: r.day, label: shortDay(r.day), v: r.value }))}
              series={[{ key: "km", label: "km", cls: "seg-place" }]}
              fmt={(v) => fmtKm(v)}
              pin={(it) => [`distance · ${it.label}`, { day: it.key, km_traveled: it.v }]}
              empty="No fixes in this range." />
            <p className="band-sub">
              {/* silence is not a failure: a still phone sends nothing, so a
                  missing day is "not seen", never "went nowhere" */}
              Days with no fixes aren't drawn — a phone that stays put sends nothing,
              so the gaps are unmeasured, not empty.
            </p>
          </Band>

          <Band title="Away" note="first fix out → last fix home"
            pin={[`away · ${range}`, spans.map((s) => s.pin[1])]}>
            <SpanBars rows={spans} originHour={DAY_START_HOUR}
              empty="No fixes away from home in this range." />
            <p className="band-sub">
              The ends are the first fix somewhere else and the last one back home,
              not the leaving and the arriving — those are usually a few minutes
              earlier and later than anything was recorded.
            </p>
          </Band>

          <YearHeat source="location" metric="km_traveled" label="distance" title="Distance this year" fmt={fmtKm} />

          <Band title="Routine">
            <SeriesRow label="Distance" rows={rows("km_traveled")} fmt={fmtKm}
              pin={["distance", rows("km_traveled")]} />
            <SeriesRow label="At home" rows={rows("time_at_home_min")} fmt={(v) => fmtDur(v)}
              pin={["time at home", rows("time_at_home_min")]} />
            <SeriesRow label="Places" rows={rows("places")} fmt={(v) => Math.round(v)}
              pin={["places", rows("places")]} />
            <SeriesRow label="Variety" rows={rows("entropy")} fmt={entropyLabel}
              pin={["variety", rows("entropy")]} />
          </Band>
        </>
      )}
    </>
  );
}
