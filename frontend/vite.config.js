import { readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.KESTREL_BACKEND_PORT || "8788";

// A window outlives its build: an open page imports its own hashed chunks on demand (KaTeX, say), so a build that
// empties dist 404s them. emptyOutDir is off; old chunks age out here (mtime this old = no build wants it).
// A week outlasts an open window in practice; past it ui/markdown.jsx fails soft.
const ASSET_KEEP_MS = 7 * 24 * 3600 * 1000;

function pruneOldAssets() {
  return {
    name: "prune-old-assets",
    apply: "build",
    closeBundle() {
      const dir = fileURLToPath(new URL("./dist/assets", import.meta.url));
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (Date.now() - statSync(path).mtimeMs > ASSET_KEEP_MS) rmSync(path);
      }
    },
  };
}

// Bitmap glyphs drawn too thin: TeX PK-font PDFs carry Type 3 bitmap glyphs (CSCI 170's listing minus is a 54x2
// mask, 0.24pt tall). pdf.js fills it at true size, half a device pixel at side-panel scale, so it smears or
// vanishes ("n 1" for "n-1"). Patch 1 gives the mask at least one whole pixel per axis, snapped to the pixel under
// its centre (as Poppler/Preview do); 6.5.438 still has the bug. Patch 2 grows the glyph's thin `d1` bbox clip by
// one device pixel on any axis under a pixel: WebKit (Kestrel.app) drops the hairline outright where it straddles
// pixel rows; Chrome renders it 50-75% grey. Anchors are pinned to package.json's pdfjs-dist: a bump that moves
// one fails the build loudly.
const PDFJS_PATCHES = [
  [`  const drawOutline = function (c) {
    c.save();
    c.scale(1 / width, -1 / height);`,
   `  const drawOutline = function (c) {
    c.save();
    const m = c.getTransform();
    if (m.b === 0 && m.c === 0) {
      const fit = (k, o) => {
        if (Math.abs(k) >= 1) return [k, o];
        const lo = Math.round(o + k / 2 - 0.5);
        return k > 0 ? [1, lo] : [-1, lo + 1];
      };
      const [a, e] = fit(m.a, m.e);
      const [d, f] = fit(m.d, m.f);
      if (a !== m.a || d !== m.d) c.setTransform(a, 0, 0, d, e, f);
    }
    c.scale(1 / width, -1 / height);`],
  [`  setCharWidthAndBounds(xWidth, yWidth, llx, lly, urx, ury) {
    this.ctx.rect(llx, lly, urx - llx, ury - lly);`,
   `  setCharWidthAndBounds(xWidth, yWidth, llx, lly, urx, ury) {
    const m = this.ctx.getTransform();
    if (m.b === 0 && m.c === 0 && m.a !== 0 && m.d !== 0) {
      const gx = Math.abs(m.a * (urx - llx)) < 1 ? 1 / Math.abs(m.a) : 0;
      const gy = Math.abs(m.d * (ury - lly)) < 1 ? 1 / Math.abs(m.d) : 0;
      llx -= gx; urx += gx; lly -= gy; ury += gy;
    }
    this.ctx.rect(llx, lly, urx - llx, ury - lly);`],
];

function pdfjsHairlineGlyphs() {
  return {
    name: "pdfjs-hairline-glyphs",
    enforce: "pre",
    transform(code, id) {
      if (!/pdfjs-dist[\\/]build[\\/]pdf\.mjs$/.test(id.split("?")[0])) return null;
      for (const [anchor, patched] of PDFJS_PATCHES) {
        if (!code.includes(anchor)) {
          throw new Error("pdfjs-hairline-glyphs: anchor not found; pdfjs-dist changed, re-check the patch");
        }
        code = code.replace(anchor, patched);
      }
      return code;
    },
  };
}

export default defineConfig({
  plugins: [react(), pruneOldAssets(), pdfjsHairlineGlyphs()],
  // Pre-bundled deps skip plugin transforms, and pdfjs-dist is already ESM.
  optimizeDeps: { exclude: ["pdfjs-dist"] },
  build: {
    emptyOutDir: false,
    // "hidden": .map files are written but unreferenced; failures.py maps a desk report's minified frames back to
    // src/file:line (the map only lasts as long as pruneOldAssets keeps it). main.py refuses to serve them.
    sourcemap: "hidden",
  },
  server: {
    // KESTREL_BACKEND_PORT points the proxy at a worktree backend (verify skill). changeOrigin off (the string
    // shorthand turns it on) so the backend sees the page's Host: any Vite port, the rig's :5199 included, is then
    // same-origin through the proxy, which localguard.origin_ok lets write.
    proxy: {
      "/api": { target: `http://localhost:${BACKEND}`, changeOrigin: false },
      "/ws": { target: `ws://localhost:${BACKEND}`, ws: true },
    },
  },
});
