import { useState } from "react";
import { useWidgetData } from "../lib/refresh";
import { dragPin } from "../lib/drag.js";
import { openNote } from "../lib/obsidian.js";
import Icon from "../ui/icons.jsx";
import { renderMarkdown } from "../ui/markdown.jsx";

// One note as a full-view tab: what it is, how it begins, and what it links
// to and from — each link a chip that opens that note as its own tab. The
// note itself is edited in Obsidian: "Open in Obsidian" goes through
// openNote() → /api/vault/open, never a plain obsidian:// link (Chrome prompts
// on every click, and the desktop app ignores the scheme).

export default function NoteTab({ path, onOpenNote, onPin }) {
  const { data: note, error } = useWidgetData(`/api/vault/note?path=${encodeURIComponent(path)}`, "notes");
  const [opening, setOpening] = useState(null);

  if (error) return <p className="widget-error">{error}</p>;
  if (!note) return <><div className="skel" /><div className="skel w60" /></>;

  const pin = [`note · ${note.title}`, { path: note.path, title: note.title, excerpt: note.excerpt,
                                         links_to: note.links_to.map((l) => l.path) }];
  // Comments (<!-- --> and %% %%) are hidden as in Obsidian's reading view (tasks.md's banner rendered as text).
  const body = (note.body ?? note.excerpt ?? "")
    .replace(/<!--[\s\S]*?-->|%%[\s\S]*?%%/g, "")
    .replace(/^\s*# [^\n]*\n/, "");
  const open = () => {
    setOpening("…");
    openNote(note.path).then(() => setOpening(null)).catch((e) => setOpening(e.message));
  };
  const chips = (list) => list.map((l) => (
    <button key={l.path} className="note-chip" title={l.path} onClick={() => onOpenNote(l.path)}
      {...dragPin(`note · ${l.title}`, l)}>{l.title}</button>
  ));

  return (
    // Only the header drags: on the whole article, selecting text picked the note up.
    <article className="note-tab">
      <header className="note-tab-head" {...dragPin(...pin)}>
        <div>
          <h2>{note.title}</h2>
          <p className="note-path">{note.path} · edited {new Date(note.edited).toLocaleString([],
            { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
            {note.generated && " · written by the console"}</p>
        </div>
        <div className="note-tab-acts">
          <button className="pbtn" onClick={() => onPin(...pin)} title="Attach this note to your next console message">
            <Icon name="pin" size={12} /> Pin to console</button>
          <button className="pbtn go" onClick={open}>Open in Obsidian</button>
        </div>
      </header>
      {opening && opening !== "…" && <p className="widget-error">{opening}</p>}
      {/* The console's renderer, so tables, callouts, code and [[links]] read
          as they do in Obsidian rather than as their markup. The whole note,
          less the H1 the header already shows — the excerpt is only the pin's
          budgeted copy, and cut mid-callout it rendered as half a box. */}
      {body.trim() ? <div className="note-excerpt">{renderMarkdown(body)}</div>
        : <p className="widget-empty">No text yet.</p>}
      <section className="note-links">
        <h3 className="note-group">Links to</h3>
        {note.links_to.length ? <div className="note-chips">{chips(note.links_to)}</div>
          : <p className="widget-empty">No links out.</p>}
        {note.unresolved.length > 0 && (
          <p className="note-unresolved" title="Links to notes that don't exist, or whose name matches several">
            Unresolved: {note.unresolved.join(", ")}</p>
        )}
        <h3 className="note-group">Linked from</h3>
        {note.linked_from.length ? <div className="note-chips">{chips(note.linked_from)}</div>
          : <p className="widget-empty">Nothing links here.</p>}
      </section>
    </article>
  );
}
