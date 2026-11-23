import { useEffect, useMemo, useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { rank } from "../lib/fuzzy";

// ⌘K over the desk. Paper material — it drives the vault side, and the console
// keeps its own ink-side affordances (slash commands, push-to-talk).
//
// Two list shapes, because they answer different questions. With no query it's
// a grouped menu: you opened it to find out what's here, so the groups are the
// content. Once you type it's one flat ranked list — you already know what you
// want, and group headers just push it further down the screen.

// Highlight the characters the query actually matched, so a fuzzy hit can be
// checked at a glance rather than taken on faith.
function Title({ text, positions }) {
  if (!positions.length) return text;
  const hit = new Set(positions);
  const out = [];
  let run = "";
  let runHit = hit.has(0);
  for (let i = 0; i < text.length; i++) {
    const isHit = hit.has(i);
    if (isHit !== runHit) {
      out.push(runHit ? <b key={i}>{run}</b> : run);
      run = "";
      runHit = isHit;
    }
    run += text[i];
  }
  out.push(runHit ? <b key="last">{run}</b> : run);
  return out;
}

// `searchNotes(q)`: optional, resolves to extra commands for the vault's
// notes (the plan view's Notes group). Asked from the second letter, a beat
// after typing stops, and listed after the ranked commands under their own
// heading — a note is a destination, never a better match than a command.
export default function CommandPalette({ commands, searchNotes, onClose }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [notes, setNotes] = useState([]);
  const listRef = useRef(null);

  const term = q.trim();
  const noteSearch = Boolean(searchNotes) && term.length >= 2;
  useDebounce((stale) => {
    if (!noteSearch) { setNotes([]); return; }
    searchNotes(term).then((n) => { if (!stale()) setNotes(n); }).catch(() => {});
  }, noteSearch ? 150 : 0, [q, searchNotes]);

  const results = useMemo(() => [...rank(commands, q), ...notes], [commands, q, notes]);

  // Clamped rather than reset: `results` is a new array on every keystroke.
  const active = results[Math.min(sel, results.length - 1)];

  useEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [sel, q]);

  // Escape lives on the window, like every other panel's does, rather than on
  // the field's onKeyDown — otherwise it only works while the input happens to
  // hold focus, and the one key everybody trusts to back out would be the one
  // key that sometimes doesn't.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onKeyDown = (e) => {
    const move = (d) => {
      e.preventDefault();
      if (!results.length) return;
      setSel((s) => (Math.min(s, results.length - 1) + d + results.length) % results.length);
    };
    // Ctrl+N/P alongside the arrows: this is a keyboard-first surface and the
    // hands are already on the home row.
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) return move(1);
    if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) return move(-1);
    if (e.key === "Home") { e.preventDefault(); return setSel(0); }
    if (e.key === "End") { e.preventDefault(); return setSel(results.length - 1); }
    // Tab moves through the results too, rather than out to the page behind
    if (e.key === "Tab") return move(e.shiftKey ? -1 : 1);
    if (e.key === "Enter" && active) {
      e.preventDefault();
      onClose();          // close first: a command may want the focus itself
      active.run();
    }
  };

  // Group headers only in menu mode; see the note at the top.
  const grouped = !q.trim();
  let lastGroup = null;

  return (
    <div className="cmdk-backdrop" onMouseDown={onClose}>
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="Commands"
        onMouseDown={(e) => e.stopPropagation()}>
        <input
          className="cmdk-input"
          autoFocus
          value={q}
          // Typing re-ranks under the cursor, so the selection comes back to
          // the top or Enter runs whatever is sitting at the old index.
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={onKeyDown}
          placeholder={searchNotes ? "Open a view, a card or a note…" : "Open a panel, jump to a card…"}
          aria-label="Search commands"
          role="combobox"
          aria-expanded="true"
          aria-controls="cmdk-list"
          aria-activedescendant={active ? `cmdk-${active.id}` : undefined}
        />

        <ul className="cmdk-list" id="cmdk-list" role="listbox" ref={listRef} tabIndex={-1}>
          {results.map((c, i) => {
            // A section (Notes) keeps its heading while searching too.
            const header = (grouped || c.section) && c.group !== lastGroup ? (lastGroup = c.group) : null;
            const selected = c === active;
            return (
              <li key={c.id}>
                {header && <div className="cmdk-group">{header}</div>}
                <div
                  id={`cmdk-${c.id}`}
                  className={`cmdk-item${selected ? " sel" : ""}${c.accent ? " accent" : ""}`}
                  role="option"
                  aria-selected={selected}
                  // Enter and click must not diverge, so both go through run().
                  onMouseDown={(e) => { e.preventDefault(); onClose(); c.run(); }}
                  onMouseMove={() => setSel(i)}
                >
                  <span className="cmdk-title">
                    <Title text={c.title} positions={c.positions} />
                  </span>
                  <span className="cmdk-hint">{grouped || c.section ? c.hint : c.group}</span>
                </div>
              </li>
            );
          })}
          {!results.length && <li className="cmdk-empty">Nothing matches “{q.trim()}”.</li>}
        </ul>

        <footer className="cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> run</span>
          <span><kbd>esc</kbd> close</span>
        </footer>
      </div>
    </div>
  );
}
