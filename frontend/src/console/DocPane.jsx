// The document beside the chat: the handout the reply is talking about, open
// at the page it's talking about.
//
// Built for a study tab running full screen (11-07). The reply said "the
// handout's recursive definition of factorial…" and the handout was in
// another window, so following along meant switching away or printing it.
// Inline `[page:]` embeds show the one slide an answer chose; this is the
// whole document, scrollable, and every citation in the reply moves it.
//
// What opens it (Console.jsx): the first doc reference in the newest reply,
// on its own, unless the pane was closed; and any cite, page embed or quiz
// slide clicked, always. The quote, when there is one, is found the same way
// a citation card finds it (lib/pdf.js `locate`), so a page number that's
// off by one still lands on the words, and the verdict goes back to the pill.
//
// Pages draw lazily — a 200-slide deck is 200 canvases otherwise — at the
// pane's width, into placeholders already sized from each page's own
// viewport, so scrolling to page 40 lands on page 40 before it has drawn.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import Icon from "../ui/icons.jsx";
import { loadDoc, loadDocUrl, drawPage, locate, fileUrl } from "../lib/pdf";
import { resolveCite } from "../lib/cite";
import { rawUrl } from "../lib/files";
import { FileActions } from "./FilePane.jsx";

const PAD = 12;   // the page column's side padding, px
const NO_SIZES = [];

