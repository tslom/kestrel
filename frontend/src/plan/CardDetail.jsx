import { NEEDS, HAS } from "../lib/salience";
import { PANEL_FOR } from "../lib/palette";
import { CARD_NAME } from "../widgets/cards.js";
import { CardView } from "../widgets/views.jsx";
import ErrorBoundary from "../ui/ErrorBoundary.jsx";
import Icon from "../ui/icons.jsx";

// A card opened into the side column: the second of three depths (node, card, sheet; DESIGN.md →
// Layout: the plan view, Card anatomy). Same component over the same hook result, so opening never
// refetches and can't drift from what ranked it. Stacks over the list: close is ✕ or Esc.
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
