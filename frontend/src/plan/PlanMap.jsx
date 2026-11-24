import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { CARD_NAME } from "../widgets/cards.js";
import { bend, createDotMotion } from "./dotMotion.js";
import MapTerrain from "./MapTerrain.jsx";

// The map: where a problem lives and what a console turn touched; a node opens its card, the list acts.
// Hand-written SVG in real pixels: positions are on a 1000 × 660 plan spread to the pane, text unscaled.
// MapTerrain is the ground. Design: DESIGN.md → The map (`plan/PlanMap.jsx`).

const W = 1000, H = 660;
const HUB = { x: 500, y: 330 };

// Where each card sits; machine nodes are where the console works, not cards.
const NODES = {
  email:     { name: "Email",      x: 110, y: 105 },
  calendar:  { name: "Calendar",   x: 300, y: 92 },
  people:    { name: "People",     x: 105, y: 250 },
  courses:   { name: "Office hrs", x: 300, y: 235 },
  tasks:     { name: "Tasks",      x: 470, y: 80 },
  habits:    { name: "Habits",     x: 630, y: 80 },
  lists:     { name: "Lists",      x: 450, y: 205 },
  generated: { name: "Generated",  x: 625, y: 205 },
  activity:  { name: "Away",       x: 680, y: 330 },
  brain:     { name: "Brain",      x: 500, y: 425 },
  health:    { name: "Health",     x: 800, y: 80 },
  location:  { name: "Location",   x: 935, y: 125 },
  trends:    { name: "Trends",     x: 800, y: 225 },
  food:      { name: "Food",       x: 955, y: 230 },
  travel:    { name: "Deliveries", x: 905, y: 330 },
  music:     { name: "Music",      x: 770, y: 450 },
  watchlist: { name: "Watchlist",  x: 870, y: 560 },
  finance:   { name: "Finance",    x: 120, y: 440 },
  expenses:  { name: "Expenses",   x: 250, y: 400 },
  debts:     { name: "Debts",      x: 255, y: 530 },
  vault:     { name: "Vault",      x: 400, y: 505, machine: true },
  db:        { name: "app.db",     x: 510, y: 590, machine: true },
  shell:     { name: "Shell",      x: 600, y: 490, machine: true },
  web:       { name: "Web",        x: 680, y: 595, machine: true },
};

const AREAS = [
  { name: "Comms",   lx: 205, ly: 30 },
  { name: "Plans",   lx: 548, ly: 30 },
  { name: "Body",    lx: 870, ly: 30 },
  { name: "Media",   lx: 950, ly: 640 },
  { name: "Money",   lx: 90,  ly: 640 },
  { name: "Machine", lx: 390, ly: 640 },
];

// Which provider's sync history rings each card (/api/status's `recent`).
const CARD_SOURCE = {
  calendar: "calendar", email: "email", people: "messages", health: "health",
  trends: "features", finance: "finance", music: "spotify", location: "location",
  food: "usc_dining",
};

// Overlapping node boxes (size plus name) are pushed apart along their shallower side; a roomy pane is untouched.
function spread(nodes, hub, w, h, k) {
  const boxW = 62 * k, boxH = 46 * k;
  const edge = 20;
  const moving = nodes.filter((n) => !n.machine || n.id === "vault");
  for (let round = 0; round < 16; round++) {
    let moved = false;
    const push = (a, b, fixed) => {
      const dx = b.x - a.x, dy = b.y - a.y;
      const over = [boxW - Math.abs(dx), boxH - Math.abs(dy)];
      if (over[0] <= 0 || over[1] <= 0) return;
      moved = true;
      const axis = over[0] < over[1] ? "x" : "y";
      const dir = (axis === "x" ? dx : dy) >= 0 ? 1 : -1;
      const by = (over[axis === "x" ? 0 : 1] / (fixed ? 1 : 2)) * dir;
      b[axis] += by;
      if (!fixed) a[axis] -= by;
    };
    for (let i = 0; i < moving.length; i++) {
      push(hub, moving[i], true);
      for (let j = i + 1; j < moving.length; j++) push(moving[i], moving[j], false);
    }
    for (const n of moving) {
      n.x = Math.max(edge, Math.min(w - edge, n.x));
      n.y = Math.max(edge, Math.min(h - edge, n.y));
    }
    if (!moved) break;
  }
  return nodes;
}

