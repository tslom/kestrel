// How much each widget has to say today — the brief's "sections are earned"
// rule (brief.py's SYSTEM) applied to the desk. With every widget always
// visible, in a fixed order, at equal weight, a day with nothing in it looks
// exactly as busy as a day with everything in it.
//
// Levels, not a boolean: a card holding real content still shouldn't outrank
// one that wants something from you.
export const NEEDS = 2; // something to act on today
export const HAS = 1;   // real content, but nothing owed
export const QUIET = 0; // nothing today

// Each card's hook (widgets/cards.js) returns its own { level, summary }, with
// a null level while the fetch is still out — a card that reported QUIET on
// its empty initial state would be ranked and buried before its data landed.

// A provider that has lost its credential is by far the most common widget
// error, and `oauth.py` writes that message for a terminal. WidgetError
// answers it with a connect button; the fold line names it the same way.
export const NO_TOKENS = /no tokens for (\w+)\/(\S+)/;

// What a card with an error says folded. A failed fetch is NEEDS whatever the
// card would otherwise report: an expired credential is the definition of
// something to act on today, and it stays broken until you do — and a broken
// credential and a broken widget must not look identical.
export function errorSummary(error) {
  const m = NO_TOKENS.exec(error || "");
  return m ? `${m[1]}/${m[2]} needs reconnecting` : `couldn't load — ${error}`;
}
