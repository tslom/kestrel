// Dragging widget content into the console: the pin button's `{ label, data }` pair carried by the pointer,
// landing through the same `pinContext`. Granularity is where you grabbed (card, group or row nest; the
// innermost dragstart wins). Design: DESIGN.md → `frontend/src/lib/pin.js`.

// Own MIME type: `getData` is blocked during dragover but `types` is readable, so the veil can name what will happen.
export const PIN_MIME = "application/x-vault-pin";

// Armed by holding ⌘ or ⌥: sources carry `data-pin-drag` and are `draggable` only then, so text stays selectable.
// Not Control (macOS right-click opens the context menu instead). Set on the DOM, not through React, so arming doesn't re-render.
const ARM_KEYS = new Set(["Meta", "Alt"]);
const held = (e) => !!(e.metaKey || e.altKey);
const ARMED_CLASS = "pin-drag-armed";
let armed = false;
let dragging = false;
function setArmed(on) {
  if (dragging && !on) return;   // a drag in flight disarms when it lands
  if (armed === on) return;
  armed = on;
  document.documentElement.classList.toggle(ARMED_CLASS, on);
  for (const el of document.querySelectorAll("[data-pin-drag]")) el.draggable = on;
}
if (typeof window !== "undefined" && typeof document !== "undefined") {
  window.addEventListener("keydown", (e) => { if (ARM_KEYS.has(e.key)) setArmed(true); }, true);
  // Let go of one, still holding the other: stay armed.
  window.addEventListener("keyup", (e) => { if (ARM_KEYS.has(e.key)) setArmed(held(e)); }, true);
  // ⌘-Tab away lands here too.
  window.addEventListener("blur", () => { dragging = false; setArmed(false); });
  // A modifier pressed while another app had focus sends no keydown; the press says whether it's held.
  window.addEventListener("pointerdown", (e) => setArmed(held(e)), true);
  window.addEventListener("dragstart", () => { dragging = armed; }, true);
  window.addEventListener("dragend", (e) => { dragging = false; setArmed(held(e)); }, true);
}

/**
 * Props making an element a drag source for `{ label, data }`, draggable while ⌘/⌥ is held.
 * Returns {} with no payload yet (a card still fetching).
 */
export function dragPin(label, data) {
  if (!label || data == null) return {};
  return {
    "data-pin-drag": "",
    onDragStart(e) {
      let json;
      try {
        json = JSON.stringify({ label, data });
      } catch {
        return;   // a cyclic payload isn't droppable; let the drag be a no-op
      }
      // Innermost source wins; otherwise ancestors fire too and the card overwrites the row.
      e.stopPropagation();
      e.dataTransfer.setData(PIN_MIME, json);
      // Dropped elsewhere, the label is the readable thing to leave behind.
      e.dataTransfer.setData("text/plain", label);
      // "copyMove": ⌘ is still down at the drop and macOS reads it as move; copy-only would be refused.
      e.dataTransfer.effectAllowed = "copyMove";
    },
  };
}

/** dragover's dropEffect must match the held modifiers ("move" under ⌘ alone, else "copy") or WebKit refuses the drop; nothing moves. */
export function pinDropEffect(e) {
  return e.metaKey && !e.altKey ? "move" : "copy";
}

/** The `{ label, data }` a pin drag carried, or null if this isn't one. */
export function readPin(dataTransfer) {
  const raw = dataTransfer?.getData(PIN_MIME);
  if (!raw) return null;
  try {
    const { label, data } = JSON.parse(raw);
    return label ? { label, data } : null;
  } catch {
    return null;   // a malformed drag is a dropped drag, not a broken console
  }
}
