// Opening a vault note in Obsidian, from anywhere on the desk.
//
// Why not a plain `<a href="obsidian://…">`: neither surface the dashboard
// runs in handles a custom scheme well. Chrome shows an "Open Obsidian?"
// interstitial on every click, and the .app's WKWebView is worse — pywebview
// allows the navigation, WebKit can't load a scheme it doesn't know, and the
// click silently does nothing at all. So the anchor keeps a URI (it's worth
// seeing on hover, and worth copying) but the click goes to the backend, which
// hands the real one to `open`. Same behaviour in a tab and in the app.
//
// The backend owns resolution — it has the vault and the note index — so the
// rules here only decide what is *worth offering* as a link. Misses come back
// 404 and are shown on the link rather than thrown: a sentence mentioning a
// file that isn't in the vault is an ordinary thing for the console to say.
import { postJSON } from "./api";

// `[[Note]]` / `[[Note|alias]]`. Nothing but Obsidian writes this, so a
// wikilink is linkified whatever it names — extension or not, spaces and all.
export const WIKILINK_SRC = "\\[\\[([^\\]\\n|]+)(?:\\|([^\\]\\n]+))?\\]\\]";

// A bare path in prose. Deliberately narrow: a slash is required, so `DESIGN.md`
// stays repo talk, and a leading slash is excluded: an absolute path is a file
// on disk, not a note, and goes through lib/paths.js FILE_PATH_SRC instead.
// Spaces aren't allowed either; a note with spaces in its name arrives as a
// wikilink or in backticks, both of which take the whole span.
export const NOTE_PATH_SRC = "(?<![\\w.\\-/])((?:[\\w.\\-]+/)+[\\w.\\-]+\\.md)\\b";

// Same rule as NOTE_PATH_SRC, for text that already arrived as one span —
// a code span, or a table cell — where the surrounding-character guards
// don't apply and spaces are fine.
export function looksLikeNote(text) {
  const t = (text || "").trim();
  return t.endsWith(".md") && !t.startsWith("/") && t.includes("/");
}

export function openNote(target) {
  return postJSON("/api/vault/open", { target });
}

export function obsidianHref(target) {
  // Display only: the real URI is built backend-side, where the vault name
  // lives. This is what hover and "copy link" show, in the same shape.
  const clean = String(target).replace(/^\[\[/, "").replace(/\]\]$/, "").split(/[#|^]/)[0];
  return `obsidian://open?file=${encodeURIComponent(clean.trim())}`;
}
