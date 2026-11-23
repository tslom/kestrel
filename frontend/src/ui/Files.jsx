// Files that came with a message or an email. The backend already writes
// "(photo)" into a text's `text` when nothing was typed, so a chip here is not a
// duplicate — it names the file, and it's the only thing that shows up at all
// when someone sent a file *with* a caption. Title carries the state of its
// extracted text, since "searchable" vs "not read yet" is the difference
// between the console being able to answer a question about it or not.
//
// A chip opens its file. An image opens here, over the page; anything else is
// a link, so a PDF gets the browser's viewer and the rest download
// (routes/attachments.py decides which). Bytes are fetched on the click and not
// before: for an email that's a Gmail round-trip per chip.
//
// Shared by the message log, the email review card and the reader: every
// carrier pins its files to the console (pin.js:pinFiles), so each has to show
// what it would send.

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
