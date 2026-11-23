import { useState } from "react";
import { postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";

// Settings → Scheduled prompts (app/schedules.py): a prompt that opens a new
// tab at a time, once or on repeat. lib/schedules.js is what opens them; this
// only lists and edits.

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const REPEATS = [["weekly", "weekly"], ["daily", "daily"], ["once", "once"]];

function fmtNext(iso) {
  if (!iso) return "done";
  const d = new Date(iso);
  return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric",
                                hour: "numeric", minute: "2-digit" });
}

function NewSchedule({ onMade }) {
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("general");
  const [project, setProject] = useState("");
  const [repeat, setRepeat] = useState("weekly");
  const [days, setDays] = useState([6]);
  const [time, setTime] = useState("20:00");
  const [date, setDate] = useState("");
  const [error, setError] = useState(null);
  const toggle = (d) => setDays((ds) => (ds.includes(d) ? ds.filter((x) => x !== d) : [...ds, d]));
  const submit = (e) => {
    e.preventDefault();
    setError(null);
    const body = {
      title, prompt, mode, project: mode === "dev" ? project : null,
      at_time: repeat === "once" ? `${date}T${time}` : time,
      repeat: repeat === "once" ? null : repeat === "daily" ? "daily" : `weekly:${[...days].sort().join(",")}`,
    };
    postJSON("/api/schedules", body)
      .then(() => { setTitle(""); setPrompt(""); onMade(); })
      .catch((err) => setError(err.message));
  };
  return (
    <form className="sched-new" onSubmit={submit}>
      <input className="set-input" placeholder="Title (Sunday digest)" value={title}
        onChange={(e) => setTitle(e.target.value)} required maxLength={80} />
      <textarea className="set-input sched-prompt" rows={3} required
        placeholder="What's due this week, and any unpaid expenses?"
        value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      <div className="sched-when">
        <select className="set-select" value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Mode">
          <option value="general">general</option>
          <option value="study">study</option>
          <option value="dev">dev</option>
        </select>
        {mode === "dev" && (
          <input className="set-input sched-project" placeholder="project" value={project}
            onChange={(e) => setProject(e.target.value)} required />
        )}
        <select className="set-select" value={repeat} onChange={(e) => setRepeat(e.target.value)} aria-label="Repeat">
          {REPEATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        {repeat === "once" && (
          <input className="set-input" type="date" value={date} required
            onChange={(e) => setDate(e.target.value)} aria-label="Date" />
        )}
        <input className="set-input" type="time" value={time} required
          onChange={(e) => setTime(e.target.value)} aria-label="Time" />
      </div>
      {repeat === "weekly" && (
        <div className="set-seg sched-days" role="group" aria-label="Days">
          {DAYS.map((d, i) => (
            <button type="button" key={d} aria-pressed={days.includes(i)}
              className={days.includes(i) ? "on" : ""} onClick={() => toggle(i)}>{d}</button>
          ))}
        </div>
      )}
      <div className="sched-actions">
        <button className="pbtn" type="submit" disabled={repeat === "weekly" && !days.length}>Schedule</button>
        {error && <span className="widget-error">{error}</span>}
      </div>
    </form>
  );
}

export default function Schedules() {
  const { data, error, setError, reload } = useWidgetData("/api/schedules");
  const [adding, setAdding] = useState(false);
  const act = (p) => p.then(reload).catch((e) => setError(e.message));
  const rows = data?.schedules || [];
  return (
    <div className="sched">
      <p className="set-note">
        Each opens a new tab at its time. Kestrel has to be open: with the lid
        closed nothing runs, and a missed time runs once when you're back (within
        12 hours; past that a repeating one waits for its next time).
      </p>
      {error && <p className="widget-error">{error}</p>}
      {rows.map((s) => (
        <div key={s.id} className={`set-row sched-row${s.enabled ? "" : " off"}`}>
          <div className="set-what">
            <span>{s.title} <small className="sched-mode">{s.mode}{s.project ? ` · ${s.project}` : ""}</small></span>
            <small>{s.when} · {s.enabled ? `next ${fmtNext(s.next_at)}` : s.next_at ? "paused" : "ran"}
              {s.runs ? ` · ran ${s.runs}×` : ""}</small>
          </div>
          <div className="set-how sched-btns">
            {(s.enabled || s.next_at || s.repeat) && (
              <button className="pbtn" onClick={() => act(postJSON(`/api/schedules/${s.id}/enabled`, { enabled: !s.enabled }))}>
                {s.enabled ? "Pause" : "Resume"}
              </button>
            )}
            <button className="pbtn" aria-label={`Delete ${s.title}`}
              onClick={() => act(postJSON(`/api/schedules/${s.id}`, null, "DELETE"))}>Delete</button>
          </div>
        </div>
      ))}
      {adding ? <NewSchedule onMade={() => { setAdding(false); reload(); }} />
        : <button className="pbtn" onClick={() => setAdding(true)}>New scheduled prompt</button>}
    </div>
  );
}
