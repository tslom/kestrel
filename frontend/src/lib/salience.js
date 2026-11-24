// How much each widget has to say today: the desk's "sections are earned" rule.
// Design: DESIGN.md → Desk rank (`lib/salience.js`).
export const NEEDS = 2; // something to act on today
export const HAS = 1;   // real content, but nothing owed
export const QUIET = 0; // nothing today

// A card's hook returns a null level while its fetch is out: reporting QUIET on empty initial state would bury it.

// `oauth.py` writes this message for a terminal; WidgetError answers it with a connect button.
export const NO_TOKENS = /no tokens for (\w+)\/(\S+)/;

// A failed fetch is NEEDS whatever the card would otherwise report; an expired credential must not look like a broken widget.
export function errorSummary(error) {
  const m = NO_TOKENS.exec(error || "");
  return m ? `${m[1]}/${m[2]} needs reconnecting` : `couldn't load — ${error}`;
}
