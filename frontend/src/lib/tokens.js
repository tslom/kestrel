// The desk's colour tokens, read off the root at call time for the surfaces
// that paint outside CSS (the map's canvas, the terrain's WebGL scene). Called
// per build/draw rather than once at load, so a theme flip is picked up like
// everywhere else. Fallbacks are the light Flexoki values, for a page whose
// stylesheet hasn't landed yet.
export function palette() {
  const css = getComputedStyle(document.documentElement);
  const tok = (name, fallback) =>
    (css.getPropertyValue(name) || "").trim() || fallback;
  return {
    paper: tok("--paper", "#F2F0E5"),
    raise: tok("--paper-raise", "#FFFCF0"),
    ink: tok("--paper-ink", "#100F0F"),
    mid: tok("--paper-mid", "#6F6E69"),
    faint: tok("--paper-faint", "#B7B5AC"),
    accent: tok("--accent", "#BC5215"),
    data: tok("--data", "#205EA6"),
  };
}
