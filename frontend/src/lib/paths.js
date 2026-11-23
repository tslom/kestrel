// Which spans of a reply are worth asking the backend about as files.
//
// Pure, so node can test it (tests/paths.test.mjs). Generous on purpose: the
// backend (app/files.py) is what decides a path is real, and only a real one
// becomes a link, so a false positive here costs one line in a batched
// request and nothing on screen.

// A bare path in prose. Absolute (`/Users/…`, at least two segments, so km/s
// and a lone /etc stay prose) or ~-prefixed; or relative with a slash and an
// extension that has a letter in it (so 1.5/2.0 and and/or don't qualify).
// Every segment ends on a non-dot, which keeps a sentence's full stop off
// the end and refuses `..` outright.
const SEG = "[\\w.@+\\-]*[\\w@+\\-]";
export const FILE_PATH_SRC =
  "(?<![\\w.\\-/~:])(" +
    `(?:~(?=/)|(?=/[\\w.@+\\-]+/[\\w.@+\\-]))(?:/${SEG})+` +
    "|" +
    `(?:[\\w.@+\\-]+/)+${SEG}\\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,10}` +
  ")(?![\\w/])";

// A whole code span or link target, where the surrounding text is already
// cut away and spaces are fine (vault folders have them).
export function looksLikePath(text) {
  const t = (text || "").trim();
  if (!t || t.length > 600 || /[\n<>"|*?]/.test(t) || t.includes("://")) return false;
  if (t.startsWith("~/") || (t.startsWith("/") && /^\/[^/\s]+\/[^/\s]/.test(t))) return true;
  if (t.startsWith("-") || /^\w+:/.test(t)) return false;
  // relative: a slash, and a final segment with an extension
  return t.includes("/") && /\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,10}$/.test(lineless(t));
}

// `src/x.py:42` and `x.py:42:7` name the file; the line is for the reader.
export function lineless(path) {
  return String(path || "").trim().replace(/:\d+(?::\d+)?$/, "");
}

export const isRelative = (path) => !/^[~/]/.test(String(path || "").trim());

// A relative .md in a tab without a project is a vault note: it stays an
// Obsidian link (VaultLink), as it was before files were linkable.
export const isVaultNote = (path, bases) =>
  !bases?.length && isRelative(path) && lineless(path).endsWith(".md");
