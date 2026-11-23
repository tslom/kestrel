import { useCallback, useEffect, useRef, useState } from "react";
import { GMAIL_URL, pinEmail, dragEmail, shortLabel } from "../lib/emailPin.js";
import { useGoogleAddresses } from "../lib/googleLinks";
import { fetchJSON, postJSON } from "../lib/api";
import { broadcastRefresh } from "../lib/refresh";
import { offerUndo } from "../lib/undo.js";
import EmailReader from "../ui/EmailReader.jsx";
import Files from "../ui/Files.jsx";
import Icon from "../ui/icons.jsx";
import SheetHead from "../ui/SheetHead.jsx";

// Email review queue (IDEAS §5): one card per inbox message — AI summary,
// recommended action, and the label it will be filed under. Three actions:
// archive, unsubscribe, keep — and every one files the message under its
// label, because after review the only thing a message is still for is being
// found. The label arrives already picked (the sender's record, else the
// model's); correcting it is picking another before acting, and two
// corrections teach the sender. Everything mutates Gmail itself via
// POST /api/email/{id}/action; the card leaves the queue on success.

function fmtWhen(iso) {
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

const ACTION_LABELS = { archive: "Archive", unsubscribe: "Unsub", keep: "Keep" };
// What the undo toast says was done (`performed` is the capability for an
// unsubscribe, so those map too).
const PAST = { archive: "Archived", keep: "Kept", unsubscribe: "Unsubscribed",
               one_click: "Unsubscribed", mailto: "Unsubscribed", delete: "Deleted" };

function LabelPicker({ labels, current, onPick }) {
  const [draft, setDraft] = useState("");
  return (
    <div className="email-labels">
      {labels.map((l) => (
        <button key={l.name} title={l.use || l.name}
          className={`chip${current === l.name ? " active" : ""}`}
          onClick={() => onPick(l.name)}>
          {shortLabel(l.name)}
        </button>
      ))}
      <input value={draft} placeholder="new label…"
        title="A new label under Filed/ — use / to nest (Courses/CSCI 104)"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && draft.trim()) onPick(`Filed/${draft.trim()}`);
        }} />
    </div>
  );
}

