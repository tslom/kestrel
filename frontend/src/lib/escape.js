import { useEffect, useRef } from "react";

// An Escape this soon after leaving fullscreen still belongs to that exit, not "close the sheet".
const FULLSCREEN_ESCAPE_MS = 300;

// WKWebView hands an unhandled Escape back to AppKit, which exits a fullscreen NSWindow: so cancel every Escape
// (capture phase, so stopPropagation can't hide it), except while an element is fullscreen (the map's exit is WebKit's own).
export function installEscapeGuard() {
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !e.isComposing && !document.fullscreenElement) e.preventDefault();
  }, true);
}

// Escape for a stack of open views. The browser exits fullscreen on that key itself, and `fullscreenElement` alone
// races (it may already be cleared), so we also remember when we last left. Escape over the console pane or
// composer (.console-pane, .prompt-bar) belongs to the composer, not to closing a sheet.
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
      // A field with a draft: the first Escape leaves it, the next closes (leaving a room would lose the draft).
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
