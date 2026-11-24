import { useState } from "react";
import Icon from "./icons.jsx";

// Named-range chips plus a custom span, shared by provider and trends expansions. The token grammar
// `start..end` is parsed by providers/ranges.py: don't hand-roll a second copy.
// `ranges` null = meta not loaded; [] = no named ranges, so the current range must be custom.
export default function RangePicker({ ranges, range, onPick }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const customActive = !!ranges && !ranges.includes(range);
  // Date fields stay folded unless asked for or a custom range is in force.
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
