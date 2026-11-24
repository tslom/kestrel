import { useState } from "react";
import { fetchJSON } from "../lib/api.js";
import { useVisiblePoll } from "../lib/poll.js";
import { toolLabel } from "../plan/StatusBar.jsx";
import Icon from "../ui/icons.jsx";

// An Agent call with what its subagent did (#77). Subagents run in the background and their calls
// never reach this tab's stream, so the backend reads the subagent's transcript (sessions.subagent_run,
// by call id), which also works on a resumed session. Collapsed: task, call count, current call;
// open: every call, then the report. `running`: the strip (console_agents) has it at work.

const POLL_MS = 2500;
// Empty asks before giving up (the transcript lands a moment after the call; an old session's may not exist).
const MAX_MISSES = 6;
const IDLE_S = 30;

export default function AgentCall({ t, sessionId, projectQ, running }) {
  const [run, setRun] = useState(null);
  const [open, setOpen] = useState(false);
  const [misses, setMisses] = useState(0);
  const [settled, setSettled] = useState(false);
  let input = {};
  try { input = JSON.parse(t.raw || "{}") || {}; } catch { /* clipped */ }
  const description = input.description || t.desc || run?.description || t.detail || "subagent";
  // Ask until finished, or one read after the strip drops it (a subagent that died stays "unfinished" on disk forever).
  const done = settled || misses >= MAX_MISSES || !sessionId || !t.id;
  useVisiblePoll(() => {
    if (done) return;
    const q = `${projectQ ? `${projectQ}&` : "?"}tool=${encodeURIComponent(t.id)}`;
    fetchJSON(`/api/sessions/${sessionId}/subagent${q}`)
      .then((r) => {
        if (!r?.run) { setMisses((n) => n + 1); return; }
        setRun(r.run);
        // Not `!running` alone: the strip can lag the transcript, so a quiet file counts too.
        if (r.run.finished || (!running && (r.run.idle_s ?? 0) > IDLE_S)) setSettled(true);
      })
      .catch(() => setMisses((n) => n + 1));
  }, run || misses ? POLL_MS : 300, { catchUp: true });

  const calls = run?.calls || [];
  const current = calls[calls.length - 1];
  const kind = input.subagent_type || run?.agent_type;
  const meta = [kind && kind !== "general-purpose" ? kind : null, input.model || run?.model,
    running ? (current ? `on ${toolLabel(current.name)}` : "starting") : run?.finished ? "done" : null];
  return (
    <div className={`activity agent-call${open ? " open" : ""}${running ? " live" : ""}`}>
      <button className="activity-head" onClick={() => setOpen((v) => !v)}
              title={open ? "Hide what it did" : input.prompt?.slice(0, 600) || description}>
        <Icon name={open ? "chevronDown" : "chevronRight"} size={10} />
        <span className={`activity-count${running ? " pulse" : ""}`}>{run ? run.total : "…"}</span>
        <span className="tool-name">{toolLabel(t.name)}</span>
        <span className="tool-desc">{description}</span>
        <span className="tool-detail">{meta.filter(Boolean).join(" · ")}</span>
      </button>
      {!open && running && current && (
        <div className="agent-call-now">
          <span className="tool-name">↳ {toolLabel(current.name)}</span>{" "}
          <span className="tool-detail">{current.desc || current.detail}</span>
        </div>
      )}
      {open && (
        <div className="activity-calls">
          {run && run.total > calls.length && (
            <div className="entry tool"><span className="tool-detail">{run.total - calls.length} earlier calls</span></div>
          )}
          {calls.map((c) => (
            <div key={c.id} className={`entry tool${c.state === "error" ? " error" : ""}`} title={c.detail}>
              <span className="tool-name">↳ {toolLabel(c.name)}</span>{" "}
              {c.desc && <span className="tool-desc">{c.desc} </span>}
              <span className="tool-detail">{c.detail}</span>
            </div>
          ))}
          {!calls.length && (
            <div className="entry tool"><span className="tool-detail">
              {run ? "No calls yet." : done ? "Its transcript isn't on disk." : "Looking…"}
            </span></div>
          )}
          {running && run?.said && <p className="agent-call-report">{run.said}</p>}
          {run?.report && <p className="agent-call-report">{run.report}</p>}
        </div>
      )}
    </div>
  );
}
