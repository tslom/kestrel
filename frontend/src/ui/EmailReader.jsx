import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Icon from "./icons.jsx";
import { EmailText } from "./EmailBody.jsx";
import Files from "./Files.jsx";
import { fetchJSON } from "../lib/api";
import { GMAIL_URL } from "../lib/googleLinks";
import { emailPin } from "../lib/emailPin.js";
import { pinConsole } from "../lib/turn.js";
import { isDark } from "../lib/theme";
import { isPlain, darken } from "./emailDark.js";
import { fmtDay, fmtTime } from "../lib/time";

// One message read whole over everything else. Formatted (HTML in an iframe) by default; Text is one click away.
// The iframe is the containment, not the backend's scrub: sandbox without allow-scripts
// (allow-same-origin only so Escape inside is heard), a CSP allowing no remote loads until "Load
// images" (no tracking pixel on open), <base target=_blank> so links leave for the system browser.
// Bodies are never stored: each open is a Gmail round-trip; the cache is per page load.
const cache = new Map();

function load(id) {
  if (!cache.has(id)) {
    const p = fetchJSON(`/api/email/${encodeURIComponent(id)}/body?html=true`);
    cache.set(id, p);
    p.catch(() => cache.delete(id));   // let a failed read be retried
  }
  return cache.get(id);
}

// The reader's own ground, so dark mail sits on the panel it's in.
const groundColor = () =>
  getComputedStyle(document.documentElement).getPropertyValue("--paper-raise").trim() || "#2B2825";

function srcdoc(html, images, ground) {
  const img = images ? "data: cid: https: http:" : "data: cid:";
  const csp = `default-src 'none'; style-src 'unsafe-inline'; img-src ${img}; font-src data:`;
  return `<meta http-equiv="Content-Security-Policy" content="${csp}">`
    + `<base target="_blank">`
    + `<style>html{color-scheme:light;background:${ground || "#fff"}}`
    + `body{margin:16px;font:14px/1.5 -apple-system,system-ui,sans-serif;color:#111;`
    + `overflow-wrap:anywhere}img{max-width:100%;height:auto}</style>`
    + html;
}

// `row`: the list row it was opened from (triage summary, action) so the pin matches the row's own; none off a citation.
export default function EmailReader({ id, row, onClose }) {
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState("html");
  const [images, setImages] = useState(false);
  const [pinned, setPinned] = useState(false);
  const frame = useRef(null);

  const pin = () => {
    const { label, data } = emailPin({ ...msg, ...row, id });
    pinConsole(label, data);
    setPinned(true);
  };

  useEffect(() => {
    let live = true;
    load(id)
      .then((m) => { if (live) setMsg(m); })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [id]);

  // Capture phase, ahead of every other Escape listener: the reader is on top. preventDefault keeps
  // AppKit from taking the window out of fullscreen.
  const onKey = useCallback((e) => {
    if (e.key !== "Escape") return;
    // An attachment open over the reader is what Escape closes first.
    if (document.querySelector(".doc-lightbox")) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    onClose();
  }, [onClose]);

  useEffect(() => {
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onKey]);

  // The prompt stays usable: styles.css lifts it over the backdrop and the reader ends above it (measured; the prompt grows).
  const backdrop = useRef(null);
  useLayoutEffect(() => {
    const bar = document.querySelector(".prompt-bar");
    document.body.classList.add("email-reading");
    const fit = () => {
      const top = bar?.getClientRects().length ? bar.getBoundingClientRect().top : null;
      backdrop.current?.style.setProperty("--reader-clear",
        top == null ? "" : `${Math.max(0, window.innerHeight - top) + 12}px`);
    };
    fit();
    const ro = bar && new ResizeObserver(fit);
    ro?.observe(bar);
    window.addEventListener("resize", fit);
    return () => {
      document.body.classList.remove("email-reading");
      ro?.disconnect();
      window.removeEventListener("resize", fit);
    };
  }, []);

  // Dark theme: mail with no colours of its own reads dark (emailDark.js). The frame stays hidden until
  // the first load is judged (no white flash); the verdict is frozen per [html, images] so it doesn't reload the frame.
  const [dark] = useState(isDark);
  const [tone, setTone] = useState(null);         // null until judged
  const toneRef = useRef(null);

  // Focus inside the frame sends keys to the frame's document, not ours.
  const onFrameLoad = () => {
    const doc = frame.current?.contentDocument;
    if (!doc) return;
    doc.addEventListener("keydown", onKey);
    let t = "light";
    if (dark && (toneRef.current === "dark" || isPlain(doc))) {
      darken(doc, groundColor());
      t = "dark";
    }
    toneRef.current = t;
    setTone(t);
  };

  const html = msg?.html;
  const showHtml = html && view === "html";
  const page = useMemo(() => html && srcdoc(html, images,
    toneRef.current === "dark" ? groundColor() : null), [html, images]);

  return createPortal(
    <div className="email-reader-backdrop" ref={backdrop}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="email-reader" role="dialog"
        aria-label={msg?.subject || "Email"}>
        <header className="email-reader-head">
          <div className="email-reader-title">
            <p className="email-reader-subject">{msg?.subject || "…"}</p>
            {msg && (
              <p className="email-reader-meta">
                {msg.from}{msg.from_addr && msg.from_addr !== msg.from
                  ? <span className="dim"> &lt;{msg.from_addr}&gt;</span> : null}
                {" · "}{fmtDay(msg.day)} · {fmtTime(msg.received)}
              </p>
            )}
            <Files items={msg?.attachments} />
          </div>
          <div className="email-reader-tools">
            {html && (
              <div className="seg">
                <button className={view === "html" ? "on" : undefined}
                  onClick={() => setView("html")}>Formatted</button>
                <button className={view === "text" ? "on" : undefined}
                  onClick={() => setView("text")}>Text</button>
              </div>
            )}
            {showHtml && !images && (
              <button className="email-reader-btn" onClick={() => setImages(true)}
                title="Remote images can tell the sender you opened this">
                Load images
              </button>
            )}
            <button className={`email-reader-btn${pinned ? " on" : ""}`}
              onClick={pin} disabled={!msg && !row}
              title="Attach this email to your next console message">
              <Icon name="pin" size={11} /> {pinned ? "In chat" : "Add to chat"}
            </button>
            <a className="email-reader-btn" href={GMAIL_URL(id)} target="_blank"
              rel="noopener noreferrer">Gmail <Icon name="external" size={11} /></a>
            <button className="email-reader-btn" onClick={onClose} title="Close (Esc)">
              <Icon name="close" size={12} />
            </button>
          </div>
        </header>
        <div className="email-reader-body">
          {error && <p className="email-body error">{error}</p>}
          {!error && !msg && <p className="email-body dim">Reading…</p>}
          {showHtml && (
            <iframe ref={frame} title="Email" onLoad={onFrameLoad}
              className={tone === "dark" ? "dark" : undefined}
              style={tone ? undefined : { visibility: "hidden" }}
              sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
              srcDoc={page} />
          )}
          {msg && !showHtml && <EmailText msg={msg} id={id} />}
        </div>
      </div>
    </div>,
    document.body);
}
