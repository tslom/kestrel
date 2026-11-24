// Subsequence matching for the command palette. Greedy left-most scan: enough for ~30 fixed commands; replace
// it if it ever ranks note titles (greedy misses better alignments in long strings).

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

  // Weak tiebreaks (a real bonus outranks them): early matches, then short labels.
  score -= positions[0] * 0.5;
  score -= text.length * 0.05;
  return { score, positions };
}

// Keywords match as whole substrings, not fuzzily: subsequence over the keyword blob finds nonsense ("debug"
// picks letters out of "Debts") and beats the command that lists "debug".
const KEYWORD_HIT = -100;

/**
 * Filter and rank `commands`: titles match fuzzily and score; keywords only decide survival, so a title match
 * always outranks a keyword mention. Ties keep declaration order (an empty query returns the list untouched).
 * @returns {Array} commands with `positions` for title highlighting.
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
