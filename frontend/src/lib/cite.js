// Resolving the `[cite:...]` refs in console answers: a ref is six hash chars; its content is behind /api/cite/{ref},
// read only when a pill opens (a long answer carries a dozen refs and few get clicked). The pill's collapsed text is
// the ref's kind, readable with no network. Cache is per page load, permanent, misses included.
// Design: DESIGN.md → `frontend/src/lib/cite.js`.
import { fetchJSON } from "./api";

const cache = new Map();   // ref -> Promise<resolved | null>

// Kind off the ref's first letter, no network: lets the collapsed pill say "email" before loading.
const LETTERS = { e: "email", v: "event", m: "message", d: "doc" };

export const KIND_RE = /^([evmd])[0-9a-f]{5,10}$/;

export function kindOf(ref) {
  const m = KIND_RE.exec(String(ref || "").toLowerCase());
  return m ? LETTERS[m[1]] : null;
}

export function resolveCite(ref) {
  const key = String(ref || "").toLowerCase();
  if (!cache.has(key)) {
    const p = fetchJSON(`/api/cite/${encodeURIComponent(key)}`)
      // A 404 is the expected answer for an invented ref: resolve null and the pill renders dead.
      .catch(() => null);
    cache.set(key, p);
  }
  return cache.get(key);
}
