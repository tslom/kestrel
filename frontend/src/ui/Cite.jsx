// A citation the console wrote into a sentence: one glyph collapsed, a card (email body on demand) opened.
// States that must not look alike: loading; resolved (`found: false` = real ref, row left the cache);
// dead (404, or not ref-shaped: struck through like a missing VaultLink).
// Design: DESIGN.md → Citations — what a sentence was read off.
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import Icon from "./icons.jsx";
import EmailReader from "./EmailReader.jsx";
import DocPage from "./DocPage.jsx";
import Peek from "./Peek.jsx";
import { resolveCite, kindOf } from "../lib/cite";
import { DocPaneContext } from "../lib/docPane";
import { GMAIL_URL, CALENDAR_URL } from "../lib/googleLinks";
import { fmtDay, fmtTime, timeRange } from "../lib/time";

const GLYPH = { email: "mail", event: "calendar", message: "message", doc: "file" };

// A document citation's card is the page itself.
function DocCard({ src, page, quote, onClose, onVerdict }) {
  return (
    <div className="cite-card cite-card-doc">
      <div className="cite-card-head">
        <span className="cite-card-kind">document</span>
        <span>{src.detail}</span>
        <button className="cite-close" onClick={onClose} title="Close">
          <Icon name="close" size={12} />
        </button>
      </div>
      <p className="cite-title">{src.title}</p>
      {quote && <p className="cite-quote">“{quote}”</p>}
      <DocPage docRef={src.ref} page={page || 1} quote={quote} onVerdict={onVerdict} />
    </div>
  );
}

function When({ src }) {
  if (!src.at) return src.day ? <span>{fmtDay(src.day)}</span> : null;
  if (src.kind === "event") {
    if (src.all_day) return <span>{fmtDay(src.day)} · all-day</span>;
    return <span>{fmtDay(src.day)} · {timeRange(src.at, src.end).inline}</span>;
  }
  return <span>{fmtDay(src.day)} · {fmtTime(src.at)}</span>;
}

function Card({ src, onClose }) {
  const [body, setBody] = useState(false);

  // The ref resolved but its row is gone: say so rather than render an empty card.
  if (!src.found)
    return (
      <div className="cite-card">
        <div className="cite-card-head">
          <span className="cite-card-kind">{src.kind}</span>
          <button className="cite-close" onClick={onClose} title="Close">
            <Icon name="close" size={12} />
          </button>
        </div>
        <p className="cite-title">{src.label}</p>
        <p className="cite-gone">
          No longer in the {src.kind} cache — the citation is real, the row
          isn’t there to open.
        </p>
      </div>
    );

  return (
    <div className="cite-card">
      <div className="cite-card-head">
        <span className="cite-card-kind">{src.kind}</span>
        <When src={src} />
        <button className="cite-close" onClick={onClose} title="Close">
          <Icon name="close" size={12} />
        </button>
      </div>

      <p className="cite-title">{src.title}</p>
      {src.who && <p className="cite-who">{src.who}</p>}
      {src.detail && <p className="cite-detail">{src.detail}</p>}

      {src.kind === "email" && (
        <div className="cite-actions">
          <button className="cite-act" onClick={() => setBody(true)}>
            Read it here
          </button>
          <a className="cite-act" href={GMAIL_URL(src.id)}
             target="_blank" rel="noopener noreferrer">
            Gmail <Icon name="external" size={11} />
          </a>
        </div>
      )}
      {src.kind === "email" && body &&
        <EmailReader id={src.id} onClose={() => setBody(false)} />}

      {src.kind === "event" && (
        <div className="cite-actions">
          <a className="cite-act" href={CALENDAR_URL()}
             target="_blank" rel="noopener noreferrer">
            Calendar <Icon name="external" size={11} />
          </a>
        </div>
      )}
    </div>
  );
}

// The pill's hover: what the source is, short of opening it.
async function peek(refId, kind, page, quote) {
  const src = kind ? await resolveCite(refId) : null;
  if (!src)
    return <p className="peek-note">Nothing was ever cited as <code>{refId}</code>. Treat
      the claim as uncited.</p>;
  if (!src.found)
    return <p className="peek-note">{src.label}, no longer in the {src.kind} cache.</p>;
  return (
    <>
      <p className="peek-head"><span className="peek-kind">{src.kind}</span>
        {page ? <span>page {page}</span> : <When src={src} />}</p>
      <p className="peek-title">{src.title}</p>
      {src.who && <p className="peek-note">{src.who}</p>}
      {quote && <p className="peek-note">“{quote}”</p>}
    </>
  );
}

