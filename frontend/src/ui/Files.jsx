// Chips for files that came with a message or email (names the file even when a caption exists; the
// title carries extracted-text state, "searchable" vs "not read yet"). An image opens here; else a
// link (routes/attachments.py decides). Bytes load on click: for email, a Gmail round-trip per chip.
// Shared by the message log, email review card and reader; each pins files to the console (pin.js:pinFiles).

import { useState } from "react";
import { Lightbox } from "./DocPage.jsx";
import Icon from "./icons.jsx";

const KB = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)}MB`
  : n ? `${Math.round(n / 1024)}KB` : "");

// What every browser here decodes. HEIC isn't in it, and goes down the link.
const isImage = (a) => /^image\/(png|jpeg|gif|webp)$/i.test(a.mime || "")
  || /\.(png|jpe?g|gif|webp)$/i.test(a.filename || "");

function File({ a }) {
  const [open, setOpen] = useState(false);
  const [broken, setBroken] = useState(false);
  const url = `/api/attachments/${a.id}/file`;
  const image = isImage(a);
  return (
    <>
      <a href={url} target="_blank" rel="noopener noreferrer"
        className={`msg-file${a.status === "ok" ? " msg-file-read" : ""}`}
        title={[a.filename, a.mime, KB(a.size_bytes),
          a.status === "ok" ? `${a.chars} chars extracted` : a.status]
          .filter(Boolean).join(" · ")}
        onClick={(e) => {
          e.stopPropagation();           // a chip can sit inside a clickable row
          if (image) { e.preventDefault(); setBroken(false); setOpen(true); }
        }}>
        <Icon name="paperclip" size={10} /> {a.filename || a.mime}
      </a>
      {open && (
        <Lightbox onClose={() => setOpen(false)}>
          {broken
            ? <p className="file-broken">Couldn’t open {a.filename || "this file"}. It
                may no longer be in Gmail, or not be on this Mac.</p>
            : <img className="file-view" src={url} alt={a.filename || ""}
                onError={() => setBroken(true)} />}
        </Lightbox>
      )}
    </>
  );
}

export default function Files({ items }) {
  if (!items?.length) return null;
  return (
    <span className="msg-files">
      {items.map((a) => <File key={a.id} a={a} />)}
    </span>
  );
}
