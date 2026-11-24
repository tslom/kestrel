import { useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { fetchJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { dragPin } from "../lib/drag.js";

// The vault's way in (Vault node, ⌘K): search, pinned notes (Obsidian bookmarks), then
// 06-Generated/. All reads index.db (derived).

const edited = (iso) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });

function NoteRow({ n, onOpen }) {
  return (
    <button className="note-row" onClick={() => onOpen(n.path)} title={n.path}
      {...dragPin(`note · ${n.title}`, { path: n.path, title: n.title })}>
      <span className="note-title">{n.title}</span>
      <span className="note-folder">{n.folder || "vault"}</span>
      {n.edited && <span className="note-edited">{edited(n.edited)}</span>}
      {n.snippet && <span className="note-snippet">{n.snippet}</span>}
    </button>
  );
}

export default function VaultNotes({ onOpenNote }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const { data: pinned } = useWidgetData("/api/vault/pinned");
  const { data: written } = useWidgetData("/api/vault/recent?folder=06-Generated&limit=20", "notes");

  // Searches from the second letter, a beat after typing stops.
  const term = q.trim();
  useDebounce((stale) => {
    if (term.length < 2) { setHits(null); return; }
    fetchJSON(`/api/vault/search?q=${encodeURIComponent(term)}&limit=30`)
      .then((r) => { if (!stale()) setHits(r.notes); })
      .catch(() => { if (!stale()) setHits([]); });
  }, term.length < 2 ? 0 : 150, [q]);

  return (
    <div className="vault-notes">
      <input className="note-search" autoFocus value={q} placeholder="Search your notes — titles, paths and text"
        onChange={(e) => setQ(e.target.value)} aria-label="Search notes" />
      {hits ? (
        <section>
          <h3 className="note-group">{hits.length ? `${hits.length} note${hits.length === 1 ? "" : "s"}` : `Nothing matches “${q.trim()}”`}</h3>
          {hits.map((n) => <NoteRow key={n.path} n={n} onOpen={onOpenNote} />)}
        </section>
      ) : (
        <>
          <section>
            <h3 className="note-group">Pinned</h3>
            {pinned?.error && <p className="widget-error">{pinned.error}</p>}
            {pinned && !pinned.pinned.length && !pinned.error && (
              <p className="widget-empty">Nothing pinned — bookmark a note in Obsidian and it shows here.</p>
            )}
            {pinned?.pinned.map((n) => <NoteRow key={n.path} n={n} onOpen={onOpenNote} />)}
          </section>
          <section>
            <h3 className="note-group">Written by the console</h3>
            {written && !written.notes.length && <p className="widget-empty">Nothing in 06-Generated yet.</p>}
            {written?.notes.map((n) => <NoteRow key={n.path} n={n} onOpen={onOpenNote} />)}
          </section>
        </>
      )}
    </div>
  );
}
