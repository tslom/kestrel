import { useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { fetchJSON } from "../lib/api";
import { useLoad } from "../lib/refresh";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { renderInline } from "../ui/markdown.jsx";

// Daily brief (IDEAS round 2): generated lazily on the first dashboard open
// of each day_key, cached in app.db — GET /api/brief kicks generation off
// server-side and we poll while it writes. DB-only; never touches the vault.

// The SYSTEM prompt constrains output to ## headings, - bullets and **bold**,
// so a tiny renderer covers the blocks. Parsed into a lede + per-heading
// sections (rather than a flat node list) so the widget can lay sections out
// in the design's two-column grid and clamp by whole section, never mid-bullet
// — which is why the console's flat renderMarkdown can't stand in here.
//
// The inline half is the console's, though: told "no links", the model writes
// one anyway often enough, and a literal [Kagi](url) in the prose was the tell.
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
      // `text` alongside the rendered heading: the rail's fold names what's
      // behind it, and that label has to be a string, not React nodes.
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
  // While the brief is being written (a model call, tens of seconds), ask
  // again every POLL_MS.
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

  // Shown whole: it's bounded (SYSTEM caps it at 250 words / six sections),
  // and in the plan view it's a full view you opened on purpose — the bar
  // above the map carries the one line (plan/BriefLine.jsx).
  return (
    <section className="widget brief"
      {...dragPin(`brief · ${brief?.day}`,
                  brief?.status === "ready"
                    ? { source: "brief", day: brief.day, body: brief.body }
                    : undefined)}>
      <header>
        {/* Every other widget shows data straight from a provider; this one
            is the only prose a model wrote, and nothing said so. The badge
            marks it, and names the sources the prose was written from — so
            "is this real or did it make it up?" has an answer in the UI. */}
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
              // One section of the brief, so "expand on this" doesn't have
              // to carry the other four with it.
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
