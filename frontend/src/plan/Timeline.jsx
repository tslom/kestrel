import { toolLabel, secs } from "./StatusBar.jsx";

// Every tool call of the turn on one time axis, including the time a call
// spent waiting on your answer (striped). Opened from the status bar, or
// already filtered to the failures from its "N failed" link.
export default function Timeline({ trace, failedOnly, onShowAll }) {
  const calls = failedOnly ? trace.calls.filter((c) => c.state === "err") : trace.calls;
  const end = trace.running && trace.startedAt != null
    ? performance.now() - trace.startedAt
    : Math.max(1, ...trace.calls.map((c) => c.t1 ?? c.t0));
  const pct = (ms) => `${Math.min(100, (ms / end) * 100).toFixed(2)}%`;

  return (
    <div className="timeline">
      <div className="tl-head">
        <span>{failedOnly ? `${calls.length} failed of ${trace.calls.length} calls`
          : `${trace.calls.length} call${trace.calls.length === 1 ? "" : "s"} · ${secs(end)}`}</span>
        {failedOnly && <button className="st-link" onClick={onShowAll}>show all calls</button>}
      </div>
      <div className="tl-rows">
        {calls.map((c) => {
          const t1 = c.t1 ?? end;
          return (
            <div key={c.id} className={`tl-row ${c.state === "live" && trace.waiting?.name === c.name ? "wait" : c.state}`}>
              <span className="tl-who">{toolLabel(c.name)}</span>
              <span className="tl-dt" title={c.detail}>{c.detail}</span>
              <span className="tl-track">
                <i className="tl-span" style={{ left: pct(c.t0), width: pct(Math.max(t1 - c.t0, end / 200)) }} />
                {c.waitMs > 0 && <i className="tl-wait" style={{ left: pct(c.t0), width: pct(c.waitMs) }} />}
              </span>
              <span className="tl-ms">{c.t1 == null ? "…" : secs(c.t1 - c.t0)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
