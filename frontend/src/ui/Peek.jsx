// What a reference points at, shown on hover, so "f997" or a citation glyph
// can be read without opening anything. Clicks stay with what's inside: a pill
// still opens its card, a note link still opens Obsidian.
//
// `load` runs once the pointer has settled, not on render: an answer can carry
// a dozen references and most are never hovered. It returns what to show, or
// null for nothing. It should cache, as lib/cite.js does; this re-asks on every
// hover.
//
// Portaled and position:fixed because references sit inside scrolling panes
// that would clip a card. It takes no pointer events, so it can't sit between
// the pointer and the text under it, and it leaves on any scroll rather than
// float away from its anchor.
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

  // Under the reference, or over it when there's no room below. Measured from
  // the anchor's box and the card's offset size, neither of which this moves.
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
