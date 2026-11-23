import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Icon from "../ui/icons.jsx";
import { MAX_TABS } from "../lib/tabs.js";
import { childState, queuePlace, summary, tabOrder } from "../lib/children.js";
import { canRest, isResting, restLine, snoozeChoices, stepTarget } from "../lib/tabRest.js";

// The console's tabs, at the head of the status bar so they show at every
// sheet height. Each says what it's doing the way the bar's own dot does —
// working, waiting on you, or a reply you haven't read — since the point of a
// second tab is to leave it running and look away.
//
// A tab another tab opened (open_tab) isn't a peer in the bar: it belongs to
// its opener's group. The bar holds the parent and a badge, a dot per child
// and how they stand ("3 running · 1 needs you"); the badge drops the group
// open under the parent, a row per child with its state (lib/children.js),
// done ones last. The strip is a few hundred pixels wide in the plan view, so
// five children inline would push everything else off it; the one you're
// looking at is drawn beside its parent, so the bar still says where you are.
//
// One tab draws nothing but the "+": a strip of one is furniture.
//
// Right-click a tab for its menu (#102, #91): pin, move, Later (snooze, the
// desk's choices), settle, regenerate the title. Top-level tabs drag to
// reorder; pinned ones stay first. Snoozed and settled tabs leave the strip
// for the rest button at its end, which lists them and brings one back.

const TAB_MIME = "application/x-kestrel-tab";
// The Later row takes focus as it opens (Rooms.jsx's focusFirst, for the
// same WebKit reason); module-level, so the ref runs once per open.
const focusFirst = (el) => el?.querySelector("button")?.focus({ preventScroll: true });

const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export function tabState(turn, unread) {
  if (turn?.waiting) return "waiting";
  if (turn?.running) return "working";
  if (unread) return "unread";
  return "idle";
}

// What the strip draws: every tab but the resting ones (the one on screen
// always, whatever it is).
export const barTabs = (tabs, active) => tabs.filter((t) => !isResting(t) || t.id === active);

// The bar's order, flattened: what ⌥1–9 count through.
export function barOrder(tabs, active = null) {
  return tabOrder(barTabs(tabs, active)).flatMap(({ tab, children }) => [tab, ...children]);
}

const tagOf = (t) => (t.profile === "dev" ? (t.project || "").split("/").pop()
  : t.profile === "study" ? "study" : null);
const nameOf = (t, n) => (t.name ? clip(t.name, n) : t.title ? clip(t.title.replace(/\s+/g, " "), n)
  : t.brief ? clip(t.brief.replace(/\s+/g, " "), n) : t.profile ? "new" : "pick a mode");

const WORD = { working: "running", needs: "needs you", done: "done", queued: "queued", idle: "idle" };
const RANK = { needs: 0, working: 1, queued: 2, idle: 3, done: 4 };

