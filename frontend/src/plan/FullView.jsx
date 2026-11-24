import { useCallback, useRef } from "react";
import Icon from "../ui/icons.jsx";
import ErrorBoundary from "../ui/ErrorBoundary.jsx";
import { SheetBody, sheetTitle } from "../widgets/views.jsx";

// Full views: a strip of tabs taking the stage and side column; each stays mounted (data warm), newest
// last. Esc closes the newest, "← Back" all. Re-opening one shows it where it sits (no shuffle).
export default function FullView({ stack, showing, onShow, onClose, onBack, onPin, onOpen }) {
  // Focus enters the view on open (Tab starts in it) and returns on close.
  const opener = useRef(null);
  const holdFocus = useCallback((el) => {
    if (el) {
      opener.current = document.activeElement;
      el.querySelector(".fv-body:not([hidden])")?.focus({ preventScroll: true });
    } else if (opener.current?.isConnected) {
      opener.current.focus({ preventScroll: true });
    }
  }, []);
  return (
    <section className="full-view" ref={holdFocus}>
      <div className="fv-tabs" role="tablist">
        <button className="pbtn fv-back" onClick={onBack}
          title="Close every view · Esc closes the newest · ⌘K opens another">← Back</button>
        {stack.map((key) => (
          <span key={key} role="tab" aria-selected={key === showing} tabIndex={0}
            className={`fv-tab${key === showing ? " on" : ""}`} onClick={() => onShow(key)}
            onKeyDown={(e) => {
              if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); onShow(key); }
            }}>
            {sheetTitle(key)}
            <button className="fv-x" aria-label={`Close ${sheetTitle(key)}`}
              onClick={(e) => { e.stopPropagation(); onClose(key); }}>
              <Icon name="close" size={11} />
            </button>
          </span>
        ))}
      </div>
      {stack.map((key) => (
        <div key={key} className="fv-body sheet-body" data-sheet={key} hidden={key !== showing} tabIndex={-1}>
          <ErrorBoundary label={sheetTitle(key)}>
            <SheetBody id={key} onPin={onPin} onOpen={onOpen} />
          </ErrorBoundary>
        </div>
      ))}
    </section>
  );
}
