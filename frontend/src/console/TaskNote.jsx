import { useState } from "react";

// A background agent or shell finishing, failing, hitting its time limit, or
// found cut off when the last session ended (backend: task_notes.py). The CLI
// tells the model with a <task-notification> message, which used to show as a
// bubble of raw XML in your own voice. One status-style line instead; open it
// for the CLI's note, the agent's report and the ids.

const ICON = { completed: "✓", failed: "✕", killed: "⏱", stopped: "⏹" };

export default function TaskNote({ e }) {
  const [open, setOpen] = useState(false);
  const more = e.note || e.result || e.output_file || e.task_ids?.length;
  return (
    <div className={`entry status tasknote ${e.status || ""}${open ? " open" : ""}`}>
      <button type="button" className="tasknote-head" disabled={!more}
              onClick={() => setOpen((v) => !v)} title={more ? (open ? "Hide" : "Show the details") : undefined}>
        <span className="tasknote-icon" aria-hidden="true">{ICON[e.status] || "•"}</span>
        <span className="tasknote-title">{e.title}</span>
      </button>
      {open && (
        <div className="tasknote-body">
          {e.note && <p>{e.note}</p>}
          {e.result && <pre className="tasknote-result">{e.result}</pre>}
          {(e.output_file || e.task_ids?.length > 0) && (
            <p className="tasknote-ids">
              {e.task_ids?.length > 0 && <>task {e.task_ids.join(", ")}</>}
              {e.output_file && <> · output <code>{e.output_file}</code></>}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
