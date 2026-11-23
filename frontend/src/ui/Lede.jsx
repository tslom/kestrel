import { deltaLine, restLine } from "../lib/notable";
import { dragPin } from "../lib/drag.js";

// The body of a measurement card: one reading, or none.
//
// The shape every card in this family now shares — health, screen, music,
// location, trends, money. What each of them used to render was an inventory
// (a four-cell grid, a top-N list, a sparkline, a median delta) laid out
// identically whether or not the day had anything in it, which put the job of
// finding the one number that moved onto the reader, every morning, twelve
// times over.
//
// Now: `lib/notable.js` picks the reading, this draws it, and on an ordinary
// day there is nothing to draw but the rest line. The numbers that don't lead
// haven't gone anywhere — they're in the sheet, and they're on the end of the
// card's pin, which is the surface that actually wants all of them. That's the
// division the desk was missing. The console reads data; you read conclusions.
//
// `rest` is the same string `Slot` shows when the card folds, and that is the
// point rather than a coincidence: a folded card and an open quiet card say
// the same thing, so folding is never hiding.
// `tail` is context that is never a candidate to lead: money's week total,
// which is a running sum rather than a reading with a baseline behind it. It
// rides the rest line instead of taking a second muted line of its own —
// two quiet lines on a quiet card is the clutter this form exists to remove.
export default function Lede({ hit, readings, label, tail }) {
  // The lede's own metric comes out of the line under it — it's already the
  // biggest thing on the card, and printing "resting hr 76 bpm" beneath a
  // 76 bpm headline is the card padding itself. `Slot`'s fold summary still
  // gets the unfiltered `restLine(readings)`, so the collapsed line stays the
  // same four numbers no matter which of them happened to lead that day; a
  // fold summary that changed shape with the weather would be unreadable at
  // a glance, which is the one thing it exists to be.
  const rest = restLine(hit ? readings.filter((r) => r.key !== hit.key) : readings);
  // Joined, not stacked: when the lede takes the card's only metric out of
  // `rest`, the tail is what's left, and it still reads as one line.
  const quiet = [rest, tail].filter(Boolean).join(" · ");
  return (
    <div className="lede-wrap">
      {hit && (
        // Pinned as one reading, not as the card: "resting HR is 4 bpm over
        // your median" is a question you ask on its own, and pinning the whole
        // card to ask it makes the console re-find what you were looking at.
        <div className={`lede${hit.tone ? ` tone-${hit.tone}` : ""}`}
          {...dragPin(`${label} · ${hit.label.toLowerCase()}`,
                      { reading: hit.key, value: hit.value, median: hit.median,
                        days: hit.n, delta: hit.delta })}>
          <div className="lede-head">
            <span className="lede-label">{hit.label}</span>
            <span className="lede-value">{hit.fmt(hit.value)}</span>
          </div>
          {/* Signed, in the metric's own units, against a median that names
              how many days it stands on. "vs typical" would be shorter and
              unauditable; this you can check. */}
          <div className="lede-delta" title={`${hit.asFar === 0
            ? "No other day in the window went this far"
            : `${hit.asFar} other day${hit.asFar === 1 ? "" : "s"} in the window went at least this far`}`}>
            {deltaLine(hit)}
          </div>
        </div>
      )}
      {quiet && (
        <p className={`lede-rest${hit ? "" : " alone"}`}
          title={hit ? "The rest of the day's numbers" : "Nothing unusual today"}>
          {quiet}
        </p>
      )}
    </div>
  );
}
