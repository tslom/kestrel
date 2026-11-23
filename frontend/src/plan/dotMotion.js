// How the map's Kestrel dots move (plan/PlanMap.jsx). One loop owns a dot's
// whole motion — coming out of Kestrel, flying a line, circling, landing —
// because the two halves of it have to agree: a CSS transition for the flight
// and SVG animateMotion for the orbit couldn't see each other, so a dot jumped
// whenever the orbit changed size or the count of dots changed.
//
// It writes the `transform` attribute straight to the element, once a frame,
// and never touches React: a turn already changes state many times a second
// (lib/turn.js), and this would otherwise re-render the map at 60Hz. The loop
// only runs while a dot exists, so an idle map costs nothing.
//
// A body stays where it is for DWELL once it lands, and only heads home after
// SETTLE with nothing to do. Calls come in bursts, each a few hundred ms with
// gaps between; taken literally that sends a dot back toward Kestrel between
// every pair, reversing mid-flight and arriving nowhere. Only the way home
// waits: a new call is real the moment it's made, and a burst's gaps would
// keep resetting the wait if the way out had one.

const HOME = "home";
const V = 24;             // px/s round an orbit, whatever its size
const SUB_ORBIT = 11;     // a subagent's circle round its tab's dot
const DWELL = 900;
const SETTLE = 280;
const TAU = Math.PI * 2;

const ease = (dt, ms) => 1 - Math.exp(-dt / ms);
const inOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const wrap = (a) => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;

// The control point of a map line from a to b. PlanMap draws its lines with
// this too, which is what lets a dot fly exactly along one.
export const bend = (a, b) => ({
  x: (a.x + b.x) / 2 - (b.y - a.y) * 0.12,
  y: (a.y + b.y) / 2 + (b.x - a.x) * 0.12,
});

