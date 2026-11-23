import { useEffect, useRef } from "react";
import { palette } from "../lib/tokens.js";
import { useLook } from "../lib/look.js";
import { GROUND_PAINTERS } from "./mapGrounds.js";

// The ground under the map, and the one place on the desk that moves. It is a
// height field, not a texture: every card raises a hill as tall as it needs you
// (`hills`), so a heavy day is a range of peaks and a clear one goes almost
// flat. A console call hitting a node sends a ripple through the field, and the
// contours bend as it passes; the ground also swells a little under the pointer.
// At rest it barely breathes: movement here means the agent, or you.
//
// Drawn on a canvas under the SVG, which keeps every node, label and click.
// Heights ease towards their targets in the loop, so nothing here re-renders
// React. No frames while hidden, off screen or under a full view; with reduced
// motion it draws one still frame per change, with no ripples or swell.

const STEP = 9;                 // px between samples: finer costs more than it shows
const FRAME_MS = 1000 / 24;
const REST_FRAME_MS = 1000 / 6;
const LEVELS = Array.from({ length: 12 }, (_, i) => 0.24 + i * 0.17);
const EASE = 0.05;              // of the way to a hill's target, per frame
const RIPPLE = { speed: 150, life: 2.1, width: 24, height: 0.55 };
const SWELL = { height: 0.42, reach: 70 };
const HUB_PULSE_S = 2.6;        // a ripple from Kestrel while a turn runs
const MAX_TINTED = 6;           // strokes per frame, so the cost stays bounded
const PALETTE_EVERY = 48;       // frames between re-reading the theme

const hash = (i, j) => { const t = Math.sin(i * 127.1 + j * 311.7 + 7.7) * 43758.5453; return t - Math.floor(t); };
function noise(x, y) {
  const i = Math.floor(x), j = Math.floor(y), fx = x - i, fy = y - j;
  const u = fx * fx * (3 - 2 * fx), v = fy * fy * (3 - 2 * fy);
  return (hash(i, j) * (1 - u) + hash(i + 1, j) * u) * (1 - v)
    + (hash(i, j + 1) * (1 - u) + hash(i + 1, j + 1) * u) * v;
}

// Marching squares for one level over one cell. Corners run clockwise from the
// top left; each crossed edge is interpolated, so a contour glides as the field
// moves rather than stepping from sample to sample.
function cell(path, x, y, a, b, c, d, l) {
  const code = (a > l ? 8 : 0) | (b > l ? 4 : 0) | (c > l ? 2 : 0) | (d > l ? 1 : 0);
  if (code === 0 || code === 15) return;
  const top = () => [x + STEP * (l - a) / (b - a), y];
  const right = () => [x + STEP, y + STEP * (l - b) / (c - b)];
  const bottom = () => [x + STEP * (l - d) / (c - d), y + STEP];
  const left = () => [x, y + STEP * (l - a) / (d - a)];
  const seg = (p, q) => { path.moveTo(p[0], p[1]); path.lineTo(q[0], q[1]); };
  switch (code) {
    case 1: case 14: seg(left(), bottom()); break;
    case 2: case 13: seg(bottom(), right()); break;
    case 3: case 12: seg(left(), right()); break;
    case 4: case 11: seg(top(), right()); break;
    case 6: case 9: seg(top(), bottom()); break;
    case 7: case 8: seg(left(), top()); break;
    case 5: seg(left(), top()); seg(bottom(), right()); break;
    case 10: seg(left(), bottom()); seg(top(), right()); break;
    default:
  }
}

