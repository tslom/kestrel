import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import { Bars } from "../ui/charts.jsx";
import { useSheetData } from "../lib/sheet.js";
import { capRows } from "../lib/pin.js";
import { fmtAgo, fmtDay, fmtHM, shortDay, timeRange } from "../lib/time";
import { dragPin } from "../lib/drag.js";
import EventTime, { EventEnd } from "../ui/EventTime.jsx";

// The Calendar sheet — a schedule, not a list of rows.
//
// The generic panel showed events as a flat day-grouped list, which answers
// "what is on my calendar" and nothing else. The three questions actually
// asked of a calendar are *what's next*, *how full is this*, and *where are
// the holes* — so this sheet leads with the next few events and their
// countdowns, charts booked hours per day, and draws the gaps between events
// as rows of their own. A free afternoon is information; a list can only show
// it as an absence.
//
// One read per range. All wall-clock times come out of the ISO string rather
// than a Date, so the backend's per-date zone choice (`app/tz.py`) survives —
// see `lib/time`. Durations are the exception: those are real elapsed time,
// so they go through Date, which is what the offsets are for.

const VIEWS = [null];

const MINUTE = 60000;

// A banner frames the day instead of filling it: all-day events, and timed
// ones over a day long, which the provider hands out as all-day with `span`
// (providers/calendar.py SPAN_SQL; a mis-dated 1h appointment once read as
// 696h booked for the week).
const isBanner = (ev) => ev.all_day;

