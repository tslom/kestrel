import { useEffect, useRef, useState } from "react";
import { staleReason } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import RefreshButton from "../ui/RefreshButton.jsx";
import StaleBadge from "../ui/StaleBadge.jsx";
import { CALENDAR_URL, useGoogleAddresses } from "../lib/googleLinks";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { CLOCK } from "../lib/asks.js";
import { useVisiblePoll } from "../lib/poll";
import { fmtTime, timeRange } from "../lib/time";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import EventTime, { EventEnd } from "../ui/EventTime.jsx";
import { dragPin } from "../lib/drag.js";
import Folded from "../ui/Folded.jsx";

// A leave-by this close is the calendar needing you; the list turns the row
// red inside 15. Re-read leave times this often while the tab is visible.
const LEAVE_SOON_MIN = 30;
const LEAVE_POLL_MS = 3 * 60 * 1000;

const VIEWS = [
  { range: "today", label: "today" },
  { range: "next-day", label: "tomorrow" },
];

function EventRow({ ev, past, onPin }) {
  // All-day banners and spill-overs get no trailing end time.
  const range = ev.all_day || ev.continued ? null : timeRange(ev.start, ev.end);
  return (
    <li className={past ? "past" : ""} {...dragPin(`event · ${ev.summary}`, ev)}>
      {/* A spill-over's start is yesterday's (raw, the day looked like it began at 10pm): show its end. */}
      {ev.span ? <EventTime text="multi-day" />
        : ev.all_day ? <EventTime text="all-day" />
        : ev.continued ? <EventTime text={`\u2192 ${fmtTime(ev.end)}`} />
        : <EventTime range={range} />}
      <span className="event-summary"
        title={[ev.continued && `started ${fmtTime(ev.start)} the day before`,
                ev.location].filter(Boolean).join(" · ") || undefined}>
        {ev.summary}
      </span>
      <EventEnd range={range} />
      <button className="pin-mini" onClick={() => onPin(`event · ${ev.summary}`, ev)}
        title="Pin this event to your next console message"><Icon name="pin" /></button>
    </li>
  );
}

export function useCalendarCard() {
  const [view, setView] = useState("today");

  const { data: payload, setData: setPayload, error, reload, busy: refreshing } =
    useWidgetData(`/api/calendar?range=${view}`, "calendar");
  // useLoad doesn't fire on a view switch; clear first so the skeleton shows, not the other day.
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) { firstView.current = false; return; }
    setPayload(null);
    reload();
  }, [view, reload, setPayload]);

  // Leave-by times: a clock, so polled; the backend caches Routes lookups 5 minutes (the glance strip asks too).
  const { data: leave, reload: reloadLeave } = useWidgetData("/api/leave", "calendar");
  useVisiblePoll(reloadLeave, LEAVE_POLL_MS);

  const label = VIEWS.find((v) => v.range === view).label;
  const events = payload?.data || [];
  // One date, one zone: label in the header, always shown (a time you must remember to doubt is worse than none).
  const zone = events.find((ev) => ev.tz)?.tz;

  // Tomorrow is all ahead.
  const ahead = view !== "today" ||
    events.some((ev) => ev.all_day || new Date(ev.end) > new Date());
  // A leave-by inside LEAVE_SOON_MIN (or past, event not started: late) is the one clock-tier item.
  const now = Date.now();
  const leaving = (leave?.data || [])
    .filter((r) => r.leave_by && new Date(r.start).getTime() > now
      && new Date(r.leave_by).getTime() - now <= LEAVE_SOON_MIN * 60000)
    .sort((a, b) => new Date(a.leave_by) - new Date(b.leave_by))[0] || null;
  // All-day events are never "earlier": they cover the part still to come.
  const left = events.filter((ev) => ev.all_day || new Date(ev.end) > new Date());
  const upcoming = left;
  const past = events.filter((ev) => !left.includes(ev));
  const next = left.find((ev) => !ev.all_day) || left[0];
  // Folded summary: the next thing on the day; a meeting in progress says "till <end>", not its start.
  const running = next && !next.all_day && new Date(next.start) <= new Date();
  const level = !payload ? null : leaving ? NEEDS : ahead && events.length ? HAS : QUIET;
  const summary = (leaving ? `leave by ${fmtTime(leaving.leave_by)} · ` : "")
    + (events.length === 0 ? `no events ${label}`
      : !next ? `${events.length} done, nothing left`
      : `${next.all_day ? "all-day" : running ? `till ${fmtTime(next.end)}`
          : fmtTime(next.start)} ${next.summary}`
        + (left.length > 1 ? ` · +${left.length - 1}` : ""));

  // `clock`: "later" offers minutes, since tomorrow is no answer to a leave-by.
  const mins = leaving ? Math.round((new Date(leaving.leave_by).getTime() - now) / 60000) : 0;
  const asks = leaving ? [{
    id: `calendar:leave:${leaving.event_id || leaving.start}`, card: "calendar",
    tier: CLOCK, kind: mins <= 15 ? "late" : "", clock: true,
    title: `Leave by ${fmtTime(leaving.leave_by)} for ${leaving.event}`,
    why: [leaving.location, leaving.drive_min != null && `${leaving.drive_min} min drive`,
          mins > 0 ? `in ${mins} min` : `${-mins} min late`].filter(Boolean).join(" · "),
    acts: [{ label: "Open", open: true }],
  }] : [];

  return { view, setView, payload, error, reload, refreshing,
           label, events, zone, upcoming, past, level, summary, asks };
}

export default function CalendarWidget({ card, onPin }) {
  const { view, setView, payload, error, reload, refreshing,
          label, events, zone, upcoming, past } = card;
  const addrs = useGoogleAddresses(); // re-render when authuser= resolves

  return (
    <section className="widget" {...dragPin(`calendar · ${label}`, payload)}>
      <header>
        <h2>Calendar</h2>
        <StaleBadge reason={staleReason(payload)} />
        {zone && <span className="event-zone" title="Times are shown in the zone you'll be in on this date">{zone}</span>}
        <div className="seg">
          {VIEWS.map((v) => (
            <button key={v.range} className={view === v.range ? "on" : ""}
              onClick={() => setView(v.range)}>{v.label}</button>
          ))}
        </div>
        <div className="widget-actions">
          <a className="cal-link" href={CALENDAR_URL()} target="_blank" rel="noreferrer"
            title={`Open Google Calendar${addrs.calendar ? ` ()` : ""}`}><Icon name="external" /></a>
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!payload}
            onClick={() => onPin(`calendar · ${label}`, payload)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!payload && !error && <><div className="skel" /><div className="skel w60" /></>}
      {payload && events.length === 0 && <p className="widget-empty">No events {label}.</p>}
      {/* Ended events fold away; the fold line is the now boundary. */}
      {past.length > 0 && (
        <Folded above summary={`${past.length} earlier`} hideLabel="hide earlier"
          open={!upcoming.length}>
          <ul className="event-list" {...dragPin(`events · earlier ${label}`, past)}>
            {past.map((ev) => <EventRow key={ev.id} ev={ev} past onPin={onPin} />)}
          </ul>
        </Folded>
      )}
      {upcoming.length > 0 && (
        <ul className="event-list" {...dragPin(`events · ${label}`, upcoming)}>
          {upcoming.map((ev) => <EventRow key={ev.id} ev={ev} onPin={onPin} />)}
        </ul>
      )}
    </section>
  );
}