const hexToRgb = (hex) => {
  const n = parseInt(hex.replace("#", "").slice(0, 6), 16);
  return Number.isNaN(n) ? "188, 82, 21" : `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
};

export default function MapTerrain({ width, height, hills, ripples, working, still }) {
  const canvas = useRef(null);
  const scene = useRef({ hills: new Map(), ripples: [], pointer: { x: 0, y: 0, a: 0, on: false },
                         lastPulse: 0, working: false, still, draw: null, seen: new Set() });
  const s = scene.current;
  s.working = working;
  s.still = still;
  // The look lab's map ground (round 4): contours are drawn here, the rest by
  // a painter over the same field (plan/mapGrounds.js).
  const { ground } = useLook();
  const painter = GROUND_PAINTERS[ground] || null;

  // Keep hills as eased objects: a new one rises out of the ground, and one
  // that changes band moves there rather than jumping.
  useEffect(() => {
    const live = new Set();
    for (const h of hills) {
      live.add(h.id);
      const cur = s.hills.get(h.id);
      if (cur) Object.assign(cur, h);
      else s.hills.set(h.id, { ...h, a: still ? h.lift : 0 });
    }
    for (const id of s.hills.keys()) if (!live.has(id)) s.hills.delete(id);
    // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- syncs props into the canvas scene (s), an external system the draw loop reads
    if (still) for (const h of s.hills.values()) h.a = h.lift;
    s.draw?.();
  }, [hills, still, s]);

  // A node newly in use throws one ripple, and only one.
  useEffect(() => {
    const now = performance.now() / 1000;
    const next = new Set(ripples.map((r) => r.id));
    // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- syncs props into the canvas scene (s), an external system the draw loop reads
    if (!still) {
      for (const r of ripples) {
        if (!s.seen.has(r.id)) s.ripples.push({ x: r.x, y: r.y, t0: now });
      }
    }
    s.seen = next;
  }, [ripples, still, s]);

  useEffect(() => {
    const el = canvas.current;
    const ctx = el.getContext("2d");
    if (!ctx) return undefined;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    el.width = Math.round(width * dpr);
    el.height = Math.round(height * dpr);
    const nx = Math.ceil(width / STEP) + 2, ny = Math.ceil(height / STEP) + 2;
    const field = new Float32Array(nx * ny);
    let colors = palette(), frame = 0, raf = 0, last = 0;

    const fill = (t) => {
      const hs = [...s.hills.values()].filter((h) => h.a > 0.01);
      const drift = t * 0.012;
      const { pointer: p, ripples: rs } = s;
      for (let j = 0, at = 0; j < ny; j++) {
        const y = j * STEP;
        for (let i = 0; i < nx; i++, at++) {
          const x = i * STEP;
          let v = noise(x / 150 + drift, y / 150 - drift) * 0.42 + noise(x / 58 - drift, y / 58) * 0.1;
          for (const h of hs) {
            const dx = x - h.x, dy = y - h.y;
            const d2 = dx * dx + dy * dy;
            if (d2 < h.far) v += h.a * Math.exp(-d2 / h.sig2);
          }
          for (const r of rs) {
            const age = t - r.t0, dx = x - r.x, dy = y - r.y;
            const off = Math.sqrt(dx * dx + dy * dy) - age * RIPPLE.speed;
            if (Math.abs(off) < RIPPLE.width * 3) {
              v += RIPPLE.height * (1 - age / RIPPLE.life)
                * Math.exp(-(off * off) / (2 * RIPPLE.width * RIPPLE.width));
            }
          }
          if (p.a > 0.01) {
            const dx = x - p.x, dy = y - p.y;
            v += p.a * Math.exp(-(dx * dx + dy * dy) / (2 * SWELL.reach * SWELL.reach));
          }
          field[at] = v;
        }
      }
    };

    const paint = () => {
      const minor = new Path2D(), major = new Path2D();
      LEVELS.forEach((l, k) => {
        const path = k % 4 === 3 ? major : minor;
        for (let j = 0; j < ny - 1; j++) {
          for (let i = 0; i < nx - 1; i++) {
            const at = j * nx + i;
            cell(path, i * STEP, j * STEP, field[at], field[at + 1], field[at + nx + 1], field[at + nx], l);
          }
        }
      });
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      // What wants you warms the ground under it: a wash, then its contours.
      const tinted = [...s.hills.values()].filter((h) => h.tint && h.a > 0.05)
        .sort((a, b) => b.a - a.a).slice(0, MAX_TINTED);
      for (const h of tinted) {
        const rgb = hexToRgb(h.tint === "bad" ? colors.bad : colors.accent);
        const r = Math.sqrt(h.sig2) * 1.9;
        const wash = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, r);
        // Round 2 (docs/ui-overhaul.md): half the old strength. The tint is
        // a hint of where to look, the node's ring and count say what.
        wash.addColorStop(0, `rgba(${rgb}, ${0.05 * Math.min(1, h.a)})`);
        wash.addColorStop(1, `rgba(${rgb}, 0)`);
        ctx.fillStyle = wash;
        ctx.fillRect(h.x - r, h.y - r, r * 2, r * 2);
      }
      ctx.strokeStyle = colors.line;
      ctx.lineWidth = 1;
      ctx.stroke(minor);
      ctx.strokeStyle = colors.line2;
      ctx.lineWidth = 1.3;
      ctx.stroke(major);
      for (const h of tinted) {
        const rgb = hexToRgb(h.tint === "bad" ? colors.bad : colors.accent);
        const r = Math.sqrt(h.sig2) * 1.55;
        const warm = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, r);
        warm.addColorStop(0, `rgba(${rgb}, 0.35)`);
        warm.addColorStop(1, `rgba(${rgb}, 0)`);
        ctx.strokeStyle = warm;
        ctx.lineWidth = 1;
        ctx.stroke(minor);
        ctx.lineWidth = 1.3;
        ctx.stroke(major);
      }
    };

    const mem = {};
    const draw = (t) => {
      if (!painter) return paint();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      painter.paint(ctx, { field, nx, ny, step: STEP, width, height, colors, t, mem,
                           working: s.working, hills: [...s.hills.values()] });
    };
    const once = () => {
      if (!el.offsetWidth) return;
      colors = terrainColors(el);
      fill(0);
      draw(0);
    };
    s.draw = s.still ? once : null;

    const tick = (ms) => {
      raf = requestAnimationFrame(tick);
      // This sits open all day, so at rest it only keeps up with the slow drift.
      const p = s.pointer;
      const moving = s.working || s.ripples.length > 0 || p.on || p.a > 0.01
        || [...s.hills.values()].some((h) => Math.abs(h.lift - h.a) > 0.01);
      const rest = painter ? 1000 / painter.fps : REST_FRAME_MS;
      if (ms - last < (moving ? Math.min(FRAME_MS, rest) : rest) || document.hidden || !el.offsetWidth) return;
      last = ms;
      const t = ms / 1000;
      if (frame++ % PALETTE_EVERY === 0) colors = terrainColors(el);
      for (const h of s.hills.values()) h.a += (h.lift - h.a) * EASE;
      p.a += ((p.on ? SWELL.height : 0) - p.a) * 0.08;
      s.ripples = s.ripples.filter((r) => t - r.t0 < RIPPLE.life);
      if (s.working && t - s.lastPulse > HUB_PULSE_S) {
        s.lastPulse = t;
        const hub = s.hills.get("hub");
        if (hub) s.ripples.push({ x: hub.x, y: hub.y, t0: t });
      }
      fill(t);
      draw(t);
    };
    if (s.still) once();
    else raf = requestAnimationFrame(tick);

    const stage = el.parentElement;
    const move = (e) => {
      const box = el.getBoundingClientRect();
      Object.assign(s.pointer, { x: e.clientX - box.left, y: e.clientY - box.top, on: true });
    };
    const leave = () => { s.pointer.on = false; };
    if (!s.still) {
      stage.addEventListener("pointermove", move);
      stage.addEventListener("pointerleave", leave);
    }
    return () => {
      cancelAnimationFrame(raf);
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerleave", leave);
      s.draw = null;
    };
  }, [width, height, still, s, painter]);

  return <canvas ref={canvas} className="map-terrain" aria-hidden="true"
    style={{ width, height }} />;
}

// Read each time, so a theme flip lands within a second; off the canvas, not
// the root, so a ground's own tokens on the map pane (looks/grounds.css) win.
function terrainColors(el) {
  const css = getComputedStyle(el || document.documentElement);
  const tok = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return { ...palette(), raise: tok("--paper-raise", "#FFFFFF"), ink: tok("--paper-ink", "#19191C"),
           mid: tok("--paper-mid", "#636368"), accent: tok("--accent", "#006CE2"),
           line: tok("--paper-line", "#E6E4D9"), line2: tok("--paper-line-2", "#DAD8CE"),
           bad: tok("--bad", "#AF3029") };
}