// dots: [{ id, node, held, sub, parent }], parents before their subagents.
// scene: { hub: {x, y}, orbit, nodes: { id: {x, y, orbit} } }.
export function createDotMotion({ onGone }) {
  const bodies = new Map();
  const els = new Map();
  let scene = null, still = false, raf = null, last = 0, clock = 0;

  // Where a body's circle is centred, for what it's at. A subagent at home
  // circles its tab's dot, which is itself moving.
  const focus = (b, at) => {
    if (at === HOME) {
      const p = b.parent && bodies.get(b.parent);
      return p ? { x: p.px, y: p.py, orbit: SUB_ORBIT } : { ...scene.hub, orbit: scene.orbit };
    }
    return scene.nodes[at] || { ...scene.hub, orbit: scene.orbit };
  };

  const write = (b) => {
    const el = els.get(b.id);
    if (el) el.setAttribute("transform",
      `translate(${b.px.toFixed(2)} ${b.py.toFixed(2)}) scale(${b.s.toFixed(3)})`);
  };

  const depart = (b, to) => {
    const from = { x: b.cx, y: b.cy };
    const p = focus(b, to);
    // Home runs back down the line it went out on, not a mirror of it.
    const on = b.at !== HOME && to === HOME ? scene.nodes[b.at] : null;
    const ctl = on ? bend(scene.hub, on) : bend(b.at === HOME && !b.parent ? scene.hub : from, p);
    const dist = Math.hypot(p.x - from.x, p.y - from.y);
    b.flight = { from, ctl, to, t: 0, ms: Math.max(420, Math.min(1000, 320 + dist * 1.4)) };
  };

  // Everyone circling one thing shares it evenly. Eased, so a dot arriving or
  // leaving spreads the rest instead of snapping them to their new places.
  const spread = (dt) => {
    const groups = new Map();
    for (const b of bodies.values()) {
      if (b.flight) continue;
      const key = b.at === HOME ? b.parent || HOME : b.at;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(b);
    }
    for (const g of groups.values()) g.forEach((b, j) => {
      if (j === 0 || b.held) return;
      const miss = wrap(g[0].ang + (j / g.length) * TAU - b.ang);
      b.ang += dt == null ? miss : miss * ease(dt, 350);
    });
  };

  const advance = (b, dt) => {
    const goal = b.gone ? HOME : b.want;
    if (b.gone && b.flight && b.flight.to !== HOME) depart(b, HOME);
    if (!b.flight && b.at !== goal
        && (b.gone || (clock >= b.landed + DWELL && (goal !== HOME || clock - b.wantAt >= SETTLE))))
      depart(b, goal);

    let p = focus(b, b.at);
    if (b.flight) {
      const f = b.flight, to = focus(b, f.to);
      f.t = Math.min(1, f.t + dt / f.ms);
      const e = inOut(f.t), u = 1 - e;
      b.cx = u * u * f.from.x + 2 * u * e * f.ctl.x + e * e * to.x;
      b.cy = u * u * f.from.y + 2 * u * e * f.ctl.y + e * e * to.y;
      if (f.t === 1) { b.at = f.to; b.landed = clock; b.flight = null; }
      p = to;
    } else { b.cx = p.x; b.cy = p.y; }

    // In flight the circle closes so the dot runs on the line itself; it
    // opens again on arrival.
    const landing = b.gone && !b.flight && b.at === HOME;
    b.r += ((b.flight || landing ? 0 : p.orbit) - b.r) * ease(dt, 160);
    b.speed += ((b.held ? 0 : 1) - b.speed) * ease(dt, 220);
    b.ang += (V / Math.max(b.r, 12)) * b.speed * (dt / 1000);
    b.s += ((landing && b.r < 10 ? 0 : 1) - b.s) * ease(dt, 90);
    b.px = b.cx + Math.cos(b.ang) * b.r;
    b.py = b.cy + Math.sin(b.ang) * b.r;
    return landing && b.s < 0.03;
  };

  const frame = (now) => {
    const dt = Math.min(50, now - last);   // a hidden window stops frames
    last = now;
    clock += dt;
    spread(dt);
    for (const b of [...bodies.values()]) {
      if (advance(b, dt)) { bodies.delete(b.id); onGone(b.id); }
      else write(b);
    }
    raf = bodies.size ? requestAnimationFrame(frame) : null;
  };

  const snap = () => {
    for (const b of bodies.values()) {
      b.flight = null; b.at = b.want; b.s = 1; b.ang = 0;
      b.cx = focus(b, b.at).x; b.cy = focus(b, b.at).y;
      b.r = focus(b, b.at).orbit;
    }
    spread();
    for (const b of bodies.values()) {
      b.px = b.cx + Math.cos(b.ang) * b.r;
      b.py = b.cy + Math.sin(b.ang) * b.r;
      write(b);
    }
  };

  return {
    // After each map render, in a layout effect, so a new dot is placed
    // before it's first painted.
    sync(dots, nextScene, reduce) {
      scene = nextScene;
      still = reduce;
      const live = new Set();
      for (const d of dots) {
        live.add(d.id);
        const want = d.node || HOME;
        let b = bodies.get(d.id);
        if (!b) {
          const p = d.parent && bodies.get(d.parent);
          const x = p ? p.px : scene.hub.x, y = p ? p.py : scene.hub.y;
          b = { id: d.id, parent: d.parent || null, at: HOME, flight: null, gone: false,
                want, wantAt: clock - SETTLE, landed: -Infinity, hover: false,
                cx: x, cy: y, px: x, py: y, r: 0, s: 0, speed: 1, ang: -Math.PI / 2, held: false };
          bodies.set(d.id, b);
        }
        if (b.want !== want) { b.want = want; b.wantAt = clock; }
        b.gone = false;
        b.waiting = !!d.held;
        b.held = b.waiting || b.hover;
      }
      for (const b of [...bodies.values()]) {
        if (live.has(b.id)) continue;
        if (still) bodies.delete(b.id); else b.gone = true;
      }
      if (still) {
        if (raf != null) { cancelAnimationFrame(raf); raf = null; }
        snap();
      } else if (bodies.size) {
        // Written now as well as next frame, for a new element or a moved node.
        for (const b of bodies.values()) { write(b); }
        if (raf == null) { last = performance.now(); raf = requestAnimationFrame(frame); }
      }
    },
    attach(id, el) { if (el) { els.set(id, el); const b = bodies.get(id); if (b) write(b); } else els.delete(id); },
    // A dot you're reaching for stops moving away from you.
    hover(id, on) {
      const b = bodies.get(id);
      if (b) { b.hover = on; b.held = b.waiting || on; }
    },
    stop() { if (raf != null) cancelAnimationFrame(raf); raf = null; },
  };
}
