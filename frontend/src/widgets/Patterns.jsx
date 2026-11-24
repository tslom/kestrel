import { useEffect, useState } from "react";
import { fetchJSON } from "../lib/api";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// Correlations view inside TrendsExpand (/api/trends/correlations: significant Pearson pairs).
// Sample size and a "tentative" flag ride with every r so 29 days of coincidence never reads as a law.

const LABELS = {
  listen_min: "listening", novelty: "music discovery",
  sleep_hours: "sleep", resting_hr: "resting HR", hrv: "HRV",
  steps: "steps", active_zone_minutes: "active minutes",
  msgs_sent: "messages sent", msgs_received: "messages received",
  contacts: "people contacted", spend: "spending",
  calendar_events: "calendar events", busy_min: "calendar busy",
  tasks_done: "tasks done", time_at_home_min: "time at home",
  places: "places visited", entropy: "location variety",
  weather_tmax: "high temp", precip_prob: "rain chance",
};
const feat = (f) => LABELS[f] || f.replace(/_/g, " ");

const WINDOWS = [
  { v: 90, label: "90d" },
  { v: 365, label: "1y" },
  { v: 1825, label: "all" },
];

export default function Patterns({ onPin }) {
  const [window, setWindow] = useState(365);
  const [listeningOnly, setListeningOnly] = useState(false);
  const [crossOnly, setCrossOnly] = useState(true);
  const [pairs, setPairs] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({
      window: String(window),
      cross_domain: String(crossOnly),
    });
    if (listeningOnly) params.set("focus", "listen_min,novelty");
    fetchJSON(`/api/trends/correlations?${params}`)
      .then((d) => { if (live) setPairs(d.pairs); })
      .catch((e) => { if (live) setError(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [window, listeningOnly, crossOnly]);

  return (
    <div className="patterns"
      {...dragPin(`patterns · ${window}d`,
                  pairs?.length ? { source: "correlations", window, pairs } : undefined)}>
      <p className="patterns-intro">
        Signals that rise and fall together across sources — associations, not
        causes. Only statistically significant pairs (p&nbsp;&lt;&nbsp;0.05) show.
      </p>

      <div className="chip-row">
        {WINDOWS.map((w) => (
          <button key={w.v}
            className={`chip${window === w.v ? " active" : ""}`}
            onClick={() => setWindow(w.v)}>{w.label}</button>
        ))}
        <span className="patterns-sep" />
        <button className={`chip${listeningOnly ? " active" : ""}`}
          onClick={() => setListeningOnly((v) => !v)}>listening only</button>
        <button className={`chip${!crossOnly ? " active" : ""}`}
          onClick={() => setCrossOnly((v) => !v)}>include same-source</button>
      </div>

      {loading && <p className="widget-empty">Loading…</p>}
      {error && <p className="widget-error">{error}</p>}
      {pairs && !loading && !pairs.length && (
        <p className="widget-empty">
          No significant patterns in this window yet — they surface as more
          overlapping days accumulate across sources.
        </p>
      )}

      {pairs && !loading && pairs.map((c) => {
        const pos = c.r >= 0;
        const half = Math.min(Math.abs(c.r), 1) * 50; // half-track fill %
        const tentative = c.n < 40 || c.p > 0.01;
        return (
          <div className="corr-row" key={`${c.a}~${c.b}`}
            {...dragPin(`pattern · ${feat(c.a)} ~ ${feat(c.b)}`, { window, ...c })}>
            <div className="corr-pair">
              <span className="corr-feat">{feat(c.a)}</span>
              <span className={`corr-dir ${pos ? "pos" : "neg"}`}>
                {pos ? "move together" : "move opposite"}
              </span>
              <span className="corr-feat">{feat(c.b)}</span>
            </div>
            <div className="corr-bar" aria-hidden="true">
              <span className="corr-bar-center" />
              <span className={`corr-bar-fill ${pos ? "pos" : "neg"}`}
                style={pos ? { left: "50%", width: `${half}%` }
                           : { right: "50%", width: `${half}%` }} />
            </div>
            <div className="corr-meta">
              <span className="corr-r">r&nbsp;{c.r > 0 ? "+" : ""}{c.r.toFixed(2)}</span>
              <span>{c.n} days</span>
              <span>p&nbsp;{c.p}</span>
              {tentative && <span className="corr-tent">tentative</span>}
              {/* the two features count a day differently (day_key vs civil
                  date), so the pair is joined across the 4am rollover */}
              {c.cross_basis && <span className="corr-basis" title={
                "These sources key a day differently — one uses the 4am "
                + "rollover, the other the calendar date — so this pair is "
                + "matched across a boundary that can be off by a few hours."
              }>mixed days</span>}
            </div>
          </div>
        );
      })}

      {pairs && !!pairs.length && (
        <div className="patterns-foot">
          <button className="pin" onClick={() => onPin(
            `patterns · ${window}d`, { source: "correlations", window, pairs })}
            title="Attach these correlations to your next console message">
            <Icon name="pin" /> pin
          </button>
        </div>
      )}
    </div>
  );
}
