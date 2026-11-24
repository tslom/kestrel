import { createContext } from "react";

// The status bar's collapse (plan/StatusBar.jsx `fitBar`), shared with the console's ⋯ menu. `cut`: keys folded now;
// `host`: a ref to the bar's own actions, read when the menu draws, so context changes only with `cut`.
export const BarHostContext = createContext(null);

// Lowest priority first: what folds as the bar runs out of room (compactions before a control leaves).
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
  "account",      // the Claude account chip           (Settings → Claude accounts)
  "usage",       // the usage chip                    → ⋯'s usage row
  "review",       // dev: review                       → ⋯
  "merge",        // dev: merge                        → ⋯
  "mode",         // permission mode                   → ⋯
  "squeeze",      // the tab strip and status line give up their minimums
];

// Keys that only shorten something, so nothing goes to the menu for them.
const COMPACT = new Set(["elapsed", "reset", "merge-short", "model-short", "scripts-short", "tabs",
  "account", "squeeze"]);
// Keys that act on the layout, not on a tagged element.
const LAYOUT = new Set(["tabs", "squeeze"]);

// Clear the cut, then add keys in order while it overflows (synchronous, before paint); keys absent from the bar are skipped.
export function fitBar(bar) {
  const cut = [];
  // Unfolding widens the tab strip and clamps its scroll to 0: restore it.
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