// The ISO's own offset ("-08:00"), so the day's midnight is in the zone the
// backend expressed this row in (app/tz.py), not the browser's.
const offsetOf = (iso) => /([+-]\d\d:\d\d|Z)$/.exec(iso)?.[1] || "";
function nextDay(day) {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

// Minutes this row books on its own day. The provider emits one row per day
// an event touches, each carrying the event's full start and end, so a row
// counts only the part between its day's midnights: a 10pm-1am event is 2h
// on the first day and 1h on the second, not 3h on both.
function minutes(ev) {
  if (isBanner(ev) || !ev.start || !ev.end) return 0;
  let s = new Date(ev.start), e = new Date(ev.end);
  if (ev.day) {
    const d0 = new Date(`${ev.day}T00:00:00${offsetOf(ev.start)}`);
    const d1 = new Date(`${nextDay(ev.day)}T00:00:00${offsetOf(ev.end)}`);
    if (!isNaN(d0) && !isNaN(d1)) { s = new Date(Math.max(s, d0)); e = new Date(Math.min(e, d1)); }
  }
  return Math.max(0, (e - s) / MINUTE);
}

const fmtSpan = fmtHM;

// "in 25m" / "in 3h" / "in 2 days" — the form the answer is wanted in. A past
// event says nothing rather than counting up, because a calendar that reports
// how long ago something started is describing a thing you already missed.
function until(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return null;
  const min = Math.round(ms / MINUTE);
  if (min < 60) return `in ${min}m`;
  if (min < 60 * 24) {
    const h = Math.floor(min / 60), m = min % 60;
    return m >= 5 ? `in ${h}h ${m}m` : `in ${h}h`;
  }
  const d = Math.round(min / (60 * 24));
  return `in ${d} day${d === 1 ? "" : "s"}`;
}

function groupByDay(events) {
  const byDay = new Map();
  for (const ev of events) {
    if (!byDay.has(ev.day)) byDay.set(ev.day, []);
    byDay.get(ev.day).push(ev);
  }
  return byDay;
}

// A gap is only worth a row once it's long enough to put something in; below
// half an hour it's the walk between two meetings.
const GAP_MIN = 30;

// One day in the order the day happens: all-day banners first (they frame it
// rather than sit in it), then timed events with the free stretches between
// them called out.
function Day({ day, events }) {
  const banners = events.filter(isBanner);
  const timed = events.filter((e) => !isBanner(e))
    .sort((a, b) => new Date(a.start) - new Date(b.start));
  const booked = timed.reduce((s, e) => s + minutes(e), 0);
  const tz = events.find((e) => e.tz)?.tz;

  const list = [];
  timed.forEach((ev, i) => {
    const prev = timed[i - 1];
    if (prev) {
      const gap = (new Date(ev.start) - new Date(prev.end)) / MINUTE;
      if (gap >= GAP_MIN) {
        list.push(
          <li key={`gap-${ev.id}`} className="cal-gap">
            <span className="cal-gap-label">{fmtSpan(gap)} free</span>
          </li>
        );
      }
    }
    // No "1h 30m" duration at the row's end: it opens with the span itself,
    // so that would be the same fact said twice — and the day's total is in
    // the header, which is where "how full is this" gets answered.
    const when = timeRange(ev.start, ev.end);
    list.push(
      <li key={`${day}-${ev.id}`} {...dragPin(`event · ${ev.summary}`, ev)}>
        <EventTime range={when} />
        <span className="event-summary" title={ev.location || undefined}>
          {ev.summary}
          {ev.location && <span className="cal-where"> · {ev.location}</span>}
        </span>
        <EventEnd range={when} />
      </li>
    );
  });

  return (
    <section className="slice-day" {...dragPin(`calendar · ${fmtDay(day)}`, events)}>
      <h3>
        {fmtDay(day)}
        {tz && <span className="event-zone">{tz}</span>}
        {booked > 0 && <span className="cal-booked">{fmtSpan(booked)} booked</span>}
      </h3>
      {banners.length > 0 && (
        <ul className="cal-banners">
          {banners.map((ev) => (
            <li key={ev.id} {...dragPin(`event · ${ev.summary}`, ev)}
              title={ev.span ? `${ev.start.slice(0, 16).replace("T", " ")} → ${ev.end.slice(0, 16).replace("T", " ")}` : undefined}>
              {ev.summary}
            </li>
          ))}
        </ul>
      )}
      {timed.length > 0 && <ul className="event-list">{list}</ul>}
    </section>
  );
}

export default function CalendarSheet({ onPin }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("calendar", VIEWS, "this-week");

  const events = rows();
  const byDay = groupByDay(events);
  const timed = events.filter((e) => !isBanner(e));
  const bookedTotal = timed.reduce((s, e) => s + minutes(e), 0);

  // "Next up" only makes sense while some of the range is still ahead — a look
  // back at last week has no next, and a countdown to nothing is clutter. An
  // event over midnight has a row per day; it's next up once.
  const upcoming = timed
    .filter((e, i) => new Date(e.start) > Date.now() && timed.findIndex((o) => o.id === e.id) === i)
    .sort((a, b) => new Date(a.start) - new Date(b.start))
    .slice(0, 3);

  const load = [...byDay.keys()].sort().map((day) => ({
    key: day,
    label: shortDay(day),
    v: (byDay.get(day) || []).reduce((s, e) => s + minutes(e), 0) / 60,
  }));

  const pinLabel = `calendar · ${range}`;
  const pinData = payloads && {
    range, events: capRows(events),
    booked_hours: +(bookedTotal / 60).toFixed(1),
    days: load.length,
  };

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (events.length === 0
        ? <p className="widget-empty">Nothing scheduled in this range.</p>
        : <>
            {upcoming.length > 0 && (
              <Band title="Next up" pin={[`next up · ${range}`, upcoming]}>
                <ul className="cal-next">
                  {upcoming.map((ev) => {
                    // Already all one faint trailing note here, so the span
                    // reads as one string rather than two weights.
                    const when = timeRange(ev.start, ev.end);
                    return (
                      <li key={ev.id} {...dragPin(`event · ${ev.summary}`, ev)}>
                        <span className="cal-next-when">{until(ev.start)}</span>
                        <span className="event-summary">{ev.summary}</span>
                        <span className="event-cal">
                          {when.inline}{ev.tz ? ` ${ev.tz}` : ""}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </Band>
            )}

            {load.length > 1 && (
              <Band title="Load" note={`${fmtSpan(bookedTotal)} booked`}
                pin={[`calendar load · ${range}`,
                  load.map((d) => ({ day: d.key, hours: +d.v.toFixed(2) }))]}>
                <Bars items={load} series={[{ key: "v", label: "booked", cls: "seg-cal" }]}
                  fmt={(h) => (h >= 1 ? `${h.toFixed(h < 10 ? 1 : 0)}h`
                    : h > 0 ? `${Math.round(h * 60)}m` : "0")}
                  height={120}
                  pin={(it) => [`calendar · ${fmtDay(it.key)}`, byDay.get(it.key)]} />
              </Band>
            )}

            <Band title="Agenda" note={`${events.length} event${events.length === 1 ? "" : "s"}`}
              pin={[`calendar · ${range}`, capRows(events)]}>
              {[...byDay.entries()].map(([day, evs]) => (
                <Day key={day} day={day} events={evs} />
              ))}
            </Band>
          </>)}
    </>
  );
}
