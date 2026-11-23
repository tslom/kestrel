import { postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import VaultLink from "../ui/VaultLink.jsx";
import { dragPin } from "../lib/drag.js";

export function useGeneratedCard() {
  const { data: notes, error, reload, busy: refreshing } = useWidgetData("/api/generated", "generated");

  function promote(path, curated) {
    postJSON("/api/generated/promote", { path, curated }).then(reload);
  }

  // This card used to vanish outright when the inbox was clear. Now it does
  // what every other quiet card does — a labelled line, so "nothing to
  // review" and "the widget isn't rendering" stop looking identical.
  // Notes to file are "today", not "needs you" (2026-09-25, the plan view):
  // nothing is waiting on them, and they keep until you get to them.
  const level = !notes ? null : notes.length > 0 ? HAS : QUIET;
  const summary = !notes?.length ? "inbox clear"
    : `${notes.length} note${notes.length > 1 ? "s" : ""} to file`;

  return { notes, error, reload, refreshing, promote, level, summary };
}

export default function GeneratedWidget({ card, onPin }) {
  const { notes, error, reload, refreshing, promote } = card;

  return (
    <section className="widget" {...dragPin("Unreviewed generated notes", notes)}>
      <header>
        <h2>Generated inbox</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!notes}
            onClick={() => onPin("Unreviewed generated notes", notes)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!notes && !error && <div className="skel w80" />}
      {notes && notes.length === 0 && (
        <p className="widget-empty">Nothing to file — notes the console writes to 06-Generated land here.</p>
      )}
      {notes && notes.length > 0 && (
        <ul className="gen-list" {...dragPin("generated · the list", notes)}>
          {notes.map((n) => (
            <li key={n.path} {...dragPin(`note · ${n.title}`, n)}>
              {/* the whole point of the inbox is reading the note before you
                  file it, and until now that meant finding it in Obsidian by
                  hand — the title is the shortest path to the thing itself */}
              <span className="gen-title">
                <VaultLink target={n.path}>{n.title}</VaultLink>
              </span>
              <span className="gen-by">{n.generated_by}</span>
              <span className="gen-actions">
                <button onClick={() => promote(n.path, false)} title="Mark reviewed, keep in 06-Generated">reviewed</button>
                <button onClick={() => promote(n.path, true)} title="Adopt as a curated note">adopt <Icon name="star" size={11} /></button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
