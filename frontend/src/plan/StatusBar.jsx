import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import Icon from "../ui/icons.jsx";
import UsageChips from "../console/UsageChips.jsx";
import BarMenu from "../console/BarMenu.jsx";
import { answerConsole } from "../lib/turn.js";
import { BarHostContext, fitBar, folded } from "../lib/barHost.js";

// The console sheet's head (at peek, all you see): state, current action, time, cost, usage chip; on an
// approval it turns amber with Yes / No. A few hundred px wide, with the showing tab's controls
// portalled in, so it folds by priority (lib/barHost.js CUT_ORDER) into ⋯ (HostItems); nothing clips.

// `mcp__vault__task_update` → `task_update`; `mcp__claude_ai_Gmail__x` → `Gmail x`.
export function toolLabel(name) {
  const m = /^mcp__(?:claude_ai_)?([^_]+(?:_[A-Z][a-z]+)*)__(.+)$/.exec(name || "");
  if (!m) return name;
  return m[1] === "vault" ? m[2] : `${m[1].replace(/_/g, " ")} ${m[2]}`;
}

export const secs = (ms) => (ms == null ? "" : ms < 60000 ? `${(ms / 1000).toFixed(1)} s`
  : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`);
const compact = (n) => (n < 1000 ? String(n) : n < 100000 ? `${(n / 1000).toFixed(1)}k`
  : `${Math.round(n / 1000)}k`);

// The status bar's own controls, as ⋯ items, for the ones the bar folded.
// `host` is BarHostContext's ref; `done` closes the menu.
export function HostItems({ cut, host, done }) {
  const h = host?.current || {};
  const items = [];
  if (h.hasThumb ? folded(cut, "thumb") : folded(cut, "view"))
    items.push({ key: "view", label: "Claude's view", on: h.viewing, run: h.onView,
                 title: "Claude's view, and a preview of what a dev tab is building" });
  if (folded(cut, "timeline") && h.hasTimeline)
    items.push({ key: "timeline", label: "timeline", on: h.timeline, run: h.onTimeline,
                 title: "Every call of the turn on one time axis" });
  if (folded(cut, "full"))
    items.push({ key: "full", label: "full screen", kbd: "⌘F", run: h.onFull, title: "Full screen · Esc returns" });
  if (folded(cut, "new"))
    items.push({ key: "new", label: "new tab", kbd: "⌥T", run: h.onNewTab, disabled: !h.canNew,
                 title: h.canNew ? "New tab" : "Every tab slot is taken" });
  return items.map((it) => (
    <button key={it.key} role="menuitem" className={`bm-item${it.on ? " on" : ""}`} disabled={it.disabled}
      title={it.title} onClick={() => { done?.(); it.run?.(); }}>
      {it.label}{it.kbd && <kbd className="bm-kbd">{it.kbd}</kbd>}
    </button>
  ));
}

// `tabs`/`newTab`: tab strip and "+" at the bar's ends; the rest is the showing tab's. `barSlot`: ref
// setter for the console's own bar. `onCut`: the folded CUT_ORDER string for its ⋯. `ownMenu`: no
// console bar yet (tab picking a mode), so folded items get a ⋯ here.
export default function StatusBar({ trace, sheet, unread, onToggle, onFull, onTimeline, timeline, onFailed,
                                    onView, viewing, tabs, newTab, barSlot, onCut, ownMenu = false,
                                    cutoff = null }) {
  // A live clock while a turn runs; nothing ticks at rest.
  const [, tick] = useState(0);
  useEffect(() => {
    if (!trace.running) return;
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [trace.running]);

  // Fold to fit before paint, on resize and any row change (portalled bar included); coalesced to a frame.
  const barRef = useRef(null);
  const cutRef = useRef("");
  const onCutRef = useRef(onCut);
  onCutRef.current = onCut;
  const [cut, setCut] = useState("");
  useLayoutEffect(() => {
    const bar = barRef.current;
    if (!bar) return undefined;
    let frame = 0;
    const fit = () => {
      frame = 0;
      const next = fitBar(bar);
      if (next !== cutRef.current) {
        cutRef.current = next;
        setCut(next);
        onCutRef.current?.(next);
      }
    };
    fit();
    const soon = () => { if (!frame) frame = requestAnimationFrame(fit); };
    const ro = new ResizeObserver(soon);
    ro.observe(bar);
    const mo = new MutationObserver(soon);
    mo.observe(bar, { subtree: true, childList: true, characterData: true });
    document.fonts?.ready?.then(soon);
    return () => { cancelAnimationFrame(frame); ro.disconnect(); mo.disconnect(); };
  }, []);
  const host = useContext(BarHostContext)?.host;
  const [menu, setMenu] = useState(false);
  const [menuEl, setMenuEl] = useState(null);
  const ownItems = ownMenu && ["view", "thumb", "timeline", "full", "new"].some((k) => folded(cut, k));

  const w = trace.waiting;
  const seen = trace.views?.at(-1);
  // A cut-off tab says so here too (its banner is in the conversation, hidden when folded).
  const stopped = !w && !trace.running ? cutoff : null;
  const state = w ? "waiting" : trace.writing ? "writing" : trace.running ? "working"
    : stopped ? "cutoff" : trace.last?.error ? "error" : unread ? "unread" : "idle";
  const word = { waiting: "waiting on you", writing: "writing", working: "working", cutoff: stopped?.word,
                 error: "error", unread: "replied", idle: "idle" }[state];
  const current = [...trace.calls].reverse().find((c) => c.state === "live");
  const elapsed = trace.running && trace.startedAt != null
    ? performance.now() - trace.startedAt : trace.last?.duration_ms;
  const failed = !trace.running ? trace.last?.failed || 0 : 0;

  // A click on the bar raises or lowers the sheet; buttons inside it don't.
  const onBar = (e) => { if (!e.target.closest("button, a, select, .usage-chip, .st-tabs, .st-console")) onToggle(); };

  return (
    <div ref={barRef} className={`status-bar ${state}`} onClick={onBar}
      onDoubleClick={(e) => { if (!e.target.closest("button, select, .st-console")) onFull(); }}
      title={sheet === "peek" ? "Show the conversation · ⌘J · ⌘F or double-click for full screen" : undefined}>
      {tabs}
      <span className={`st-dot ${state}`} />
      <span className="st-word">{word}</span>
      {seen && (
        <button className={`st-view${viewing ? " on" : ""}`} onClick={onView} data-cut="thumb"
          title={`Claude's view${seen.detail ? `: ${seen.detail}` : ""}`}
          aria-label="Claude's view" aria-pressed={viewing}>
          <img src={seen.src} alt="" />
        </button>
      )}
      <span className="st-action" title={stopped ? stopped.text : w?.detail ? w.detail : current?.detail ? current.detail : !trace.running && trace.last?.tokens_in != null
        ? `tokens in (including cache reads) ${compact(trace.last.tokens_in)} · out ${compact(trace.last.tokens_out || 0)}`
        : undefined}>
        {stopped ? stopped.text : w ? (
          <><b>{w.question ? "a question" : toolLabel(w.name)}</b>{w.detail ? ` — ${w.detail}` : ""}</>
        ) : trace.writing ? "writing the reply…"
          : current ? <><b>{toolLabel(current.name)}</b> {current.detail}</>
          : trace.running ? "thinking…"
          : trace.last ? <>{unread && <><b>new reply</b> · ⌘J to read · </>}last turn {secs(trace.last.duration_ms)}
              {failed > 0 && (
                <> · <button className="st-link" onClick={onFailed}>{failed} failed</button></>
              )}</>
          : null}
      </span>
      {w && !w.question && (
        <span className="st-acts">
          <button className="yes" onClick={() => answerConsole(w.requestId, true)}>Yes</button>
          <button onClick={() => answerConsole(w.requestId, false)}>No</button>
        </span>
      )}
      {w?.question && (
        <span className="st-acts"><button className="yes" onClick={onToggle}>Answer</button></span>
      )}
      {stopped && (stopped.resume || stopped.dismiss) && (
        <span className="st-acts">
          {stopped.resume && <button className="yes" onClick={stopped.resume} title={stopped.resumeTitle}>Resume</button>}
          {stopped.dismiss && <button onClick={stopped.dismiss} title="Leave it as it is">Dismiss</button>}
        </span>
      )}
      {/* At rest the action line already says how long the turn took. */}
      {trace.running && elapsed != null && <span className="st-meta" data-cut="elapsed">{secs(elapsed)}</span>}
      <UsageChips rateLimits={trace.rate} rotate />
      {!seen && (
        <button className={`st-btn${viewing ? " on" : ""}`} onClick={onView} data-cut="view"
          title="Claude's view, and a preview of what a dev tab is building">view</button>
      )}
      {trace.calls.length > 0 && (
        <button className={`st-btn${timeline ? " on" : ""}`} onClick={onTimeline} data-cut="timeline"
          title="Every call of the turn on one time axis">timeline</button>
      )}
      {/* the showing tab's controls, portalled in by console/Console.jsx (one row of chrome) */}
      <span className="st-console" ref={barSlot} />
      {ownItems && (
        <>
          <button ref={setMenuEl} className={`st-icon${menu ? " on" : ""}`} aria-haspopup="menu"
            aria-expanded={menu} onClick={() => setMenu((v) => !v)} title="What the bar has no room for">⋯</button>
          <BarMenu open={menu} anchor={menuEl} onClose={() => setMenu(false)}>
            <div className="bm-acts"><HostItems cut={cut} host={host} done={() => setMenu(false)} /></div>
          </BarMenu>
        </>
      )}
      <span className="st-new" data-cut="new">{newTab}</span>
      <button className={`st-icon${sheet === "full" ? " on" : ""}`} onClick={onFull} data-cut="full"
        title={sheet === "full" ? "Leave full screen · ⌘F or Esc" : "Full screen · ⌘F · Esc returns"}
        aria-label={sheet === "full" ? "Leave full screen" : "Full screen"} aria-pressed={sheet === "full"}>
        <Icon name="expand" size={12} /></button>
      {sheet !== "full" && (
        <button className={`st-icon st-fold${sheet === "peek" ? " up" : ""}`} onClick={onToggle}
          aria-expanded={sheet !== "peek"}
          aria-label={sheet === "peek" ? "Open the conversation" : "Fold the conversation"}
          title={sheet === "peek" ? "Open the conversation · ⌘J" : "Fold the conversation to this bar · ⌘J"}>
          <Icon name="chevronDown" size={12} /></button>
      )}
    </div>
  );
}