function curve(a, b) {
  const c = bend(a, b);
  return `M${a.x} ${a.y} Q${c.x.toFixed(1)} ${c.y.toFixed(1)} ${b.x} ${b.y}`;
}

function ringSeg(r, i, n) {
  const gap = 0.04;
  const a0 = -Math.PI / 2 + (i / n) * Math.PI * 2 + gap;
  const a1 = -Math.PI / 2 + ((i + 1) / n) * Math.PI * 2 - gap;
  return `M${(Math.cos(a0) * r).toFixed(2)} ${(Math.sin(a0) * r).toFixed(2)}`
    + ` A${r} ${r} 0 0 1 ${(Math.cos(a1) * r).toFixed(2)} ${(Math.sin(a1) * r).toFixed(2)}`;
}

// A Kestrel dot's orbit round the hub, clear of the halo (styles.css .map-hub slides its words aside).
const HUB_ORBIT = 26;
const reduceQuery = () => window.matchMedia?.("(prefers-reduced-motion: reduce)");
function useReducedMotion() {
  const [still, setStill] = useState(() => !!reduceQuery()?.matches);
  useEffect(() => {
    const q = reduceQuery();
    const on = () => setStill(q.matches);
    q?.addEventListener("change", on);
    return () => q?.removeEventListener("change", on);
  }, []);
  return still;
}

// Monospace: a label's width is its length, used to keep it inside the pane near an edge.
const LABEL_CH = 6.6;
const nudge = (text, x, w, k) => {
  const half = (text.length * LABEL_CH * k) / 2 + 4;
  return (Math.max(half, Math.min(w - half, x)) - x) / k;
};

const band = (lvl) => (lvl === NEEDS ? "needs" : lvl === HAS ? "has" : "quiet");
const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

// What a dragged node carries: the card's summary and payload, without the hook's functions.
const DATA_KEYS = ["data", "queue", "sum", "payload", "notes", "series", "balances",
                   "spend", "waiting", "signals"];
function cardPin(id, card, level) {
  const data = {};
  for (const k of DATA_KEYS) if (card?.[k] != null) data[k] = card[k];
  return [`${CARD_NAME[id].toLowerCase()} · today`, {
    card: id, band: level == null ? null : ["quiet", "today", "needs you"][level],
    summary: card?.summary, ...data,
  }];
}

