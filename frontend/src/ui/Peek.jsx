// Hover preview of what a reference points at; clicks stay with the child.
// `load` runs once the pointer settles (not on render: most refs are never hovered), returns what to
// show or null, and should cache (lib/cite.js does); this re-asks every hover.
// Portaled, position:fixed (panes would clip it), no pointer events, leaves on any scroll.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { createPortal } from "react-dom";

const SETTLE_MS = 180;
const MARGIN = 10;
const GAP = 6;

export default function Peek({ load, off = false, children }) {
  const wrap = useRef(null);
  const card = useRef(null);
  const settle = useTimeout();
  const asked = useRef(0);       // a slow answer for a hover that ended is dropped
  const [body, setBody] = useState(null);
  const [at, setAt] = useState(null);

  const hide = useCallback(() => {
    settle.clear();
    asked.current += 1;
    setBody(null);
    setAt(null);
  }, [settle]);

  const show = () => {
    if (off) return;
    const mine = ++asked.current;
    settle.start(() => {
      Promise.resolve(load())
        .then((node) => { if (mine === asked.current && node) setBody(node); })
        .catch(() => {});
    }, SETTLE_MS);
  };

  // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- off comes from the parent, and hiding cancels the pending settle timer, which can't happen during render
  useEffect(() => { if (off) hide(); }, [off, hide]);
  useEffect(() => () => { asked.current += 1; }, []);

  // Under the reference, or over it with no room below; measured from boxes this doesn't move.
  useLayoutEffect(() => {
    if (!body) return;
    const a = wrap.current.getBoundingClientRect();
    const { offsetWidth: w, offsetHeight: h } = card.current;
    const below = a.bottom + GAP;
    const top = below + h > window.innerHeight - MARGIN ? a.top - GAP - h : below;
    const left = Math.max(MARGIN, Math.min(a.left, window.innerWidth - MARGIN - w));
    setAt({ top: Math.max(MARGIN, top), left });
  }, [body]);

  useEffect(() => {
    if (!body) return;
    window.addEventListener("scroll", hide, true);
    return () => window.removeEventListener("scroll", hide, true);
  }, [body, hide]);

  return (
    <span className="peek-wrap" ref={wrap}
      onMouseEnter={show} onMouseLeave={hide} onMouseDown={hide}
      onFocus={(e) => { if (e.target.matches?.(":focus-visible")) show(); }}
      onBlur={hide}>
      {children}
      {body && createPortal(
        <div className="peek-card" role="tooltip" ref={card}
          style={at ? at : { visibility: "hidden", top: 0, left: 0 }}>
          {body}
        </div>,
        document.body)}
    </span>
  );
}
