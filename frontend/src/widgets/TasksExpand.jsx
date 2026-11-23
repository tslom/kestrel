import { useCallback, useEffect, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useTopic } from "../lib/refresh";
import Icon from "../ui/icons.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import TaskDue from "../ui/TaskDue.jsx";
import { fmtTime } from "../lib/time.js";
import { dragPin } from "../lib/drag.js";

// Full task list (TASKS_PLAN.md): today-view only shows a capped slice —
// this is the "N more" destination. Grouped by status (open/waiting/someday);
// each section is the plain DB ordering (due asc, priority asc), subtasks
// nested under their parent, one level deep by convention.

function meta(t) {
  const parts = [];
  if (t.defer) parts.push(`→ defer ${t.defer}`);
  if (t.priority) parts.push(`p${t.priority}`);
  if (t.status === "waiting" && t.waiting_on) parts.push(`waiting: `);
  if (t.owed_to) parts.push(`owed to: ${t.owed_to}`);
  if (t.project) parts.push(t.project);
  // where the task came from — "ui" is the default hand-added source, not worth showing
  const src = t.source_kind || (t.source && t.source !== "ui" ? t.source : null);
  if (src) parts.push(`from ${src}`);
  return parts.join(" · ");
}

// Drill-in detail panel.
// Everything here is already on the task dict (list_tasks does SELECT *) —
// this just surfaces the fields the compact row hides: the note, the
// verbatim source_excerpt, and the
// full provenance/metadata. Only non-empty fields render.
function TaskDetail({ t }) {
  const fields = [
    ["status", t.status],
    ["due", t.due && (t.due_time ? `${t.due} ${fmtTime(t.due_at)}` : t.due)],
    ["defer", t.defer],
    ["priority", t.priority ? `p${t.priority}` : null],
    ["project", t.project],
    ["waiting on", t.waiting_on],
    ["owed to", t.owed_to],
    ["source", [t.source_kind, t.source && t.source !== "ui" ? t.source : null]
      .filter(Boolean).join(" / ") || null],
    ["ref", t.source_ref],
    ["created", t.created_at],
    ["updated", t.updated_at],
  ].filter(([, v]) => v != null && v !== "");
  return (
    <li className="task-detail-row" style={{ paddingLeft: 23 }}>
      <div className="task-detail">
        {t.note && <p className="task-detail-note">{t.note}</p>}
        {t.source_excerpt && (
          <blockquote className="task-detail-excerpt">{t.source_excerpt}</blockquote>
        )}
        <dl className="task-detail-fields">
          {fields.map(([k, v]) => (
            <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      </div>
    </li>
  );
}

function Row({ t, indent, openId, onToggle, onDone, onDrop, onPinItem }) {
  const m = meta(t);
  const open = openId === t.id;
  return (
    <>
      <li style={indent ? { paddingLeft: 20 } : undefined}
        {...dragPin(`task · ${t.title}`, t)}>
        <button className="check" onClick={() => onDone(t.id)} title="Mark done"><Icon name="check" /></button>
        <button className={`task-title task-title-btn${open ? " open" : ""}`}
          title="Show task details" aria-expanded={open}
          onClick={() => onToggle(t.id)}>{t.title}</button>
        <TaskDue t={t} />
        <button className="pin-mini" onClick={() => onPinItem(t)}
          title="Pin this task to your next console message"><Icon name="pin" /></button>
        <button className="pin-mini" onClick={() => onDrop(t.id)} title="Drop"><Icon name="close" /></button>
      </li>
      {m && <li className="task-meta-row" style={{ paddingLeft: indent ? 20 : 0 }}>
        <span className="task-meta">{m}</span>
      </li>}
      {open && <TaskDetail t={t} />}
      {(t.subtasks || []).map((s) => (
        <Row key={s.id} t={s} indent openId={openId} onToggle={onToggle}
          onDone={onDone} onDrop={onDrop} onPinItem={onPinItem} />
      ))}
    </>
  );
}

const SECTIONS = [
  { status: "open", label: "open" },
  { status: "waiting", label: "waiting" },
  { status: "someday", label: "someday" },
];

// category = the task's project; nulls sort last, ties keep DB order (stable)
function byCategory(a, b) {
  const pa = a.project || "￿", pb = b.project || "￿";
  return pa < pb ? -1 : pa > pb ? 1 : 0;
}

export default function TasksExpand({ onPin }) {
  const [byStatus, setByStatus] = useState(null);
  const [error, setError] = useState(null);
  const [showDeferred, setShowDeferred] = useState(false);
  const [sortByCategory, setSortByCategory] = useState(false);
  const [openId, setOpenId] = useState(null);

  const toggleDetail = (id) => setOpenId((cur) => (cur === id ? null : id));

  const load = useCallback(() => {
    Promise.all(SECTIONS.map((s) => fetchJSON(`/api/tasks?status=${s.status}`)))
      .then((lists) => {
        setByStatus(Object.fromEntries(SECTIONS.map((s, i) => [s.status, lists[i]])));
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => { load(); }, [load]);
  useTopic("tasks", load);


  const setStatus = (id, status) =>
    postJSON(`/api/tasks/${id}/status`, { status })
      .then(load).catch((e) => setError(e.message));

  const done = (id) => setStatus(id, "done");
  const drop = (id) => setStatus(id, "dropped");
  const pinItem = (t) => onPin(`task · ${t.title}`, t);

  const total = byStatus && Object.values(byStatus).reduce((n, l) => n + l.length, 0);
  const deferredCount = byStatus
    ? Object.values(byStatus).flat().filter((t) => !t.parent_id && t.deferred).length
    : 0;

  return (
    <>
    <SheetHead refreshedAt={byStatus && `${total} total`} label="all tasks" data={byStatus}
      onPin={onPin} pinTitle="Attach to your next console message" />
    {error && <p className="widget-error">{error}</p>}
    {!byStatus && !error && <p className="widget-empty">Loading…</p>}
    {byStatus && (
      <div className="chip-row" style={{ marginBottom: 12 }}>
        <button className={`chip ${sortByCategory ? "active" : ""}`}
          onClick={() => setSortByCategory((v) => !v)}
          title="Order each section by category (project)">
          sort: {sortByCategory ? "category" : "default"}
        </button>
        {deferredCount > 0 && (
          <button className={`chip ${showDeferred ? "active" : ""}`}
            onClick={() => setShowDeferred((v) => !v)}
            title="Deferred tasks are hidden until their defer date">
            {showDeferred ? "hide" : "show"} {deferredCount} deferred
          </button>
        )}
      </div>
    )}
    {byStatus && SECTIONS.map(({ status, label }) => {
      let items = (byStatus[status] || []).filter((t) => !t.parent_id);
      if (!showDeferred) items = items.filter((t) => !t.deferred);
      if (sortByCategory) items = [...items].sort(byCategory);
      if (items.length === 0) return null;
      return (
        <section key={status} style={{ marginBottom: 16 }}>
          <h3 className="widget-sub" style={{ margin: "0 0 6px" }}>{label} ({items.length})</h3>
          <ul className="task-list checkable" {...dragPin(`tasks · ${label}`, items)}>
            {items.map((t) => <Row key={t.id} t={t} openId={openId} onToggle={toggleDetail}
              onDone={done} onDrop={drop} onPinItem={pinItem} />)}
          </ul>
        </section>
      );
    })}
    {byStatus && total === 0 && <p className="widget-empty">Nothing tracked — the board is clear.</p>}
    </>
  );
}
