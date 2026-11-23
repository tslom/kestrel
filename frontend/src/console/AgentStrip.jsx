import { useEffect, useState } from "react";

// Subagents at work, under the log (backend: console_agents.py). The Agent
// tool runs its subagent in the background now: the call comes back at once
// and the turn can end while it works, and its own calls never reach this
// stream. Without this the only sign of one was its report, minutes later.
//
// A running row: what it was sent to do, the tool it's on, how many calls and
// tokens, how long. A finished row stays until your next message, with the
// first line of what it reported.

const TERMINAL = new Set(["completed", "failed", "stopped", "killed"]);

function age(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function tokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
}

export default function AgentStrip({ agents, hidden }) {
  const shown = agents.filter((a) => !TERMINAL.has(a.status) || !hidden.has(a.id));
  const live = shown.some((a) => !TERMINAL.has(a.status));
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live) return undefined;
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [live]);
  if (!shown.length) return null;
  return (
    <div className="agent-strip" role="status" aria-label="Subagents">
      {shown.map((a) => {
        const done = TERMINAL.has(a.status);
        const ms = done ? (a.duration_ms ?? 0) : Date.now() - a.started * 1000;
        const bad = a.status === "failed" || a.status === "killed" || a.status === "stopped";
        const kind = a.kind && a.kind !== "local_agent" ? a.kind.replace(/^local_/, "") : "agent";
        return (
          <div key={a.id} className={`agent-row ${done ? (bad ? "bad" : "done") : "live"}`}
               title={a.summary || a.description}>
            <span className="agent-dot" aria-hidden="true" />
            <span className="agent-kind">{kind}</span>
            <span className="agent-desc">{a.description || "working"}</span>
            <span className="agent-meta">
              {!done && (a.activity || a.last_tool) && (
                <span className="agent-tool" title={a.activity || undefined}>
                  {a.last_tool ? a.last_tool.split("__").pop() : "working"}
                </span>
              )}
              {a.tool_uses > 0 && <span>{a.tool_uses} call{a.tool_uses === 1 ? "" : "s"}</span>}
              {a.tokens > 0 && <span>{tokens(a.tokens)} tok</span>}
              <span>{done ? (bad ? a.status : "done") : ""}{done ? " · " : ""}{age(ms)}</span>
            </span>
            {!done && a.activity && <span className="agent-summary">{a.activity}</span>}
            {done && a.summary && a.summary !== a.description
              && <span className="agent-summary">{a.summary.split("\n")[0]}</span>}
          </div>
        );
      })}
    </div>
  );
}
