import { pinEmail, dragEmail } from "../lib/emailPin.js";
import { useWidgetData } from "../lib/refresh";
import { NEEDS, QUIET } from "../lib/salience";
import { OWED, age } from "../lib/asks.js";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// Compact email card: how big the review queue is, and who's in it.
// The queue is worked in EmailReview.jsx ("Review N"); the pane's Full view is
// the generic range expansion over GET /api/email.
export function useEmailCard() {
  // Topic "email": the review queue broadcasts after every action. /api/email/queue is an app.db read.
  const { data: queue, error, reload, busy: refreshing } =
    useWidgetData("/api/email/queue", "email");

  // The queue is the whole card: either it's asking for something or it isn't.
  const level = !queue ? null : queue.count > 0 ? NEEDS : QUIET;
  const summary = !queue?.count ? "inbox reviewed"
    : `${queue.count} to review${queue.unread ? ` · ${queue.unread} unread` : ""}`;

  // One row for the whole queue (seventeen would bury the list); the reason names the oldest.
  const oldest = queue?.cards.reduce(
    (o, c) => (!o || c.received < o.received ? c : o), null);
  const asks = queue?.count ? [{
    id: "email:review", card: "email", tier: OWED, kind: "",
    title: summary,
    why: oldest ? `oldest: “${oldest.subject}” · waiting ${age(oldest.received)}` : "",
    acts: [{ label: "Review", style: "go", sheet: "review" }],
  }] : [];

  return { queue, error, reload, refreshing, level, summary, asks };
}

export default function EmailWidget({ card, onPin, onReview }) {
  const { queue, error, reload, refreshing } = card;
  const peek = queue ? queue.cards : [];

  const pinData = queue && {
    source: "email", queue: queue.count, unread: queue.unread,
    cards: queue.cards.slice(0, 10).map(({ from_name, from_addr, subject, summary, action }) =>
      ({ from: from_name || from_addr, subject, summary, recommended: action })),
  };

  return (
    <section className="widget" {...dragPin("email · queue", pinData)}>
      <header>
        <h2>Email <span className="widget-sub">inbox</span></h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!queue}
            onClick={() => onPin("email · queue", pinData)}
            title="Attach the queue to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!queue && !error && <><div className="skel" /><div className="skel w60" /></>}
      {queue && (
        <div className="email-summary">
          {queue.count === 0
            ? <p className="widget-empty">Inbox reviewed — nothing waiting.</p>
            : <p className="email-count"><strong>{queue.count}</strong> to review · {queue.unread} unread</p>}
          {queue.count > 0 && (
            <ul className="email-peek" {...dragPin("email · the peek", pinData?.cards.slice(0, peek.length))}>
              {peek.map((c) => (
                <li key={c.id} {...dragEmail(c)}>
                  <span className="email-from">{c.from_name || c.from_addr}</span>
                  <span className="email-subject">{c.subject}</span>
                  <button className="pin-mini" onClick={() => pinEmail(onPin, c)}
                    title="Attach this email to your next console message"><Icon name="pin" /></button>
                </li>
              ))}
            </ul>
          )}
          {queue.count > 0 && (
            <button className="widget-footer-btn" onClick={onReview}>
              Review {queue.count} <Icon name="arrowRight" size={11} />
            </button>
          )}
        </div>
      )}
    </section>
  );
}
