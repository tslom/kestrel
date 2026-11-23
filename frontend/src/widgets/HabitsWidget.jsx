import { useState } from "react";
import { postJSON } from "../lib/api";
import { broadcastRefresh, useWidgetData } from "../lib/refresh";
import { dayDate } from "../lib/time";
import { HAS, QUIET } from "../lib/salience";
import { offerUndo } from "../lib/undo.js";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// The card carries the same last-7-days strip the expansion does — habit
// names are short, so the row's dead space is better spent on history than
// on nothing. The expansion still owns editing (rename, archive, wider spans).
const WEEKDAY = ["S", "M", "T", "W", "T", "F", "S"];

function cellState(habit, day, value) {
  if (value > 0) return "done";
  if (day < habit.created_day) return "na"; // predates the habit
  return "miss";
}

// A write goes out on the "habits" topic rather than reloading only this
// card, so the glance strip's "habits left" moves with the tick.
const changed = () => broadcastRefresh("habits");

const doneToday = (h) => (h.days[h.days.length - 1]?.value || 0) > 0;

export function useHabitsCard() {
  // One call feeds both the toggles and the strip: history's last span day
  // *is* today, so there's no second /api/habits fetch to keep in sync.
  // Reloads on mount, after a sync, on a rollover, on sheet close.
  const { data, error } = useWidgetData("/api/habits/history?days=7", "habits");

  // Undoable: the inverse is the same check-in with the old value.
  function toggle(h, day, value) {
    const put = (v) => postJSON(`/api/habits/${h.id}/checkin`, { day, value: v }).then(changed);
    return put(value).then(() => offerUndo({
      label: `${value > 0 ? "Checked off" : "Unchecked"} ${h.name}${day === data?.today ? "" : ` on ${day}`}`,
      undo: () => put(value > 0 ? 0 : 1),
    }));
  }

  // Evening nudge (IDEAS: attention signals): unchecked habits only demand
  // attention from 8pm; before 4am it's still "tonight" (day_key rollover).
  const hr = new Date().getHours();
  const evening = hr >= 20 || hr < 4;
  const unchecked = data ? data.habits.filter((x) => !doneToday(x)).length : 0;

  // Same evening escalation the dot uses: boxes left to tick are worth the
  // room all day, but only start demanding it after 8pm. A finished list —
  // or no habits at all — has said everything it has to say.
  // Unchecked habits are "today", not "needs you" (decided 2026-09-25, with
  // the plan view). They were NEEDS at any hour from 2026-09-13 ("it should
  // need attention if I haven't completed the habit for the day"), but "needs
  // you" now means only a turn blocked, a clock, a broken source, or a person
  // or decision waiting — "today, unless it's like a task that needs to be
  // done that day", and a habit is the day's routine rather than a deadline.
  // The card is still on screen all day as a "today" line; `evening` stays
  // for the summary, which says what's left *tonight* once that's the useful
  // framing.
  const level = !data ? null : unchecked === 0 ? QUIET : HAS;
  const summary = !data?.habits.length ? "no habits yet"
    : unchecked === 0 ? "all done today"
    : `${data.habits.length - unchecked} of ${data.habits.length} done`
      + (evening ? ` · ${unchecked} left tonight` : "");

  return { data, error, toggle, evening, unchecked, level, summary };
}

export default function HabitsWidget({ card, onPin }) {
  const { data, error, toggle, evening, unchecked } = card;
  const [name, setName] = useState("");
  const today = data?.today;

  function add() {
    const n = name.trim();
    if (!n) return;
    postJSON("/api/habits", { name: n }).then(() => { setName(""); changed(); });
  }

  return (
    <section className="widget" {...dragPin("Habits", data)}>
      <header>
        <h2>Habits <span className="widget-sub">{today}</span></h2>
        <div className="widget-actions">
          <button className="pin" disabled={!data}
            onClick={() => onPin("Habits", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && data.habits.length === 0 && (
        <p className="widget-empty">No habits yet — add one below (e.g. Exercise).</p>
      )}
      {data && data.habits.length > 0 && (
        <ul className="habit-list" {...dragPin("habits · the list", data.habits)}>
          {data.habits.map((h) => (
            <li key={h.id} {...dragPin(`habit · ${h.name}`, h)}>
              <button className={`habit-toggle ${doneToday(h) ? "done" : ""}`}
                onClick={() => toggle(h, today, doneToday(h) ? 0 : 1)}
                title={doneToday(h) ? "Logged — click to undo" : "Log for today"}>
                {doneToday(h) && <Icon name="check" size={11} />}
              </button>
              <span className="habit-name">{h.name}</span>
              <span className="habit-week" aria-label="last 7 days">
                {h.days.map((d) => {
                  const st = cellState(h, d.day, d.value);
                  const dow = WEEKDAY[dayDate(d.day).getDay()];
                  return (
                    <button key={d.day} disabled={st === "na"}
                      className={`habit-dot ${st}${d.day === today ? " is-today" : ""}`}
                      title={`${dow} ${d.day}${d.day === today ? " (today)" : ""} — ${st === "done" ? "done, click to undo" : "click to mark done"}`}
                      aria-label={`${h.name}, ${dow} ${d.day}: ${st === "done" ? "done" : "not done"}`}
                      onClick={() => toggle(h, d.day, d.value > 0 ? 0 : 1)}>
                      {/* the 11px mark inside a 24px target: a one-click edit
                          of a past day shouldn't take aim (toggle() offers undo) */}
                      <i aria-hidden="true" />
                    </button>
                  );
                })}
              </span>
              <span className={`habit-streak${h.streak >= 7 ? " hot" : ""}`}>
                {h.streak > 0 ? `${h.streak}d` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="widget-add">
        <input value={name} placeholder="Add a habit…"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()} />
      </div>
    </section>
  );
}
