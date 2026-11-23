import { useState } from "react";
import Icon from "./icons.jsx";

// The named-range chips plus the custom date span, shared by the provider and
// trends expansions. Both had their own copy, and the copied part that mattered
// was the token grammar: `start..end` (double-dot, one shell-safe word) is
// parsed by providers/ranges.py, so a second hand-rolled copy is a rule that
// can drift away from the backend that has to accept it.
//
// `ranges` is null while the caller hasn't loaded its meta yet — distinct from
// an empty list, which means the source genuinely offers no named ranges and so
// whatever range is current must be a custom one.
export default function RangePicker({ ranges, range, onPick }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const customActive = !!ranges && !ranges.includes(range);
  // The date fields are the rare case, so they stay folded until asked for —
  // or until a custom range is the one in force and needs saying.
  const [customOpen, setCustomOpen] = useState(false);
  const applyCustom = () => {
    if (!start) return;
    onPick(end && end !== start ? `${start}..${end}` : start);
  };

  return (
    <>
      <div className="range-tabs">
        {(ranges || []).map((token) => (
          <button key={token} aria-pressed={token === range}
            className={`range-tab${token === range ? " active" : ""}`}
            onClick={() => onPick(token)}>
            {token.replace(/-/g, " ")}
          </button>
        ))}
        <button className={`range-tab${customActive ? " active" : ""}`}
          aria-expanded={customOpen || customActive}
          onClick={() => setCustomOpen((v) => !v)}>custom</button>
      </div>

      {(customOpen || customActive) && (
        <div className={`custom-range${customActive ? " active" : ""}`}>
          <input type="date" value={start} aria-label="Start date"
            onChange={(e) => setStart(e.target.value)} />
          <span className="range-arrow"><Icon name="arrowRight" size={12} /></span>
          <input type="date" value={end} aria-label="End date (optional)"
            onChange={(e) => setEnd(e.target.value)} />
          <button className="chip" disabled={!start} onClick={applyCustom}>go</button>
          {customActive && <span className="custom-token">{range}</span>}
        </div>
      )}
    </>
  );
}
