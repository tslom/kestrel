// The map's grounds other than contours (round 4 of the look lab, New
// Features #28/#46: "map is kinda cool" but the terrain look has to change).
//
// Each one paints the same thing MapTerrain computes: a height field where
// every card raises a hill as tall as it needs you, ripples run out from a
// node a console call touches, and the ground swells under the pointer. Only
// the drawing differs, so a vote on a ground is a vote on the picture, not on
// what it says. Contours stay in MapTerrain itself (the default).
//
// A painter is `{ fps, paint(ctx, g) }`. `g` carries the field (`field`, `nx`,
// `ny`, `step`), the pane (`width`, `height`), the eased `hills`, `colors`
// (read off the map pane, so a ground's own CSS tokens in looks/grounds.css
// apply), the clock `t`, `working`, and `mem`: an object that lives as long
// as the ground does, for particles and cached patterns. `fps` is the rate at
// rest; anything moving runs at MapTerrain's working rate.

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const smooth = (a, b, v) => { const x = clamp((v - a) / (b - a), 0, 1); return x * x * (3 - 2 * x); };

export function rgbOf(hex, fallback = "0, 108, 226") {
  const h = String(hex || "").trim().replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6);
  const n = parseInt(full, 16);
  return Number.isNaN(n) || full.length !== 6 ? fallback : `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

// Bilinear read of the field at a pixel.
function sampler(g) {
  const { field, nx, ny, step } = g;
  return (x, y) => {
    const fx = clamp(x / step, 0, nx - 1.001), fy = clamp(y / step, 0, ny - 1.001);
    const i = Math.floor(fx), j = Math.floor(fy), u = fx - i, v = fy - j, at = j * nx + i;
    return (field[at] * (1 - u) + field[at + 1] * u) * (1 - v)
      + (field[at + nx] * (1 - u) + field[at + nx + 1] * u) * v;
  };
}

// How much of "wants you" colour a point picks up: the nearest tinted hill's
// reach, so the hue stays a hint of where to look, as on the contours.
function tintAt(hills, x, y) {
  let w = 0, bad = false;
  for (const h of hills) {
    if (!h.tint || h.a < 0.05) continue;
    const dx = x - h.x, dy = y - h.y;
    const k = Math.min(1, h.a) * Math.exp(-(dx * dx + dy * dy) / (h.sig2 * 1.4));
    if (k > w) { w = k; bad = h.tint === "bad"; }
  }
  return { w, bad };
}

// A deterministic jitter, so lights and dots don't swim between frames.
const rand = (i, j, s = 0) => { const t = Math.sin(i * 12.9898 + j * 78.233 + s * 37.719) * 43758.5453; return t - Math.floor(t); };

// ---- dot matrix: a halftone of the field ----------------------------------
const dots = {
  fps: 6,
  paint(ctx, g) {
    const { width, height, colors, hills } = g;
    const at = sampler(g);
    const gap = 11;
    const neutral = new Path2D(), warm = [new Path2D(), new Path2D(), new Path2D()], bad = new Path2D();
    const off = (gap / 2) | 0;
    for (let y = off, row = 0; y < height; y += gap, row++) {
      // every other row half a step over: a hex grid reads as print, a square one as a chart
      for (let x = off + (row % 2 ? gap / 2 : 0); x < width; x += gap) {
        const v = at(x, y);
        const r = clamp((v - 0.2) * 2.1, 0.6, gap * 0.34);
        const { w, bad: isBad } = tintAt(hills, x, y);
        const path = w > 0.12 ? (isBad ? bad : warm[w > 0.55 ? 2 : w > 0.3 ? 1 : 0]) : neutral;
        path.moveTo(x + r, y);
        path.arc(x, y, r, 0, Math.PI * 2);
      }
    }
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = colors.line2;
    ctx.fill(neutral);
    const acc = rgbOf(colors.accent);
    [0.22, 0.4, 0.62].forEach((a, k) => { ctx.fillStyle = `rgba(${acc}, ${a})`; ctx.fill(warm[k]); });
    ctx.fillStyle = `rgba(${rgbOf(colors.bad)}, 0.75)`;
    ctx.fill(bad);
  },
};

// ---- gravity grid: a square grid the hills pull in -------------------------
const grid = {
  fps: 6,
  paint(ctx, g) {
    const { width, height, colors, hills } = g;
    const at = sampler(g);
    const gap = 26, seg = 8, pull = 70;
    // The displacement is down the slope: towards the hill, like a mass in a sheet.
    const warp = (x, y) => {
      const e = 3;
      const gx = (at(x + e, y) - at(x - e, y)) / (2 * e);
      const gy = (at(x, y + e) - at(x, y - e)) / (2 * e);
      return [x + gx * pull * gap, y + gy * pull * gap];
    };
    const minor = new Path2D(), major = new Path2D();
    let k = 0;
    for (let y = 0; y <= height + gap; y += gap, k++) {
      const path = k % 4 === 0 ? major : minor;
      for (let x = -seg; x <= width + seg; x += seg) {
        const [px, py] = warp(x, y);
        if (x === -seg) path.moveTo(px, py); else path.lineTo(px, py);
      }
    }
    k = 0;
    for (let x = 0; x <= width + gap; x += gap, k++) {
      const path = k % 4 === 0 ? major : minor;
      for (let y = -seg; y <= height + seg; y += seg) {
        const [px, py] = warp(x, y);
        if (y === -seg) path.moveTo(px, py); else path.lineTo(px, py);
      }
    }
    ctx.clearRect(0, 0, width, height);
    ctx.lineJoin = "round";
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    ctx.stroke(minor);
    ctx.strokeStyle = colors.line2;
    ctx.lineWidth = 1.25;
    ctx.stroke(major);
    // the well under what wants you, warmed
    for (const h of hills) {
      if (!h.tint || h.a < 0.05) continue;
      const rgb = rgbOf(h.tint === "bad" ? colors.bad : colors.accent);
      const r = Math.sqrt(h.sig2) * 1.7;
      const grad = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, r);
      grad.addColorStop(0, `rgba(${rgb}, ${0.55 * Math.min(1, h.a)})`);
      grad.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.strokeStyle = grad;
      ctx.lineWidth = 1.1;
      ctx.stroke(minor);
      ctx.stroke(major);
    }
  },
};

// ---- flow: particles streaming around the hills ---------------------------
const flow = {
  fps: 24,
  paint(ctx, g) {
    const { width, height, colors, hills, mem, t, working } = g;
    const at = sampler(g);
    const want = Math.min(1100, Math.round((width * height) / 650));
    if (!mem.p || mem.w !== width || mem.h !== height) {
      mem.p = Array.from({ length: want }, (_, i) => ({ x: rand(i, 1) * width, y: rand(i, 2) * height,
                                                        age: rand(i, 3) * 80, life: 50 + rand(i, 4) * 70 }));
      mem.w = width; mem.h = height; mem.last = t;
      ctx.clearRect(0, 0, width, height);
    }
    const dt = clamp(t - (mem.last ?? t), 0, 0.2) * 24;
    mem.last = t;
    // Trails: fade what's there instead of clearing it.
    ctx.save();
    ctx.globalCompositeOperation = "destination-out";
    ctx.fillStyle = `rgba(0, 0, 0, ${clamp(0.07 * dt, 0.03, 0.5)})`;
    ctx.fillRect(0, 0, width, height);
    ctx.restore();
    const speed = (working ? 1.9 : 1) * dt;
    const neutral = new Path2D(), warm = new Path2D();
    const e = 3;
    for (const q of mem.p) {
      const gx = (at(q.x + e, q.y) - at(q.x - e, q.y)) / (2 * e);
      const gy = (at(q.x, q.y + e) - at(q.x, q.y - e)) / (2 * e);
      // along the contour (perpendicular to the slope), plus a slow drift so flat ground still moves
      let vx = -gy * 260 + 0.35, vy = gx * 260 + 0.12;
      const m = Math.hypot(vx, vy);
      const cap = 2.6;
      if (m > cap) { vx *= cap / m; vy *= cap / m; }
      const nx = q.x + vx * speed, ny = q.y + vy * speed;
      const { w } = tintAt(hills, q.x, q.y);
      const path = w > 0.25 ? warm : neutral;
      path.moveTo(q.x, q.y);
      path.lineTo(nx, ny);
      q.x = nx; q.y = ny; q.age += dt;
      if (q.age > q.life || q.x < -4 || q.y < -4 || q.x > width + 4 || q.y > height + 4) {
        q.x = Math.random() * width; q.y = Math.random() * height; q.age = 0;
      }
    }
    ctx.lineCap = "round";
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = colors.mid;
    ctx.globalAlpha = 0.55;
    ctx.stroke(neutral);
    ctx.strokeStyle = colors.accent;
    ctx.globalAlpha = 0.9;
    ctx.stroke(warm);
    ctx.globalAlpha = 1;
  },
};

// ---- night lights: the desk from orbit ------------------------------------
const lights = {
  fps: 8,
  paint(ctx, g) {
    const { width, height, colors, hills, t } = g;
    const at = sampler(g);
    ctx.clearRect(0, 0, width, height);
    // a glow over each lit place first, then the lights on top
    ctx.globalCompositeOperation = "lighter";
    const warmRgb = "255, 178, 92", coolRgb = "150, 190, 255";
    for (const h of hills) {
      if (h.a < 0.1) continue;
      const rgb = h.tint === "bad" ? rgbOf(colors.bad) : h.tint ? warmRgb : coolRgb;
      const r = Math.sqrt(h.sig2) * (h.tint ? 1.6 : 1.1);
      const glow = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, r);
      glow.addColorStop(0, `rgba(${rgb}, ${0.16 * Math.min(1.2, h.a)})`);
      glow.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(h.x - r, h.y - r, r * 2, r * 2);
    }
    const gap = 6;
    const buckets = [[], [], []];   // dim, mid, bright
    const warmB = [[], [], []];
    for (let j = 0, y = 0; y < height; y += gap, j++) {
      for (let i = 0, x = 0; x < width; x += gap, i++) {
        const v = at(x, y);
        // only above a level does a place have lights at all, and the density climbs with height
        const lit = smooth(0.62, 1.5, v);
        if (lit <= 0) continue;
        if (rand(i, j) > lit * 0.9 + 0.04) continue;
        const px = x + (rand(i, j, 1) - 0.5) * gap, py = y + (rand(i, j, 2) - 0.5) * gap;
        const tw = 0.75 + 0.25 * Math.sin(t * (0.6 + rand(i, j, 3) * 1.3) + i * 1.7 + j);
        const b = lit * tw;
        const { w } = tintAt(hills, px, py);
        (w > 0.2 ? warmB : buckets)[b > 0.66 ? 2 : b > 0.33 ? 1 : 0].push(px, py);
      }
    }
    const draw = (list, rgb, alpha, size) => {
      if (!list.length) return;
      ctx.fillStyle = `rgba(${rgb}, ${alpha})`;
      for (let k = 0; k < list.length; k += 2) ctx.fillRect(list[k] - size / 2, list[k + 1] - size / 2, size, size);
    };
    const ink = rgbOf(colors.ink, "233, 236, 244");
    draw(buckets[0], ink, 0.22, 1.2); draw(buckets[1], ink, 0.45, 1.4); draw(buckets[2], ink, 0.8, 1.6);
    draw(warmB[0], warmRgb, 0.35, 1.3); draw(warmB[1], warmRgb, 0.65, 1.5); draw(warmB[2], warmRgb, 0.95, 1.8);
    ctx.globalCompositeOperation = "source-over";
  },
};

// ---- aurora glass: soft colour fields under frost --------------------------
const aurora = {
  fps: 12,
  paint(ctx, g) {
    const { width, height, colors, hills, t, mem } = g;
    ctx.clearRect(0, 0, width, height);
    const [r0, g0, b0] = rgbOf(colors.raise, "255, 255, 255").split(",").map(Number);
    const dark = 0.299 * r0 + 0.587 * g0 + 0.114 * b0 < 110;
    const acc = rgbOf(colors.accent);
    // A few slow ambient fields so a clear day is still a wallpaper, not white.
    const amb = dark
      ? ["88, 86, 214", "48, 176, 199", "175, 82, 222"]
      : ["161, 196, 253", "194, 233, 251", "222, 196, 255"];
    const R = Math.max(width, height);
    amb.forEach((rgb, k) => {
      const x = width * (0.5 + 0.38 * Math.sin(t * 0.03 + k * 2.1));
      const y = height * (0.5 + 0.34 * Math.cos(t * 0.025 + k * 1.7));
      const grad = ctx.createRadialGradient(x, y, 0, x, y, R * 0.55);
      grad.addColorStop(0, `rgba(${rgb}, ${dark ? 0.32 : 0.55})`);
      grad.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);
    });
    // Each card's hill is a colour field: what wants you warm, the rest cool.
    for (const h of hills) {
      if (h.a < 0.08) continue;
      const rgb = h.tint === "bad" ? rgbOf(colors.bad) : h.tint ? (dark ? "255, 159, 10" : "255, 149, 0") : acc;
      const r = Math.sqrt(h.sig2) * (h.tint ? 2.2 : 1.5);
      // dark: orange at full strength over near-black reads as brown, not glow
      const alpha = (h.tint ? (dark ? 0.28 : 0.42) : 0.12) * Math.min(1.2, h.a);
      const grad = ctx.createRadialGradient(h.x, h.y, 0, h.x, h.y, r);
      grad.addColorStop(0, `rgba(${rgb}, ${alpha})`);
      grad.addColorStop(1, `rgba(${rgb}, 0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(h.x - r, h.y - r, r * 2, r * 2);
    }
    // Frost: a fixed grain over the colour, the "glass".
    if (!mem.grain) {
      const c = document.createElement("canvas");
      c.width = c.height = 128;
      const gx = c.getContext("2d");
      const img = gx.createImageData(128, 128);
      for (let k = 0; k < img.data.length; k += 4) {
        const v = Math.random() * 255;
        img.data[k] = img.data[k + 1] = img.data[k + 2] = v;
        img.data[k + 3] = 22;
      }
      gx.putImageData(img, 0, 0);
      mem.grain = ctx.createPattern(c, "repeat");
    }
    ctx.fillStyle = mem.grain;
    ctx.fillRect(0, 0, width, height);
  },
};

export const GROUND_PAINTERS = { dots, grid, flow, lights, aurora };
