import { useState } from "react";

// The muted line standing in for real-but-not-actionable rows (ended event, parcel next week).
// `above`: the folded rows come before what's live, so the line reads as a boundary.
// `open`: a fold hides the secondary part, never the whole; pass it when the folded rows are all the card has.
// Design: DESIGN.md → Folding a queue (`ui/Folded.jsx`).
export default function Folded({ summary, hideLabel = "hide", above, open: startOpen = false, children }) {
  const [open, setOpen] = useState(startOpen);
  // Line first as a boundary, rows first as a footer.
  const line = (
    <button className={`widget-footer-btn${above ? " folded" : ""}`}
      aria-expanded={open} onClick={() => setOpen((v) => !v)}>
      {open ? hideLabel : summary}
    </button>
  );
  return above
    ? <>{line}{open && children}</>
    : <>{open && children}{line}</>;
}