function Card({ card, labels, hovered, onHover, onPin, onDone, onHold, onError }) {
  const addrs = useGoogleAddresses();      // re-render when authuser= resolves
  const [busy, setBusy] = useState(null);   // action name in flight
  const [picking, setPicking] = useState(false);
  const [reading, setReading] = useState(false);   // reader open on this card
  // The label this card will be filed under. Starts as the suggestion; a
  // pick replaces it without acting, so the correction and the action stay
  // one gesture each and either can come first.
  const [label, setLabel] = useState(card.label);
  const corrected = label !== card.label;

  const act = (action) => {
    // A request that would actually reach the sender waits out the undo
    // window first (EmailReview's hold). "link" only opens a page, so it
    // goes straight through as before.
    if (action === "unsubscribe" && ["one_click", "mailto"].includes(card.unsubscribe)) {
      onHold(card, label);
      return;
    }
    setBusy(action);
    postJSON(`/api/email/${card.id}/action`, { action, label })
      .then((res) => {
        if (res.performed === "link" && res.open_url) {
          // no programmatic unsubscribe — open the link, keep the card
          window.open(res.open_url, "_blank", "noopener");
          setBusy(null);
          return;
        }
        onDone(card.id, res.performed, card.subject);
      })
      .catch((e) => { setBusy(null); onError(e.message); });
  };

  // The recommendation used to come off the message body alone and never saw
  // the List-Unsubscribe header, so a header-less newsletter came back with a
  // pick the Unsub button can't perform. It now comes from what you actually
  // did with this sender (providers/email_learn.py), but the same mismatch is
  // still reachable — you may have unsubscribed from this sender through a
  // message that carried a header when this one doesn't. Archive stays the
  // honest substitute: it still clears the inbox, and it keeps the highlight
  // on a button that presses instead of an ink-filled disabled one.
  const unsubDead = card.action === "unsubscribe" && card.unsubscribe === "none";
  const rec = unsubDead ? "archive" : card.action;

  // The recommendation in one gesture: click the "→ archive" chip, or hover
  // the card and hit ↵. It files under whatever the label chip says, so a
  // correction followed by ↵ is still two keystrokes' worth of review. All
  // three actions are ⌘Z-undoable — except the unsubscribe request itself,
  // which is already sent by the time the archive is reversed.
  const runRec = () => {
    if (!rec || busy !== null) return;
    act(rec);
  };

  // Only the hovered card listens, so the queue never has two ↵ handlers.
  useEffect(() => {
    if (!hovered || !rec) return;
    const onKey = (e) => {
      if (e.key !== "Enter" || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      // ↵ on a focused control is that control's own click — don't double-fire.
      const t = e.target;
      if (/^(INPUT|TEXTAREA|BUTTON|A|SELECT)$/.test(t.tagName) || t.isContentEditable) return;
      e.preventDefault();
      runRec();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const btn = (action, opts = {}) => (
    <button key={action}
      className={`email-act${rec === action ? " recommended" : ""}`}
      disabled={busy !== null || opts.disabled}
      title={opts.title}
      onClick={opts.onClick || (() => act(action))}>
      {busy === action ? "…" : ACTION_LABELS[action]}
    </button>
  );

  return (
    <article
      className={`email-card${card.unread ? " unread" : ""}${hovered && rec ? " armed" : ""}`}
      onMouseEnter={() => onHover(card.id)} onMouseLeave={() => onHover(null)}
      {...dragEmail(card)}>
      <header>
        <span className="email-from">{card.from_name || card.from_addr}</span>
        <span className="email-when">{fmtWhen(card.received)}</span>
        <a className="email-gmail" href={GMAIL_URL(card.id)} target="_blank"
          rel="noopener noreferrer"
          title={`Open in Gmail${addrs.gmail ? ` (${addrs.gmail})` : ""}`}>Gmail <Icon name="external" size={11} /></a>
        <button className="pin-mini" onClick={() => onPin(card)}
          title="Attach this email to your next console message"><Icon name="pin" /></button>
      </header>
      <p className="email-subject">
        <button className="email-open" onClick={() => setReading(true)}
          aria-haspopup="dialog" title="Read the message">
          {card.subject}</button>
        <Files items={card.attachments} /></p>
      {reading && <EmailReader id={card.id} row={card} onClose={() => setReading(false)} />}
      {card.summary
        ? <p className="email-ai">{card.summary}
            {rec && (
              <button className="email-rec" onClick={runRec} disabled={busy !== null}
                title={unsubDead
                  ? "You unsubscribed from this sender before, but this message has no List-Unsubscribe header — archiving instead"
                  : `Do it: ${rec}${label ? ` → ${shortLabel(label)}` : ""} (hover + ↵)`}>
                → {rec}
                {unsubDead && <span className="email-rec-note"> (no unsub header)</span>}
                <span className="email-rec-key">↵</span>
              </button>
            )}
          </p>
        : <p className="email-ai dim">{card.snippet}</p>}
      {/* Why this call, in your own history's words. A recommendation drawn
          from two clicks has to admit that before it is trusted — the rule
          §24 already follows for the place shortlist. `default` is the
          unknown-sender fallback and says nothing worth a line of card. */}
      {card.why && card.basis !== "default" && (
        <p className={`email-why email-why-${card.basis}`}>
          <Icon name="clock" size={11} />
          {/* One span, not loose text plus a sibling: as separate flex items
              the note detached from the sentence and drifted to its own
              column once the reason wrapped to a second line. */}
          <span>{card.why}
            {card.unsubscribable && rec !== "unsubscribe" && (
              <span className="email-why-unsub"> · looks unsubscribable</span>
            )}
          </span>
        </p>
      )}
      <div className="email-actions">
        {btn("archive")}
        {btn("unsubscribe", {
          disabled: card.unsubscribe === "none",
          title: {
            one_click: "One-click unsubscribe (RFC 8058)",
            mailto: "Unsubscribe by email via Gmail",
            link: "Opens the sender's unsubscribe page",
            none: "No List-Unsubscribe header",
          }[card.unsubscribe],
        })}
        {btn("keep", { title: "Leave in inbox (labelled), drop from this queue" })}
        {/* Where it will be filed. The chip is the correction: open, pick,
            then act — every button above files under whatever this says. */}
        <button className={`email-label${label ? "" : " none"}${corrected ? " corrected" : ""}`}
          aria-expanded={picking} disabled={busy !== null}
          onClick={() => setPicking((v) => !v)}
          title={label
            ? `Filed under ${label} — ${corrected ? "your pick"
              : card.label_basis === "sender" ? card.label_why
              : "the model's pick"}. Click to change.`
            : "No label yet — click to pick one"}>
          <Icon name="tag" size={11} />
          {label ? shortLabel(label) : "no label"}
          {card.label_basis === "sender" && !corrected && <span className="email-label-basis"> · sender</span>}
          <Icon name="chevronDown" size={11} />
        </button>
      </div>
      {picking && (
        <LabelPicker labels={labels} current={label}
          onPick={(name) => { setLabel(name); setPicking(false); }} />
      )}
    </article>
  );
}

export default function EmailReview({ onPin }) {
  const [queue, setQueue] = useState(null);
  const [labels, setLabels] = useState([]);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  // Which card ↵ acts on. Hover is the cursor here: the queue is a mouse-first
  // list, and a card that leaves on its own action would strand a selection.
  const [hoverId, setHoverId] = useState(null);
  // Undo stack, newest last: every action taken in this session, so ⌘Z can
  // walk back through a run of fast archives, not just the most recent one.
  // The ref is the stack; the state only mirrors it for rendering. React runs
  // a setState updater during render, not during the event, so reading the top
  // entry inside one saw `undefined` — undo popped the card and then returned
  // before sending anything, which looked exactly like an undo that lost the
  // mail: button gone, nothing back in the inbox.
  const stackRef = useRef([]);
  const [undoStack, setUndoStack] = useState([]);
  const [undoing, setUndoing] = useState(false);
  const setStack = useCallback((next) => {
    stackRef.current = next;
    setUndoStack(next);
  }, []);

  const load = useCallback(() => {
    fetchJSON("/api/email/queue")
      .then((q) => { setQueue(q); setError(null); })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    fetchJSON("/api/email/labels").then(setLabels).catch(() => setLabels([]));
  }, [load]);

  // Reverse one action and put its card back in the queue: the newest, from
  // the button here, or the one the undo toast was offering. Optimistic
  // removal so a double press can't fire the same undo twice.
  const undoEntry = useCallback((entry) => {
    if (!entry || !stackRef.current.includes(entry)) return;
    setStack(stackRef.current.filter((x) => x !== entry));
    setUndoing(true);
    fetchJSON(`/api/email/${entry.id}/undo`, { method: "POST" })
      .then((res) => {
        setError(res.unsubscribe_sent
          ? "Un-archived — but the unsubscribe request was already sent."
          : null);
        load();
        broadcastRefresh("email");   // the card is back; so is the count
      })
      .catch((e) => {
        setError(e.message);
        setStack([...stackRef.current, entry]); // failed: leave it undoable
      })
      .finally(() => setUndoing(false));
  }, [load, setStack]);
  const undo = useCallback(() => undoEntry(stackRef.current[stackRef.current.length - 1]),
    [undoEntry]);
  // ⌘Z is the undo toast's now (lib/undo.js): each action offers itself
  // there, so one keypress can't undo twice. The button here still walks
  // back through the whole run.

  // Sync Gmail + summarize new mail, then reload the queue. Summarization
  // runs server-side on Haiku; this can take a minute with a lot of new mail.
  const refresh = () => {
    setRefreshing(true);
    fetchJSON("/api/email/queue/refresh", { method: "POST" })
      .then(() => { setError(null); load(); broadcastRefresh("email"); })
      .catch((e) => setError(e.message))
      .finally(() => setRefreshing(false));
  };

  const done = (id, performed, subject) => {
    const entry = { id, action: performed };
    setStack([...stackRef.current, entry]);
    offerUndo({
      label: `${PAST[performed] || performed} “${subject || "message"}”`,
      askId: queue?.count === 1 ? "email:review" : undefined,   // the queue's last one
      undo: () => undoEntry(entry),
    });
    hide(id);
  };

  // An unsubscribe can't be recalled once it's sent, so it isn't sent yet:
  // the card leaves now and the request goes out when the undo toast does.
  // Undo just brings the card back; nothing ever reached the sender.
  const hold = (card, label) => {
    hide(card.id);
    offerUndo({
      label: `Unsubscribing from ${card.from_name || card.from_addr}`,
      commit: () => postJSON(`/api/email/${card.id}/action`, { action: "unsubscribe", label })
        .then((res) => {
          setStack([...stackRef.current, { id: card.id, action: res.performed }]);
          broadcastRefresh("email");
        })
        .catch((e) => { setError(e.message); load(); }),
      undo: () => load(),
    });
  };

  const hide = (id) => {
    // The desk shows this same count (card + glance chip). Targeted, not a
    // whole-desk refresh: nothing else on the desk moved because one mail was
    // archived. Fired here rather than inside load(), which also runs on mount
    // — that would reload the email card just for opening the panel.
    broadcastRefresh("email");
    setQueue((q) => q && {
      ...q,
      count: q.count - 1,
      cards: q.cards.filter((c) => c.id !== id),
    });
  };

  const lastAction = undoStack[undoStack.length - 1];

  return (
    <>
    {/* No pin and no plain refresh: the queue is worked, not quoted, and its
        refresh is a Gmail sync that says so. */}
    <SheetHead refreshedAt={queue && `${queue.count} left${queue.unsummarized ? ` · ${queue.unsummarized} unsummarized` : ""}`}>
      {lastAction && (
        <button className="email-undo" onClick={undo} disabled={undoing}
          title={`Undo — puts it back in the inbox and the queue (⌘Z)`}>
          <Icon name="undo" /> undo {lastAction.action}
        </button>
      )}
      <button onClick={refresh} disabled={refreshing}
        title="Sync Gmail + summarize new mail">
        {refreshing ? "syncing…" : <><Icon name="refresh" /> sync</>}
      </button>
    </SheetHead>
    {error && <p className="widget-error">{error}</p>}
    {!queue && !error && <p className="widget-empty">Loading…</p>}
    {queue && queue.count === 0 && (
      <p className="widget-empty email-done">
        <Icon name="checkCircle" size={15} /> Inbox reviewed — nothing waiting.
      </p>
    )}
    {queue && queue.cards.map((card) => (
      <Card key={card.id} card={card} labels={labels}
        hovered={hoverId === card.id} onHover={setHoverId}
        onPin={(c) => pinEmail(onPin, c)}
        onDone={done} onHold={hold} onError={setError} />
    ))}
    </>
  );
}
