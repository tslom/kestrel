import { useEffect, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useTopic } from "../lib/refresh";
import Icon from "../ui/icons.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import { dragPin } from "../lib/drag.js";
import { dayDate } from "../lib/time";

// Habits expansion: a week-grid of done/miss marks per day (click a cell to edit that
// day's history), plus rename + archive. Cells before a habit's birth day
// render neutral, not as a miss. Day keys come from the server (4am rollover).
const WEEKDAY = ["S", "M", "T", "W", "T", "F", "S"];

function cellState(habit, day, value) {
  if (value > 0) return "done";
  // created_day is the server's day_key for the habit's birth — not
  // created_at's calendar date, which runs a day ahead before the 4am
  // rollover and made the habit's own first day render blank.
  if (day < habit.created_day) return "na"; // predates the habit
  return "miss";
}

function HabitRow({ habit, today, onToggle, onRename, onArchive, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(habit.name);

  const commit = () => {
    const n = name.trim();
    setEditing(false);
    if (n && n !== habit.name) onRename(habit.id, n);
    else setName(habit.name);
  };

  return (
    <tr {...dragPin(`habit · ${habit.name}`, habit)}>
      <th scope="row" className="habit-grid-name">
        {editing ? (
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") { setName(habit.name); setEditing(false); }
            }} />
        ) : (
          <button className="habit-grid-rename" title="Rename"
            onClick={() => setEditing(true)}>{habit.name}</button>
        )}
        <span className={`habit-streak${habit.streak >= 7 ? " hot" : ""}`}>
          {habit.streak > 0 ? `${habit.streak}d` : ""}
        </span>
      </th>
      {habit.days.map((d) => {
        const st = cellState(habit, d.day, d.value);
        return (
          <td key={d.day}>
            <button className={`habit-cell ${st}`} disabled={st === "na"}
              title={`${d.day}${d.day === today ? " (today)" : ""} — ${st === "done" ? "done, click to undo" : "click to mark done"}`}
              onClick={() => onToggle(habit.id, d.day, d.value > 0 ? 0 : 1)}>
              {st === "done" ? <Icon name="check" size={10} /> : st === "miss" ? <Icon name="close" size={10} /> : ""}
            </button>
          </td>
        );
      })}
      <td className="habit-grid-actions">
        <button className="pin-mini" title="Archive habit (keeps history)"
          onClick={() => onArchive(habit.id, habit.name)}><Icon name="close" /></button>
        <button className="pin-mini" title="Delete habit and its history"
          onClick={() => onDelete(habit.id, habit.name, habit.streak)}>
          <Icon name="trash" /></button>
      </td>
    </tr>
  );
}

export default function HabitsExpand({ onPin }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [newName, setNewName] = useState("");

  const load = () =>
    fetchJSON("/api/habits/history?days=7")
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message));

  useEffect(() => { load(); }, []);
  useTopic("habits", load);

  const toggle = (id, day, value) =>
    postJSON(`/api/habits/${id}/checkin`, { day, value })
      .then(load).catch((e) => setError(e.message));

  const rename = (id, name) =>
    postJSON(`/api/habits/${id}`, { name }, "PATCH")
      .then(load).catch((e) => setError(e.message));

  const archive = (id, name) => {
    if (!window.confirm(`Archive "${name}"? Its history is kept, but it leaves the tracker.`)) return;
    postJSON(`/api/habits/${id}`, { archived: true }, "PATCH")
      .then(load).catch((e) => setError(e.message));
  };

  // The streak is in the question for the same reason the list count is in
  // ListsWidget's: "delete Exercise" reads harmless when it's a 40-day run.
  // Archive is offered by name here because this dialog is the last place
  // someone learns there was a non-destructive option.
  const remove = (id, name, streak) => {
    const run = streak > 0 ? ` and its ${streak}-day streak` : "";
    if (!window.confirm(
      `Delete "${name}"${run}? This erases its check-in history for good.`
      + ` Archive keeps the history instead.`)) return;
    fetchJSON(`/api/habits/${id}`, { method: "DELETE" })
      .then(load).catch((e) => setError(e.message));
  };

  const add = (e) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setNewName("");
    postJSON("/api/habits", { name })
      .then(load)
      .catch((err) => { setNewName(name); setError(err.message); });
  };

  const span = data?.span || [];
  return (
    <>
    <SheetHead refreshedAt="last 7 days" label="habits · week" data={data}
      onPin={onPin} pinTitle="Attach to your next console message" />
    {error && <p className="widget-error">{error}</p>}
    {!data && !error && <p className="widget-empty">Loading…</p>}
    {data && data.habits.length === 0 && (
      <p className="widget-empty">No habits yet.</p>
    )}
    {data && data.habits.length > 0 && (
      <table className="habit-grid">
        <thead>
          <tr>
            <td />
            {span.map((day) => (
              <th key={day} scope="col"
                className={day === data.today ? "habit-grid-today" : ""}>
                <span className="habit-grid-dow">{WEEKDAY[dayDate(day).getDay()]}</span>
                <span className="habit-grid-dom">{Number(day.slice(8))}</span>
              </th>
            ))}
            <td />
          </tr>
        </thead>
        <tbody>
          {data.habits.map((h) => (
            <HabitRow key={h.id} habit={h} today={data.today}
              onToggle={toggle} onRename={rename} onArchive={archive}
              onDelete={remove} />
          ))}
        </tbody>
      </table>
    )}
    {/* Outside both branches on purpose: the empty state is exactly when
        adding matters most, and until now "No habits yet." was a dead end —
        creating one meant asking the console. */}
    {data && (
      <form className="habit-add" onSubmit={add}>
        <input value={newName} placeholder="Add a habit…" aria-label="New habit"
          onChange={(e) => setNewName(e.target.value)} />
        <button type="submit" className="pbtn" disabled={!newName.trim()}>Add</button>
      </form>
    )}
    </>
  );
}
