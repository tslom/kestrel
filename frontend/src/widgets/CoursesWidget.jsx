import { useState } from "react";
import { postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { OWED, age } from "../lib/asks.js";
import { offerUndo } from "../lib/undo.js";
import { clockRange, fmtClock, fmtDur, shortDay } from "../lib/time";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import EventTime, { EventEnd } from "../ui/EventTime.jsx";
import { dragPin } from "../lib/drag.js";
import Folded from "../ui/Folded.jsx";

// A room is a room, but a Zoom room is a door — make it one.
function Where({ where }) {
  if (!where) return null;
  if (!/^https?:\/\//.test(where)) return <span className="check-meta">{where}</span>;
  return (
    <a className="check-meta" href={where} target="_blank" rel="noreferrer"
      title={where}>Zoom <Icon name="external" /></a>
  );
}

// A week that doesn't match the rule. Today's times are already amended (this says why); the
// week view shows the standing rule, so `dated` carries the date.
function Amended({ slot, dated }) {
  const e = slot.exception;
  if (!e) return null;
  // From parsed minutes, not the note's raw `when`: the card is a 12-hour clock.
  const what = e.cancelled ? "cancelled"
    : e.start != null ? clockRange(e.start, e.end).inline
    : e.when || "changed";
  // "cancelled" is never traded for the reason; an amended row's time is already right, so the
  // reason is what's left; a moved block says it isn't normally held today.
  const body = dated ? `${shortDay(e.date)}: ${what}`
    : e.cancelled ? what
    : slot.moved_from ? `moved from ${slot.moved_from}`
    : e.note || what;
  return (
    <span className={e.cancelled ? "course-cancelled" : "course-amended"}
      title={[dated ? e.date : null, e.note].filter(Boolean).join(" — ") || undefined}>
      {body}
    </span>
  );
}

function Slot({ slot, showDay, showCode = true, showWho = true, onPin }) {
  const label = `${slot.code} office hours`;
  const range = slot.start == null ? null : clockRange(slot.start, slot.end);
  // Struck only on today's rows: `state` is about today, the week view shows the standing rule.
  const struck = slot.state === "past" ? "past"
    : slot.state === "cancelled" && !showDay ? "cancelled" : "";
  return (
    <li className={struck} {...dragPin(label, slot)}>
      {/* "Can I still walk in" needs the close time, not just the start. */}
      {slot.start == null
        ? <EventTime text={slot.when || "TBA"}
            pre={showDay && <span className="course-day">{slot.day} </span>} />
        : <EventTime range={range}
            pre={showDay && <span className="course-day">{slot.day} </span>} />}
      <span className="event-summary" title={`${slot.course} · ${slot.who ?? ""}`}>
        {showCode && <span className="course-code">{slot.code}</span>}
        {slot.who && showWho && <span className="course-who">{showCode ? " " : ""}{slot.who}</span>}
      </span>
      {slot.state === "now" && <span className="course-open">open now</span>}
      <Amended slot={slot} dated={showDay} />
      <Where where={slot.where} />
      <EventEnd range={range} />
      <button className="pin-mini" onClick={() => onPin(label, slot)}
        title="Pin these hours to your next console message"><Icon name="pin" /></button>
    </li>
  );
}

// Card answers "who's in now, who's next" first; the week is one click behind.
// Approve writes the override into the course note; /undo takes it back out (proposal pending again).
// One line per proposal for the card, the ask and the undo toast; an add's day is when the weekly block starts.
function proposalText(p) {
  if (p.action === "cancel") return `cancelled ${shortDay(p.day)}`;
  if (p.action === "add") return `new hours ${p.when_str}, from ${shortDay(p.day)}`;
  return `moved to ${p.when_str} on ${shortDay(p.day)}`;
}

function decideProposal(p, approve, reload) {
  return postJSON(`/api/courses/proposals/${p.id}`, { approve }).then(() => {
    reload();
    offerUndo({
      label: `${approve ? "Confirmed" : "Rejected"}: ${p.course} ${proposalText(p)}`,
      askId: `courses:proposal:${p.id}`,
      undo: () => postJSON(`/api/courses/proposals/${p.id}/undo`, {}).then(reload),
    });
  });
}

export function useCoursesCard() {
  const { data, error, setError, reload } = useWidgetData("/api/courses", "courses");

  const courses = data?.courses ?? [];
  const today = data?.today ?? [];
  const open = data?.open_now ?? [];
  const next = data?.next ?? null;
  const nextRange = next ? clockRange(next.start, next.end) : null;
  // A bare weekday is ambiguous once hours can be cancelled (a struck Tuesday makes "Next · Tue"
  // name itself), so >=7 days out gets its date. Whole days off the day-key, not minutes: next
  // Tuesday is 6d23h away by the clock.
  const nextDate = next?.at?.slice(0, 10) ?? null;
  const daysOut = nextDate && data
    ? Math.round((new Date(`${nextDate}T12:00:00`) - new Date(`${data.day}T12:00:00`)) / 864e5)
    : 0;
  const nextDay = daysOut >= 7 ? shortDay(nextDate) : null;
  const ahead = today.filter((s) => s.state !== "past");
  const earlier = today.filter((s) => s.state === "past");
  // Called-off hours stay on the board ("don't walk over") but must not count as live.
  const live = ahead.filter((s) => s.state !== "cancelled");
  const called_off = ahead.filter((s) => s.state === "cancelled");

  // Reference, not a to-do: only a pending mail amendment (hours mail says are wrong) needs you.
  // One open slot can quote its close time; two can't share one, so they name themselves.
  const proposals = data?.proposals ?? [];
  const level = !data ? null : proposals.length ? NEEDS : live.length ? HAS : QUIET;
  const summary = !data ? ""
    : proposals.length
      ? `${proposals.length} office-hours change${proposals.length > 1 ? "s" : ""} from mail to confirm`
    : open.length === 1 && open[0].end != null
      ? `${open[0].code} office hours till ${fmtClock(open[0].end)}`
    : open.length ? `${open.map((s) => s.code).join(", ")} office hours open now`
    : live.length ? `${live[0].code} office hours ${fmtClock(live[0].start)}`
    : called_off.length
      ? `${called_off.map((s) => s.code).join(", ")} office hours cancelled today`
    : next ? `next hours ${next.code} ${next.day} ${fmtClock(next.start)}`
    : "no office hours on file";

  // Confirm is the card's approve POST; reject stays on the card, where the quote is shown.
  const approve = (p) => decideProposal(p, true, reload).catch((e) => setError(e.message));
  const asks = proposals.map((p) => ({
    id: `courses:proposal:${p.id}`, card: "courses", tier: OWED, kind: "", deadline: p.day,
    title: `${p.course}${p.who ? ` · ${p.who}` : ""} ${proposalText(p)}`,
    why: `from ${p.from_addr}${p.created_at ? ` · ${age(p.created_at)} ago` : ""}`,
    acts: [{ label: "Confirm", style: "go", run: () => approve(p) },
           { label: "Open", open: true }],
  }));

  return { data, error, setError, reload, courses, today, open, next, nextRange,
           nextDay, ahead, earlier, proposals, level, summary, asks };
}

export default function CoursesWidget({ card, onPin }) {
  const { data, error, setError, reload, courses, today, open, next, nextRange,
          nextDay, ahead, earlier, proposals } = card;
  const [week, setWeek] = useState(false);
  // Mid-decision proposal: both buttons disable, since approve writes a vault note (no double write).
  const [busy, setBusy] = useState(null);

  // A 409 means it couldn't be applied (note moved, entry unmatched): stays pending, show the message.
  const decide = (id, approve) => {
    setBusy(id);
    decideProposal(proposals.find((p) => p.id === id), approve, reload)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <section className="widget" {...dragPin("Courses", data)}>
      <header>
        <h2>Office hours {courses.length > 0 && <span className="widget-sub">{courses.length} classes</span>}</h2>
        <div className="widget-actions">
          <button className={`chip${week ? " active" : ""}`} onClick={() => setWeek(!week)}>
            {week ? "today" : "all classes"}
          </button>
          <button className="pin" disabled={!data}
            onClick={() => onPin(week ? "office hours · all classes" : "office hours · today", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {/* Above the hours: they say the hours below are wrong. The quote makes approving a decision, not a guess. */}
      {proposals.map((p) => (
        <div key={p.id} className="oh-proposal">
          <p className="oh-proposal-what">
            <strong>{p.course}</strong>{p.who ? ` · ${p.who}` : ""} —{" "}
            {p.action === "cancel"
              ? <>cancelled <b>{shortDay(p.day)}</b></>
              : p.action === "add"
                ? <>new hours <b>{p.when_str}</b>, from <b>{shortDay(p.day)}</b></>
                : <>moved to <b>{p.when_str}</b> on <b>{shortDay(p.day)}</b></>}
            {p.where_str ? <> · {p.where_str}</> : null}
          </p>
          <blockquote className="oh-proposal-quote" title={p.subject || ""}>
            “{p.quote}”
          </blockquote>
          <p className="oh-proposal-actions">
            <span className="oh-proposal-src">{p.from_addr}</span>
            <button className="chip" disabled={busy === p.id}
              onClick={() => decide(p.id, true)}
              title="Write this override to the course note">approve</button>
            <button className="chip" disabled={busy === p.id}
              onClick={() => decide(p.id, false)}
              title="Leave the hours as they are">dismiss</button>
          </p>
        </div>
      ))}
      {data && !week && today.length === 0 && (
        <p className="widget-empty">No office hours {data.weekday}.</p>
      )}
      {data && week && courses.length === 0 && (
        <p className="widget-empty">No course notes for {data.term} yet.</p>
      )}
      {/* Hours already run out fold away so the live one isn't hunted for. */}
      {!week && earlier.length > 0 && (
        <Folded above summary={`${earlier.length} earlier today`} hideLabel="hide earlier"
          open={!ahead.length}>
          <ul className="event-list" {...dragPin("office hours · earlier today", earlier)}>
            {earlier.map((s, i) => <Slot key={i} slot={s} onPin={onPin} />)}
          </ul>
        </Folded>
      )}
      {!week && ahead.length > 0 && (
        <ul className="event-list" {...dragPin("office hours · still ahead today", ahead)}>
          {ahead.map((s, i) => <Slot key={i} slot={s} onPin={onPin} />)}
        </ul>
      )}
      {/* Week view groups by class; a class with no hours posted must still appear. */}
      {week && courses.map((c) => (
        <div className="course-block" key={c.code}>
          <p className="course-head">
            <span className="course-code">{c.code}</span>
            {c.instructor && <span className="course-who"> {c.instructor}</span>}
            {c.lms && (
              <a className="course-lms" href={c.lms} target="_blank" rel="noreferrer"
                title={c.lms}><Icon name="external" /></a>
            )}
          </p>
          {c.office_hours.length === 0
            ? <p className="course-none">No hours posted yet.</p>
            : (
              <ul className="event-list" {...dragPin(`${c.code} office hours`, c.office_hours)}>
                {c.office_hours.map((s, i) => <Slot key={i} slot={s} showDay showCode={false}
                    // the header already names the instructor; only a TA's hours say whose
                    showWho={s.who !== c.instructor} onPin={onPin} />)}
              </ul>
            )}
        </div>
      ))}
      {/* The next block is the answer whenever today's are gone. */}
      {data && !week && !open.length && next && (
        <p className="course-next">
          Next · <span className="course-code">{next.code}</span>{" "}
          {next.day} {nextDay && `${nextDay} `}{nextRange.inline}
          {next.in_min < 720 && ` · in ${fmtDur(next.in_min)}`}
        </p>
      )}
      {/* Study mode: an exam inside two weeks plus review counts; course_context has the rest. */}
      {data && !week && (data.study ?? []).filter((s) => s.exam).map((s) => (
        <p className="course-next" key={`exam-${s.code}`}>
          <span className="course-code">{s.code}</span> {s.exam.label}
          {" · "}{s.exam.approx ? "week of " : ""}{shortDay(s.exam.date)}
          {" · "}{s.exam.in_days === 0 ? "today" : s.exam.in_days === 1 ? "tomorrow"
            : `in ${s.exam.in_days} days`}
          {s.review_items > 0 && (s.review_due > 0
            ? ` · ${s.review_due} of ${s.review_items} due for review`
            : ` · ${s.review_items} logged, none due`)}
        </p>
      ))}
      {/* Spaced review with no exam close: still worth a line when due. */}
      {data && !week && (data.study ?? []).filter((s) => !s.exam && s.review_due > 0).map((s) => (
        <p className="course-next" key={`due-${s.code}`}>
          <span className="course-code">{s.code}</span> {s.review_due} review item
          {s.review_due === 1 ? "" : "s"} due — ask the console to quiz you on them
        </p>
      ))}
      {data && !week && data.unscheduled?.length > 0 && (
        <p className="course-none">No hours posted: {data.unscheduled.join(", ")}</p>
      )}
    </section>
  );
}
