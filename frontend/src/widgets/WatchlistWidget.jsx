import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { CheckList, CheckRow, AddInput } from "../ui/CheckList.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// The queue is only half the point — `services` is the column that answers
// "what can I actually put on right now", so it rides in every row.
export function useWatchlistCard() {
  const { data, error, reload } = useWidgetData("/api/watchlist", "watchlist");

  function watched(id) {
    postJSON(`/api/watchlist/${id}/watched`, { watched: true }).then(reload);
  }

  // held until the undo toast goes (lib/undo.js holdDelete)
  function drop(it) {
    holdDelete({ label: `Removed “${it.title}”`, key: `watch:${it.id}`,
                 commit: () => fetchJSON(`/api/watchlist/${it.id}`, { method: "DELETE" }).then(reload) });
  }

  const all = data?.items ?? [];
  const movies = all.filter((i) => i.kind === "movie").length;
  const shows = all.length - movies;

  // A watchlist never asks for anything — it's a reference list, so it stays
  // out of the day's ranking and reports what it holds in one line.
  const level = !data ? null : QUIET;
  const summary = !data ? "" : all.length === 0 ? "nothing queued"
    : `${movies} ${movies === 1 ? "movie" : "movies"} · ${shows} ${shows === 1 ? "show" : "shows"}`;

  return { data, error, reload, watched, drop, all, level, summary };
}

export default function WatchlistWidget({ card, onPin }) {
  const { data, error, reload, watched, drop, all } = card;
  const [kind, setKind] = useState("all"); // all | movie | show

  // "Heat (1995) — Netflix": year and services are both optional.
  function add(text) {
    const m = text.match(/^(.+?)(?:\s*\((\d{4})\))?(?:\s*[—-]\s*(.+))?$/);
    postJSON("/api/watchlist", {
      title: (m?.[1] ?? text).trim(),
      year: m?.[2] ? parseInt(m[2], 10) : null,
      services: m?.[3]?.trim() || null,
      kind: kind === "show" ? "show" : "movie",
    }).then(reload);
  }

  const held = useHeld();
  const items = (kind === "all" ? all : all.filter((i) => i.kind === kind)).filter((i) => !held.has(`watch:${i.id}`));

  return (
    <section className="widget" {...dragPin("Watchlist", data && { items })}>
      <header>
        <h2>Watchlist {all.length > 0 &&<span className="widget-sub">{all.length}</span>}</h2>
        <div className="widget-actions">
          {["all", "movie", "show"].map((k) => (
            <button key={k} className={`chip${kind === k ? " active" : ""}`}
              onClick={() => setKind(k)}>
              {k === "all" ? "all" : k === "movie" ? "movies" : "shows"}
            </button>
          ))}
          <button className="pin" disabled={!data}
            onClick={() => onPin("Watchlist", { items })}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && items.length === 0 && (
        <p className="widget-empty">Nothing queued — add one as “Heat (1995) — Netflix”.</p>
      )}
      {data && items.length > 0 && (
        <CheckList drag={dragPin(`watchlist · ${kind === "all" ? "everything" : `${kind}s`}`, items)}>
          {items.map((it) => (
            <CheckRow key={it.id} done={false}
              toggleTitle="Mark watched"
              drag={dragPin(`watch · ${it.title}`, it)}
              onToggle={() => watched(it.id)} onDelete={() => drop(it)}>
              <span className="check-name">{it.title}</span>
              {it.year && <span className="check-tag">{it.year}</span>}
              {it.services && <span className="check-meta">{it.services}</span>}
            </CheckRow>
          ))}
        </CheckList>
      )}
      <AddInput placeholder="Add — “Heat (1995) — Netflix”…" onAdd={add} />
    </section>
  );
}
