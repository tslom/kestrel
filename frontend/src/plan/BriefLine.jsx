import { useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { postJSON } from "../lib/api";
import { useLoad } from "../lib/refresh";
import { dragPin } from "../lib/drag.js";

// The brief bar: one line saying only what the list can't (how rows bear on each other, tomorrow's
// first thing; POST /api/brief/line, cached per day and set of asks); hidden if nothing to add.
// The full morning brief (for /daily-brief) opens from here.

// A burst of changes (a snooze, then its settle) is one ask.
const DEBOUNCE_MS = 1500;

// Strip the trailing age ("waiting 27 h", "in 12 min") from reasons: left in, every hour is a new list and model call.
const AGE = /\s*·?\s*(?:waiting|asked|open|in|due)?\s*\d+(?:\.\d+)?\s*(?:min|h|d)\b(?:\s*(?:ago|overdue|late))?/g;

export default function BriefLine({ asks, onPin, onFull }) {
  const [state, setState] = useState(null);   // { line } | { error }
  const [open, setOpen] = useState(false);
  // Not `console:` rows: they last seconds and were about half the background calls (#53).
  const payload = asks.filter((a) => !String(a.id || "").startsWith("console:"))
    .map((a) => ({ title: a.title, why: (a.why || "").replace(AGE, "").trim(),
                   card: a.card || null }));
  const sig = JSON.stringify(payload);
  const [epoch, setEpoch] = useState(0);
  const latest = useRef(sig);
  latest.current = sig;

  // Also on the refresh bus: the day rolling over means a new line.
  useLoad(() => setEpoch((n) => n + 1));

  useDebounce(() => {
    const sent = sig;
    postJSON("/api/brief/line", { asks: JSON.parse(sent) })
      .then((r) => { if (latest.current === sent) setState(r.error ? { error: r.error } : { line: r.line }); })
      .catch((e) => { if (latest.current === sent) setState({ error: e.message }); });
  }, DEBOUNCE_MS, [sig, epoch]);

  if (!state) return null;
  if (state.error) {
    return (
      <div className="brief-line failed" title={state.error}>
        <span className="ai-tag">AI</span>
        <span className="brief-text">The brief line couldn't be written.</span>
        <button className="side-link" onClick={() => setEpoch((n) => n + 1)}>retry</button>
        <button className="side-link" onClick={onFull}>morning brief</button>
      </div>
    );
  }
  if (!state.line) return null;
  return (
    <div className="brief-line" {...dragPin("brief · today", { line: state.line })}>
      <span className="ai-tag" title="Written by the model from today's list">AI</span>
      <button className={`brief-text${open ? " open" : ""}`} onClick={() => setOpen((v) => !v)}
        title={open ? "Collapse" : state.line}>{state.line}</button>
      <button className="side-link" onClick={onFull} title="The full morning brief">morning brief</button>
      <button className="side-link" onClick={() => onPin("brief · today", { line: state.line })}
        title="Attach to your next console message">pin</button>
    </div>
  );
}
