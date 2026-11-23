import { useEffect, useRef } from "react";

// How long after leaving fullscreen an Escape still counts as that exit rather
// than as "close the sheet". Long enough to cover the event race, short enough
// that a deliberate second press lands — nobody taps Escape twice in 300ms
// meaning two different things.
const FULLSCREEN_ESCAPE_MS = 300;

// In the desktop app (pywebview → WKWebView) an Escape the page leaves
// unhandled is handed back to AppKit, and a fullscreen NSWindow answers it by
// leaving fullscreen — so closing a sheet, or pressing Escape over nothing,
// dropped the whole window out of full screen. WKWebView counts a keydown as
// handled only when the page cancels it, so cancel every Escape. Capture
// phase on the window, so a handler that stops propagation (the sessions
// panel does) can't hide the key from it; cancelling changes nothing for the
// other listeners, since none of them read `defaultPrevented`. Not while an
// element is fullscreen (the map): that exit is WebKit's own and must still
// get the key.
export function installEscapeGuard() {
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !e.isComposing && !document.fullscreenElement) e.preventDefault();
  }, true);
}

// Escape for a stack of open views (the dock's sheets, the plan view's tabs).
//
// Escape is overloaded once anything in a sheet can go fullscreen (the map).
// The browser exits fullscreen on that keypress itself, and if we also close
// the sheet on it, leaving fullscreen takes the map away with it.
//
// Checking `document.fullscreenElement` alone is not enough, because the two
// events race and the order is not ours to pick: Chrome swallows the Escape
// that exits fullscreen, but a browser that does deliver it may have already
// cleared `fullscreenElement` by the time this handler runs, and the guard
// reads as "not fullscreen" for the one keypress it exists for. So we also
// remember when we last left, and treat an Escape arriving right behind the
// exit as belonging to it.
//
// The console is a live pane, not something behind a backdrop: Escape while
// writing a message belongs to the composer (it closes the / menu), and
// closing somebody's open sheet from over there would read as a glitch. The
// plan view draws the composer outside the pane (.prompt-bar), so both count.
export function useStackEscape(onEscape, enabled = true) {
  const leftFullscreenAt = useRef(0);

  useEffect(() => {
    const onChange = () => {
      if (!document.fullscreenElement) leftFullscreenAt.current = performance.now();
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (e.target.closest?.(".console-pane, .prompt-bar")) return;
      // A field you've typed into: the first Escape leaves the field, the
      // next one closes. Leaving a room on it threw the draft away (11-22).
      const t = e.target;
      if (t?.matches?.("input:not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true]")
          && (t.value || t.textContent)) { t.blur(); return; }
      if (document.fullscreenElement) return;
      if (performance.now() - leftFullscreenAt.current < FULLSCREEN_ESCAPE_MS) return;
      onEscape();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onEscape, enabled]);
}
