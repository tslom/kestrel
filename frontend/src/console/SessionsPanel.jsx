import { useEffect, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { fetchJSON } from "../lib/api.js";
import { dragPin } from "../lib/drag.js";
import { sessionPin } from "../lib/pills.js";

const clip = (s, n = 40) => { const t = s.replace(/\s+/g, " ").trim(); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };

// Past sessions in two places: the resume panel (all, or transcript search) and the idle-log standby
// list. Both read /api/sessions through one loader; fetchJSON coalesces a same-tick double ask.

// who said the matching line, for search results in the resume panel
const SPEAKER = { user: "you", text: "claude", tool: "tool" };

// Newest first, or those whose transcript mentions `q`. Never throws (a failed read is an empty list).
// `project`: a dev tab's repo (what `claude --resume` lists there); absent, the vault's.
function listSessions(q = "", project = null) {
  const p = project ? `project=${encodeURIComponent(project)}` : "";
  const url = q ? `/api/sessions/search?q=${encodeURIComponent(q)}${p ? `&${p}` : ""}`
    : `/api/sessions${p ? `?${p}` : ""}`;
  return fetchJSON(url)
    .then((d) => (Array.isArray(d) ? d : []))
    .catch(() => []);
}

// Age for the standby list; the desk's "1d"s come precomputed, so this stays local.
function ago(mtime) {
  const mins = Math.max(0, (Date.now() - mtime * 1000) / 60000);
  if (mins < 60) return `${Math.round(mins)}m`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

/** The resume panel; mounted only while open, so closing resets the search. */
export function SessionsPanel({ onResume, onClose, project = null }) {
  // null until the first read lands (no "no past sessions" flash).
  const [sessions, setSessions] = useState(null);
  const [query, setQuery] = useState("");

  // One effect fills the panel (empty = all, typed = transcript matches); debounced, and a stale in-flight response is dropped.
  const q = query.trim();
  useDebounce((stale) => {
    listSessions(q, project).then((d) => { if (!stale()) setSessions(d); });
  }, q ? 150 : 0, [query, project]);

  return (
    <div className="sessions-panel">
      <input
        className="session-search" autoFocus value={query}
        placeholder="search past chats — yours and the terminal's"
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") { e.stopPropagation(); onClose(); }
        }}
      />
      <div className="sessions-list">
        {sessions?.length === 0 && (
          <p className="status">
            {query.trim()
              ? `No past chat mentions "${query.trim()}".`
              : "No past sessions found for this vault."}
          </p>
        )}
        {(sessions || []).map((s) => (
          <button key={s.id} className="session-row" onClick={() => onResume(s.id)}
            title="Resume it · ⌘-drag onto the prompt to attach a digest instead"
            {...dragPin(`session · ${clip(s.preview || s.id.slice(0, 8))}`, sessionPin(s.id, project))}>
            <span className="session-id">{s.id.slice(0, 8)}</span>
            <span className="session-body">
              <span className="session-preview">{s.preview}</span>
              {(s.matches || []).map((m, i) => (
                <span key={i} className="session-match">
                  <span className="match-who">{SPEAKER[m.kind] || m.kind}</span>
                  {m.snippet}
                </span>
              ))}
              {s.match_count > (s.matches || []).length && (
                <span className="session-match more">
                  +{s.match_count - s.matches.length} more in this chat
                </span>
              )}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Standby: an idle console offers recent work instead of a void; mounted only while the log is idle. */
export function RecentSessions({ onResume, project = null }) {
  const [recent, setRecent] = useState([]);

  useEffect(() => {
    let live = true;
    listSessions("", project).then((d) => { if (live) setRecent(d.slice(0, 4)); });
    return () => { live = false; };
  }, [project]);

  if (!recent.length) return null;
  return (
    <div className="hello-recent">
      <div className="hello-recent-label">pick up where you left off</div>
      {recent.map((s) => (
        <button key={s.id} className="hello-session"
          onClick={() => onResume(s.id)}
          title={`Resume ${s.id.slice(0, 8)}`}>
          <span className="when">{ago(s.mtime)}</span>
          <span className="what">{s.preview}</span>
        </button>
      ))}
    </div>
  );
}
