import { useState } from "react";
import Icon from "./icons.jsx";

// Shared pieces for the three small lists (debts, user lists, watchlist).
// They're the same card three times over — a round toggle, a label block, a
// delete — differing only in what the label block holds, so that arrives as
// children rather than as three near-identical widgets.
//
// Every row is drawn. The six-row cut (and the "+N more" foot and the
// keep-what-you-just-added window it needed) was the rail desk's economy, where
// eighteen cards shared one column every morning; a card is only drawn now when
// you open it into the side pane, which scrolls (DESIGN.md → Card anatomy).

// `drag` is the drag-source props for this row, from `dragPin` — the three
// cards each name their own row differently ("debt · Rowan", "buy · milk"), so
// the pair is built by the caller and only spread here.
export function CheckRow({ done, onToggle, onDelete, toggleTitle, drag, children }) {
  return (
    <li className={`check-row${done ? " is-done" : ""}`} {...drag}>
      <button className={`habit-toggle${done ? " done" : ""}`}
        onClick={onToggle} title={toggleTitle}>
        {done && <Icon name="check" size={11} />}
      </button>
      <span className="check-body">{children}</span>
      <button className="check-drop" onClick={onDelete} title="Remove">
        <Icon name="close" size={11} />
      </button>
    </li>
  );
}

export function CheckList({ children, drag }) {
  return <ul className="check-list" {...drag}>{children}</ul>;
}

// The add box clears itself on submit and ignores whitespace-only input —
// the same three lines every one of these cards would otherwise repeat.
//
// `initial`/`onCancel` are for the boxes that appear on demand rather than
// living at the foot of a card: naming a new list, renaming one. Those open
// focused, start from the current name where there is one, and close on Escape
// — a box you opened by accident must not need a click somewhere else to
// dismiss. Both are optional, and without them this is the plain add box.
export function AddInput({ placeholder, onAdd, initial = "", onCancel }) {
  const [value, setValue] = useState(initial);
  function submit() {
    const text = value.trim();
    if (!text) return;
    onAdd(text);
    setValue("");
  }
  return (
    <div className="widget-add">
      <input value={value} placeholder={placeholder} autoFocus={!!onCancel}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape" && onCancel) onCancel();
        }} />
    </div>
  );
}
