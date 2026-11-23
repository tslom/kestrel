import { createContext } from "react";

// The status bar's collapse (plan/StatusBar.jsx `fitBar`), shared with the
// console's ⋯ menu so whatever the bar folded away is still a click off.
// `cut`: the keys folded right now ("timeline view full …"); `host`: a ref to
// the status bar's own actions ({ onView, onTimeline, onFull, onNewTab, … }),
// read when the menu draws, so the context only changes when `cut` does and
// the memoised tabs don't re-render on every turn event.
export const BarHostContext = createContext(null);

// Lowest priority first: what folds as the bar runs out of room. Compactions
// (a shorter label) come before a control leaves the row, and the row keeps
// identity, status, model · effort, mode, usage, review and merge longest.
// Never folded: the tab strip, the state, Yes/No, the model chip and ⋯.
export const CUT_ORDER = [
  "timeline",     // the turn's timeline button        → ⋯
  "view",         // "view" (no thumbnail yet)         → ⋯
  "elapsed",      // the running clock                 (the status word says "working")
  "reset",        // the usage chip's countdown        (in its title, and ⋯'s usage row)
  "full",         // full screen                       → ⋯ (and ⌘F, double-click)
  "voice",        // read aloud / wake word, when on   → ⋯
  "thumb",        // Claude's view thumbnail           → ⋯
  "merge-short",  // "merge → main" → "merge"
  "model-short",  // "opus 5.5" → "opus"
  "scripts-short", // dev: script buttons icon-only, the PR chip just "#14"
  "new",          // new tab                           → ⋯ (and ⌥T)
  "scripts",      // dev: project.json scripts and preview → ⋯
  "pr",           // dev: the watched PR's chip         → ⋯
  "tabs",         // the tab strip narrows and scrolls (the one on screen kept in view)
  "usage",        // the usage chip                    → ⋯'s usage row
  "review",       // dev: review                       → ⋯
  "merge",        // dev: merge                        → ⋯
  "mode",         // permission mode                   → ⋯
  "squeeze",      // the tab strip and status line give up their minimums
];

// Keys that only shorten something, so nothing goes to the menu for them.
const COMPACT = new Set(["elapsed", "reset", "merge-short", "model-short", "scripts-short", "tabs", "squeeze"]);
// Keys that act on the layout, not on a tagged element.
const LAYOUT = new Set(["tabs", "squeeze"]);

// Fold the bar until it fits: clear the cut, then add keys in order while it
// overflows. Synchronous, so it lands before paint. Keys with nothing on the
// bar are skipped, so `cut` names only things that were really folded.
export function fitBar(bar) {
  const cut = [];
  // Unfolding to measure widens the tab strip, which clamps its scroll to 0;
  // the strip ends the same width, so nothing would scroll it back.
  const strips = [...bar.querySelectorAll(".st-tabs")];
  const scrolled = strips.map((s) => s.scrollLeft);
  try {
    bar.setAttribute("data-cut", "");
    fold(bar, cut);
  } finally {
    strips.forEach((s, i) => { s.scrollLeft = scrolled[i]; });
  }
  return cut.join(" ");
}

function fold(bar, cut) {
  const over = () => bar.scrollWidth > bar.clientWidth + 1;
  for (const key of CUT_ORDER) {
    if (!over()) break;
    if (!LAYOUT.has(key) && !bar.querySelector(`[data-cut="${key}"]`)) continue;
    cut.push(key);
    bar.setAttribute("data-cut", cut.join(" "));
  }
}

export const folded = (cut, key) => !COMPACT.has(key) && (cut || "").split(" ").includes(key);