function Sheet({ doc, n, width, size, rects, root, sheetRef }) {
  const box = useRef(null);
  const canvas = useRef(null);
  const [near, setNear] = useState(false);
  const [drawn, setDrawn] = useState(false);

  useEffect(() => {
    const el = box.current;
    if (!el || !root) return;
    const io = new IntersectionObserver(
      ([e]) => { if (e.isIntersecting) setNear(true); },
      { root, rootMargin: "900px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [root]);

  useEffect(() => {
    if (!near || !width || !canvas.current) return;
    let live = true;
    let task = null;
    drawPage(doc, n, canvas.current, width).then(async (d) => {
      task = d.task;
      await d.task.promise;
      if (live) setDrawn(true);
    }).catch(() => {});
    return () => { live = false; task?.cancel(); };
  }, [near, doc, n, width]);

  const h = size ? Math.round((width * size.h) / size.w) : Math.round(width * 1.294);
  return (
    <div className="dpn-sheet" data-page={n} style={{ height: h }}
         ref={(el) => { box.current = el; sheetRef(el); }}>
      <canvas ref={canvas} className={drawn ? "" : "doc-pending"} />
      {rects?.map((r, i) => (
        <span key={i} className="doc-mark" style={{
          left: `${r.left * 100}%`, top: `${r.top * 100}%`,
          width: `${r.width * 100}%`, height: `${r.height * 100}%`,
        }} />
      ))}
      <span className="dpn-n">{n}</span>
    </div>
  );
}

// `view` = { ref, page, quote, seq, onVerdict } — seq changes on every
// request, so asking for the page already shown scrolls back to it.
export default function DocPane({ view, docs, recent = [], onPick, onClose, onCover, onSplit }) {
  // What's loaded, with which document it is: another one reads as not yet
  // loaded until its own answer lands, so nothing needs resetting by hand.
  const docKey = view.file ? `file:${view.file.path}` : `ref:${view.ref}`;
  const [loaded, setLoaded] = useState(null);   // { key, doc, sizes, err }
  const mine = loaded?.key === docKey ? loaded : null;
  const doc = mine?.doc || null;
  const err = mine?.err || null;
  const sizes = mine?.sizes || NO_SIZES;
  const [found, setFound] = useState(null);     // locate()'s answer, with the request it's for
  const [current, setCurrent] = useState(view.page);
  const [width, setWidth] = useState(0);
  const [root, setRoot] = useState(null);
  const scroller = useRef(null);
  const scrollerRef = useCallback((el) => { scroller.current = el; setRoot(el); }, []);
  const sheets = useRef([]);
  const pending = useRef(null);               // a page to scroll to once laid out

  // Names for the picker (and the title): `recent` already has most; the
  // rest are resolved once per ref.
  const [resolved, setResolved] = useState({});
  const asked = useRef(new Set());
  const nameOf = (r) => recent.find((d) => d.ref === r)?.name ?? resolved[r];
  useEffect(() => {
    for (const r of [...docs, ...(view.file ? [] : [view.ref])]) {
      // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-pass-data-to-parent -- a fetch, once per ref; resolveCite is a lookup, not a parent
      if (asked.current.has(r) || recent.some((d) => d.ref === r)) continue;
      asked.current.add(r);
      resolveCite(r).then((c) => setResolved((p) => ({ ...p, [r]: c?.title || r })));
    }
  }, [docs, recent, view.ref, view.file]);
  // The picker: this chat's documents first, then the other recent ones.
  const chatDocs = docs.includes(view.ref) ? docs : [view.ref, ...docs];
  const others = recent.filter((d) => !chatDocs.includes(d.ref));

  useEffect(() => {
    let live = true;
    sheets.current = [];
    const key = docKey;
    (view.file ? loadDocUrl(rawUrl(view.file.path)) : loadDoc(view.ref)).then(async (d) => {
      const n = Math.min(d.numPages, 400);
      const vps = await Promise.all(Array.from({ length: n }, (_, i) =>
        d.getPage(i + 1).then((p) => { const v = p.getViewport({ scale: 1 }); return { w: v.width, h: v.height }; })));
      if (live) setLoaded({ key, doc: d, sizes: vps, err: null });
    }, (e) => { if (live) setLoaded({ key, doc: null, sizes: [], err: e?.message || "could not load the document" }); });
    return () => { live = false; };
  }, [docKey]);   // eslint-disable-line react-hooks/exhaustive-deps -- docKey is view.ref / view.file's path, which is all the load reads

  // Where this request lands: the cited page, or wherever the quote really
  // is. Without a quote it's the cited page, known now; with one it's
  // locate()'s answer, kept with the request it answers.
  const request = `${docKey}\u0000${view.seq}`;
  const cited = doc ? Math.min(Math.max(1, view.page || 1), doc.numPages) : null;
  const hit = !doc ? null : !view.quote ? { page: cited, rects: [], status: "none" }
    : found?.request === request ? found : null;
  // A layout effect, declared before the scroll below, so a request with no
  // quote scrolls in the same commit that brings it.
  useLayoutEffect(() => {
    if (doc && !view.quote) pending.current = cited;
  }, [doc, view.seq]);   // eslint-disable-line react-hooks/exhaustive-deps -- seq is what makes a repeat request scroll again; the rest is the request
  useEffect(() => {
    if (!doc || !view.quote) return;
    let live = true;
    const page = cited;
    locate(doc, page, view.quote).then((v) => {
      if (!live) return;
      setFound({ ...v, request });
      pending.current = v.page;
      view.onVerdict?.(v.status);
    }, () => { if (live) { setFound({ page, rects: [], status: "notext", request }); pending.current = page; } });
    return () => { live = false; };
  }, [doc, view.seq]);   // eslint-disable-line react-hooks/exhaustive-deps -- seq is what makes a repeat request scroll again; the rest is the request

  // Scroll to a pending page once its sheet is laid out: after a request
  // (above), a located quote, or the sheets and width arriving.
  useLayoutEffect(() => {
    const p = pending.current;
    const el = sheets.current[p - 1];
    if (!p || !el || !width) return;
    pending.current = null;
    scroller.current.scrollTop = el.offsetTop - 8;
    setCurrent(p);
  }, [doc, sizes, width, view.seq, found]);

  // Side by side needs room for both: a console under 760px wide (a phone,
  // a sheet half up) gets the pane over the log instead of a sliver of each.
  const self = useRef(null);
  useEffect(() => {
    const host = self.current?.closest(".console") || self.current?.parentElement;
    if (!host) return;
    const ro = new ResizeObserver(() => onCover?.(host.clientWidth < 760));
    ro.observe(host);
    return () => { ro.disconnect(); onCover?.(false); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once on mount; onCover is a callback prop whose identity changes every parent render
  }, []);

  // Draw at the pane's width, in steps, so dragging a window edge doesn't
  // redraw every page on every pixel.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const w = Math.max(160, el.clientWidth - 2 * PAD);
      setWidth((prev) => (Math.abs(prev - w) >= 24 || !prev ? Math.floor(w) : prev));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  function onScroll() {
    const el = scroller.current;
    const mark = el.scrollTop + el.clientHeight / 3;
    let n = 1;
    sheets.current.forEach((s, i) => { if (s && s.offsetTop <= mark) n = i + 1; });
    if (n !== current) setCurrent(n);
  }

  function go(n) {
    const el = sheets.current[n - 1];
    if (el) scroller.current.scrollTo({ top: el.offsetTop - 8, behavior: "smooth" });
  }

  const total = doc?.numPages || 0;
  const title = view.file?.name || nameOf(view.ref) || "document";
  // A file off disk (FileLink.jsx) has no picker of its own: one document.
  const picks = view.file ? 0 : chatDocs.length + others.length;
  return (
    <aside className="doc-pane" ref={self} tabIndex={-1} aria-label={`${title}, beside the chat`}
      onKeyDown={(e) => {
        if (e.key === "Escape") { e.stopPropagation(); onClose(); }
      }}>
      <div className="dpn-head">
        {picks > 1 ? (
          <select className="dpn-pick" value={view.ref} onChange={(e) => onPick(e.target.value)}
                  title="Documents in this chat, then recent ones">
            <optgroup label="In this chat">
              {chatDocs.map((r) => <option key={r} value={r}>{nameOf(r) || r}</option>)}
            </optgroup>
            {others.length > 0 && (
              <optgroup label="Recent">
                {others.map((d) => <option key={d.ref} value={d.ref}>{d.name}</option>)}
              </optgroup>
            )}
          </select>
        ) : <span className="dpn-title" title={title}>{title}</span>}
        <span className="dpn-nav">
          <button className="dp-x" onClick={() => go(current - 1)} disabled={!total || current <= 1}
                  aria-label="Previous page" title="Previous page"><Icon name="arrowLeft" size={11} /></button>
          <span className="dpn-num">{total ? `${current} / ${total}` : "…"}</span>
          <button className="dp-x" onClick={() => go(current + 1)} disabled={!total || current >= total}
                  aria-label="Next page" title="Next page"><Icon name="arrowRight" size={11} /></button>
        </span>
        {onSplit && picks > 1 && (
          <button className="dp-x dpn-split" onClick={onSplit}
                  title="Open a second document below this one">+ doc</button>
        )}
        {view.file ? <FileActions path={view.file.path} />
          : <a className="dp-x" href={`${fileUrl(view.ref)}#page=${current}`} target="_blank"
               rel="noopener noreferrer" title="Open in a browser tab"><Icon name="external" size={11} /></a>}
        <button className="dp-x" onClick={onClose} aria-label="Close the document" title="Close · Esc">
          <Icon name="close" size={11} /></button>
      </div>
      {hit?.status === "moved" && (
        <p className="dpn-note">Those words are on p.{hit.page}, not p.{view.page} as cited.</p>)}
      {hit?.status === "missing" && (
        <p className="dpn-note doc-bad">The quoted words aren’t in this document. Treat the claim as unsupported.</p>)}
      <div className="dpn-pages" ref={scrollerRef} onScroll={onScroll}>
        {err && <p className="dpn-note doc-bad">Couldn’t open the document — {err}.</p>}
        {!doc && !err && <p className="dpn-note">Opening…</p>}
        {doc && width > 0 && sizes.map((s, i) => (
          <Sheet key={`${view.ref}-${i}`} doc={doc} n={i + 1} width={width} size={s} root={root}
                 rects={hit && hit.page === i + 1 ? hit.rects : null}
                 sheetRef={(el) => { sheets.current[i] = el; }} />
        ))}
      </div>
    </aside>
  );
}

