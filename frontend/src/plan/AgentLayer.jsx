import { useEffect, useRef, useSyncExternalStore } from "react";
import { useLook } from "../lib/look.js";
import { toolLabel } from "./StatusBar.jsx";
import { roomOf } from "./Rooms.jsx";
import { CARD_NAME } from "../widgets/cards.js";
import "./agents.css";

// The agent visualizer, from round 5 of the look lab (New Features #46);
// Orbs by default, Cursors or Off in the look lab (data-agents). The user on
// the map's agent dots: "good but not useful enough". His idea: the agents
// move around the UI itself, to whatever they're reading or writing, so you
// can see what's being touched.
//
// Each working tab (and each live subagent) is one agent. Its current tool
// call already names a map node (lib/turn.js nodeFor: a provider's source, a
// custom tool's card, or a machine: vault, db, shell, web). Here that node
// becomes a place on screen, best first:
//   the card itself, if it's on screen      [data-card=<id>]  (a room page, a tile, a map node)
//   an ask row of that card                 [data-ask-card=<id>]
//   its room's tile on Today, else its room in the sidebar  [data-room-door] / [data-room]
//   the vault → Notes in the sidebar        [data-room=vault]
//   anything else (shell, db, web, a dev    the console's ask bar
//   tab's Bash/Edit) → the console
// A call that writes (task_create, event_update, Edit…) also rings the
// element it lands on.
//
// Drawn as fixed elements over everything, positioned straight from the DOM
// on a slow loop (no React render per frame); the glide is a CSS transition,
// so the loop only moves the target. A replay (a past session's tool calls,
// started from the look lab) plays through the same path, one call a second.

// The replay lives in a module store, not the layer: the look lab starts it,
// and its clock is a plain interval, so neither needs an effect here.
// { title, calls, at, paused } | null
let replayState = null;
let replayClock = 0;
const replaySubs = new Set();
function setReplayState(next) {
  replayState = typeof next === "function" ? next(replayState) : next;
  const run = replayState && !replayState.paused;
  if (run && !replayClock) {
    replayClock = setInterval(() => setReplayState((r) => (!r ? r
      : r.at + 1 >= r.calls.length ? null : { ...r, at: r.at + 1 })), STEP_MS);
  } else if (!run && replayClock) { clearInterval(replayClock); replayClock = 0; }
  replaySubs.forEach((f) => f());
}
export function startReplay(title, calls) { setReplayState({ title, calls, at: 0, paused: false }); }
const subscribeReplay = (f) => { replaySubs.add(f); return () => replaySubs.delete(f); };
const WRITE = /(create|update|delete|remove|add|draft|send|set_|check|log|move|complete|done|Edit|Write|NotebookEdit)/i;
const HUES = ["#64D2FF", "#FF9F0A", "#BF5AF2", "#30D158", "#FF375F", "#FFD60A"];
const STEP_MS = 1100;

const tabName = (t, i) => (t.profile === "dev" ? `dev · ${(t.project || "").split("/").pop()}`
  : t.title ? t.title.slice(0, 28) : `tab ${i + 1}`);

function liveAgents(trace, tabs) {
  const out = [];
  tabs.forEach((tab, i) => {
    const t = trace.tabs?.[tab.id];
    if (!t?.running) return;
    const calls = t.calls || [];
    const own = [...calls].reverse().find((c) => !c.parent && c.node);
    if (own) out.push({ key: tab.id, hue: HUES[i % HUES.length], name: tabName(tab, i), node: own.node,
                        call: own, live: own.state === "live", waiting: !!t.waiting, dev: tab.profile === "dev" });
    const subs = new Map();
    for (const c of calls) if (c.parent && c.node && c.state === "live") subs.set(c.parent, c);
    [...subs].forEach(([p, c], k) => out.push({ key: `${tab.id}:${p}`, hue: HUES[(i + k + 2) % HUES.length],
      name: `${tabName(tab, i)} · subagent`, node: c.node, call: c, live: true, sub: true }));
  });
  return out;
}

const visible = (el) => {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
};
const firstVisible = (sel) => [...document.querySelectorAll(sel)].find(visible) || null;

// A query against app.db names its table, and most tables are a card's: so
// "db" (the machine) sharpens to what was read, which is the point here.
const TABLE_CARD = [[/\b(tasks)\b/, "tasks"], [/\b(habits|checkins)\b/, "habits"], [/\b(list_items|lists)\b/, "lists"],
  [/\b(debts|beers)\b/, "debts"], [/\b(expenses)\b/, "expenses"], [/\b(watchlist)\w*/, "watchlist"],
  [/\b(packages|trips)\b/, "travel"], [/\b(brain_\w+)/, "brain"], [/\b(food_\w+|meals)\b/, "food"]];
export function sharpen(node, detail = "") {
  if (node !== "db") return node;
  // `python -m app.providers tasks`: not a provider, but plainly the card
  const named = /app\.providers\s+(\w+)/.exec(detail)?.[1];
  if (named && CARD_NAME[named]) return named;
  const sql = /\b(?:from|into|update|join)\s+(\w+)/i.exec(detail)?.[1] || "";
  return TABLE_CARD.find(([re]) => re.test(sql))?.[1] || node;
}

