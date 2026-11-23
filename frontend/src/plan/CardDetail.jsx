import { NEEDS, HAS } from "../lib/salience";
import { PANEL_FOR } from "../lib/palette";
import { CARD_NAME } from "../widgets/cards.js";
import { CardView } from "../widgets/views.jsx";
import ErrorBoundary from "../ui/ErrorBoundary.jsx";
import Icon from "../ui/icons.jsx";

// A card opened into the side column (which widens to a reading width for
// it): the second of a card's three depths — the node says whether it needs
// you, this is the card itself, whole, the full view is its sheet. Only a card
// with a sheet has the third; the rest are complete here (DESIGN.md → Card
// anatomy). It stacks over the "needs you" list, which stays below it, so
// closing it is ✕ (or Esc) rather than a way back to somewhere.
//
// The card is the same component the rail desk drew, over the same hook
// result, so opening it never refetches and what it shows can't drift from
// what ranked it. (The source's 24-sync history joins it with the map's
// rings, IDEAS step 5.)
export default function CardDetail({ id, card, level, onClose, onFull, onPin, openSheet }) {
  const band = level === NEEDS ? "needs" : level === HAS ? "has" : "quiet";
  const word = level === NEEDS ? "needs you" : level === HAS ? "today" : "quiet";
  const sheet = PANEL_FOR[id];
  return (
    <>
      <div className="side-head">
        {level != null && <span className={`band-pill ${band}`}>{word}</span>}
        <span className="grow" />
        {sheet && (
          <button className="pbtn" onClick={() => onFull(sheet)}
            title="Open the full view · Esc closes">⤢ Full view</button>
        )}
        <button className="side-x" aria-label={`Close ${CARD_NAME[id]}`} title="Close · Esc"
          onClick={onClose}><Icon name="close" size={12} /></button>
      </div>
      <div className="side-body detail">
        <ErrorBoundary label={CARD_NAME[id]}>
          <CardView id={id} card={card} onPin={onPin} openSheet={openSheet} />
        </ErrorBoundary>
      </div>
    </>
  );
}
