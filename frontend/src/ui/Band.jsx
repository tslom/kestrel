import { dragPin } from "../lib/drag.js";

// One section of a sheet: a named question with its headline answer.
//
// The name is the point. A band says "Flow" or "Sleep", not `cashflow` or
// `sleep_deep_min`, and the number that answers it sits in the header rather
// than inside the body — so the body is free to be a chart, a list, or a wall
// of receipts without the answer getting lost among them. `pin` makes the
// header a drag handle for that band alone, which is how a section reaches the
// console without dragging the whole sheet.
export default function Band({ title, note, pin, children }) {
  return (
    <section className="band">
      <h2 {...(pin ? dragPin(pin[0], pin[1]) : {})}>
        <span>{title}</span>
        {note != null && note !== "" && <span className="band-note">{note}</span>}
      </h2>
      {children}
    </section>
  );
}
