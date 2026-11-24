import { useRef, useState } from "react";
import { NEEDS, HAS } from "../lib/salience";
import { PANEL_FOR } from "../lib/palette";
import { CARD_NAME } from "../widgets/cards.js";
import { CardView } from "../widgets/views.jsx";
import ErrorBoundary from "../ui/ErrorBoundary.jsx";
import { fmtTime } from "../lib/time";
import Icon from "../ui/icons.jsx";

// The rooms desk (#46, look lab round 5; `data-layout="hybrid"`; Classic remains as fallback): sidebar
// of rooms, a page per room, Today as home, under a glass bar (TodaySentence + FocusLine.jsx).
// Cards are the same CardView over the same hook results; asks run through PlanApp's `act`. This only
// adds siblings in .plan-main and moves panes with CSS (plan/desk.css): re-parenting the console would
// remount it and drop live sessions. `view` (room id or "map") is held in PlanApp for .plan[data-cview].

// The rooms: every desk card in exactly one, in the map's areas.
export const ROOMS = [
  { id: "plans", name: "Plans", icon: "listCheck", tint: "#007AFF",
    cards: ["tasks", "calendar", "courses", "habits", "lists"] },
  { id: "people", name: "People", icon: "message", tint: "#34C759",
    cards: ["email", "people", "activity"] },
  { id: "money", name: "Money", icon: "tag", tint: "#FF9500",
    cards: ["finance", "expenses", "debts"] },
  { id: "health", name: "Health", icon: "sun", tint: "#FF2D55",
    cards: ["health", "trends", "food"] },
  { id: "places", name: "Places", icon: "car", tint: "#30B0C7",
    cards: ["location", "travel", "music", "watchlist"] },
  { id: "mind", name: "Mind", icon: "star", tint: "#AF52DE",
    cards: ["brain", "generated"] },
];
export const roomOf = (card) => ROOMS.find((r) => r.cards.includes(card))?.id || null;
export const roomById = (id) => ROOMS.find((r) => r.id === id) || null;

const rank = (lvl) => (lvl === NEEDS ? 0 : lvl === HAS ? 1 : 2);
const byNeed = (sal) => (a, b) => rank(sal[a]?.level) - rank(sal[b]?.level);

// A card's pill: its salience, or "snoozed" when every ask is (else it contradicts its room).
function Band({ level, snoozed }) {
  if (snoozed) return <span className="band-pill snoozed">snoozed</span>;
  if (level == null) return null;
  const band = level === NEEDS ? "needs" : level === HAS ? "has" : "quiet";
  return <span className={`band-pill ${band}`}>{level === NEEDS ? "needs you" : level === HAS ? "today" : "quiet"}</span>;
}

