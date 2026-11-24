import { useEffect, useState } from "react";
import { fetchJSON } from "../lib/api.js";
import { useVisiblePoll } from "../lib/poll.js";

// The console bar's word on the CLI (app/claude_cli.py). Silent when nothing's to do (the version
// rides the session label's tooltip). Auto-updates are off here, so it speaks only for a release not
// installed (runs `claude update`) or an install this session predates (reconnects; a running CLI keeps its binary).

const POLL_MS = 30 * 60_000;   // the backend caches the release check an hour

export function ContextChip({ ctx }) {
  if (!ctx?.max) return null;
  const k = (n) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M`
    : n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`);
  const pct = Math.round(ctx.percentage ?? (100 * ctx.total) / ctx.max);
  // Free space and the buffer are the room left, not what's in it.
  const rows = (ctx.categories || [])
    .filter((c) => c.tokens && !/^(free space|autocompact buffer)$/i.test(c.name))
    .map((c) => `${c.name}: ${k(c.tokens)}`);
  const near = ctx.autocompact_at && ctx.total >= 0.85 * ctx.autocompact_at;
  const title = [
    `context ${k(ctx.total)} of ${k(ctx.max)} (${pct}%)`,
    ...rows,
    ctx.autocompact_at ? `autocompacts at ${k(ctx.autocompact_at)} — /compact to do it now` : null,
  ].filter(Boolean).join("\n");
  return (
    <span className={`bar-item usage-chip ctx-chip${near ? " usage-allowed_warning" : ""}`}
          title={title}>
      ctx {k(ctx.total)} · {pct}%
    </span>
  );
}

export default function CliStatus({ sessionVersion, busy, onReconnect }) {
  const [cli, setCli] = useState(null);
  const [state, setState] = useState(null);   // null | "updating" | {error} | {note}

  const load = (refresh = false) =>
    fetchJSON(`/api/console/cli${refresh ? "?refresh=1" : ""}`).then(setCli).catch(() => {});
  useEffect(() => { load(); }, []);
  useVisiblePoll(load, POLL_MS, { catchUp: true });

  async function update() {
    setState("updating");
    try {
      const r = await fetchJSON("/api/console/cli/update", { method: "POST" });
      setCli(r);
      if (!r.ok) setState({ error: r.output || "claude update failed" });
      else setState(null);   // updated, or already current — either way `r` says
    } catch (e) {
      setState({ error: e.message });
    }
  }

  if (state === "updating")
    return <span className="bar-item cli-chip" title="claude update">updating Claude Code…</span>;
  if (state?.error)
    return (
      <button className="bar-btn cli-chip bad" title={state.error} onClick={() => setState(null)}>
        update failed
      </button>
    );

  if (cli?.update_available)
    return (
      <button className="bar-btn cli-chip on" onClick={update}
        title={`Claude Code ${cli.latest} is out (${cli.channel}); this machine has ${cli.installed}. Runs \`claude update\`.`}>
        update {cli.latest}
      </button>
    );

  // Installed moved on under this session (updated here, or in a terminal).
  if (cli?.installed && sessionVersion && cli.installed !== sessionVersion)
    return (
      <button className="bar-btn cli-chip on" disabled={busy} onClick={onReconnect}
        title={busy ? "After this turn — reconnecting would cut it"
          : `This session runs ${sessionVersion}; ${cli.installed} is installed. Reconnect resumes the conversation on it.`}>
        reconnect → {cli.installed}
      </button>
    );

  // Up to date: nothing in the bar, which is full enough. The version rides
  // the session label's tooltip instead (Console passes it there).
  return null;
}
