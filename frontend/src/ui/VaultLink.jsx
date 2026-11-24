// A note reference that opens in Obsidian: an anchor (hover URI, copyable) whose click is handled
// here (reasons in lib/obsidian.js). A failed open marks the link, text untouched, title saying why.
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
