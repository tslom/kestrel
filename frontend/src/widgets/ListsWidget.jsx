import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { CheckList, CheckRow, AddInput } from "../ui/CheckList.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// The lists card. It opens on the buy list — that's the one seeded, and the
// first chip — but a list is a row the user makes, so
// packing for college or a party each get their own instead of being filed as
// categories on a list that isn't theirs.
//
// One list is shown at a time. All of them arrive in the one /api/lists read
// (they're a few dozen rows between them), so switching chips is instant and
// costs no fetch, and the card's height doesn't grow with the number of lists.
export function useListsCard() {
  const { data, error, reload } = useWidgetData("/api/lists", "lists");

  function tick(id) {
    postJSON(`/api/list-items/${id}/done`, { done: true }).then(reload);
  }

  // held until the undo toast goes (lib/undo.js holdDelete)
  function drop(it) {
    holdDelete({ label: `Removed “${it.name}”`, key: `item:${it.id}`,
                 commit: () => fetchJSON(`/api/list-items/${it.id}`, { method: "DELETE" }).then(reload) });
  }

  const lists = data?.lists ?? [];
  const open = lists.reduce((n, l) => n + l.open, 0);
  const busy = lists.filter((l) => l.open > 0);

  // HAS on any open item. "Needs you" is only a blocked turn, a clock, a
  // broken source, or a person or decision waiting, and a shopping list isn't
  // due. Folded away as HAS on the old rail desk it drew zero opens; its
  // "today" line is always visible now, and if the usage sheet shows it back
  // at zero opens, that's the evidence to revisit.
  const level = !data ? null : open === 0 ? QUIET : HAS;
  const summary = !data ? ""
    : open === 0 ? (lists.length ? "nothing on the lists" : "no lists yet")
    : busy.length === 1 ? `${busy[0].open} on ${busy[0].name}`
    : `${open} open · ${busy.map((l) => `${l.name} ${l.open}`).join(", ")}`;

  return { data, error, reload, tick, drop, lists, open, level, summary };
}

export default function ListsWidget({ card, onPin }) {
  const { data, error, reload, tick, drop, lists, open } = card;
  // Which chip is selected — id, or null for "whichever is first". Held by id
  // rather than index so a list deleted elsewhere doesn't silently select a
  // different one, and per-mount rather than persisted: the first chip is the
  // buy list, which is the right place to land on a fresh page.
  const [pick, setPick] = useState(null);
  // "new" while naming a list, "rename" while renaming the open one.
  const [mode, setMode] = useState(null);
  // A rejected name (duplicates are refused server-side) — said next to the
  // box that caused it rather than replacing the card the way a failed load
  // does.
  const [notice, setNotice] = useState(null);

  // a list or row whose delete is waiting on the undo toast is already gone here
  const held = useHeld();
  const shownLists = lists.filter((l) => !held.has(`list:${l.id}`));
  const active = shownLists.find((l) => l.id === pick) ?? shownLists[0] ?? null;
  const items = (active?.items ?? []).filter((it) => !held.has(`item:${it.id}`));

  // "Groceries: milk" files it under a category; a bare "milk" doesn't.
  function add(text) {
    if (!active) return;
    const m = text.match(/^([^:]{1,24}):\s*(.+)$/);
    postJSON(`/api/lists/${active.id}/items`,
      m ? { name: m[2], category: m[1].trim() } : { name: text })
      .then(reload)
      .catch((e) => setNotice(String(e.message || e)));
  }

  // A new list opens selected: you named it in order to put something on it.
  function makeList(name) {
    setNotice(null);
    postJSON("/api/lists", { name })
      .then((row) => { setPick(row.id); setMode(null); return reload(); })
      .catch((e) => setNotice(String(e.message || e)));
  }

  function renameList(name) {
    setNotice(null);
    postJSON(`/api/lists/${active.id}`, { name }, "PATCH")
      .then(() => { setMode(null); return reload(); })
      .catch((e) => setNotice(String(e.message || e)));
  }

  // Deleting takes the items with it (FK cascade), so the count is in the
  // toast — "deleted Party" reads harmless when it's eleven things. Held
  // until the toast goes, like every delete (lib/undo.js holdDelete), in
  // place of the browser confirm it had.
  function removeList() {
    const { id, name, open: n } = active;
    setPick(null); setMode(null);
    holdDelete({
      label: `Deleted the ${name} list${n ? ` and its ${n} item${n > 1 ? "s" : ""}` : ""}`,
      key: `list:${id}`,
      commit: () => fetchJSON(`/api/lists/${id}`, { method: "DELETE" }).then(reload)
        .catch((e) => setNotice(String(e.message || e))),
    });
  }

  return (
    <section className="widget" {...dragPin("Lists", data)}>
      <header>
        <h2>Lists {open > 0 &&<span className="widget-sub">{open}</span>}</h2>
        <div className="widget-actions">
          {/* Deleting can be undone (removeList), so it can sit in plain view
              with the rest of the card's controls. */}
          {active && (
            <>
              <button onClick={() => { setNotice(null); setMode(mode === "rename" ? null : "rename"); }}
                title={`Rename the ${active.name} list`}><Icon name="edit" /></button>
              <button onClick={removeList}
                title={`Delete the ${active.name} list`}><Icon name="close" /></button>
            </>
          )}
          <button className="pin" disabled={!data}
            onClick={() => onPin("Lists", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>

      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}

      {data && (
        <div className="chip-row list-chips">
          {shownLists.map((l) => (
            <button key={l.id}
              className={`chip${l.id === active?.id ? " active" : ""}`}
              onClick={() => { setPick(l.id); setMode(null); setNotice(null); }}
              title={`${l.open} open`}>
              {l.name}{l.open > 0 && <span className="chip-n">{l.open}</span>}
            </button>
          ))}
          <button className="chip list-new"
            onClick={() => { setNotice(null); setMode(mode === "new" ? null : "new"); }}
            title="Start a new list">+ list</button>
        </div>
      )}

      {mode === "new" && (
        <AddInput placeholder="Name the list — “College packing”…"
          onAdd={makeList} onCancel={() => setMode(null)} />
      )}
      {mode === "rename" && active && (
        <AddInput placeholder="Rename this list…" initial={active.name}
          onAdd={renameList} onCancel={() => setMode(null)} />
      )}
      {notice && <p className="widget-note">{notice}</p>}

      {data && !active && (
        <p className="widget-empty">No lists yet — “+ list” starts one.</p>
      )}
      {active && items.length === 0 && (
        <p className="widget-empty">
          Nothing on {active.name} — add one as “Groceries: milk”.
        </p>
      )}
      {active && items.length > 0 && (
        <CheckList drag={dragPin(`${active.name} · open`, items)}>
          {items.map((it) => (
            <CheckRow key={it.id} done={false}
              toggleTitle="Tick it off"
              drag={dragPin(`${active.name} · ${it.name}`, it)}
              onToggle={() => tick(it.id)} onDelete={() => drop(it)}>
              <span className="check-name">{it.name}</span>
              {it.category && <span className="check-tag">{it.category}</span>}
              {it.note && <span className="check-meta">{it.note}</span>}
            </CheckRow>
          ))}
        </CheckList>
      )}

      {/* Keyed on the list: half a typed item is meant for the list you were
          looking at, and shouldn't follow you to the next chip. */}
      {active && (
        <AddInput key={active.id} placeholder={`Add to ${active.name} — “milk”…`}
          onAdd={add} />
      )}
    </section>
  );
}
