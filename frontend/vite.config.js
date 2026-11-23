import { readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.KESTREL_BACKEND_PORT || "8788";

// A window outlives the build it loaded. Dev tabs build all day under an open
// Kestrel window, whose page still imports its own hashed chunks on demand, so
// a build that empties dist 404s whatever that page hasn't fetched yet — KaTeX
// on 11-03, which blanked the desk on the first formula of the afternoon. So
// emptyOutDir is off below and old chunks age out here instead. Rollup rewrites
// everything a build emits, so an mtime this old means no build wants the file.
// A week outlasts an open window in practice; past it, ui/markdown.jsx fails soft.
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

// Bitmap glyphs drawn too thin to see. TeX PDFs built from PK fonts carry some
// glyphs as Type 3 bitmaps — CSCI 170's handouts draw the listing minus as a
// 54×2 image mask, 0.24pt tall. pdf.js traces such a mask into a path and fills
// it at true size, so at a side-panel scale it's half a device pixel and
// antialiases to a faint smear or nothing ("n 1" for "n−1"), depending on where
// it lands. Poppler and Preview give a mask at least one whole pixel per axis;
// this does the same in pdf.js's compiled-glyph painter, snapped to the pixel
// row/column under the glyph's centre. 6.5.438 (11-07) still has the bug.
//
// That alone isn't enough: the glyph's `d1` bbox (5 20 60 22 for the minus) is
// just as thin, and pdf.js clips to it before painting. Chrome antialiases the
// clip, so the snapped bar came out 50–75% grey; WebKit — what Kestrel.app
// runs — drops it outright wherever the hairline straddles pixel rows, so
// "a[j 1]" survived the first fix there (11-08). So the clip grows one device
// pixel each way on any axis under a pixel; the image row it must admit lies
// within half a pixel of the bbox, and nothing outside the mask paints anyway.
//
// The anchors are pinned to the exact pdfjs-dist in package.json: a version
// bump that moves one fails the build loudly rather than silently losing it.
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
    // "hidden": the .map files are written next to each bundle but the bundle
    // carries no sourceMappingURL, so the page never asks for them. They are
    // for the backend — failures.py maps a desk report's minified frames
    // (gk@…/assets/index-D2c-yNSi.js:55:48155) back to src/file:line when the
    // report arrives, because the map for that hash only lasts as long as
    // pruneOldAssets lets it. main.py refuses to serve them.
    sourcemap: "hidden",
  },
  server: {
    // KESTREL_BACKEND_PORT points the proxy at a backend started from a
    // worktree (verify skill, Launch), so backend changes can be driven
    // before they're merged.
    // changeOrigin off (the string shorthand turns it on) so the backend sees
    // the Host the page was served from: a page on any Vite port, the verify
    // rig's :5199 included, is then same-origin through the proxy, which
    // localguard.origin_ok lets write.
    proxy: {
      "/api": { target: `http://localhost:${BACKEND}`, changeOrigin: false },
      "/ws": { target: `ws://localhost:${BACKEND}`, ws: true },
    },
  },
});
