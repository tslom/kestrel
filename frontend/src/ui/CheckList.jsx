import { useState } from "react";
import Icon from "./icons.jsx";

// Shared pieces for the three small lists (debts, user lists, watchlist): toggle, label (children), delete.
// Every row is drawn, no cut (DESIGN.md → Layout: the plan view, Card anatomy).

// `drag`: the row's `dragPin` props, built by the caller (each card names its rows differently).
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

// The add box clears on submit and ignores whitespace-only input. `initial`/`onCancel` (optional) are
// for on-demand boxes (new/rename list): they open focused and close on Escape.
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
