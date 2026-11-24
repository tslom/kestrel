import { dragPin } from "../lib/drag.js";

// One sheet section: a plain-named question ("Flow", not `cashflow`) with its headline answer in the
// header. `pin` makes the header a drag handle for that band alone.
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
