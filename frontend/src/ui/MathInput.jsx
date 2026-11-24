// Calculator-style math entry (MathLive's React edge). Loaded on first use (about as big as the rest of
// the page); uses the console's bundled KaTeX fonts (fontsDirectory off) so it works offline. A failed
// load forgets itself so the next open retries.
import { useEffect, useRef, useState } from "react";

let ready = null;
function loadField() {
  ready ??= Promise.all([import("mathlive"), import("katex/dist/katex.min.css")])
    .then(([{ MathfieldElement }]) => {
      MathfieldElement.fontsDirectory = null;
      MathfieldElement.soundsDirectory = null;
    })
    .catch((e) => { ready = null; throw e; });
  return ready;
}

// MathLive brackets single-atom fraction terms; strip them ((1)/(2) → 1/2), keep needed ones.
const tidy = (ascii) => ascii
  .replace(/\((\w+)\)(?=\/)/g, "$1")
  .replace(/\/\((\w+)\)/g, "/$1");

// `onChange(latex, ascii)`: ascii is the typed form ("x^2"), what a quiz's `accept` list uses.
export default function MathInput({ value = "", onChange, onEnter, onEscape, placeholder = "",
                                    autoFocus = false, label = "Math" }) {
  const [state, setState] = useState("loading");     // loading | ready | failed
  const field = useRef(null);
  // The element's listeners outlive any one render.
  const handlers = useRef({});
  handlers.current = { onChange, onEnter, onEscape };

  useEffect(() => {
    let live = true;
    loadField().then(() => { if (live) setState("ready"); },
                     () => { if (live) setState("failed"); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    const mf = field.current;
    if (state !== "ready" || !mf) return;
    mf.mathVirtualKeyboardPolicy = "manual";
    mf.menuItems = [];
    mf.placeholder = placeholder;
    if (value) mf.value = value;
    const input = () => handlers.current.onChange?.(
      mf.getValue("latex"), tidy(mf.getValue("ascii-math")));
    // On the way down: the field acts on these keys itself once they reach it.
    const key = (e) => {
      const act = e.key === "Enter" ? handlers.current.onEnter
        : e.key === "Escape" ? handlers.current.onEscape : null;
      if (!act) return;
      e.preventDefault();
      e.stopPropagation();
      act();
    };
    mf.addEventListener("input", input);
    mf.addEventListener("keydown", key, true);
    if (autoFocus) mf.focus();
    return () => {
      mf.removeEventListener("input", input);
      mf.removeEventListener("keydown", key, true);
    };
  }, [state]);   // eslint-disable-line react-hooks/exhaustive-deps -- binds once the field is ready; props are read through handlers.current, and placeholder/value only seed it

  if (state === "failed")
    return <span className="math-field-note">Math typing didn’t load. Reload (⌘R) to try again.</span>;
  if (state === "loading") return <span className="math-field math-field-wait" />;
  return <math-field ref={field} class="math-field" aria-label={label} />;
}
