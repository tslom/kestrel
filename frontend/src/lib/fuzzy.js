// Subsequence matching for the command palette. Small on purpose: the palette
// ranks ~30 fixed commands, not a vault's worth of notes, so a greedy
// left-most scan is enough and stays readable. If this ever ranks note titles,
// replace it — greedy can miss the better alignment of a query against a long
// string, and at that size you'd notice.

// Anything that starts a "word" — a match landing here reads as intentional
// ("cal" hitting Calendar) rather than incidental ("cal" hitting Local).
const BOUNDARY = /[\s·\-–—_/:.]/;

/**
 * Score `query` against `text`, case-insensitively.
 * @returns {{score: number, positions: number[]} | null} null if no match.
 */
export function match(text, query) {
  if (!query) return { score: 0, positions: [] };
  const t = text.toLowerCase();
  const q = query.toLowerCase();

  const positions = [];
  let at = 0;
  let score = 0;

  for (const ch of q) {
    const found = t.indexOf(ch, at);
    if (found === -1) return null;

    score += 1;
    const prev = found > 0 ? t[found - 1] : " ";
    if (found === 0 || BOUNDARY.test(prev)) score += 8;   // starts a word
    if (positions[positions.length - 1] === found - 1) score += 5;  // runs on

    positions.push(found);
    at = found + 1;
  }

  // Two tiebreaks, both weak enough that a real bonus above always outranks
  // them: matches that start early beat late ones, and short labels beat long
  // ones holding the same match.
  score -= positions[0] * 0.5;
  score -= text.length * 0.05;
  return { score, positions };
}

// Keywords are alternative names for a command, and they are matched as
// substrings rather than fuzzily. Subsequence matching over the keyword blob
// finds nonsense — "debug" picks the d-e-b out of "Debts" and the u-g out of
// its own trailing "jump…widget" — and beats the command that actually lists
// "debug". You type a keyword because you know the word, so requiring it whole
// costs nothing and removes the junk.
const KEYWORD_HIT = -100;

/**
 * Filter and rank `commands` against `query`.
 *
 * Titles match fuzzily and score; keywords only decide whether a command
 * survives at all. That gap is deliberate and wide — a title match always
 * sorts above a command that merely mentions the word — and ties fall back to
 * declaration order, which is why an empty query returns the list untouched.
 *
 * @returns {Array} commands, each with `positions` for title highlighting.
 */
export function rank(commands, query) {
  const q = query.trim();
  if (!q) return commands.map((c) => ({ ...c, positions: [] }));

  const lower = q.toLowerCase();
  return commands
    .map((c, i) => {
      const onTitle = match(c.title, q);
      if (onTitle) return { ...c, i, score: onTitle.score, positions: onTitle.positions };
      if (c.keywords?.toLowerCase().includes(lower)) {
        return { ...c, i, score: KEYWORD_HIT, positions: [] };
      }
      return null;
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score || a.i - b.i);
}
