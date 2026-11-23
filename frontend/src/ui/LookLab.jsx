import { useCallback, useEffect, useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { fetchJSON, postJSON } from "../lib/api.js";
import { anyLookById, flipLook, removeTheme, setAxis, setGround, setLayout, setLook, setLookPair, stepGround,
         useLook } from "../lib/look.js";
import { isDark } from "../lib/theme.js";
import { AGENT_STYLES, GROUNDS, LAYOUTS, LOOKS, ROUND, agentsById, groundById, layoutById,
         lookById } from "../looks/index.js";
import { nodeForDetail } from "../lib/turn.js";
import { startReplay } from "../plan/AgentLayer.jsx";

// The look lab: pick UI design variants, pin two layouts to flip between
// (⌥⇧L), step the map's ground (⌥⇧M), and say what you think of what's on
// screen. Verdicts go to app.db (/api/looks) for the next redesign round to
// read. Opens from ⌘K ("Look lab"); on the classic desk a small chip at the
// bottom right says which variants are on, flips back, and reopens this.
//
// Three axes since round 4 (looks/index.js): layout, map ground, colour. A
// vote is about one of them, picked under "Your verdict"; layouts and grounds
// are stored prefixed (`layout-rooms`, `ground-dots`).

const AXES = {
  layout: { label: "Layout", byId: layoutById, key: (id) => `layout-${id}` },
  ground: { label: "Map ground", byId: groundById, key: (id) => `ground-${id}` },
  look: { label: "Colour", byId: anyLookById, key: (id) => id },   // a theme's id is its vote key too
  agents: { label: "Agents", byId: agentsById, key: (id) => `agents-${id}` },
};

export function LookChip({ onOpen }) {
  const { look, layout, ground } = useLook();
  // Only on the fallback desk, as its way back. On the rooms desk it sat on
  // the composer's send button whenever a ground or colour variant was on.
  if (layout === LAYOUTS[0].id) return null;
  const on = [layoutById(layout).name,
              ground !== GROUNDS[0].id && groundById(ground).name,
              look !== "baseline" && anyLookById(look).name].filter(Boolean);
  if (!on.length) return null;
  return (
    <div className="look-chip" role="status">
      <button onClick={onOpen} title="Open the look lab">Look · {on.join(" · ")}</button>
      <button onClick={flipLook} title="Flip layout A ⇄ B (⌥⇧L)">A⇄B</button>
      <button onClick={() => stepGround(1)} title="Next map ground (⌥⇧M)">Map ›</button>
    </div>
  );
}

export default function LookLab({ onClose, view }) {
  // Focus moves in on open, so Esc (handled on the dialog) works without a
  // click first; from ⌘K it was ignored until something in the lab was clicked.
  const grabFocus = useCallback((el) => el?.focus(), []);
  const { look, layout, ground, pair, agents, themes } = useLook();
  const onTheme = themes.find((t) => t.id === look);
  const [note, setNote] = useState("");
  const [axis, setVoteAxis] = useState("layout");   // what the verdict is about (not lib/look's setAxis)
  const [votes, setVotes] = useState({});
  const [saved, setSaved] = useState(null);
  const savedFade = useTimeout();
  const load = () => fetchJSON("/api/looks").then((r) => setVotes(Object.fromEntries(
    (r.looks || []).map((s) => [s.look, s])))).catch(() => {});
  useEffect(() => { load(); }, []);

  // The agent visualizer's replay: a recent session's tool calls, mapped to
  // the places they touch, handed to plan/AgentLayer.jsx.
  const [sessions, setSessions] = useState(null);
  const [loadingReplay, setLoadingReplay] = useState(null);
  const [replayNote, setReplayNote] = useState({});
  const pickReplay = () => (sessions ? setSessions(null)
    : fetchJSON("/api/sessions").then((r) => setSessions((Array.isArray(r) ? r : []).slice(0, 14)))
      .catch(() => setSessions([])));
  async function replay(s) {
    setLoadingReplay(s.id);
    try {
      const r = await fetchJSON(`/api/sessions/${encodeURIComponent(s.id)}/transcript`);
      const calls = (r.entries || []).filter((e) => e.kind === "tool")
        .map((e) => ({ name: e.name, detail: e.detail || "", node: nodeForDetail(e.name, e.detail) }))
        .filter((c) => c.node);
      if (!calls.length) { setReplayNote((n) => ({ ...n, [s.id]: "no calls that touch anything" })); return; }
      if (agents === "off") setAxis("agents", "orbs");
      startReplay((s.preview || "a session").slice(0, 48), calls);
      onClose();
    } catch (e) {
      setReplayNote((n) => ({ ...n, [s.id]: `couldn't load: ${e.message}` }));
    } finally { setLoadingReplay(null); }
  }

  const onScreen = { layout, ground, look, agents };
  const target = onScreen[axis];
  const tally = (key) => { const v = votes[key]; return v ? `${v.yes}↑ ${v.no}↓` : ""; };

  async function vote(verdict) {
    if (!verdict && !note.trim()) return;
    const other = axis === "layout" && pair.includes(layout) ? pair.find((p) => p !== layout) : null;
    await postJSON("/api/looks/votes", { look: AXES[axis].key(target), verdict, note: note.trim(),
      against: other ? AXES.layout.key(other) : null,
      // what else was on screen, so a layout's vote says which ground and colour
      // it was seen with: layout/ground/colour first, as the column holds 60
      view: [`${layout}/${ground}/${look}`, view].filter(Boolean).join(" · ").slice(0, 60) });
    setNote("");
    setSaved(verdict > 0 ? "Kept: a yes" : verdict < 0 ? "Kept: a no" : "Note kept");
    savedFade.start(() => setSaved(null), 1800);
    load();
  }

  const current = layoutById(layout);
  const notes = votes[AXES[axis].key(target)]?.notes || [];
  return (
    <div className="look-lab ll4" role="dialog" aria-label="Look lab" tabIndex={-1} ref={grabFocus}
         onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
      <div className="ll-head">
        <b>Look lab</b><span className="ll-round">round {ROUND}</span>
        <button className="ll-x" onClick={onClose} aria-label="Close">×</button>
      </div>
      <p className="ll-help">Rooms is the desk since 11-22; Classic is kept here as a fallback (⌥⇧L flips them).
        Pick how agents are drawn, or replay a past session to see them.</p>

      <div className="ll-sec">Layout</div>
      <ul className="ll-list" role="radiogroup" aria-label="Layout on screen">
        {LAYOUTS.map((l) => {
          const on = l.id === layout;
          return (
            <li key={l.id} className={on ? "on" : ""}>
              <button className="ll-pick" role="radio" aria-checked={on} onClick={() => setLayout(l.id)}
                title={on ? "On screen now" : `Put ${l.name} on screen`}>
                <span className="ll-dot" aria-hidden="true" />
                <span className="ll-text">
                  <span className="ll-name">{l.name}{on && <span className="ll-badge">On screen</span>}</span>
                  <span className="ll-q">{l.question}</span>
                </span>
              </button>
              <span className="ll-ab">
                {["A", "B"].map((slot, i) => (
                  <button key={slot} className={pair[i] === l.id ? "on" : ""} aria-pressed={pair[i] === l.id}
                    onClick={() => { const p = [...pair]; p[i] = l.id; setLookPair(p); }}
                    title={pair[i] === l.id ? `Pinned as ${slot}` : `Pin as ${slot} for the flip`}>{slot}</button>
                ))}
              </span>
              <span className="ll-tally" title="yes / no so far">{tally(AXES.layout.key(l.id))}</span>
            </li>
          );
        })}
      </ul>
      <p className="ll-cuts"><b>What {current.name.split(" (")[0]} cuts:</b> {current.cuts}</p>
      <div className="ll-flip">
        <span className="ll-seg" role="group" aria-label="Compare A and B">
          {pair.map((id, i) => (
            <button key={i} className={id === layout ? "on" : ""} aria-pressed={id === layout}
              onClick={() => setLayout(id)} title={`Show ${"AB"[i]}`}>
              <b>{"AB"[i]}</b> {layoutById(id).name.split(" (")[0]}
            </button>
          ))}
        </span>
        <span className="ll-key" title="Flip A ⇄ B from anywhere">⌥⇧L</span>
      </div>

      <div className="ll-sec">Agents <span className="ll-key">⌥⇧A steps</span></div>
      <div className="ll-chips" role="radiogroup" aria-label="Agent visualizer">
        {AGENT_STYLES.map((t) => (
          <button key={t.id} role="radio" aria-checked={t.id === agents} className={t.id === agents ? "on" : ""}
            onClick={() => setAxis("agents", t.id)} title={t.question}>
            <span>{t.name}</span><span className="ll-tally">{tally(AXES.agents.key(t.id))}</span>
          </button>
        ))}
        <button onClick={pickReplay} title="Play a past session's tool calls through the visualizer">
          <span>Replay a session…</span></button>
      </div>
      <p className="ll-cuts">{agentsById(agents).question}</p>
      {sessions && (
        <ul className="ll-sessions">
          {sessions.length === 0 && <li className="ll-q">No sessions found.</li>}
          {sessions.map((s) => (
            <li key={s.id}><button onClick={() => replay(s)} disabled={loadingReplay === s.id}>
              <span className="ll-sess-when">{new Date(s.mtime * 1000).toLocaleDateString([], { month: "short", day: "numeric" })}</span>
              <span className="ll-sess-what">{loadingReplay === s.id ? "loading…" : s.preview || "(untitled)"}</span>
              {replayNote[s.id] && <span className="ll-q">{replayNote[s.id]}</span>}
            </button></li>
          ))}
        </ul>
      )}

      <div className="ll-sec">Map ground <span className="ll-key">⌥⇧M steps</span></div>
      <div className="ll-chips" role="radiogroup" aria-label="Map ground">
        {GROUNDS.map((g) => (
          <button key={g.id} role="radio" aria-checked={g.id === ground} className={g.id === ground ? "on" : ""}
            onClick={() => setGround(g.id)} title={g.question}>
            <span className={`ll-swatch g-${g.id}`} aria-hidden="true" />
            <span>{g.name}</span>
            <span className="ll-tally">{tally(AXES.ground.key(g.id))}</span>
          </button>
        ))}
      </div>
      <p className="ll-cuts">{groundById(ground).question}</p>

      <div className="ll-sec">Colour <span className="ll-key">round 3</span></div>
      <div className="ll-chips" role="radiogroup" aria-label="Colour">
        {LOOKS.map((l) => (
          <button key={l.id} role="radio" aria-checked={l.id === look} className={l.id === look ? "on" : ""}
            onClick={() => setLook(l.id)} title={l.question}>
            <span>{l.name}</span>
            <span className="ll-tally">{tally(l.id)}</span>
          </button>
        ))}
        {themes.map((t) => (
          <button key={t.id} role="radio" aria-checked={t.id === look} className={t.id === look ? "on" : ""}
            onClick={() => setLook(t.id)} title={t.note || "A theme Kestrel made"}>
            <span className="ll-swatch ll-theme-swatch" aria-hidden="true"
              style={{ background: t.dark.surface || t.dark.desk || "var(--paper-raise)", color: t.dark.accent || "var(--accent)" }} />
            <span>{t.name}</span>
            <span className="ll-tally">{tally(t.id)}</span>
          </button>
        ))}
      </div>
      <p className="ll-cuts">
        {onTheme ? (
          <>{onTheme.note || "A theme Kestrel made."}{" "}
            <a href={`#specimen/${isDark() ? "dark" : "light"}/${onTheme.id}`} target="_blank" rel="noreferrer">Specimen</a>
            {" · "}<button className="ll-link" onClick={() => removeTheme(onTheme.id)}>Remove it</button></>
        ) : "Ask Kestrel for colours (\"make it warmer\") and the theme lands here: colour roles only, contrast-checked, and it has to look at a screenshot before it answers."}
      </p>

      <div className="ll-vote">
        <div className="ll-on">Your verdict on the
          <span className="ll-seg ll-axis" role="group" aria-label="What the verdict is about">
            {Object.entries(AXES).map(([k, a]) => (
              <button key={k} className={axis === k ? "on" : ""} aria-pressed={axis === k}
                onClick={() => setVoteAxis(k)}>{a.label}</button>
            ))}
          </span>
          <b>{AXES[axis].byId(target).name}</b>
        </div>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3}
          placeholder="What works, what doesn't, what to keep from it…" />
        <div className="ll-btns">
          <button className="go" onClick={() => vote(1)}>Like it</button>
          <button onClick={() => vote(-1)}>Not it</button>
          <button onClick={() => vote(0)} disabled={!note.trim()}>Just the note</button>
          {saved && <span className="ll-saved">{saved}</span>}
        </div>
      </div>
      {notes.length > 0 && (
        <ul className="ll-notes">
          {notes.slice(0, 4).map((n) => (
            <li key={n.id}><span>{n.verdict > 0 ? "↑" : n.verdict < 0 ? "↓" : "·"}</span> {n.note || "(no note)"}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
