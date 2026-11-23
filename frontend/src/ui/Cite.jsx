// A citation the console wrote into a sentence, rendered where the claim is.
//
// Collapsed it is one glyph — the kind of thing the claim came off, and
// nothing else. That restraint is the design: a pill carrying a subject line
// would be a second voice inside the paragraph, and the paragraph already
// said the thing. The glyph is a promise that there is a source, not a
// preview of it.
//
// Opened it is a card under the line, holding what the source actually was,
// and for email the body on demand. The body is the case the whole feature
// was built for — "it's at 1149 S Hill St" is exactly the claim you want to
// check and exactly the string that only exists in the mail — so reading it
// must not mean leaving for Gmail. The Gmail link stays, one click further
// out, for everything the text/plain part loses.
//
// Three states, and they must not look alike:
//   loading   — asked, waiting
//   resolved  — the row, or `found: false` when the ref was real and the row
//               has since left the provider cache
//   dead      — 404: never handed out, which in practice means the console
//               made the ref up. Struck through, like a missing VaultLink,
//               for the same reason: the sentence stays as written and the
//               marker says not to trust it.
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

// A document citation's card is the page itself: the claim was read off a
// page, and the page is what there is to check it against.
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

  // The ref resolved, but the row behind it is gone. Say which, rather than
  // rendering an empty card that reads like a bug.
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

// The pill's hover: what the source is, in the card's own terms, short of
// opening it.
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
  // In a console, a document citation opens the page beside the chat
  // (console/DocPane.jsx) instead of a card under the line: reading along
  // wants the page to stay put while the reply scrolls.
  const pane = useContext(DocPaneContext);
  const [src, setSrc] = useState(undefined);   // undefined = not asked yet
  const [open, setOpen] = useState(false);
  const wrap = useRef(null);

  // Reset when the marker under this instance becomes a different citation.
  //
  // Not defensive: markdown.jsx keys inline nodes by character offset, and
  // two different answers put a `[cite:` at the same offset often enough that
  // React reuses this instance rather than remounting it. Without this, the
  // card would open showing the *previous* ref's email — resolved, confident,
  // and wrong, which is the one outcome the whole feature is built to avoid.
  // (markdown.jsx also folds the ref into the key, so this is the second of
  // two locks on the same door; the other one can be undone by a caller, this
  // one can't.) Adjusting state during render is the documented pattern for
  // this and costs one extra render pass, only on the tick the prop changes.
  // The page and quote are part of which citation this is: two cites into
  // the same deck at the same offset are different claims.
  const ident = `${refId}#${page ?? ""}#${quote ?? ""}`;
  const [shown, setShown] = useState(ident);
  // What the document check found, once a doc card has been opened — a quote
  // that isn't in the document strikes the pill like a ref that isn't real.
  const [verdict, setVerdict] = useState(null);
  if (shown !== ident) {
    setShown(ident);
    setSrc(undefined);
    setOpen(false);
    setVerdict(null);
  }

  // Resolved on open, not on render: a long answer can carry a dozen of these
  // and almost none get clicked. See lib/cite.js.
  useEffect(() => {
    if (!open || kind === null || src !== undefined) return;
    let live = true;
    resolveCite(refId).then((s) => { if (live) setSrc(s); });
    return () => { live = false; };
  }, [open, src, refId, kind]);

  // Keep the opened card on screen.
  //
  // The card hangs off a marker that can sit anywhere in a line, and most
  // markers land near the end of a sentence — so left-aligning it to the
  // marker puts it off the right edge most of the time. The first version was
  // a CSS `:nth-last-child` flip, which is wrong for the common case: that
  // selector counts element children, so a mid-paragraph marker with no
  // elements after it reads as "last" and flips anyway.
  //
  // Measured from the MARKER's box and the card's untransformed `offsetWidth`
  // — never from the card's own rect. That is the whole trick, and it was a
  // real hang before it was a comment: reading `getBoundingClientRect` on the
  // element this effect transforms makes the effect its own input, which
  // converges only because a browser happens to report the transformed rect.
  // Under anything that doesn't, it diverges, and React kills the tree with
  // "maximum update depth exceeded". Computed from layout that this effect
  // does not touch, the answer is the same on every pass by construction.
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
    // Pull left by the overflow, but never past the left margin doing it.
    const overRight = right - (window.innerWidth - margin);
    if (overRight > 0) next = -Math.min(overRight, left - margin);
    // Then the left edge wins outright. This is the order that decides the one
    // case where both rules can't hold — a card wider than the window — and
    // pinning it left is what keeps its first words readable.
    if (left + next < margin) next = margin - left;
    setShift(next);
  }, [open, src]);

  // Click-away and Escape. The card sits inside a paragraph of running text,
  // so leaving it open while you read on is the wrong default — but it must
  // not close on a click inside itself, or selecting the address you came to
  // check would dismiss the thing showing it.
  useEffect(() => {
    if (!open) return;
    // The email reader is portaled to <body>, outside `wrap`, but it is this
    // card's child — a click in it (or on its backdrop) isn't a click away.
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

  // Not ref-shaped at all — the model wrote something else between the
  // brackets. Dead on sight, with no network call: there is nothing on the
  // backend that could resolve it, and the reader still needs to see that a
  // citation was attempted and is worth nothing.
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
          {/* The page is worth showing collapsed: "p12" is where to look, not a
              second voice in the sentence. */}
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
