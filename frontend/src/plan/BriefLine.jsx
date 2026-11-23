import { useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { postJSON } from "../lib/api";
import { useLoad } from "../lib/refresh";
import { dragPin } from "../lib/drag.js";

// The brief bar: one line that says only what the "needs you" list can't —
// how its rows bear on each other, and tomorrow's first thing
// (POST /api/brief/line, cached per day and per set of asks). Hidden when
// there's nothing to add. The full morning brief is still written each day
// for the /daily-brief skill, and opens from here as its own view.

// Long enough that a burst of changes (a snooze, then its settle) is one ask.
const DEBOUNCE_MS = 1500;

// A reason line ends in an age ("waiting 27 h", "7 h ago", "in 12 min") that
// changes on its own. Left in, every hour would be a "new" list and a new
// model call for the same rows; the line is about how rows relate, not how
// old they are, so the ages come out before it's asked.
const AGE = /\s*·?\s*(?:waiting|asked|open|in|due)?\s*\d+(?:\.\d+)?\s*(?:min|h|d)\b(?:\s*(?:ago|overdue|late))?/g;

export default function BriefLine({ asks, onPin, onFull }) {
  const [state, setState] = useState(null);   // { line } | { error }
  const [open, setOpen] = useState(false);
  // Not the console's own rows (`console:` ids: an approval card, a question,
  // tabs the limit stopped). They last seconds and are already on screen, and
  // each one was a new set of asks, so a model call: about 250 of the
  // ~530 background calls in the week to 11-17 (New Features #53).
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
