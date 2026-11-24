import { useEffect, useRef, useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { UNDO_MS, dismissUndo, runUndo, useUndo } from "../lib/undo";

// The undo toast (lib/undo.js): 8s, paused while hovered/focused/hidden; ⌘Z works unless a field can undo itself.
export default function UndoToast() {
  const entry = useUndo();
  const [paused, setPaused] = useState(false);
  const left = useRef(UNDO_MS);
  const startedAt = useRef(0);
  const timer = useTimeout();
  const hover = useRef(false);

  // A fresh offer starts a fresh window.
  useEffect(() => {
    left.current = UNDO_MS;
    hover.current = false;
    setPaused(document.hidden);
  }, [entry?.id]);

  useEffect(() => {
    if (!entry) return;
    timer.clear();
    if (paused) return;
    startedAt.current = performance.now();
    timer.start(() => dismissUndo(entry.id), left.current);
    return () => {
      timer.clear();
      left.current -= performance.now() - startedAt.current;
    };
  }, [entry, paused, timer]);

  useEffect(() => {
    const onVis = () => setPaused(document.hidden || hover.current);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  useEffect(() => {
    if (!entry) return;
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.key.toLowerCase() !== "z") return;
      const t = e.target;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable) return;
      e.preventDefault();
      runUndo();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [entry]);

  if (!entry) return null;
  const hold = (on) => { hover.current = on; setPaused(on || document.hidden); };
  return (
    <div className={`undo-toast${paused ? " paused" : ""}`} role="status"
      style={{ "--undo-ms": `${UNDO_MS}ms` }}
      onPointerEnter={() => hold(true)}
      onPointerLeave={(e) => { if (!e.currentTarget.contains(document.activeElement)) hold(false); }}
      onFocus={() => hold(true)}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) hold(false); }}>
      <span className="undo-label">{entry.label}</span>
      <button onClick={runUndo}>Undo <kbd>⌘Z</kbd></button>
      <button className="undo-x" onClick={() => dismissUndo(entry.id)} aria-label="Dismiss">×</button>
      <i key={entry.id} className="undo-bar" aria-hidden="true" />
    </div>
  );
}
