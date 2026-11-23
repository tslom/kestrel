import { useEffect, useState } from "react";
import { timer } from "../lib/timers.js";
import { fetchJSON, postJSON } from "../lib/api";
import { broadcastRefresh } from "../lib/refresh";

// Every few hours while Kestrel is open, a card asks which of today's habits
// are done (New Features #68, 11-17). It used to take opening the habits card
// to log anything, so a day's routine went unlogged unless you happened to
// look. Only habits not yet checked in today are asked about, and a day with
// none left asks nothing.
//
// The timing, in one place:
export const HABIT_NUDGE = {
  everyHours: 3,      // at most once per this many hours (launches included)
  quietFrom: 23,      // no asking from this hour…
  quietUntil: 9,      // …until this one (local time; wraps past midnight)
  firstAfterMin: 1,   // after a launch or reload, wait this long before the first
};

// When it last asked (or found nothing to ask), across reloads and relaunches.
const LAST_KEY = "habit_nudge_at";
const TICK_MS = 60_000;

function quiet(now = new Date()) {
  const h = now.getHours();
  const { quietFrom: a, quietUntil: b } = HABIT_NUDGE;
  return a > b ? h >= a || h < b : h >= a && h < b;
}

function due(now = Date.now()) {
  const last = Number(localStorage.getItem(LAST_KEY) || 0);
  return now - last >= HABIT_NUDGE.everyHours * 3_600_000 && !quiet(new Date(now));
}

const stamp = () => { try { localStorage.setItem(LAST_KEY, String(Date.now())); } catch { /* fine */ } };

export default function HabitNudge() {
  // { day, habits } once it's asking; `picked` is what's ticked so far.
  const [ask, setAsk] = useState(null);
  const [picked, setPicked] = useState(() => new Set());
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    // Only while the window is the one in front: a card that pops up behind
    // another app is one you meet hours later, already out of date.
    const check = () => {
      if (!alive || ask || document.hidden || !document.hasFocus() || !due()) return;
      // not over something you're in the middle of: the palette, an approval,
      // the look lab or an open sheet. The next tick asks again.
      if (document.querySelector(".cmdk, .card-modal, .look-lab, .plan.full")) return;
      fetchJSON("/api/habits").then((d) => {
        if (!alive) return;
        const left = (d?.habits || []).filter((h) => !h.archived && !(h.today > 0));
        stamp();   // asked, or nothing to ask: either way, not again for a while
        if (left.length) { setPicked(new Set()); setAsk({ day: d.day, habits: left }); }
      }).catch(() => { /* the backend is down; the next tick tries again */ });
    };
    const first = timer();
    first.start(check, HABIT_NUDGE.firstAfterMin * 60_000);
    const every = setInterval(check, TICK_MS);
    window.addEventListener("focus", check);
    return () => {
      alive = false;
      first.clear();
      clearInterval(every);
      window.removeEventListener("focus", check);
    };
  }, [ask]);

  useEffect(() => {
    if (!ask) return;
    // Esc closes the top layer only: the palette, opened over this, first
    const onKey = (e) => { if (e.key === "Escape" && !document.querySelector(".cmdk")) { e.preventDefault(); setAsk(null); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ask]);

  if (!ask) return null;

  const toggle = (id) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  // The day is the backend's (day_key, 4am rollover), from the same read
  // that listed the habits — never the browser's date.
  function save() {
    if (!picked.size) { setAsk(null); return; }
    setSaving(true);
    Promise.all([...picked].map((id) =>
      postJSON(`/api/habits/${id}/checkin`, { day: ask.day, value: 1 })))
      .then(() => { broadcastRefresh("habits"); setAsk(null); })
      .catch(() => { /* left open, so the ticks aren't lost */ })
      .finally(() => setSaving(false));
  }

  return (
    <div className="habit-nudge-scrim" onClick={(e) => { if (e.target === e.currentTarget) setAsk(null); }}>
      <div className="habit-nudge" role="dialog" aria-modal="true" aria-label="Habits check-in">
        <div className="habit-nudge-head">Done any of these today?</div>
        <ul className="habit-nudge-list">
          {ask.habits.map((h, i) => (
            <li key={h.id}>
              {/* focus starts in the card, so the keyboard acts on it, not the page behind */}
              <button type="button" className={`habit-nudge-row${picked.has(h.id) ? " on" : ""}`} autoFocus={i === 0}
                      aria-pressed={picked.has(h.id)} onClick={() => toggle(h.id)}>
                <span className="habit-nudge-box" aria-hidden="true">{picked.has(h.id) ? "✓" : ""}</span>
                {h.name}
              </button>
            </li>
          ))}
        </ul>
        <div className="habit-nudge-actions">
          <span className="habit-nudge-when">asks again in {HABIT_NUDGE.everyHours}h</span>
          <button type="button" className="btn" onClick={() => setAsk(null)}>Not now</button>
          <button type="button" className="btn primary" onClick={save} disabled={saving || !picked.size}>
            {picked.size ? `Log ${picked.size}` : "Log"}
          </button>
        </div>
      </div>
    </div>
  );
}