// One card, whole, with its way to the full view.
function CardBox({ id, card, level, snoozed, onPin, openFull, focus }) {
  const sheet = PANEL_FOR[id];
  const scrolled = useRef(false);
  return (
    <article className={`cx-card${focus ? " focus" : ""}`} data-card={id}
      ref={(el) => {
        if (el && focus && !scrolled.current) { scrolled.current = true; el.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
        if (!focus) scrolled.current = false;
      }}>
      <header className="cx-card-head">
        <h3>{CARD_NAME[id]}</h3>
        <Band level={level} snoozed={snoozed} />
        <span className="grow" />
        {sheet && <button className="cx-icon-btn" onClick={() => openFull(sheet)} title="Full view" aria-label="Full view">
          <Icon name="expand" size={13} /></button>}
      </header>
      <div className="cx-card-body">
        <ErrorBoundary label={CARD_NAME[id]}>
          <CardView id={id} card={card} onPin={onPin} openSheet={openFull} />
        </ErrorBoundary>
      </div>
    </article>
  );
}

// A snooze menu takes focus on open (WebKit doesn't focus a clicked button, so Esc left the room). Module-level: the ref runs once per open.
export const focusFirst = (el) => el?.querySelector("button")?.focus({ preventScroll: true });

// An ask, compact. `snooze` ({ options, run } from PlanApp) gives "Later" the list's `s` choices.
export function AskRow({ a, act, snooze }) {
  const [later, setLater] = useState(false);
  const opts = snooze && !String(a.id).startsWith("console:") ? snooze.options(a) : [];
  return (
    <li className={`cx-ask${a.kind === "blocking" ? " blocking" : ""}`} data-ask-card={a.card || undefined}
      onKeyDown={(e) => {
        // Esc folds the snooze row, not the room (which threw the row away with it)
        if (e.key !== "Escape" || !later) return;
        e.stopPropagation();
        setLater(false);
        e.currentTarget.querySelector(".cx-ask-acts .pbtn.ghost")?.focus();
      }}>
      <span className={`cx-ask-dot${a.kind === "fix" ? " bad" : ""}`} />
      <span className="cx-ask-text">
        <span className="cx-ask-title">{a.title}</span>
        {a.why && <span className="cx-ask-why" title={a.why}>{a.why}</span>}
        {later && (
          <span className="cx-later" role="group" aria-label="Snooze until" ref={focusFirst}>
            {opts.map((o) => (
              <button key={o.label} className="pbtn" title={o.hint}
                onClick={() => { setLater(false); snooze.run(a, o); }}>{o.label}</button>
            ))}
          </span>
        )}
      </span>
      <span className="cx-ask-acts">
        {a.acts.slice(0, 2).map((x) => (
          <button key={x.label} className={`pbtn${x.style === "go" ? " go" : ""}`} onClick={() => act(a, x)}>{x.label}</button>
        ))}
        {opts.length > 0 && <button className={`pbtn ghost${later ? " on" : ""}`} aria-expanded={later}
          onClick={() => setLater((v) => !v)} title="Snooze it">Later</button>}
      </span>
    </li>
  );
}

const roomNeeds = (room, asks) => asks.filter((a) => room.cards.includes(a.card)).length;
// A room's one line: its most pressing card's summary.
function roomLine(room, sal) {
  // nothing read yet isn't "quiet"
  if (!room.cards.some((c) => sal[c])) return "…";
  const top = [...room.cards].sort(byNeed(sal)).find((c) => sal[c]?.summary);
  return top ? `${CARD_NAME[top]} · ${sal[top].summary}` : "Quiet";
}

function RoomPage({ room, ctx }) {
  const { cards, sal, asks, act, onPin, openFull, focus, snoozedCards } = ctx;
  const ids = [...room.cards].sort(byNeed(sal));
  const mine = asks.filter((a) => room.cards.includes(a.card));
  const dozing = room.cards.filter((c) => snoozedCards.has(c)).length;
  const said = [mine.length ? `${mine.length} need${mine.length === 1 ? "s" : ""} you` : "Nothing needs you here",
                dozing ? `${dozing} snoozed` : null].filter(Boolean).join(", ");
  return (
    <div className="cx-room" style={{ "--room": room.tint }}>
      <header className="cx-room-head">
        <span className="cx-room-icon"><Icon name={room.icon} size={18} /></span>
        <div>
          <h2>{room.name}</h2>
          <p>{said} · {room.cards.map((c) => CARD_NAME[c]).join(", ")}</p>
        </div>
      </header>
      {mine.length > 0 && <ul className="cx-asks">
        {mine.map((a) => <AskRow key={a.id} a={a} act={act} snooze={ctx.snooze} />)}</ul>}
      <div className="cx-cards">
        {ids.map((id) => <CardBox key={id} id={id} card={cards[id]} level={sal[id]?.level}
          snoozed={snoozedCards.has(id)} onPin={onPin} openFull={openFull} focus={focus === id} />)}
      </div>
    </div>
  );
}

// The glance as a sentence (day, what's next) in the floating top bar, replacing the strip's date and "next" chip.
export function TodaySentence({ cards, loading }) {
  const now = new Date();
  const cal = cards.calendar?.view === "today" ? cards.calendar.upcoming || [] : [];
  const next = cal.filter((e) => !e.all_day).find((e) => new Date(e.end || e.start) > now);
  const soon = next && new Date(next.start) - now < 3 * 3600e3;
  const when = !next ? null : new Date(next.start) <= now ? "now"
    : soon ? `in ${Math.max(1, Math.round((new Date(next.start) - now) / 60000))} min` : `at ${fmtTime(next.start)}`;
  return (
    <div className="hy-sentence" title={next ? `next is ${next.summary} ${when}` : undefined}>
      <span className="hy-day">{now.toLocaleDateString([], { weekday: "long" })}</span>
      <span className="hy-sep">·</span>
      {next ? <span className="hy-next">next is <b>{next.summary}</b></span>
        : <span className="hy-next">{loading ? "…" : "nothing else on the calendar"}</span>}
      {next && <span className="hy-when">{when}</span>}
    </div>
  );
}

// The console's own asks (approval, cut-off tabs, unmerged branches) belong to no room: Today puts them first.
const consoleAsks = (asks) => asks.filter((a) => !a.card);

// Today's order (the focus line follows it): approvals, cut-off tabs, unmerged branches (PlanApp's
// order), then each room's asks in sidebar order; a card no room holds goes last rather than missing.
export function todayOrder(asks) {
  const inRooms = ROOMS.flatMap((r) => asks.filter((a) => r.cards.includes(a.card)));
  const placed = new Set([...consoleAsks(asks), ...inRooms]);
  return [...consoleAsks(asks), ...inRooms, ...asks.filter((a) => !placed.has(a))];
}

function RoomDoors({ ctx }) {
  const { asks, sal, setView } = ctx;
  return (
    <div className="hy-rooms">
      {ROOMS.map((r) => {
        const n = roomNeeds(r, asks);
        return (
          <button key={r.id} className={`cx-door${n ? " needs" : ""}`} style={{ "--room": r.tint }}
            data-room-door={r.id} onClick={() => setView(r.id)} title={`${r.name}: ${roomLine(r, sal)}`}>
            <span className="cx-door-top">
              <span className="cx-room-icon"><Icon name={r.icon} size={15} /></span>
              <span className="cx-door-name">{r.name}</span>
              {n > 0 && <span className="cx-badge">{n}</span>}
            </span>
            <span className="cx-door-line">{roomLine(r, sal)}</span>
          </button>
        );
      })}
    </div>
  );
}

// Today: every ask under its room, the console's own first, then the rooms at a glance; the brief sits under the heading.
function TodayPage({ ctx }) {
  const { asks, act, snooze, setView, loading } = ctx;
  const own = consoleAsks(asks);
  return (
    <div className="cx-room hy-today">
      <h2 className="hy-h">Today</h2>
      {ctx.brief}
      {own.length > 0 && <section className="hy-group">
        <h3>Kestrel</h3>
        <ul className="cx-asks">{own.map((a) => <AskRow key={a.id} a={a} act={act} snooze={snooze} />)}</ul>
      </section>}
      {ROOMS.map((r) => {
        const mine = asks.filter((a) => r.cards.includes(a.card));
        if (!mine.length) return null;
        return (
          <section key={r.id} className="hy-group" style={{ "--room": r.tint }}>
            <h3><button className="hy-roomlink" onClick={() => setView(r.id)}>
              <span className="cx-room-icon"><Icon name={r.icon} size={11} /></span>{r.name}
              <Icon name="chevronRight" size={11} /></button></h3>
            <ul className="cx-asks">{mine.map((a) => <AskRow key={a.id} a={a} act={act} snooze={snooze} />)}</ul>
          </section>
        );
      })}
      {!asks.length && (loading
        ? <p className="hy-clear">Reading your cards…</p>
        : <p className="hy-clear">Nothing needs you.{ctx.cleared ? ` ${ctx.cleared} cleared today.` : ""}</p>)}
      <h3 className="hy-sub">Rooms</h3>
      <RoomDoors ctx={ctx} />
    </div>
  );
}

function Sidebar({ ctx }) {
  const { view, setView, asks, onPalette, openFull } = ctx;
  const item = (id, name, icon, tint, n) => (
    <li key={id}>
      <button className={`cx-nav-item${(view || "today") === id ? " on" : ""}`} style={{ "--room": tint }}
        data-room={id} onClick={() => setView(id === "today" ? null : id)} title={name}
        aria-label={n > 0 ? `${name}, ${n} need${n === 1 ? "s" : ""} you` : name}
        aria-current={(view || "today") === id ? "page" : undefined}>
        <span className="cx-room-icon"><Icon name={icon} size={13} /></span>
        <span className="cx-nav-name">{name}</span>
        {n > 0 && <span className="cx-badge">{n}</span>}
      </button>
    </li>
  );
  return (
    <nav className="cx-nav" aria-label="Rooms">
      <ul>{item("today", "Today", "calendar", "#FF3B30", asks.length)}</ul>
      <div className="cx-nav-label">Rooms</div>
      <ul>{ROOMS.map((r) => item(r.id, r.name, r.icon, r.tint, roomNeeds(r, asks)))}</ul>
      <div className="cx-nav-label">Views</div>
      <ul>
        {item("map", "Map", "pin", "#8E8E93", 0)}
        <li><button className="cx-nav-item" style={{ "--room": "#5856D6" }} data-room="vault" title="Notes" aria-label="Notes"
          onClick={() => openFull("notes")}>
          <span className="cx-room-icon"><Icon name="folder" size={13} /></span><span className="cx-nav-name">Notes</span></button></li>
      </ul>
      <span className="grow" />
      <button className="cx-nav-search" onClick={onPalette} title="Search everything · ⌘K" aria-label="Search everything">
        <Icon name="search" size={13} /><span className="cx-nav-name">Search everything</span> <kbd>⌘K</kbd></button>
    </nav>
  );
}

export default function Rooms(ctx) {
  const room = roomById(ctx.view);
  return (
    <>
      <Sidebar ctx={ctx} />
      {room ? <RoomPage key={room.id} room={room} ctx={ctx} /> : ctx.view !== "map" && <TodayPage ctx={ctx} />}
    </>
  );
}
