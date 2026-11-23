import { useState } from "react";
import { postJSON } from "../lib/api";
import { broadcastRefresh, useWidgetData } from "../lib/refresh";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import TaskDue from "../ui/TaskDue.jsx";
import { dragPin } from "../lib/drag.js";
import { parseQuickDue } from "../lib/taskDue.js";
import { CLOCK, OWED, age, dayGap } from "../lib/asks.js";
import { offerUndo } from "../lib/undo.js";
import { fmtTime, shortDay } from "../lib/time.js";

// Today-view triage (TASKS_PLAN.md): overdue, then due today, then a capped
// pick of what's available — everything else (deferred, someday, fresh
// waiting) stays invisible here until "N more" opens the full list.

function Row({ t, overdue, onComplete, onPinItem }) {
  return (
    <li {...dragPin(`task · ${t.title}`, t)}>
      <button className="check" onClick={() => onComplete(t)} title="Mark done"><Icon name="check" /></button>
      {overdue && <span className="dot overdue" title="Overdue" />}
      <span className="task-title" title={t.title}>{t.title}</span>
      <TaskDue t={t} />
      <button className="pin-mini" onClick={() => onPinItem(t)}
        title="Pin this task to your next console message"><Icon name="pin" /></button>
    </li>
  );
}

// A write goes out on the "tasks" topic rather than reloading only this
// card: the glance strip's open count answers to it too, and would
// otherwise go on counting a task you just ticked off.
const changed = () => broadcastRefresh("tasks");

export function useTasksCard() {
  const { data, error, setError, reload, busy: refreshing } =
    useWidgetData("/api/tasks/today", "tasks");

  // Done at once, undoable for the toast's 8s: undo reopens it (and the
  // subtasks the close swept up, db.set_task_status).
  function complete(t) {
    return postJSON(`/api/tasks/${t.id}/status`, { status: "done" })
      .then(() => {
        changed();
        offerUndo({
          label: `Checked off “${t.title}”`, askId: `task:${t.id}`,
          undo: () => postJSON(`/api/tasks/${t.id}/status`, { status: "open" }).then(changed),
        });
      })
      .catch((e) => setError(e.message));
  }

  const shown = data ? data.overdue.length + data.due_today.length + data.available.length : 0;
  const more = data ? Math.max(0, data.counts.open - shown) : 0;
  const stale = data ? data.waiting_stale : [];
  const dated = data ? data.overdue.length + data.due_today.length : 0;

  // Dated work is the demand; the available pick is worth a look but isn't
  // owed today. Collapsing takes the quick-add box with it — the console adds
  // tasks too, and one click brings it back.
  const level = !data ? null : dated > 0 ? NEEDS : shown > 0 || stale.length > 0 ? HAS : QUIET;
  const summary = !data ? "the board is clear"
    : dated > 0
      ? [data.overdue.length && `${data.overdue.length} overdue`,
         data.due_today.length && `${data.due_today.length} due today`,
         `${data.counts.open} open`].filter(Boolean).join(" · ")
    : shown > 0 ? `${data.counts.open} open, none dated`
    : stale.length > 0 ? `${stale.length} waiting on someone`
    : "the board is clear";

  // One row per dated task. Overdue is a clock; due today is a clock only
  // when it has a time on it — a bare due date has all day.
  const checkOff = (t) => ({ label: "Check off", style: "go", run: () => complete(t) });
  const asks = !data ? [] : [
    ...data.overdue.map((t) => {
      // A deadline that passed today is hours late, not "0 d": count from
      // the due instant itself (elapsed time, so the browser clock is fine).
      const late = dayGap(t.due, data.today);
      const why = late > 0 ? `due ${shortDay(t.due)} · ${late} d overdue`
        : t.due_time ? `due ${fmtTime(`T${t.due_time}`)} · ${age(`${t.due}T${t.due_time}`)} overdue`
        : "due today";
      return { id: `task:${t.id}`, card: "tasks", tier: CLOCK, kind: "late", title: t.title,
               why, acts: [checkOff(t)] };
    }),
    ...data.due_today.map((t) => ({
      id: `task:${t.id}`, card: "tasks", tier: t.due_time ? CLOCK : OWED, kind: "",
      title: t.title, why: t.due_time ? `due today at ${fmtTime(`T${t.due_time}`)}` : "due today",
      acts: [checkOff(t)] })),
  ];

  return { data, error, setError, reload, refreshing, complete,
           shown, more, stale, level, summary, asks };
}

export default function TasksWidget({ card, onPin, onExpand }) {
  const { data, error, setError, reload, refreshing, complete, shown, more, stale } = card;
  const [title, setTitle] = useState("");

  // "draft the memo @ 5pm" files a due time, which is what a countdown counts.
  // The day key comes off the payload rather than the browser clock — the 4am
  // rollover is the backend's call (see tasks_today).
  const quick = parseQuickDue(title, data && data.today);

  function add() {
    if (!quick.title) return;
    postJSON("/api/tasks", { title: quick.title, due: quick.due, due_time: quick.due_time })
      .then(() => { setTitle(""); changed(); }).catch((e) => setError(e.message));
  }

  const pinItem = (t) => onPin(`task · ${t.title}`, t);

  return (
    <section className="widget" {...dragPin("Today's tasks", data)}>
      <header>
        <h2>Tasks</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!data}
            onClick={() => onPin("Today's tasks", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && shown === 0 && stale.length === 0 && (
        <p className="widget-empty">Nothing due — the board is clear.</p>
      )}
      {data && shown > 0 && (
        <ul className="task-list checkable"
          {...dragPin("tasks · the shown rows",
                      [...data.overdue, ...data.due_today, ...data.available])}>
          {data.overdue.map((t) => <Row key={t.id} t={t} overdue onComplete={complete} onPinItem={pinItem} />)}
          {data.due_today.map((t) => <Row key={t.id} t={t} onComplete={complete} onPinItem={pinItem} />)}
          {data.available.map((t) => <Row key={t.id} t={t} onComplete={complete} onPinItem={pinItem} />)}
        </ul>
      )}
      {stale.length > 0 && (
        <p className="task-stale">
          <Icon name="clock" size={12} /> {stale.length === 1
            ? `waiting on ${stale[0].waiting_on || stale[0].title} — stale`
            : `${stale.length} waiting items gone stale`}
        </p>
      )}
      {/* Not a cut for space: these are open but not on today's board
          (tasks_today), and the full view is where every list lives. */}
      {more > 0 && (
        <button className="widget-footer-btn" onClick={onExpand}>
          {more} more in all tasks <Icon name="arrowRight" size={11} />
        </button>
      )}
      <div className="widget-add">
        <input value={title} placeholder="Add a task… (@ 5pm for a deadline)"
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()} />
      </div>
      {/* Echo what the `@` was read as before it's committed — a due date the
          parser guessed wrong is worth catching while the text is still in the
          box, and an `@` it couldn't read at all needs saying out loud or the
          task files with the phrase stuck in its title. */}
      {quick.hint && <p className="task-add-hint">due {quick.hint}</p>}
      {quick.unparsed && <p className="task-add-hint dim">no date read after “@” — filing it as part of the title</p>}
    </section>
  );
}
