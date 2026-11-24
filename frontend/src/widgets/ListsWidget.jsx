import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { CheckList, CheckRow, AddInput } from "../ui/CheckList.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// The lists card: opens on the seeded buy list; user-made lists get their own chip. One list shows at
// a time; all arrive in one /api/lists read, so switching chips costs no fetch.
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

  // HAS on any open item; a shopping list is never "needs you". Revisit if the usage sheet shows zero opens.
  const level = !data ? null : open === 0 ? QUIET : HAS;
  const summary = !data ? ""
    : open === 0 ? (lists.length ? "nothing on the lists" : "no lists yet")
    : busy.length === 1 ? `${busy[0].open} on ${busy[0].name}`
    : `${open} open · ${busy.map((l) => `${l.name} ${l.open}`).join(", ")}`;

  return { data, error, reload, tick, drop, lists, open, level, summary };
}

export default function ListsWidget({ card, onPin }) {
  const { data, error, reload, tick, drop, lists, open } = card;
  // Selected id (null = first). By id so a delete elsewhere doesn't shift the selection; not persisted.
  const [pick, setPick] = useState(null);
  // "new" while naming a list, "rename" while renaming the open one.
  const [mode, setMode] = useState(null);
  // A rejected name (server refuses duplicates), shown by its box, not as a card-level error.
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

  // The FK cascade takes the items too, so the count goes in the toast; held like every delete (lib/undo.js holdDelete).
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
