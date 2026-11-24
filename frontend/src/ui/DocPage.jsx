// One page of a document the console pointed into: a citation card (`[cite:d3f9a1#p12 "…"]`) or an
// embed (`[page:d3f9a1#p12]`), with the quote highlighted. lib/pdf.js checks the quote; the verdict
// shows under the page and goes up via `onVerdict`. Outcomes that must not read alike:
//   here (highlighted) / moved (on another page: show it, the cited number was wrong) /
//   notext (scanned: "can't check", never "not found") / missing (claim unsupported)
import { useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Icon from "./icons.jsx";
import { loadDoc, drawPage, locate, findOnPage, fileUrl } from "../lib/pdf";
import { DocPaneContext } from "../lib/docPane";

// The enlarged page, portaled to <body> so no card's overflow or transform clips it.
export function Lightbox({ onClose, children }) {
  useEffect(() => {
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [onClose]);
  return createPortal(
    <div className="doc-lightbox" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="doc-lightbox-body">
        <button className="doc-lightbox-x" onClick={onClose} title="Close (Esc)">
          <Icon name="close" size={14} />
        </button>
        {children}
      </div>
    </div>,
    document.body);
}

// `big` = the enlarged copy (no further zoom). Keyed on what it shows, so a new doc/page/quote starts over.
export default function DocPage(props) {
  return <DocPageBody key={`${props.docRef}\u0000${props.page}\u0000${props.quote ?? ""}`} {...props} />;
}

function DocPageBody({ docRef, page, quote, width = 396, embed = false, big = false, onVerdict }) {
  const [zoom, setZoom] = useState(false);
  // In a console a click opens the pane beside the chat; elsewhere the lightbox.
  const pane = useContext(DocPaneContext);
  const [doc, setDoc] = useState(null);
  const [err, setErr] = useState(null);
  const [shown, setShown] = useState(page);
  const [verdict, setVerdict] = useState(quote ? null : { status: "none" });
  const [rects, setRects] = useState([]);
  // Which draw last finished; the page reads as drawn only while it's still on screen.
  const [drawnFor, setDrawnFor] = useState(null);
  const canvas = useRef(null);

  useEffect(() => {
    let live = true;
    loadDoc(docRef).then(
      (d) => { if (live) setDoc(d); },
      (e) => { if (live) setErr(e?.message || "could not load the document"); });
    return () => { live = false; };
  }, [docRef, page, quote]);

  // Find the quote once; it decides the first page shown.
  useEffect(() => {
    if (!doc || !quote) return;
    let live = true;
    locate(doc, page, quote).then((v) => {
      if (!live) return;
      setVerdict(v);
      setShown(v.page);
      onVerdict?.(v.status);
    }, () => { if (live) setVerdict({ status: "notext", page }); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onVerdict is a callback prop; its identity changes every parent render (above)
  }, [doc, page, quote]);

  // Draw whichever page is shown, and its highlight if the quote is on it.
  const drawKey = `${doc ? "doc" : ""}|${shown}|${width}|${verdict?.status ?? ""}|${verdict?.page ?? ""}`;
  const drawn = drawnFor === drawKey;
  useEffect(() => {
    if (!doc || !canvas.current || (quote && !verdict)) return;
    let live = true;
    let task = null;
    const n = Math.min(Math.max(1, shown), doc.numPages);
    drawPage(doc, n, canvas.current, width).then(async (d) => {
      task = d.task;
      await d.task.promise;
      if (!live) return;
      setDrawnFor(drawKey);
      if (quote && verdict && verdict.page === n && verdict.rects?.length) setRects(verdict.rects);
      else if (quote) {
        // Paging away and back, or onto another page with the same words.
        const got = await findOnPage(d.page, quote);
        if (live) setRects(got.rects || []);
      } else setRects([]);
    }).catch((e) => {
      if (live && e?.name !== "RenderingCancelledException") setErr(e?.message || "could not draw the page");
    });
    return () => { live = false; task?.cancel(); };
  }, [doc, shown, width, verdict, quote, drawKey]);

  if (err) {
    return (
      <div className={`doc-page${embed ? " doc-embed" : ""}`}>
        <p className="doc-note doc-bad">Couldn’t open the document — {err}.</p>
      </div>
    );
  }

  const total = doc?.numPages;

  // No paging (the answer chose the pages; "Open" browses). A click enlarges: half-width slides can't show code.
  return (
    <div className={`doc-page${embed ? " doc-embed" : ""}`} style={{ maxWidth: width }}>
      <div className={`doc-sheet${big ? "" : " doc-zoomable"}`}
           onClick={big ? undefined : () => (pane
             ? pane.open({ ref: docRef, page: shown, quote: verdict?.page === shown ? quote : undefined })
             : setZoom(true))}
           title={big ? undefined : pane ? "Open beside the chat" : "Enlarge"}>
        <canvas ref={canvas} className={drawn ? "" : "doc-pending"} />
        {rects.map((r, i) => (
          <span key={i} className="doc-mark" style={{
            left: `${r.left * 100}%`, top: `${r.top * 100}%`,
            width: `${r.width * 100}%`, height: `${r.height * 100}%`,
          }} />
        ))}
        {!drawn && <span className="doc-loading">{doc ? "Drawing…" : "Opening…"}</span>}
      </div>
      {zoom && (
        <Lightbox onClose={() => setZoom(false)}>
          <DocPage docRef={docRef} page={shown} quote={verdict?.page === shown ? quote : undefined}
                   width={Math.min(1100, Math.round(window.innerWidth * 0.86))} big />
        </Lightbox>
      )}
      <div className="doc-bar">
        <span className="doc-num">{total ? `p.${shown} of ${total}` : `p.${shown}`}</span>
        <a className="doc-open" href={`${fileUrl(docRef)}#page=${shown}`}
           target="_blank" rel="noopener noreferrer" title="Open the whole document">
          Open <Icon name="external" size={11} />
        </a>
      </div>
      {verdict?.status === "moved" && (
        <p className="doc-note">Those words are on p.{verdict.page}, not p.{page} as cited.</p>
      )}
      {verdict?.status === "missing" && (
        <p className="doc-note doc-bad">
          The quoted words aren’t in this document. Treat the claim as unsupported.
        </p>
      )}
      {verdict?.status === "notext" && (
        <p className="doc-note">This page is a scan with no text layer, so the quote can’t be checked.</p>
      )}
    </div>
  );
}
