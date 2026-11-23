// A note reference that opens in Obsidian when clicked.
//
// Rendered as an anchor rather than a button so it reads as a link, gets the
// URI on hover, and can be copied — but the click is handled here, for the
// reasons in lib/obsidian.js. A failed open marks the link instead of throwing
// it away: the text stays exactly as the console wrote it, and the title says
// what went wrong, so "that note doesn't exist" never looks like "the link is
// broken".
import { useState } from "react";
import { openNote, obsidianHref } from "../lib/obsidian";
import { fetchJSON } from "../lib/api";
import Peek from "./Peek.jsx";

const notes = new Map();   // target -> Promise<note | null>; per page load

async function peek(target) {
  if (!notes.has(target))
    notes.set(target, fetchJSON(`/api/vault/note?path=${encodeURIComponent(target)}`)
      .catch(() => null));
  const n = await notes.get(target);
  if (!n) return <p className="peek-note">Open {target} in Obsidian</p>;
  return (
    <>
      <p className="peek-head"><span className="peek-kind">Note</span>
        {n.folder && <span>{n.folder}</span>}</p>
      <p className="peek-title">{n.title}</p>
      {n.excerpt && <p className="peek-excerpt">{n.excerpt}</p>}
    </>
  );
}

export default function VaultLink({ target, children }) {
  const [error, setError] = useState(null);

  function click(e) {
    e.preventDefault();
    setError(null);
    openNote(target).catch((err) => setError(String(err.message || err)));
  }

  return (
    <Peek off={!!error} load={() => peek(target)}>
      <a
        className={`vault-link${error ? " vault-link-missing" : ""}`}
        href={obsidianHref(target)}
        onClick={click}
        title={error || undefined}
      >
        {children}
      </a>
    </Peek>
  );
}
