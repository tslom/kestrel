import { useState } from "react";

// The muted line a card shows in place of rows that are real but not
// actionable now: an event that already ended, office hours that have run
// out, a parcel that lands next week.
//
// The measurement cards got `Lede` — one reading, the rest compressed. Queue
// cards can't collapse the same way, because you work them: a task you can't
// see is a task you can't tick. What they can do is stop spending equal height
// on the part of the queue that has already happened or hasn't come near yet.
// So the rows don't disappear, they fold: one line saying how many, one click
// to get them back.
//
// `above` is for the cards where the folded rows belong *before* what's live
// (a day's earlier events sit above its next one), so the line reads as a
// boundary rather than as a footer.
//
// `open` is where it starts. A fold hides the secondary part of a card, never
// the whole of it: a card whose folded rows are all it has (three trips
// booked, nothing landing today) passes `open`, or the pane shows a count and
// nothing to read.
export default function Folded({ summary, hideLabel = "hide", above, open: startOpen = false, children }) {
  const [open, setOpen] = useState(startOpen);
  // Line first when it's a boundary, rows first when it's a footer — so the
  // rows always land on the side of the line the card's order implies.
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