export default function PlanMap({ cards, sal, sources, trace, askCount = {}, snoozedCards = new Set(),
                                  hot, onHot, onOpen, onFull,
                                  onPin, onHub, hubDrop, onVault, keyed, onKey,
                                  unread, agents = [], activeTab, onAgent }) {
  const svgRef = useRef(null);
  const wrapRef = useRef(null);
  const [box, setBox] = useState({ w: W, h: H });
  useEffect(() => {
    const ro = new ResizeObserver(([e]) => {
      const { width: w, height: h } = e.contentRect;
      if (w > 0 && h > 0) setBox({ w, h });   // 0 while the pane is hidden
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);
  const sx = box.w / W, sy = box.h / H;
  // Nodes shrink in a small pane, to 0.8 at most.
  const k = Math.max(0.8, Math.min(1, Math.sqrt(sx * sy)));
  const hubAt = { x: HUB.x * sx, y: HUB.y * sy };
  // A short pane has no room for a line under each node; hover still has it.
  const short = box.h < 340;
  const [tapped, setTapped] = useState(null);
  const drag = useRef(null);
  const [ghost, setGhost] = useState(null);
  const suppressClick = useRef(false);
  const still = useReducedMotion();

  const nodes = useMemo(() => spread(Object.entries(NODES).map(([id, n]) => {
    const card = cards[id];
    // Every ask snoozed: draws as "today", dashed.
    const snoozed = snoozedCards.has(id);
    const lvl = n.machine ? null : snoozed ? HAS : sal[id]?.level ?? null;
    const recent = sources[CARD_SOURCE[id]]?.recent ?? null;
    const broken = !!card?.error || (recent != null && recent.endsWith("xx"));
    const R = n.machine ? 13 : lvl === NEEDS ? 17 : lvl === HAS ? 14 : 6;
    const asks = askCount[id] || 0;
    return { id, ...n, x: n.x * sx, y: n.y * sy, lvl, R, recent, broken, asks, snoozed,
             summary: id === "vault" ? "your notes · click to browse and search"
               : n.machine ? null : `${snoozed ? "snoozed · " : ""}${sal[id]?.summary || ""}` };
  }), { x: HUB.x * sx, y: HUB.y * sy }, box.w, box.h, k),
  [cards, sal, sources, askCount, snoozedCards, sx, sy, box.w, box.h, k]);

  // The ground: height = need (asks add). Machine nodes raise nothing; Kestrel always stands.
  const scale = Math.sqrt(sx * sy);
  const hills = useMemo(() => {
    const out = [{ id: "hub", x: hubAt.x, y: hubAt.y, lift: 0.75, sig2: 2 * (70 * scale) ** 2,
                   far: (260 * scale) ** 2 }];
    for (const n of nodes) {
      if (n.machine) continue;
      const lift = n.lvl === NEEDS ? 1.15 + Math.min(n.asks, 4) * 0.22 : n.lvl === HAS ? 0.55 : 0.14;
      const sig = (n.lvl === NEEDS ? 88 : n.lvl === HAS ? 62 : 44) * scale;
      out.push({ id: n.id, x: n.x, y: n.y, lift, sig2: 2 * sig * sig, far: (sig * 3.6) ** 2,
                 tint: n.broken ? "bad" : n.lvl === NEEDS ? "accent" : null });
    }
    return out;
  }, [nodes, hubAt.x, hubAt.y, scale]);
  // Key on node ids: `trace.live` (calls in flight) is a new object every render.
  const liveKey = Object.keys(trace.live || {}).sort().join(" ");
  const ripples = useMemo(() => (liveKey ? liveKey.split(" ") : [])
    .map((id) => nodes.find((n) => n.id === id)).filter(Boolean)
    .map((n) => ({ id: n.id, x: n.x, y: n.y })), [liveKey, nodes]);

  // ---- drag a node to the prompt (or Kestrel): SVG can't use HTML5 drag, so
  // it's pointer events, a floating pill, and a hit test on release.
  const startDrag = (e, n) => {
    if (n.machine || e.button !== 0) return;
    drag.current = { id: n.id, x: e.clientX, y: e.clientY, on: false };
  };
  // Read at drop time through refs so the window listeners subscribe once (`cards` is new each render).
  const latest = useRef({ cards, sal, onPin });
  latest.current = { cards, sal, onPin };
  useEffect(() => {
    const move = (e) => {
      const d = drag.current;
      if (!d) return;
      if (!d.on && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 6) return;
      d.on = true;
      document.body.classList.add("pin-dragging");
      setGhost({ x: e.clientX, y: e.clientY, label: `${CARD_NAME[d.id].toLowerCase()} · today` });
    };
    const up = (e) => {
      const d = drag.current;
      drag.current = null;
      if (!d?.on) return;
      document.body.classList.remove("pin-dragging");
      setGhost(null);
      // Swallow the click this release fires; the next press lifts it.
      suppressClick.current = true;
      // The drop targets: the prompt, the console sheet (any height), Kestrel.
      const over = document.elementFromPoint(e.clientX, e.clientY);
      if (over?.closest(".prompt-bar, .stage-sheet, .map-hub")) {
        const { cards: c, sal: s, onPin: pin } = latest.current;
        pin(...cardPin(d.id, c[d.id], s[d.id]?.level));
      }
    };
    // Capture phase, so it lands before any node's own pointerdown.
    const down = () => { suppressClick.current = false; };
    window.addEventListener("pointerdown", down, true);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointerdown", down, true);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, []);

  const click = useCallback((e, n) => {
    if (suppressClick.current) { suppressClick.current = false; return; }
    // No hover on touch: the first tap shows the label, the second opens.
    if (e.nativeEvent?.pointerType === "touch" || e.pointerType === "touch") {
      if (tapped !== n.id) { setTapped(n.id); onHot(n.id); return; }
    }
    setTapped(null);
    // The vault is the one openable machine node.
    if (n.id === "vault") onVault();
    else if (!n.machine) onOpen(n.id);
  }, [tapped, onHot, onOpen, onVault]);

  const maxUse = Math.max(1, ...Object.values(trace.use || {}));
  // The hub speaks for the tab on screen, unless others are at work too.
  const anyWaiting = agents.some((a) => a.waiting);
  const hub = trace.waiting || anyWaiting ? "asking" : agents.length ? "working" : unread ? "replied" : "idle";
  const hotNode = nodes.find((n) => n.id === hot);

  // ---- Kestrel dots: bodies in the one motion loop (dotMotion.js); a finished one stays a ghost until flown home.
  const dots = useMemo(() => agents.flatMap((a) => [
    { id: a.id, tab: a, node: a.node, held: a.waiting },
    ...a.subagents.map((s) => ({ id: `${a.id}/${s.id}`, tab: a, node: s.node, sub: true, parent: a.id })),
  ]), [agents]);
  const [ghosts, setGhosts] = useState([]);
  const motion = useRef(null);
  if (!motion.current) motion.current = createDotMotion({
    onGone: (id) => setGhosts((g) => g.filter((d) => d.id !== id)),
  });
  const shownDots = useRef([]);
  useLayoutEffect(() => {
    const live = new Set(dots.map((d) => d.id));
    const left = still ? [] : shownDots.current.filter((d) => !live.has(d.id));
    setGhosts((g) => {
      const next = g.filter((d) => !live.has(d.id) && !left.some((l) => l.id === d.id));
      return !next.length && !g.length && !left.length ? g : [...next, ...left];
    });
    shownDots.current = dots;
  }, [dots, still]);
  useLayoutEffect(() => {
    const scene = { hub: hubAt, orbit: HUB_ORBIT * k, nodes: {} };
    for (const n of nodes) scene.nodes[n.id] = { x: n.x, y: n.y, orbit: (n.R + (n.recent != null ? 12 : 9)) * k };
    motion.current.sync(dots, scene, still);
  });
  useEffect(() => () => motion.current.stop(), []);

  return (
    <div ref={wrapRef} className={`map-wrap${keyed ? " keyed" : ""}`}>
      <MapTerrain width={box.w} height={box.h} hills={hills} ripples={ripples}
        working={agents.length > 0} still={still} />
      <svg ref={svgRef} className="map" viewBox={`0 0 ${box.w} ${box.h}`}
        onClick={(e) => {
          // Open map: put away a touch label.
          if (e.target === svgRef.current || e.target.closest(".map-bg")) {
            setTapped(null); onHot(null);
          }
        }}
        role="img" aria-label="Map of every card, with Kestrel at the centre">
        <g className="map-bg">
          {AREAS.map((a) => (
            // Top row stops short of the Key button.
            <text key={a.name} textAnchor="middle" className="region"
              x={a.ly < H / 2 ? Math.min(a.lx * sx, box.w - 90) : a.lx * sx}
              y={a.ly < H / 2 ? 22 : box.h - 14}>{a.name}</text>
          ))}
        </g>

        <g>
          {nodes.map((n) => {
            const state = trace.live[n.id] || (trace.liveBg?.[n.id] && "bg");
            const weight = keyed ? 0.6 + ((trace.use?.[n.id] || 0) / maxUse) * 3.2 : 1.5;
            const cls = ["edge", state, trace.touched?.has(n.id) && "touched",
                         (hot === n.id || keyed) && "show"].filter(Boolean).join(" ");
            return <path key={n.id} d={curve(hubAt, n)} className={cls} strokeWidth={weight.toFixed(2)} />;
          })}
        </g>

        <g>
          {nodes.map((n) => {
            const live = trace.live[n.id] || trace.liveBg?.[n.id];
            const cls = ["node", n.machine ? "machine" : band(n.lvl), n.snoozed && "snoozed", n.broken && "broken",
                         hot === n.id && "hot", live && "busy", n.id === "vault" && "entry"]
              .filter(Boolean).join(" ");
            const ringR = n.R + 5;
            const labelY = n.R + (n.recent != null ? 20 : 15);
            return (
              <g key={n.id} className={cls} transform={`translate(${n.x} ${n.y}) scale(${k})`}
                tabIndex={n.machine && n.id !== "vault" ? -1 : 0}
                role={n.machine && n.id !== "vault" ? undefined : "button"} data-card={n.id}
                aria-label={n.id === "vault" ? "Vault — your notes"
                  : n.machine ? n.name
                  : `${n.name}, ${n.lvl == null ? "loading" : ["quiet", "today", "needs you"][n.lvl]}, ${n.summary}`}
                onPointerDown={(e) => startDrag(e, n)}
                onPointerEnter={(e) => { if (e.pointerType !== "touch") onHot(n.id); }}
                onPointerLeave={(e) => { if (e.pointerType !== "touch") onHot(null); }}
                onFocus={() => onHot(n.id)} onBlur={() => onHot(null)}
                onClick={(e) => click(e, n)}
                onDoubleClick={() => !n.machine && onFull(n.id)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  if (n.id === "vault") { e.preventDefault(); onVault(); }
                  else if (!n.machine) { e.preventDefault(); onOpen(n.id); }
                }}>
                <circle r={34} className="hit" />
                {/* The ring shows only for a broken source, the hovered node, or the key. */}
                {n.recent != null && (n.broken || keyed || hot === n.id) && Array.from({ length: 24 }, (_, i) => {
                  // right-aligned: the newest sync is always the last segment
                  const ch = n.recent[n.recent.length - 24 + i];
                  return <path key={i} d={ringSeg(ringR, i, 24)} fill="none" strokeWidth={n.lvl === QUIET ? 2 : 3}
                    className={ch === "o" ? "seg-ok" : ch === "x" ? "seg-fail" : ch === "." ? "seg-skip" : "seg-none"} />;
                })}
                {n.machine
                  ? <><rect x={-n.R + 2} y={-n.R + 2} width={2 * n.R - 4} height={2 * n.R - 4} rx={5} className="core" />
                      <rect x={-3} y={-3} width={6} height={6} rx={1} className="glyph" /></>
                  : <><circle r={n.R - 2} className="core" />{n.lvl !== QUIET && <circle r={3.5} className="glyph" />}</>}
                <text x={nudge(n.name, n.x, box.w, k)} y={labelY} textAnchor="middle" className="nm">{n.name}</text>
                {n.lvl === NEEDS && !short && (() => {
                  const st = clip(n.summary.split(" · ")[0], 24);
                  return <text x={nudge(st, n.x, box.w, k)} y={labelY + 15} textAnchor="middle" className="st">{st}</text>;
                })()}
                {n.lvl === NEEDS && n.asks > 0 && (
                  <g className={`badge${n.broken ? " bad" : ""}`} transform={`translate(${n.R + 3} ${-n.R - 3})`}>
                    <circle r={8} /><text y={3.5} textAnchor="middle">{n.asks}</text>
                  </g>
                )}
              </g>
            );
          })}
        </g>

        <g className={`map-hub ${hub}`} transform={`translate(${hubAt.x} ${hubAt.y}) scale(${k})`}
          onClick={onHub} role="button" tabIndex={0} {...hubDrop}
          onKeyDown={(e) => { if (e.key === "Enter") onHub(); }}
          aria-label="Kestrel — the console">
          <circle r={40} className="hit" />
          <circle r={18} className="halo" />
          <g className="halo-on"><circle r={18} /></g>
          <circle r={9} className="core" />
          {hub === "replied" && <>
            <circle cx={13} cy={-13} r={5} className="unread-ping" />
            <circle cx={13} cy={-13} r={5} className="unread" />
          </>}
          <text y={-28} textAnchor="middle" className="nm">Kestrel</text>
          {!(short && hub === "idle") && <text y={38} textAnchor="middle" className="st">
            {hub === "asking" ? "waiting on you"
              : hub === "working" ? (agents.length > 1 ? `${agents.length} working…` : "working…")
              : hub === "replied" ? "replied" : "idle"}
          </text>}
        </g>

        {/* Kestrel dots (click goes to the tab); dotMotion.js owns each transform. */}
        {[...dots, ...ghosts.filter((g) => !dots.some((d) => d.id === g.id))].map((d) => {
          const a = d.tab, gone = !dots.includes(d);
          const open = gone ? {} : {
            role: "button", tabIndex: d.sub ? -1 : 0,
            onClick: () => onAgent?.(a.id),
            onKeyDown: (e) => { if (e.key === "Enter") onAgent?.(a.id); },
            onPointerEnter: () => motion.current.hover(d.id, true),
            onPointerLeave: () => motion.current.hover(d.id, false),
          };
          return (
            <g key={d.id} ref={(el) => motion.current.attach(d.id, el)}
              className={`agent-dot${d.sub ? " sub" : ""}${!d.sub && !gone && a.waiting ? " waiting" : ""}${a.id === activeTab ? " on" : ""}${gone ? " gone" : ""}`}
              {...open} aria-hidden={gone || d.sub || undefined}
              aria-label={gone || d.sub ? undefined : `Tab ${a.n}: ${a.title}${a.waiting ? ", waiting on you" : ", working"}`}>
              <title>{`Tab ${a.n} · ${a.title}${d.sub ? " · subagent" : a.waiting ? " · waiting on you" : ""}`}</title>
              <circle r={d.sub ? 6 : 9} className="hit" />
              <circle r={d.sub ? 2.2 : 4.5} className="dot" />
              {!d.sub && agents.length > 1 && <text y={-7.5} textAnchor="middle" className="n">{a.n}</text>}
            </g>
          );
        })}

        {/* Top layer: the hovered node's full summary, drawn last so no neighbour covers it. */}
        {hotNode?.summary && (() => {
          const text = clip(hotNode.summary, Math.floor((box.w - 16) / LABEL_CH));
          return (
            <text className="hover-label" textAnchor="middle"
              x={hotNode.x + nudge(text, hotNode.x, box.w, 1)}
              y={hotNode.y + (hotNode.R + (hotNode.recent != null ? 20 : 15) + 15) * k}>
              {text}
            </text>
          );
        })()}
      </svg>

      <button className="key-btn" aria-expanded={keyed} onClick={onKey}>Key</button>
      {keyed && (
        <div className="map-key">
          <span><i className="k-dot needs" />needs you · <i className="k-dot has" />today · <i className="k-dot quiet" />quiet</span>
          <span>The ground: each card is a hill as tall as it needs you, warmer where it wants you. A clear day goes flat. Calls ripple out from the node they hit.</span>
          <span>Quiet cards are bare dots, and the machine's nodes stay hidden until a call uses them. Hover for a name.</span>
          <span>Ring: the source's last 24 syncs — <b className="k-ok">ok</b> · <b className="k-fail">failed</b> · <b className="k-skip">nothing to do</b>. Shown on hover, and always once two failures in a row mark it broken.</span>
          <span>Lines: where the console goes, heavier the more often. At rest only the last turn's show; during a turn each call lights its line, dashed while it waits on you, red if it fails.</span>
          <span>Dots: one per console tab at work, circling Kestrel while it thinks and the node it's on while it calls one; small ones are its subagents, which go off to their own nodes. Click a dot for its tab.</span>
          <span>Drag a card onto the console or Kestrel to pin it.</span>
        </div>
      )}
      {ghost && <div className="drag-ghost" style={{ left: ghost.x + 12, top: ghost.y + 10 }}>{ghost.label}</div>}
    </div>
  );
}
