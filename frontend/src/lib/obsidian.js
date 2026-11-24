// Opening a vault note in Obsidian from anywhere on the desk. The anchor keeps an obsidian:// URI for hover/copy
// but the click goes to the backend, which runs `open` (no custom schemes in Chrome or WKWebView). The backend
// owns resolution; the rules here only decide what is worth offering as a link; misses (404) show on the link.
// Design: DESIGN.md → `frontend/src/lib/obsidian.js`.
import { postJSON } from "./api";

// `[[Note]]` / `[[Note|alias]]`: only Obsidian writes it, so linkify whatever it names.
export const WIKILINK_SRC = "\\[\\[([^\\]\\n|]+)(?:\\|([^\\]\\n]+))?\\]\\]";

// A bare path in prose: a slash is required (`DESIGN.md` stays repo talk) and a leading slash is excluded
// (absolute = a disk file, lib/paths.js FILE_PATH_SRC); no spaces (those arrive as wikilinks or in backticks).
export const NOTE_PATH_SRC = "(?<![\\w.\\-/])((?:[\\w.\\-]+/)+[\\w.\\-]+\\.md)\\b";

// NOTE_PATH_SRC's rule for text already isolated as one span (code span, table cell): spaces are fine.
export function looksLikeNote(text) {
  const t = (text || "").trim();
  return t.endsWith(".md") && !t.startsWith("/") && t.includes("/");
}

export function openNote(target) {
  return postJSON("/api/vault/open", { target });
}

export function obsidianHref(target) {
  // Display only: the real URI is built backend-side where the vault name lives.
  const clean = String(target).replace(/^\[\[/, "").replace(/\]\]$/, "").split(/[#|^]/)[0];
  return `obsidian://open?file=${encodeURIComponent(clean.trim())}`;
}
