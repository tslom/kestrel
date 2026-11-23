// The console's document pane, as something a citation deep in a reply can
// reach: `useContext(DocPaneContext)?.open({ ref, page, quote })`.
//
// A context rather than a window event because a console has tabs, each its
// own Console with its own pane, and a cite clicked in one must open in that
// one. Outside a console (a brief, a note tab) there is no provider, the
// value is null, and callers fall back to what they did before: the inline
// card, the lightbox.
import { createContext } from "react";

export const DocPaneContext = createContext(null);

// Every doc reference a reply can make, in the order written: inline cites,
// page embeds, and a quiz or cards item's `slide`. The quote only rides on
// the bracket forms; a slide has none.
const REF_RE = /\[(?:cite|page):(d[0-9a-f]{5,10})#p(\d{1,4})(?:\s+"([^"\]\n]{1,300})")?\]|"slide"\s*:\s*"(d[0-9a-f]{5,10})#p(\d{1,4})"/gi;

export function docRefsIn(text) {
  const out = [];
  for (const m of String(text || "").matchAll(REF_RE)) {
    out.push(m[1]
      ? { ref: m[1].toLowerCase(), page: Number(m[2]), quote: m[3] || undefined }
      : { ref: m[4].toLowerCase(), page: Number(m[5]) });
  }
  return out;
}
