import { useState } from "react";
import { CARD_NAME } from "../widgets/cards.js";
import Icon from "../ui/icons.jsx";
import { focusFirst, roomById, roomOf, todayOrder } from "./Rooms.jsx";

// The focus line (#46): the top bar's second line carries the most urgent ask (answer, Later, or step
// past with ›). It follows Today's order (Rooms.jsx todayOrder); a stepped-past ask goes to the back,
// not away (still in its room and on Today).

export default function FocusLine({ asks: given, act, snooze, onOpen }) {
  const [passed, setPassed] = useState([]);   // ids stepped past, oldest first
  const asks = todayOrder(given);
  if (!asks.length) return null;
  const back = passed.map((id) => asks.find((a) => a.id === id)).filter(Boolean);
  const queue = [...asks.filter((a) => !passed.includes(a.id)), ...back];
  const top = queue[0];
  const step = () => setPassed((p) => [...p.filter((id) => id !== top.id), top.id]);
  // keyed by the ask, so an open Later menu doesn't carry over to the next one
  return <FocusItem key={top.id} a={top} n={asks.indexOf(top) + 1} of={asks.length}
    act={act} snooze={snooze} onOpen={onOpen} onStep={asks.length > 1 ? step : null} />;
}

function FocusItem({ a, n, of, act, snooze, onOpen, onStep }) {
  const [later, setLater] = useState(false);
  const opts = snooze && !String(a.id).startsWith("console:") ? snooze.options(a) : [];
  const room = roomById(roomOf(a.card));
  const where = a.card ? CARD_NAME[a.card] : "Kestrel";
  return (
    <div className={`hy-focus${a.kind === "blocking" ? " blocking" : ""}${a.kind === "fix" ? " fix" : ""}`}
      role="group" aria-label={`Needs you, ${n} of ${of}`} data-ask-card={a.card || undefined}>
      <span className="hy-focus-dot" aria-hidden="true" />
      {a.card ? (
        <button className="hy-focus-where" style={room ? { "--room": room.tint } : undefined}
          onClick={() => onOpen(a.card)} title={`Open ${where}`}>
          {room && <span className="cx-room-icon"><Icon name={room.icon} size={10} /></span>}
          {where}
        </button>
      ) : <span className="hy-focus-where static">{where}</span>}
      <span className="hy-focus-text" title={a.why ? `${a.title} · ${a.why}` : a.title}>
        <b>{a.title}</b>{a.why && <span className="hy-focus-why"> · {a.why}</span>}
      </span>
      <span className="hy-focus-acts">
        {a.acts.slice(0, 2).map((x) => (
          <button key={x.label} className={`pbtn${x.style === "go" ? " go" : ""}`} onClick={() => act(a, x)}>{x.label}</button>
        ))}
        {opts.length > 0 && (
          <span className="hy-later-wrap"
            onKeyDown={(e) => {
              if (e.key !== "Escape" || !later) return;
              e.stopPropagation();
              setLater(false);
              e.currentTarget.querySelector(".pbtn")?.focus();
            }}
            >
            <button className={`pbtn ghost${later ? " on" : ""}`} aria-expanded={later} aria-haspopup="menu"
              onClick={() => setLater((v) => !v)} title="Snooze it">Later</button>
            {later && (
              <span className="hy-later" role="menu" aria-label="Snooze until" ref={focusFirst}>
                {opts.map((o) => (
                  <button key={o.label} role="menuitem" onClick={() => { setLater(false); snooze.run(a, o); }}>
                    <span>{o.label}</span>{o.hint && <span className="hy-later-hint">{o.hint}</span>}
                  </button>
                ))}
              </span>
            )}
          </span>
        )}
      </span>
      <span className="hy-focus-nav">
        <span className="hy-focus-count">{n} of {of}</span>
        {onStep && <button className="cx-icon-btn" onClick={onStep} title="Next: step past this one for now"
          aria-label="Next ask"><Icon name="chevronRight" size={13} /></button>}
      </span>
    </div>
  );
}