// `page` and `quote` only for a document citation (`[cite:d3f9a1#p12 "…"]`).
export default function Cite({ refId, page, quote }) {
  const kind = kindOf(refId);
  // In a console a document citation opens the page beside the chat (console/DocPane.jsx), not a card.
  const pane = useContext(DocPaneContext);
  const [src, setSrc] = useState(undefined);   // undefined = not asked yet
  const [open, setOpen] = useState(false);
  const wrap = useRef(null);

  // Reset when this instance's marker becomes a different citation (page and quote count too).
  // Not defensive: markdown.jsx keys by character offset, so React can reuse the instance and show
  // the previous ref's email, resolved and wrong. Second lock beside the ref in the key.
  const ident = `${refId}#${page ?? ""}#${quote ?? ""}`;
  const [shown, setShown] = useState(ident);
  // A quote not in the document strikes the pill like an unreal ref.
  const [verdict, setVerdict] = useState(null);
  if (shown !== ident) {
    setShown(ident);
    setSrc(undefined);
    setOpen(false);
    setVerdict(null);
  }

  // Resolved on open, not render: a long answer carries a dozen and few get clicked (lib/cite.js).
  useEffect(() => {
    if (!open || kind === null || src !== undefined) return;
    let live = true;
    resolveCite(refId).then((s) => { if (live) setSrc(s); });
    return () => { live = false; };
  }, [open, src, refId, kind]);

  // Keep the opened card on screen (markers sit near sentence ends, so left-aligned overflows right).
  // Measure from the MARKER's box and the card's untransformed `offsetWidth`, never its own rect:
  // reading the rect of the element this effect transforms diverges into "maximum update depth exceeded".
  const card = useRef(null);
  const [shift, setShift] = useState(0);
  useLayoutEffect(() => {
    if (!open) { setShift(0); return; }
    const el = card.current;
    const anchor = wrap.current;
    if (!el || !anchor) return;
    const margin = 10;
    const POP_LEFT = -6;                  // .cite-pop's own `left`, in px
    const left = anchor.getBoundingClientRect().left + POP_LEFT;
    const right = left + el.offsetWidth;

    let next = 0;
    // Pull left by the overflow, never past the left margin.
    const overRight = right - (window.innerWidth - margin);
    if (overRight > 0) next = -Math.min(overRight, left - margin);
    // The left edge wins when the card is wider than the window (keeps its first words readable).
    if (left + next < margin) next = margin - left;
    setShift(next);
  }, [open, src]);

  // Click-away and Escape, but not a click inside the card (selecting text must not dismiss it).
  useEffect(() => {
    if (!open) return;
    // The email reader is portaled to <body> but is this card's child: a click in it isn't a click away.
    const away = (e) => {
      if (wrap.current?.contains(e.target)) return;
      if (e.target.closest?.(".email-reader-backdrop, .doc-lightbox")) return;
      setOpen(false);
    };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  // Not ref-shaped: dead on sight, no network call.
  const dead = kind === null || src === null;
  const unsupported = verdict === "missing";
  const isDoc = kind === "doc";

  return (
    <span className="cite-wrap" ref={wrap}>
      <Peek off={open} load={() => (unsupported
        ? <p className="peek-note">The quoted words aren’t in the document.</p>
        : peek(refId, kind, page, quote))}>
        <button
          className={`cite${dead || unsupported ? " cite-dead" : ""}${open ? " cite-open" : ""}`}
          onClick={() => (isDoc && pane
            ? pane.open({ ref: refId, page, quote, onVerdict: setVerdict })
            : setOpen((v) => !v))}
          aria-expanded={open}
          aria-label={dead ? `No such citation (${refId})` : `Source: ${kind}`}
        >
          <Icon name={kind ? GLYPH[kind] : "close"} size={11} />
          {/* "p12" is where to look, so it shows collapsed. */}
          {isDoc && page && <span className="cite-page">p{page}</span>}
        </button>
      </Peek>
      {open && (
        <span className="cite-pop" ref={card}
              style={shift ? { transform: `translateX(${shift}px)` } : undefined}>
          {kind !== null && src === undefined
            && <div className="cite-card cite-loading">Looking it up…</div>}
          {dead && (
            <div className="cite-card">
              <p className="cite-gone">
                <code>{refId}</code> was never handed out — this citation
                points at nothing. Treat the claim as uncited.
              </p>
            </div>
          )}
          {src && (isDoc && src.found
            ? <DocCard src={src} page={page} quote={quote}
                       onClose={() => setOpen(false)} onVerdict={setVerdict} />
            : <Card src={src} onClose={() => setOpen(false)} />)}
        </span>
      )}
    </span>
  );
}
