import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// The console bar's ⋯: the set-and-leave controls (effort, style, voice, MCP)
// and the session actions (docs, resume, new), in a menu under the button so
// the bar stays one row. Portalled to <body> and placed `fixed` from the
// button's rect: the bar lives in the stage sheet, which clips (overflow and
// a size container), and at its 40px peek a menu inside it would be cut off.
//
// `open` / `onClose` are the caller's; any click outside, Escape, a scroll or
// a resize closes it. `anchor` is the ⋯ button.
const MENU_ROOM = 340;   // about the tallest menu here (the model picker)

export default function BarMenu({ open, onClose, anchor, children }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);

  useLayoutEffect(() => {
    if (!open || !anchor) return;
    const r = anchor.getBoundingClientRect();
    const right = Math.max(8, window.innerWidth - r.right);
    // Under the button, unless the button is low on the page (a reply's
    // "retry with…" at the foot of the log): then above it.
    const below = window.innerHeight - r.bottom;
    setPos(below < MENU_ROOM && r.top > below
      ? { bottom: window.innerHeight - r.top + 4, right }
      : { top: r.bottom + 4, right });
  }, [open, anchor]);

  useEffect(() => {
    if (!open) return;
    const away = (e) => {
      if (ref.current?.contains(e.target) || anchor?.contains(e.target)) return;
      onClose();
    };
    const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); anchor?.focus(); } };
    const gone = () => onClose();
    document.addEventListener("pointerdown", away, true);
    document.addEventListener("keydown", key, true);
    window.addEventListener("resize", gone);
    return () => {
      document.removeEventListener("pointerdown", away, true);
      document.removeEventListener("keydown", key, true);
      window.removeEventListener("resize", gone);
    };
  }, [open, onClose, anchor]);

  if (!open || !pos) return null;
  return createPortal(
    <div className="bar-menu" role="menu" ref={ref} style={pos}>
      {children}
    </div>,
    document.body,
  );
}
