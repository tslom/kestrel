import { useCallback, useEffect, useState } from "react";
import { useTimeout } from "../lib/timers.js";
import { dragPin } from "../lib/drag.js";
import { HAS, QUIET } from "../lib/salience";
import { BLOCKING, CLOCK, BROKEN } from "../lib/asks.js";
import { DESK, CARD_NAME } from "../widgets/cards.js";
import Icon from "../ui/icons.jsx";

// The plan view's side column at rest: what needs you, then everything else
// as one line per card.
//
// A row says what, since when, and what to do, and has one way out: act,
// snooze ("later"), or open. Acting holds the list's order in place — the
// finished row stays, marked done — until your pointer leaves the list, so
// nothing jumps under the cursor mid-triage (the settle rule). The keyboard
// works the list like an inbox: j/k move, ↵ does the row's first action,
// s then 1–4 snoozes, o opens the card, Esc backs out.

const TIER_WORD = { [BLOCKING]: "blocking a turn", [CLOCK]: "has a clock on it",
                    [BROKEN]: "broken source" };

// How long the settle rule holds after the last action if the pointer never
// leaves (a keyboard-only run).
const SETTLE_MS = 6000;

// What a row sends when it's dragged into the prompt: the ask itself, minus
// the buttons' functions, which don't survive JSON anyway.
function askPin(a) {
  return [`needs you · ${a.title}`, {
    card: a.card, what: a.title, why: a.why, kind: TIER_WORD[a.tier] || "owed",
    actions: a.acts.map((x) => x.label), note: a.note?.path,
  }];
}

const fmtUntil = (iso) => {
  const d = new Date(iso), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
  return days === 0 ? time : days === 1 ? `tomorrow ${time}`
    : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
};

