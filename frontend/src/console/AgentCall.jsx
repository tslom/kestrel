import { useState } from "react";
import { fetchJSON } from "../lib/api.js";
import { useVisiblePoll } from "../lib/poll.js";
import { toolLabel } from "../plan/StatusBar.jsx";
import Icon from "../ui/icons.jsx";

// An Agent call in the log, with what its subagent did (#77). The Agent tool
// runs subagents in the background: the call returns "launched" at once and
// the subagent's own calls never reach this tab's stream, so the log had one
// "Agent" line and nothing under it, and the strip under the log forgot it at
// your next message. The backend reads the subagent's own transcript instead
// (sessions.subagent_run, by this call's id), so the same card works live and
// on a resumed session.
//
// Collapsed: what it was sent to do, how many calls, and while it runs the
// call it's on. Open: every call, then its report once it's done.
//
// `running`: the strip (console_agents) has this call's subagent at work.

const POLL_MS = 2500;
// Asks that found nothing before giving up: the transcript lands a moment
// after the call, but an old session's may simply not exist.
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
  // Asked until it's finished, or until the strip no longer has it running
  // and one read after that is in: a subagent that died with its session
  // stays "unfinished" on disk forever, and polling that would never stop.
  const done = settled || misses >= MAX_MISSES || !sessionId || !t.id;
  useVisiblePoll(() => {
    if (done) return;
    const q = `${projectQ ? `${projectQ}&` : "?"}tool=${encodeURIComponent(t.id)}`;
    fetchJSON(`/api/sessions/${sessionId}/subagent${q}`)
      .then((r) => {
        if (!r?.run) { setMisses((n) => n + 1); return; }
        setRun(r.run);
        // Not `!running` alone: the strip can hear of a subagent a beat
        // after its transcript appears, so a quiet file is the other half.
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
