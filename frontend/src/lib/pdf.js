// Document pages for the console: load a kept PDF, draw a page, find a quote. pdf.js (~1MB with worker) loads on
// first draw; bytes are decoded in the browser, never the backend, with `isEvalSupported: false` (CVE-2024-4367:
// the dropped PDF is untrusted). Quote matching is on letters and digits only, case- and ligature-folded.
// Design: DESIGN.md → Documents: citing a page, showing a page (`app/docs.py`).

let lib = null;
function pdfjs() {
  if (!lib) {
    lib = Promise.all([
      import("pdfjs-dist"),
      import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
    ]).then(([m, worker]) => {
      m.GlobalWorkerOptions.workerSrc = worker.default;
      return m;
    });
    lib.catch(() => { lib = null; });
  }
  return lib;
}

const docs = new Map();   // ref -> Promise<PDFDocumentProxy>

export const fileUrl = (ref) => `/api/cite/${encodeURIComponent(ref)}/file`;

export function loadDoc(ref) {
  const key = String(ref || "").toLowerCase();
  if (!docs.has(key)) {
    const p = pdfjs().then((m) => m.getDocument({
      url: fileUrl(key), isEvalSupported: false, enableXfa: false,
    }).promise);
    // A failed load isn't cached (the backend may have been restarting).
    p.catch(() => docs.delete(key));
    docs.set(key, p);
  }
  return docs.get(key);
}

// A PDF off disk (/api/files/raw); not cached, the file may have been rewritten.
export function loadDocUrl(url) {
  return pdfjs().then((m) => m.getDocument({
    url, isEvalSupported: false, enableXfa: false,
  }).promise);
}

// Draw a page at `width` CSS px; returns the viewport (highlight rects are measured in it) and the render task.
export async function drawPage(doc, pageNo, canvas, width) {
  const page = await doc.getPage(pageNo);
  const base = page.getViewport({ scale: 1 });
  const viewport = page.getViewport({ scale: width / base.width });
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  const task = page.render({
    canvasContext: canvas.getContext("2d"),
    viewport,
    transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
  });
  return { page, viewport, task };
}

const KEEP = /[\p{L}\p{N}]/u;
// Spellings a text layer uses that nobody types.
const FOLD = { "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ß": "ss" };

function fold(ch) {
  const f = FOLD[ch] ?? ch.normalize("NFKD").replace(/[̀-ͯ]/g, "");
  return f.toLowerCase();
}

// A string's matchable letters, plus where each came from.
function keyOf(text) {
  let key = "";
  const at = [];
  for (let i = 0; i < text.length; i++) {
    for (const c of fold(text[i])) {
      if (KEEP.test(c)) { key += c; at.push(i); }
    }
  }
  return { key, at };
}

// One page's text layer flattened: joined strings plus each character's owning item.
async function layerOf(page) {
  const content = await page.getTextContent();
  const items = content.items.filter((it) => typeof it.str === "string");
  let text = "";
  const owner = [];
  items.forEach((it, n) => {
    for (let i = 0; i < it.str.length; i++) owner.push([n, i]);
    text += it.str;
  });
  return { items, text, owner };
}

// Where `quote` sits on `page`, as rects in 0-1 page fractions; rects null = not on this page.
// `noText` = no text layer (a scan): "can't check", not "isn't there".
export async function findOnPage(page, quote) {
  const layer = await layerOf(page);
  if (!layer.text.trim()) return { noText: true, rects: null };
  const q = keyOf(quote || "").key;
  if (q.length < 3) return { noText: false, rects: null };
  const hay = keyOf(layer.text);
  const hit = hay.key.indexOf(q);
  if (hit < 0) return { noText: false, rects: null };

  const first = hay.at[hit];
  const last = hay.at[hit + q.length - 1];
  const vp = page.getViewport({ scale: 1 });
  // Per item, the run of its characters the match covers.
  const spans = new Map();
  for (let c = first; c <= last; c++) {
    const [n, i] = layer.owner[c];
    const s = spans.get(n);
    if (s) s.end = i + 1; else spans.set(n, { start: i, end: i + 1 });
  }
  const rects = [];
  for (const [n, { start, end }] of spans) {
    const it = layer.items[n];
    const len = it.str.length || 1;
    const [a, b, c, d, e, f] = it.transform;
    const h = it.height || Math.hypot(c, d) || Math.hypot(a, b);
    // pdf.js gives the item's width, not each glyph's: an even split is close enough.
    const x0 = e + (it.width * start) / len;
    const x1 = e + (it.width * end) / len;
    const [vx0, vy0, vx1, vy1] = vp.convertToViewportRectangle([x0, f - h * 0.2, x1, f + h * 0.9]);
    rects.push({
      left: Math.min(vx0, vx1) / vp.width, top: Math.min(vy0, vy1) / vp.height,
      width: Math.abs(vx1 - vx0) / vp.width, height: Math.abs(vy1 - vy0) / vp.height,
    });
  }
  return { noText: false, rects };
}

// The cited page first, then neighbours (off-by-one: printed slide number vs file page), then everywhere.
// status: "here" | "moved" (`page` names where) | "notext" (cited page is a scan) | "missing".
export async function locate(doc, pageNo, quote) {
  const n = doc.numPages;
  const cited = Math.min(Math.max(1, pageNo), n);
  const order = [cited, cited - 1, cited + 1, cited - 2, cited + 2]
    .filter((p, i, all) => p >= 1 && p <= n && all.indexOf(p) === i);
  for (let p = 1; p <= Math.min(n, 400); p++) if (!order.includes(p)) order.push(p);
  let scan = false;
  for (const p of order) {
    const got = await findOnPage(await doc.getPage(p), quote);
    if (p === cited && got.noText) scan = true;
    if (got.rects) return { page: p, rects: got.rects, status: p === cited ? "here" : "moved" };
  }
  return { page: cited, rects: [], status: scan ? "notext" : "missing" };
}
