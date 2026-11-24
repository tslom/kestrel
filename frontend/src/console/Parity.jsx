import { useState } from "react";

// TUI pieces a console tab lacked: task list, /mcp panel, rewind. The CLI's state, not ours (console_tasks.py; `mcp`/`rewind` frames in console.py).

const MARK = { completed: "✓", in_progress: "▸", pending: "○" };

// Above the composer while anything is left; folds to a line; a finished list goes away on its own.
export function TaskPanel({ tasks }) {
  const [folded, setFolded] = useState(false);
  if (!tasks?.length || tasks.every((t) => t.status === "completed")) return null;
  const done = tasks.filter((t) => t.status === "completed").length;
  const now = tasks.find((t) => t.status === "in_progress");
  return (
    <div className="task-panel">
      <button className="task-head" onClick={() => setFolded((f) => !f)}
        title={folded ? "Show the task list" : "Fold the task list"}>
        <span className="task-count">tasks {done}/{tasks.length}</span>
        {folded && now && <span className="task-now">{now.active_form || now.subject}</span>}
      </button>
      {!folded && (
        <ol className="task-list">
          {tasks.map((t) => (
            <li key={t.id} className={`task-row ${t.status}`}>
              <span className="task-mark">{MARK[t.status]}</span>
              <span className="task-subject">
                {t.status === "in_progress" && t.active_form ? t.active_form : t.subject}
              </span>
              {t.blocked_by?.length > 0 && t.status === "pending" && (
                <span className="task-blocked">after #{t.blocked_by.join(", #")}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

const MCP_TONE = { connected: "ok", pending: "wait", "needs-auth": "warn", failed: "bad", disabled: "off" };

// /mcp as a panel. Authenticating is an interactive browser flow the SDK can't run, so needs-auth says where, with no dead button.
export function McpPanel({ state, onAction, onClose }) {
  const servers = state?.servers || [];
  return (
    <div className="mcp-panel">
      <div className="help-head">
        mcp servers{state?.loading ? " — checking…" : ` — ${servers.length}`}
        <button className="bar-btn mcp-close" onClick={onClose}>close</button>
      </div>
      {state?.error && <p className="mcp-error">{state.error}</p>}
      {!state?.loading && servers.length === 0 && !state?.error && (
        <p className="status">This session has no MCP servers.</p>
      )}
      {servers.map((s) => (
        <div key={s.name} className="mcp-row">
          <span className={`mcp-dot ${MCP_TONE[s.status] || "off"}`} />
          <span className="mcp-name">{s.name}</span>
          <span className="scope-tag">{s.status}</span>
          <span className="mcp-meta">
            {[s.source || s.scope, s.transport, s.tools != null ? `${s.tools} tools` : null]
              .filter(Boolean).join(" · ")}
          </span>
          <span className="bar-spacer" />
          {s.status === "needs-auth" && (
            <span className="mcp-hint" title="The sign-in is a browser flow the console's session can't run">
              sign in: <code>claude</code> → <code>/mcp</code> in a terminal
            </span>
          )}
          {(s.status === "failed" || s.status === "needs-auth") && (
            <button className="bar-btn" disabled={state?.loading}
              onClick={() => onAction("reconnect", s.name)}>reconnect</button>
          )}
          <button className="bar-btn" disabled={state?.loading}
            onClick={() => onAction("toggle", s.name, s.status === "disabled")}>
            {s.status === "disabled" ? "enable" : "disable"}
          </button>
        </div>
      ))}
    </div>
  );
}

// Rewind to just before one of your messages (Esc Esc in the terminal): "conversation" drops it and
// later turns (text returns to the composer); in a dev tab "code" restores the repo's files, or both.
// Two clicks because the second throws work away and the viewer has no confirm().
export function RewindButton({ entry, disabled, files, onRewind }) {
  const [arming, setArming] = useState(false);
  if (!entry.checkpoint) return null;
  if (entry.rewind === "pending") return <span className="rewind-note">rewinding…</span>;
  if (entry.rewind === "done") return <span className="rewind-note">files rewound to here</span>;
  const go = (scope) => { setArming(false); onRewind(entry, scope); };
  if (arming)
    return (
      <span className="rewind-confirm">
        rewind to before this:
        {files && <button className="bar-btn rewind-yes" onClick={() => go("both")}
          title="Restore the files and drop this message and everything after it">code + conversation</button>}
        <button className="bar-btn rewind-yes" onClick={() => go("conversation")}
          title="Drop this message and everything after it; files stay as they are">conversation</button>
        {files && <button className="bar-btn rewind-yes" onClick={() => go("code")}
          title="Restore the repo's files; the conversation stays">code</button>}
        <button className="bar-btn" onClick={() => setArming(false)}>never mind</button>
      </span>
    );
  return (
    <span className="rewind-wrap">
      {entry.rewind?.error && <span className="rewind-note bad" title={entry.rewind.error}>rewind failed</span>}
      <button className="bar-btn rewind-btn" disabled={disabled}
        title={disabled ? "After this turn" : "Go back to just before this message"}
        onClick={() => setArming(true)}>rewind</button>
    </span>
  );
}
