// Resolving the `[cite:…]` refs the console writes into its answers.
//
// A ref is six characters of hash; everything worth showing lives behind
// /api/cite/{ref}. That read is deferred until a pill is opened, for the
// obvious reason and a less obvious one: a long answer can carry a dozen
// citations and almost none of them get clicked, so resolving on render would
// spend twelve round trips to change nothing on screen. The pill's own text
// is the ref's kind, which the ref itself already says.
//
// The cache is per page load and permanent within it. A ref is immutable by
// construction — it is a hash of what it points at — so the only thing that
// can change underneath is the row's own fields, and a transcript that has
// been on screen for an hour showing an hour-old subject line is not a
// problem worth a refetch. A miss is cached too: a 404 means "never handed
// out", which is a fact about the ref and not about the moment.
import { fetchJSON } from "./api";

const cache = new Map();   // ref -> Promise<resolved | null>

// Which kind a ref is, straight off its first letter — no network. This is
// what lets the collapsed pill say "email" before anything is loaded.
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
      // A 404 is the expected answer for a ref the console invented, not a
      // failure to report. It resolves to null and the pill renders dead.
      .catch(() => null);
    cache.set(key, p);
  }
  return cache.get(key);
}