function targetFor(node) {
  if (!node) return null;
  // the card (or an ask row of its), then its room's tile on the page, then the sidebar
  const card = firstVisible(`[data-card="${node}"]`) || firstVisible(`[data-ask-card="${node}"]`);
  if (card) return card;
  const room = node === "vault" ? "vault" : roomOf(node);
  if (room) {
    const spot = firstVisible(`[data-room-door="${room}"]`) || firstVisible(`[data-room="${room}"]`);
    if (spot) return spot;
  }
  return firstVisible(".stage-sheet .status-bar") || firstVisible(".prompt-bar");
}

const clampX = (x) => Math.max(8, Math.min(innerWidth - 8, x));
const clampY = (y) => Math.max(8, Math.min(innerHeight - 8, y));

export default function AgentLayer({ trace, tabs }) {
  const { agents: style } = useLook();
  const replay = useSyncExternalStore(subscribeReplay, () => replayState);
  const setReplay = setReplayState;
  const layer = useRef(null);

  const agents = style === "off" ? [] : liveAgents(trace, tabs);
  if (replay && style !== "off") {
    const c = replay.calls[replay.at];
    agents.push({ key: "replay", hue: "#64D2FF", name: `replay · ${replay.title}`, node: c.node, call: c, live: true });
  }
  for (const a of agents) a.node = sharpen(a.node, a.call?.detail);
  const sig = agents.map((a) => `${a.key}>${a.node}`).join(" ");

  // Where each agent is: read off the DOM on a slow loop while any are out
  // (a room change or a scroll moves the target), written straight to style.
  useEffect(() => {
    if (!sig) return undefined;
    let raf = 0, last = 0;
    const place = (ms) => {
      raf = requestAnimationFrame(place);
      if (ms - last < 120) return;
      last = ms;
      const els = layer.current?.querySelectorAll("[data-agent]") || [];
      els.forEach((el, k) => {
        const target = targetFor(el.dataset.node);
        if (!target) return;
        const r = target.getBoundingClientRect();
        const cursor = el.classList.contains("cursors");
        const x = clampX(cursor ? r.left + Math.min(r.width * 0.32, 90) + k * 14 : r.right - 18 - k * 20);
        const y = clampY(cursor ? r.top + Math.min(r.height / 2, 26) : r.top + 16 + k * 4);
        const at = `translate(${x.toFixed(0)}px, ${y.toFixed(0)}px)`;
        el.style.transform = at;
        // near the right edge the label goes on the orb's left, or it ran
        // off the window (the open console's bar is at the right edge)
        el.classList.toggle("flip", x > innerWidth - 290);
        layer.current.querySelectorAll(`[data-trail="${el.dataset.agent}"]`).forEach((t) => { t.style.transform = at; });
        const ring = el.nextElementSibling;
        if (ring?.classList.contains("agent-ring")) {
          Object.assign(ring.style, { left: `${r.left - 3}px`, top: `${r.top - 3}px`,
                                      width: `${r.width + 6}px`, height: `${r.height + 6}px` });
        }
      });
    };
    raf = requestAnimationFrame(place);
    return () => cancelAnimationFrame(raf);
  }, [sig]);

  if (style === "off" || (!agents.length && !replay)) return null;
  return (
    <>
    <div className={`agent-layer ${style}`} ref={layer} aria-hidden="true">
      {agents.map((a) => {
        const write = WRITE.test(a.call?.name || "");
        const label = `${toolLabel(a.call?.name || "")}${a.call?.detail ? ` · ${a.call.detail}` : ""}`;
        return [
          // the comet trail: the same point, on slower transitions, so it follows
          ...(style === "orbs" ? [1, 2, 3].map((n) => (
            <div key={`${a.key}-t${n}`} className={`ag-trail t${n}`} data-trail={a.key} style={{ "--hue": a.hue }} />
          )) : []),
          <div key={a.key} data-agent={a.key} data-node={a.node}
            className={`cx-agent ${style}${write ? " write" : ""}${a.waiting ? " waiting" : ""}${a.sub ? " sub" : ""}`}
            style={{ "--hue": a.hue }}>
            {style === "orbs" ? <span className="ag-orb" /> : (
              <svg className="ag-pointer" width="18" height="20" viewBox="0 0 18 20">
                <path d="M2 1.5 L2 16.5 L6.2 12.6 L9 18.6 L11.6 17.4 L8.9 11.5 L14.6 11.5 Z" />
              </svg>
            )}
            <span className="ag-tag"><b>{a.name}</b><span className="ag-what">{label}</span></span>
          </div>,
          write ? <div key={`${a.key}-ring`} className="agent-ring" style={{ "--hue": a.hue }} /> : null,
        ];
      })}
    </div>
      {replay && (
        <div className="ag-replay" role="status">
          <span className="ag-replay-dot" />
          <span className="ag-replay-what">Replaying <b>{replay.title}</b> · {replay.at + 1}/{replay.calls.length}</span>
          <button onClick={() => setReplay((r) => r && { ...r, paused: !r.paused })}>{replay.paused ? "Play" : "Pause"}</button>
          <button onClick={() => setReplay((r) => r && { ...r, at: Math.max(0, r.at - 1), paused: true })}>‹</button>
          <button onClick={() => setReplay((r) => r && { ...r, at: Math.min(r.calls.length - 1, r.at + 1), paused: true })}>›</button>
          <button onClick={() => setReplay(null)} aria-label="Stop the replay">×</button>
        </div>
      )}
    </>
  );
}
