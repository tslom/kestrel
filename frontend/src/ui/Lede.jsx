import { deltaLine, restLine } from "../lib/notable";
import { dragPin } from "../lib/drag.js";

// The body of a measurement card: one reading (`lib/notable.js` picks it), or just the rest line.
// `rest` is the same string `Slot` shows folded, so folding is never hiding.
// `tail` never leads (money's week total, a sum with no baseline); it rides the rest line.
// Design: DESIGN.md → Folding a queue (`ui/Folded.jsx`) (the lede form).
export default function Lede({ hit, readings, label, tail }) {
  // The lede's metric leaves the line under it. `Slot`'s fold summary keeps the unfiltered
  // `restLine(readings)` so the collapsed line stays the same four numbers whichever led.
  const rest = restLine(hit ? readings.filter((r) => r.key !== hit.key) : readings);
  // Joined, not stacked: two quiet lines is the clutter this form removes.
  const quiet = [rest, tail].filter(Boolean).join(" · ");
  return (
    <div className="lede-wrap">
      {hit && (
        // Pinned as one reading, not the whole card.
        <div className={`lede${hit.tone ? ` tone-${hit.tone}` : ""}`}
          {...dragPin(`${label} · ${hit.label.toLowerCase()}`,
                      { reading: hit.key, value: hit.value, median: hit.median,
                        days: hit.n, delta: hit.delta })}>
          <div className="lede-head">
            <span className="lede-label">{hit.label}</span>
            <span className="lede-value">{hit.fmt(hit.value)}</span>
          </div>
          {/* Signed, against a median that names its day count ("vs typical" would be unauditable). */}
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