function AskRow({ a, done, hot, on, cursor, snoozeOpen, options, onAct, onOpen, onOpenNote, onLater, onSnooze }) {
  const canSnooze = a.tier !== BLOCKING;
  return (
    <div className={`q${a.kind ? ` ${a.kind}` : ""}${hot ? " hot" : ""}${on ? " on" : ""}${cursor ? " kcur" : ""}${done ? " done" : ""}`}
      data-card={a.card || undefined} data-ask={a.id} {...(done ? {} : dragPin(...askPin(a)))}>
      <span className="q-dot" />
      {a.card ? (
        <button className="q-title" onClick={() => onOpen(a.card)}
          title={`Open ${CARD_NAME[a.card] || a.card}`}>{a.title}</button>
      ) : (
        // A console ask belongs to no card: its buttons are the way out.
        <span className="q-title">{a.title}</span>
      )}
      <div className="q-acts">
        {done ? (
          <span className="q-done">{done === true ? "done" : `until ${fmtUntil(done)}`}</span>
        ) : a.acts.map((x, i) => (
          <button key={i} className={`pbtn${x.style ? ` ${x.style}` : ""}`}
            onClick={() => onAct(a, x)}>{x.label}</button>
        ))}
      </div>
      {(a.why || a.note) && (
        <div className="q-why">
          {a.why}
          {a.note && (
            <button className="note-pill" title={a.note.path}
              onClick={(e) => { e.stopPropagation(); onOpenNote(a.note.path); }}
              {...dragPin(`note · ${a.note.title}`, { path: a.note.path, title: a.note.title })}>
              {a.note.title}
            </button>
          )}
        </div>
      )}
      {!done && canSnooze && (
        <button className="q-later" aria-expanded={snoozeOpen} onClick={() => onLater(a.id)}
          title="Snooze: it leaves the list and comes back on its own">later</button>
      )}
      {!done && snoozeOpen && (
        <div className="q-snooze">
          back
          {options.map((o, i) => (
            <button key={o.label} className="pbtn" title={o.hint} onClick={() => onSnooze(a, o)}>
              {cursor && <kbd>{i + 1}</kbd>} {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CardLine({ id, summary, quiet, hot, on, onOpen }) {
  return (
    <button className={`row-line${quiet ? " quiet" : ""}${hot ? " hot" : ""}${on ? " on" : ""}`} data-card={id}
      aria-current={on || undefined}
      onClick={() => onOpen(id)} {...dragPin(`${CARD_NAME[id].toLowerCase()} · today`, { card: id, summary })}>
      <span className="row-nm">{CARD_NAME[id]}</span>
      <span className="row-sm">{summary}</span>
    </button>
  );
}

// The quiet cards fold away: the map already shows they're there, and a list
// of what doesn't need you is the longest thing in the column. Remembered per
// browser, like the theme.
const QUIET_KEY = "needs_quiet_open";

export default function NeedsList({ asks, snoozed, sal, loading, cleared, nextClock, waiting,
                                    keysOn, snoozeOptions, onAct, onSnooze, onWake, onOpen, onOpenNote,
                                    onHide, onDock, hot, onHot, current }) {
  const lines = (lvl) => DESK.filter(([id]) => sal[id]?.level === lvl).map(([id]) => id);
  const today = lines(HAS);
  const quiet = lines(QUIET);
  const [quietOpen, setQuietOpen] = useState(() => localStorage.getItem(QUIET_KEY) === "1");
  const toggleQuiet = () => setQuietOpen((v) => {
    localStorage.setItem(QUIET_KEY, v ? "0" : "1");
    return !v;
  });

  // ---- the settle rule: after an action, hold the order and show the
  // finished rows in place until the pointer leaves (or SETTLE_MS passes).
  const [frozen, setFrozen] = useState(null);   // { order: [id], items: {id: ask}, done: {id: true|until} }
  const settleT = useTimeout();
  const settle = useCallback(() => { settleT.clear(); setFrozen(null); }, [settleT]);
  const hold = (id, how) => {
    setFrozen((f) => {
      const base = f || { order: asks.map((a) => a.id), items: Object.fromEntries(asks.map((a) => [a.id, a])), done: {} };
      return { ...base, done: { ...base.done, [id]: how } };
    });
    settleT.start(settle, SETTLE_MS);
  };

  const live = Object.fromEntries(asks.map((a) => [a.id, a]));
  const rows = frozen
    ? [...frozen.order.map((id) => ({ a: live[id] || frozen.items[id],
                                      done: live[id] && !frozen.done[id] ? null : frozen.done[id] || true })),
       ...asks.filter((a) => !frozen.order.includes(a.id)).map((a) => ({ a, done: null }))]
    : asks.map((a) => ({ a, done: null }));
  const actionable = rows.filter((r) => !r.done).map((r) => r.a);

  const act = (a, x) => {
    // Only an action that does something finishes the row; opening a card,
    // a sheet or a draft leaves it where it is.
    if (x.run) hold(a.id, true);
    onAct(a, x);
  };
  const [snoozeOpen, setSnoozeOpen] = useState(null);
  const snooze = (a, o) => {
    setSnoozeOpen(null);
    onSnooze(a, o).then((until) => { if (until) hold(a.id, until); });
  };

  // ---- keyboard triage
  const [kcur, setKcur] = useState(null);
  const [showSnoozed, setShowSnoozed] = useState(false);
  useEffect(() => {
    if (!keysOn) return;
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable) return;
      if (t.closest?.(".console-pane, .prompt-bar")) return;
      // A focused control's own Enter wins; a waiting approval card keeps
      // Enter and Esc for itself.
      if (e.key === "Enter" && /^(BUTTON|A)$/.test(t.tagName)) return;
      if ((e.key === "Enter" || e.key === "Escape") && waiting) return;
      const ids = actionable.map((a) => a.id);
      if (!ids.length) return;
      const i = ids.indexOf(kcur);
      const a = i >= 0 ? actionable[i] : null;
      let handled = true;
      if (e.key === "j" || e.key === "k") {
        const next = ids[i < 0 ? 0 : Math.max(0, Math.min(ids.length - 1, i + (e.key === "j" ? 1 : -1)))];
        setKcur(next);
        if (snoozeOpen && snoozeOpen !== next) setSnoozeOpen(null);
        const na = actionable.find((x) => x.id === next);
        onHot(na?.card || null);
      } else if (!a) handled = false;
      else if (e.key === "Enter") act(a, a.acts[0]);
      else if (e.key === "s" && a.tier !== BLOCKING) setSnoozeOpen((s) => (s === a.id ? null : a.id));
      else if (/^[1-4]$/.test(e.key) && snoozeOpen === a.id) {
        const o = snoozeOptions(a)[+e.key - 1];
        if (o) snooze(a, o); else handled = false;
      } else if (e.key === "o" && a.card) onOpen(a.card);
      else if (e.key === "Escape" && snoozeOpen === a.id) setSnoozeOpen(null);
      else if (e.key === "Escape") { setKcur(null); onHot(null); }
      else handled = false;
      if (handled) { e.preventDefault(); e.stopImmediatePropagation(); }
    };
    // Capture, so a handled Esc never reaches the stack's Esc (which would
    // close the side column under the cursor).
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  });
  useEffect(() => {
    document.querySelector(`.q.kcur`)?.scrollIntoView({ block: "nearest" });
  }, [kcur]);

  const count = asks.length;
  return (
    <>
      <div className="side-head">
        <h2 {...dragPin(`needs you · all ${count}`, asks.map((a) => askPin(a)[1]))}>
          Needs you {count > 0 && <span className="count">{count}</span>}
        </h2>
        {cleared > 0 && (
          <span className="cleared" title="Asks you finished today; snoozed ones don't count">
            {cleared} cleared today
          </span>
        )}
        {/* Put away, the column is a slide-over; `onDock` (set only then)
            brings it back as a column on a wide window. */}
        {onDock && (
          <button className="side-x side-dock" onClick={onDock} aria-label="Keep Needs you open beside the map"
            title="Keep open beside the map"><Icon name="pin" size={12} /></button>
        )}
        <button className="side-x" onClick={onHide} aria-label="Put Needs you away"
          title="Put away — the map and Kestrel take the window · the strip's “need you” brings it back">
          <Icon name="close" size={12} /></button>
      </div>
      {/* Hovering any row lights its card's node on the map, and the other
          way round (`hot`, shared through PlanApp). Leaving the list lets
          the settle rule go. */}
      <div className="side-body"
        onPointerOver={(e) => {
          const id = e.target.closest?.("[data-card]")?.dataset.card;
          if (id && id !== hot) onHot(id);
        }}
        onPointerLeave={() => { onHot(null); if (frozen) settle(); }}>
        {rows.map(({ a, done }) => (
          <AskRow key={a.id} a={a} done={done} hot={hot && hot === a.card}
            on={!!current && current === a.card} cursor={kcur === a.id}
            snoozeOpen={snoozeOpen === a.id} options={snoozeOpen === a.id ? snoozeOptions(a) : []}
            onAct={act} onOpen={onOpen} onOpenNote={onOpenNote} onSnooze={snooze}
            onLater={(id) => setSnoozeOpen((s) => (s === id ? null : id))} />
        ))}
        {/* Once you're on the keys, not before: help for a mode you aren't in
            is clutter. */}
        {kcur != null && actionable.length > 0 && (
          <p className="keys-hint"><kbd>j</kbd><kbd>k</kbd> move · <kbd>↵</kbd> act · <kbd>s</kbd> snooze · <kbd>o</kbd> open</p>
        )}
        {rows.length === 0 && (loading
          ? <><div className="skel" /><div className="skel w60" /></>
          : (
            // The state to aim for every day, drawn rather than left blank.
            <div className="all-clear">
              <svg width="28" height="28" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="8" cy="8" r="6.5" /><path d="m5.2 8.2 1.9 1.9 3.8-4" />
              </svg>
              <b>Nothing needs you</b>
              {cleared > 0 && <span>You cleared {cleared} today.</span>}
              {nextClock && <span>Next with a clock: {nextClock}.</span>}
              {snoozed.length > 0 && <span>{snoozed.length} snoozed. They come back on their own.</span>}
            </div>
          ))}
        {snoozed.length > 0 && (
          <>
            <button className="row-line snz-line" aria-expanded={showSnoozed}
              onClick={() => setShowSnoozed((v) => !v)}>
              <span className="row-nm">snoozed</span>
              <span className="row-sm">{snoozed.length} · {showSnoozed ? "hide" : "show"}</span>
            </button>
            {showSnoozed && snoozed.map(({ ask, until }) => (
              <div key={ask.id} className="snz-row">
                <span>{ask.title}<em>until {fmtUntil(until)}</em></span>
                <button className="pbtn" onClick={() => onWake(ask)}>Bring back</button>
              </div>
            ))}
          </>
        )}
        {today.length > 0 && (
          <>
            <div className="band-line">today</div>
            {today.map((id) => <CardLine key={id} id={id} summary={sal[id].summary} hot={hot === id}
              on={current === id} onOpen={onOpen} />)}
          </>
        )}
        {quiet.length > 0 && (
          <>
            <button className="band-line band-toggle" aria-expanded={quietOpen} onClick={toggleQuiet}>
              <Icon name={quietOpen ? "chevronDown" : "chevronRight"} size={9} />
              quiet · {quiet.length}
            </button>
            {quietOpen && quiet.map((id) => <CardLine key={id} id={id} summary={sal[id].summary} quiet hot={hot === id}
              on={current === id} onOpen={onOpen} />)}
          </>
        )}
      </div>
    </>
  );
}
