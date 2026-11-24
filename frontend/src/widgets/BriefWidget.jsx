import { useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { fetchJSON } from "../lib/api";
import { useLoad } from "../lib/refresh";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { renderInline } from "../ui/markdown.jsx";

// Daily brief: generated lazily on the first open of each day_key and cached in app.db; GET
// /api/brief starts generation and we poll. DB-only, never touches the vault.

// SYSTEM limits output to ## headings, - bullets and **bold**, so a tiny renderer suffices. Parsed
// into lede + per-heading sections (not a flat list) for the two-column grid and whole-section
// clamping, which the console's flat renderMarkdown can't do. Inline rendering is the console's:
// the model writes links despite "no links", and a literal [Kagi](url) was the tell.
function parseBrief(text) {
  const lede = [];
  const sections = [];
  let cur = null;
  let list = null;

  const flushList = (key) => {
    if (list) {
      (cur ? cur.nodes : lede).push(<ul key={key}>{list}</ul>);
      list = null;
    }
  };

  text.split("\n").forEach((ln, i) => {
    const b = ln.match(/^\s*[-*]\s+(.*)/);
    const h = ln.match(/^#{1,4}\s+(.*)/);
    if (b) { (list ??= []).push(<li key={i}>{renderInline(b[1], `b${i}`)}</li>); return; }
    flushList(`u${i}`);
    if (h) {
      // `text`: the fold label must be a string, not React nodes.
      cur = { heading: renderInline(h[1], `h${i}`), text: h[1], nodes: [] };
      sections.push(cur);
    } else if (ln.trim()) {
      (cur ? cur.nodes : lede).push(<p key={i}>{renderInline(ln, `p${i}`)}</p>);
    }
  });
  flushList("u$");
  return { lede, sections };
}

const POLL_MS = 2500;

export default function BriefWidget({ onPin }) {
  const [brief, setBrief] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Poll every POLL_MS while the model writes it (tens of seconds).
  const poll = useTimeout();
  const schedule = () => poll.start(load, POLL_MS);

  const load = () =>
    fetchJSON("/api/brief")
      .then((b) => {
        setBrief(b);
        setError(null);
        if (b.status === "generating") schedule();
      })
      .catch((e) => { setError(e.message); });

  const regenerate = () => {
    setBusy(true);
    fetchJSON("/api/brief/regenerate", { method: "POST" })
      .then((b) => { setBrief(b); setError(null); schedule(); })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  useLoad(load);  // mount + after a sync + on a day-key rollover

  const failed = brief ? Object.keys(brief.sources?.failed || {}) : [];
  const loaded = brief?.sources?.loaded || [];

  const parsed = brief?.status === "ready" ? parseBrief(brief.body) : null;

  // Shown whole: bounded (250 words / six sections); the plan view's one line is plan/BriefLine.jsx.
  return (
    <section className="widget brief"
      {...dragPin(`brief · ${brief?.day}`,
                  brief?.status === "ready"
                    ? { source: "brief", day: brief.day, body: brief.body }
                    : undefined)}>
      <header>
        {/* The only model-written prose on the desk: the badge says so and names its sources. */}
        <h2>
          Brief <span className="widget-sub">{brief?.day}</span>
          {brief?.status === "ready" && (
            <span className="ai-badge"
              title={`Written by ${brief.model || "Claude"} from your data — `
                + `${loaded.length ? loaded.join(", ") : "no sources"}. `
                + `Every other widget shows unedited provider data.`}>
              AI
            </span>
          )}
        </h2>
        <div className="widget-actions">
          <button onClick={regenerate} disabled={busy || brief?.status === "generating"}
            title="Regenerate today's brief"><Icon name="refresh" /></button>
          <button className="pin" disabled={brief?.status !== "ready"}
            onClick={() => onPin(`brief · ${brief.day}`,
              { source: "brief", day: brief.day, body: brief.body })}
            title="Attach the brief to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!brief && !error && <><div className="skel" /><div className="skel w80" /><div className="skel w60" /></>}
      {brief?.status === "generating" && (
        <p className="widget-empty pulse">Writing today's brief…</p>
      )}
      {brief?.status === "error" && (
        <p className="widget-error">
          Brief failed: {brief.error}{" "}
          <button className="chip" onClick={regenerate} disabled={busy}>retry</button>
        </p>
      )}
      {brief?.status === "ready" && (
        <div className="brief-body">
          {parsed.lede}
          <div className="brief-cols">
            {parsed.sections.map((s, i) => (
              // Per-section drag, so "expand on this" doesn't carry the rest.
              <div key={i}
                {...dragPin(`brief · ${s.text}`,
                            { source: "brief", day: brief.day,
                              section: s.text, body: s.text })}>
                <h3>{s.heading}</h3>
                {s.nodes}
              </div>
            ))}
          </div>
        </div>
      )}
      {brief?.status === "ready" && failed.length > 0 && (
        <p className="brief-failed">unavailable: {failed.join(", ")}</p>
      )}
    </section>
  );
}