export default function ConsoleTabs({ tabs: all, active, turns, unread, onShow, onClose,
                                      onPin, onMove, onRest, onRetitle, onRename, canRetitle }) {
  const tabs = barTabs(all, active);
  const resting = all.filter((t) => isResting(t) && t.id !== active)
    .sort((a, b) => (a.snoozedUntil && b.snoozedUntil ? a.snoozedUntil - b.snoozedUntil
      : a.snoozedUntil ? -1 : b.snoozedUntil ? 1 : (b.settledAt || 0) - (a.settledAt || 0)));
  // The open group: its parent's id and where to draw it (fixed, from the
  // badge's rect: the strip scrolls sideways, so it clips anything absolute).
  const [menu, setMenu] = useState(null);
  // A tab's own menu ({ id, left, top, later, busy }) and the rest list's
  // ({ left, top }), placed the same way.
  const [tabMenu, setTabMenu] = useState(null);
  const [restMenu, setRestMenu] = useState(null);
  // The chip a dragged tab would land before, while one is over it.
  const [dropAt, setDropAt] = useState(null);
  // Under the chip, or over it when the bar is in the window's lower half
  // (the folded console's bar sits at the bottom edge).
  const place = (r, width) => ({
    left: Math.max(8, Math.min(r.left - 20, window.innerWidth - width - 8)),
    ...(r.top > window.innerHeight / 2 ? { bottom: window.innerHeight - r.top + 8 } : { top: r.bottom + 8 }),
  });
  // A crowded bar narrows the strip (lib/barHost.js), so it scrolls: the tab
  // on screen is kept in view of it. scrollLeft, not scrollIntoView, which
  // would scroll the page's own ancestors too.
  // Again when the strip itself narrows: the bar's fold runs after this renders.
  const stripRef = useRef(null);
  const keepInView = () => {
    const strip = stripRef.current;
    const on = strip?.querySelector(".st-tab.on, .st-tab.st-kid-on");
    if (!on) return;
    const s = strip.getBoundingClientRect(), r = on.getBoundingClientRect();
    if (r.left < s.left) strip.scrollLeft -= s.left - r.left + 4;
    else if (r.right > s.right) strip.scrollLeft += r.right - s.right + 4;
  };
  useLayoutEffect(keepInView);
  useLayoutEffect(() => {
    const ro = new ResizeObserver(() => keepInView());
    if (stripRef.current) ro.observe(stripRef.current);
    return () => ro.disconnect();
  }, []);
  // Focused as it opens, so Esc closes it and the keys stay in it.
  const focusMenu = useCallback((el) => el?.focus(), []);
  if (tabs.length < 2 && !resting.length) return null;
  const order = barOrder(tabs);
  const top = tabOrder(tabs).map((g) => g.tab);
  const key = (t) => {
    const i = order.indexOf(t);
    return i >= 0 && i < 9 ? ` · ⌥${i + 1}` : "";
  };

  const chip = (t, { family = false, child = false } = {}) => {
    const state = child ? childState(t, turns[t.id]) : tabState(turns[t.id], unread.has(t.id));
    const dot = child ? (state === "needs" ? "needs" : state === "idle" && unread.has(t.id) ? "unread" : state)
      : state;
    const busy = !!(turns[t.id]?.running || turns[t.id]?.waiting);
    const tag = tagOf(t);
    // Only a top-level tab drags: a child goes where its parent goes.
    const drags = !child && top.includes(t);
    return (
      <span key={t.id} role="tab" aria-selected={t.id === active}
        className={`st-tab ${child ? `st-kid-on ${state}` : state}${t.id === active ? " on" : ""}`
                   + `${family ? " st-parent" : ""}${t.pinned ? " pinned" : ""}${dropAt === t.id ? " drop-before" : ""}`}
        draggable={drags || undefined}
        onDragStart={drags ? (e) => {
          e.stopPropagation();
          e.dataTransfer.setData(TAB_MIME, t.id);
          e.dataTransfer.effectAllowed = "move";
        } : undefined}
        onDragOver={drags ? (e) => {
          if (!e.dataTransfer.types.includes(TAB_MIME)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          if (dropAt !== t.id) setDropAt(t.id);
        } : undefined}
        onDragLeave={drags ? () => setDropAt((d) => (d === t.id ? null : d)) : undefined}
        onDrop={drags ? (e) => {
          const id = e.dataTransfer.getData(TAB_MIME);
          setDropAt(null);
          if (!id) return;
          e.preventDefault();
          // Past the middle of the chip lands after it.
          const r = e.currentTarget.getBoundingClientRect();
          const i = top.indexOf(t);
          onMove(id, e.clientX > r.left + r.width / 2 ? (top[i + 1]?.id ?? null) : t.id);
        } : undefined}
        onDragEnd={() => setDropAt(null)}
        onContextMenu={(e) => {
          e.preventDefault();
          setMenu(null);
          setRestMenu(null);
          setTabMenu({ id: t.id, ...place(e.currentTarget.getBoundingClientRect(), 260), later: false });
        }}>
        <button className="st-tab-pick" onClick={() => onShow(t.id)}
          title={`${tag ? `${tag} — ` : ""}${t.name || t.title || "New conversation"}`
                 + `${t.spawned ? " · handed off" : ""}${t.pinned ? " · pinned" : ""}${key(t)} · right-click for more`}>
          {t.pinned && <Icon name="tack" size={10} className="st-tab-tack" />}
          <span className={`st-tab-dot ${dot}`} />
          {(tag || (t.spawned && !child)) && (
            <span className={`st-tab-mode mode-${t.profile}`}>{t.spawned && !child ? "↗" : ""}{tag || ""}</span>
          )}
          {nameOf(t, child || family ? (tag ? 10 : 12) : tag ? 12 : 18)}
        </button>
        {!t.pinned && (
          <button className="st-tab-x" onClick={() => onClose(t.id)} disabled={busy}
            title={busy ? "Stop the turn first" : "Close tab · ⌥W — the session stays in resume"
                   + (family ? "; its done children go with it" : "")}
            aria-label="Close tab"><Icon name="close" size={9} /></button>
        )}
      </span>
    );
  };

  // A tab's menu: what you can do to it besides go to it.
  const tabMenuBody = (m) => {
    const t = all.find((x) => x.id === m.id);
    if (!t) return null;
    const rest = canRest(t, turns[t.id]);
    const i = top.findIndex((x) => x.id === t.id);
    const left = i > 0 ? stepTarget(top, t.id, -1) : undefined;
    const right = i >= 0 && i < top.length - 1 ? stepTarget(top, t.id, 1) : undefined;
    const busy = !!(turns[t.id]?.running || turns[t.id]?.waiting);
    const done = (fn) => () => { setTabMenu(null); fn(); };
    const item = (label, fn, { disabled = false, title, hint } = {}) => (
      <button key={label} role="menuitem" className="st-tm-item" disabled={disabled} title={title}
        onClick={fn}>
        <span>{label}</span>{hint && <span className="st-tm-hint">{hint}</span>}
      </button>
    );
    return (
      <>
        <span className="st-menu-head"><b>{nameOf(t, 34)}</b></span>
        {item(t.pinned ? "Unpin tab" : "Pin tab", done(() => onPin(t.id, !t.pinned)),
          { hint: t.pinned ? null : "first in the bar, no ×" })}
        {i >= 0 && item("Move left", done(() => onMove(t.id, left)), { disabled: left === undefined })}
        {i >= 0 && item("Move right", done(() => onMove(t.id, right)), { disabled: right === undefined })}
        <span className="st-tm-sep" />
        {item("Later", () => setTabMenu({ ...m, later: !m.later }),
          { disabled: !rest.ok, title: rest.why || "Snooze it: it leaves the bar and comes back", hint: m.later ? null : "snooze…" })}
        {m.later && rest.ok && (
          <span className="cx-later st-tm-later" role="group" aria-label="Snooze until"
            ref={focusFirst}>
            {snoozeChoices().map((o) => (
              <button key={o.label} className="pbtn" title={o.hint}
                onClick={done(() => onRest(t.id, { until: o.until }))}>{o.label}</button>
            ))}
          </span>
        )}
        {item("Settle", done(() => onRest(t.id, {})),
          { disabled: !rest.ok, title: rest.why || "Put it on the Settled list; unsettle it from there", hint: "put away" })}
        <span className="st-tm-sep" />
        {item(m.busy ? "Naming…" : "Regenerate title", async () => {
          setTabMenu({ ...m, busy: true, error: null });
          const err = await onRetitle(t.id);
          if (err) setTabMenu({ ...m, busy: false, error: err }); else setTabMenu(null);
        }, { disabled: m.busy || !canRetitle(t.id),
             title: canRetitle(t.id) ? "Name it from the whole conversation (one Haiku turn)" : "Nothing to read yet: it needs a finished turn" })}
        {m.error && <span className="st-tm-error">{m.error}</span>}
        {t.name && item("Use the first message", done(() => onRename(t.id, null)))}
        {!t.pinned && item("Close tab", done(() => onClose(t.id)), { disabled: busy, hint: "⌥W" })}
      </>
    );
  };

  const restRow = (t) => (
    <li key={t.id} className={`st-kid st-rest-row${t.snoozedUntil ? " snoozed" : " settled"}`} role="none">
      <button className="st-kid-pick" role="menuitem"
        onClick={() => { setRestMenu(null); onShow(t.id); }}
        title={t.snoozedUntil ? "Bring it back now" : "Unsettle it: back in the bar, resumed"}>
        <Icon name={t.snoozedUntil ? "clock" : "check"} size={10} className="st-kid-check" />
        <span className="st-kid-main">
          <span className="st-kid-line">
            {tagOf(t) && <span className={`st-tab-mode mode-${t.profile}`}>{tagOf(t)}</span>}
            <span className="st-kid-name">{nameOf(t, 40)}</span>
          </span>
        </span>
        <span className="st-kid-state">{restLine(t)}</span>
      </button>
      <button className="st-tab-x" onClick={() => onClose(t.id)}
        title="Forget it · the session stays in resume" aria-label="Forget tab"><Icon name="close" size={9} /></button>
    </li>
  );

  const row = (c) => {
    const turn = turns[c.id];
    const state = childState(c, turn);
    const tag = tagOf(c);
    const place = state === "queued" ? queuePlace(tabs, c.id) : 0;
    const why = state === "needs"
      ? (turn?.waiting ? (turn.waiting.question ? `asking: ${turn.waiting.detail || "a question"}`
                                                : `wants to run ${turn.waiting.name || "a tool"}`)
        : c.needs || "its last turn didn't finish; open it to resume")
      : null;
    const busy = !!(turn?.running || turn?.waiting);
    return (
      <li key={c.id} className={`st-kid ${state}${c.id === active ? " on" : ""}`} role="none">
        <button className="st-kid-pick" role="menuitem"
          onClick={() => { setMenu(null); onShow(c.id); }}
          title={state === "queued" ? "Waiting for a working slot; click to start it now"
            : state === "done" ? "Landed and closed; click to reopen it" : `${c.title || c.brief || ""}${key(c)}`}>
          {state === "done" ? <Icon name="check" size={10} className="st-kid-check" />
            : <span className={`st-tab-dot ${state}`} />}
          <span className="st-kid-main">
            <span className="st-kid-line">
              {tag && <span className={`st-tab-mode mode-${c.profile}`}>{tag}</span>}
              <span className="st-kid-name">{nameOf(c, 40)}</span>
            </span>
            {why && <span className="st-kid-why">{why}</span>}
          </span>
          <span className="st-kid-state">{state === "queued" ? `queued #${place}` : WORD[state]}</span>
        </button>
        <button className="st-tab-x" onClick={() => onClose(c.id)} disabled={busy}
          title={busy ? "Stop the turn first" : state === "queued" ? "Take it out of the queue"
                 : "Close · the session stays in resume"}
          aria-label="Close tab"><Icon name="close" size={9} /></button>
      </li>
    );
  };

  // Most urgent first, in the badge's dots and the menu's rows alike; the
  // sort is stable, so within a state they stay in the order they opened.
  const urgent = (list) => [...list].sort((a, b) => RANK[childState(a, turns[a.id])] - RANK[childState(b, turns[b.id])]);
  const groups = tabOrder(tabs);
  const open = menu && groups.find((g) => g.tab.id === menu.id && g.children.length);
  return (
    <span className="st-tabs" role="tablist" ref={stripRef}>
      {groups.map(({ tab: t, children }) => {
        if (!children.length) return chip(t);
        const sum = summary(children, turns);
        const lead = sum.parts.find((p) => p.state === "needs") || sum.parts[0];
        const shown = children.find((c) => c.id === active);
        const isOpen = menu?.id === t.id;
        return (
          <span key={t.id} className={`st-family${isOpen ? " open" : ""}`}
            role="group" aria-label={`${t.title || "Tab"} and the tabs it opened`}>
            {chip(t, { family: true })}
            {shown && chip(shown, { child: true })}
            <button className="st-fam-badge" aria-haspopup="menu" aria-expanded={isOpen}
              title={`${children.length} tab${children.length === 1 ? "" : "s"} this one opened: ${sum.text}`}
              onClick={(e) => {
                if (isOpen) return setMenu(null);
                // Under the badge, kept on screen: the strip scrolls, so the
                // group's own left edge can be off to the side.
                const r = e.currentTarget.getBoundingClientRect();
                const left = Math.max(8, Math.min(r.left - 120, window.innerWidth - 348));
                setMenu({ id: t.id, left, top: r.bottom + 8 });
              }}>
              <span className="st-fam-dots" aria-hidden="true">
                {urgent(children).slice(0, 6).map((c) => (
                  <span key={c.id} className={`st-tab-dot ${childState(c, turns[c.id])}`} />
                ))}
              </span>
              {/* The bar is a couple of hundred pixels in the plan view: the
                  most urgent count here (needs you first), the rest in the
                  title and the menu's head. */}
              {lead && (
                <span className="st-fam-sum"><span className={`sum-${lead.state}`}>{lead.text}</span></span>
              )}
              <Icon name="chevronDown" size={10} />
            </button>
          </span>
        );
      })}
      {open && createPortal(
        <>
          <span className="st-menu-scrim" onClick={() => setMenu(null)} />
          <span className="st-menu" role="menu" style={{ left: menu.left, top: menu.top }}
            ref={focusMenu} tabIndex={-1}
            onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setMenu(null); } }}>
            <span className="st-menu-head">
              <span>Opened by <b>{nameOf(open.tab, 34)}</b></span>
              <span className="st-fam-sum">
                {summary(open.children, turns).parts.map((p, i) => (
                  <span key={p.state} className={`sum-${p.state}`}>{i ? " · " : ""}{p.text}</span>
                ))}
              </span>
            </span>
            <ul className="st-menu-list">
              {urgent(open.children).map(row)}
            </ul>
          </span>
        </>, document.body)}
      {resting.length > 0 && (
        <button className={`st-rest${restMenu ? " open" : ""}`} aria-haspopup="menu" aria-expanded={!!restMenu}
          title={`${resting.length} tab${resting.length === 1 ? "" : "s"} put away: snoozed or settled`}
          onClick={(e) => {
            if (restMenu) return setRestMenu(null);
            setTabMenu(null);
            setRestMenu(place(e.currentTarget.getBoundingClientRect(), 340));
          }}>
          <Icon name="moon" size={10} />
          <span>{resting.length}</span>
        </button>
      )}
      {restMenu && createPortal(
        <>
          <span className="st-menu-scrim" onClick={() => setRestMenu(null)} />
          <span className="st-menu st-rest-menu" role="menu" style={restMenu} ref={focusMenu} tabIndex={-1}
            onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setRestMenu(null); } }}>
            {[["Snoozed", resting.filter((t) => t.snoozedUntil), "back by itself; click to bring one back now"],
              ["Settled", resting.filter((t) => t.settled), "click to unsettle"]]
              .filter(([, list]) => list.length).map(([head, list, sub]) => (
                <span key={head} className="st-rest-group">
                  <span className="st-menu-head"><b>{head}</b><span>{sub}</span></span>
                  <ul className="st-menu-list">{list.map(restRow)}</ul>
                </span>
              ))}
          </span>
        </>, document.body)}
      {tabMenu && createPortal(
        <>
          <span className="st-menu-scrim" onClick={() => setTabMenu(null)}
            onContextMenu={(e) => { e.preventDefault(); setTabMenu(null); }} />
          <span className="st-menu st-tab-menu" role="menu"
            style={{ left: tabMenu.left, top: tabMenu.top, bottom: tabMenu.bottom }}
            ref={focusMenu} tabIndex={-1}
            onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setTabMenu(null); } }}>
            {tabMenuBody(tabMenu)}
          </span>
        </>, document.body)}
    </span>
  );
}

export function NewTabButton({ count, onOpen }) {
  const full = count >= MAX_TABS;
  return (
    <button className="st-icon" onClick={onOpen} disabled={full}
      title={full ? `${MAX_TABS} tabs is the most` : "New tab · ⌥T"} aria-label="New tab">
      <Icon name="plus" size={12} />
    </button>
  );
}
