import { useEffect, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { fetchJSON } from "../lib/api.js";
import { dragPin } from "../lib/drag.js";
import { sessionPin } from "../lib/pills.js";

const clip = (s, n = 40) => { const t = s.replace(/\s+/g, " ").trim(); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };

// Past sessions, in the two places the console offers them: the resume panel
// under the bar (every session, or a transcript search), and the standby list
// in an idle log. Both read /api/sessions through the one loader below; they
// are open at different moments (the standby list is gone once a turn lands),
// and fetchJSON coalesces the rare load where both ask in the same tick.

// who said the matching line, for search results in the resume panel
const SPEAKER = { user: "you", text: "claude", tool: "tool" };

// Newest first, or the ones whose transcript mentions `q`. Never throws: a
// failed read is an empty list, which each place words for itself.
// `project`: a dev tab's repo, whose sessions are its own (the ones the
// terminal's `claude --resume` lists there); absent, the vault's.
function listSessions(q = "", project = null) {
  const p = project ? `project=${encodeURIComponent(project)}` : "";
  const url = q ? `/api/sessions/search?q=${encodeURIComponent(q)}${p ? `&${p}` : ""}`
    : `/api/sessions${p ? `?${p}` : ""}`;
  return fetchJSON(url)
    .then((d) => (Array.isArray(d) ? d : []))
    .catch(() => []);
}

// Age of a past session, for the standby list. Nothing else in the app
// formats a relative time — the "1d"s on the desk arrive precomputed from the
// backend — so this stays local rather than moving into time.js for one user.
function ago(mtime) {
  const mins = Math.max(0, (Date.now() - mtime * 1000) / 60000);
  if (mins < 60) return `${Math.round(mins)}m`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

/**
 * The resume panel. Mounted only while open, so closing it is what resets the
 * search box — the next open starts on the full list.
 */
export function SessionsPanel({ onResume, onClose, project = null }) {
  // null until the first read lands, so opening the panel doesn't flash "no
  // past sessions" at you while the list is still on its way.
  const [sessions, setSessions] = useState(null);
  const [query, setQuery] = useState("");

  // The list and the search share a row shape, so this one effect fills the
  // panel either way: empty box = every session, newest first; typed = the
  // ones whose transcript mentions it. Debounced, and the in-flight response
  // is dropped on the next keystroke so a slow one can't overwrite a fast one.
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

/**
 * Standby. An idle console is ~40% of the window holding one line, so it
 * offers the way back into recent work instead of a void. Mounted only while
 * the log is idle — the moment a real turn lands this stops being rendered.
 */
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
