// Dragging widget content into the console.
//
// The click path already exists: every pin button hands `pinContext(label,
// data)` a payload `pin.js` has already budgeted. So a drag source is not new
// plumbing — it is that same pair, carried by the pointer instead of by a
// button, and it lands through the same `pinContext`. Nothing here decides
// what a payload should contain; it decides only *which* payload the gesture
// picked up.
//
// Granularity falls out of where you grabbed, not out of a mode or a modifier.
// Cards, groups and rows nest, and each is a drag source over its own slice:
// `dragstart` fires on the innermost draggable under the pointer and then
// bubbles, so a handler that stops propagation lets the row beat the list it
// sits in, and the list beat the card around it. Grab one flight and you send
// one flight; grab the flights heading and you send the flights; grab the
// card's header and you send the card. The browser's own drag image is the
// feedback — it renders exactly the element that won, so how much you're about
// to send is visible before you let go, and no extra highlight is needed.
//
// Why it earns its place next to the buttons it duplicates: a pin button is
// one payload per surface, and adding a second and third button to every row
// to expose the other levels would cost more chrome than the levels are worth.
// Dragging exposes all of them with no chrome at all.

// Our own type, so the console can tell a context drag from a file drag before
// the drop — `dataTransfer.getData` is blocked during dragover (the pointer is
// still over unknown territory), but `types` is readable, so the veil can name
// what will happen while there is still time to not do it.
export const PIN_MIME = "application/x-vault-pin";

// Dragging is armed by holding ⌘ or ⌥ (New Features, 11-10). A draggable
// element takes the mouse-down for a drag, so with every card, row and heading
// a drag source, text on the desk couldn't be highlighted at all. Now sources
// carry `data-pin-drag` and only become `draggable` while a modifier is down;
// the rest of the time a press-and-drag selects text like anywhere else.
//
// Not Control, which was the first ask: on macOS Control-click is the
// right-click, and WebKit opens the context menu instead of starting a drag.
// Nothing in the app reads ⌘ or ⌥ on a click, so neither collides; ⌘ is the
// one the user reached for. A ⌘K or ⌥1 arms for the moment the key is held,
// which only shows as an outline under the pointer.
//
// The attribute is set on the DOM directly rather than through React, so
// arming doesn't re-render the desk; React never writes `draggable` on these.
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
  // ⌘-Tab away lands here too, so an app switch never leaves it armed.
  window.addEventListener("blur", () => { dragging = false; setArmed(false); });
  // A modifier pressed while another app had focus sends no keydown here; the
  // press itself says whether it's held, and runs before the browser decides
  // between a drag and a selection.
  window.addEventListener("pointerdown", (e) => setArmed(held(e)), true);
  window.addEventListener("dragstart", () => { dragging = armed; }, true);
  window.addEventListener("dragend", (e) => { dragging = false; setArmed(held(e)); }, true);
}

/**
 * Props making an element a drag source for `{ label, data }` — the same pair
 * its pin button would pass. Spread onto the card, group or row. Draggable
 * while ⌘ or ⌥ is held (above).
 *
 * Returns nothing when there's no payload yet (a card still fetching), which
 * leaves the element undraggable rather than a source of `null`.
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
      // The innermost source wins. Without this the same drag would also fire
      // on every draggable ancestor and the card would overwrite the row.
      e.stopPropagation();
      e.dataTransfer.setData(PIN_MIME, json);
      // Dropped anywhere that isn't the console (another app, a text field),
      // the label is the readable thing to leave behind — not the JSON.
      e.dataTransfer.setData("text/plain", label);
      // Both, because the arming key is still down at the drop: macOS reads
      // ⌘ held over a target as a request to move, and a source that allows
      // only "copy" has nothing in common with that, so the drop is refused
      // (it worked only if you let go of ⌘ first). See pinDropEffect.
      e.dataTransfer.effectAllowed = "copyMove";
    },
  };
}

/**
 * The dropEffect a pin target answers dragover with. It has to be the
 * operation the held modifiers ask for — "move" under ⌘ alone, "copy"
 * otherwise (⌥ is copy) — or WebKit treats the target as refusing the drop.
 * Nothing moves either way; the console only reads the payload.
 */
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
