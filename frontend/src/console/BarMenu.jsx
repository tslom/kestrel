import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// The console bar's ⋯: set-and-leave controls (effort, style, voice, MCP) and session actions, keeping
// the bar one row. Portalled to <body>, `fixed` from the button's rect (the stage sheet clips, and at
// its 40px peek an inside menu is cut off). `open`/`onClose` are the caller's; outside click, Escape,
// scroll or resize closes it; `anchor` is the ⋯ button.
const MENU_ROOM = 340;   // about the tallest menu here (the model picker)

export default function BarMenu({ open, onClose, anchor, children }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);

  useLayoutEffect(() => {
    if (!open || !anchor) return;
    const r = anchor.getBoundingClientRect();
    const right = Math.max(8, window.innerWidth - r.right);
    // Under the button, above it when the button is low (a reply's "retry with…").
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
