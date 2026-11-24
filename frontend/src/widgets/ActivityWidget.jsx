import { useState } from "react";
import { postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { HAS, NEEDS, QUIET } from "../lib/salience";
import { OWED, age } from "../lib/asks.js";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import { dragPin } from "../lib/drag.js";

// What the backend changed on its own (app/activity.py): applied first, reviewed here; every row
// has an Undo and a text to yourself can be re-filed.

const SOURCE = { selftext: "text to self", finance: "bill", travel: "flight" };

// Short names: the tag column on each row, and the "file as" chips.
const KIND = {
  task: "task", event: "event", food: "food", watch: "watch",
  list_item: "list", note: "note", keep: "kept",
};

// The tag already says what it became; drop the summary's own lead-in.
const LEAD = /^(Saved a note:|New task:|Kept as is\s*—|Kept as is$|Kept\s*—)\s*/;
const tidy = (s) => (s || "").replace(LEAD, "") || s;

// Host + path start identify a link; the full text stays in the tooltip.
const shortUrls = (t) => t.replace(/https?:\/\/[^\s]+/g, (u) => {
  try {
    const x = new URL(u);
    const path = x.pathname.replace(/\/$/, "");
    const host = x.host.replace(/^www\./, "");
    return path.length > 28 ? `${host}${path.slice(0, 28)}…` : host + path;
  } catch { return u; }
});

// The backend writes "(photo)" when nothing was typed; redundant beside the thumbnail.
const PLACEHOLDER = /^\((photo|image|video|file|attachment)s?\)$/i;

export function useActivityCard() {
  const { data, error, setError, reload, busy } = useWidgetData("/api/activity");

  const items = data?.items ?? [];
  const unseen = items.filter((i) => i.unseen);
  const newest = items.length ? items[0].id : 0;

  function seen() {
    return postJSON("/api/activity/seen", { through: newest }).then(reload);
  }

  // "Needs you" only while unseen: they already happened, so seen ones are history.
  const day = Date.now() - 24 * 3600 * 1000;
  const level = !data ? null : unseen.length ? NEEDS
    : items.some((i) => new Date(i.at).getTime() > day) ? HAS : QUIET;
  const n = unseen.length;
  const summary = !data ? ""
    : n ? `${n} change${n > 1 ? "s" : ""} while you were away`
    : items.length ? `${items.length} change${items.length > 1 ? "s" : ""} this week, all seen`
    : "nothing changed on its own";

  const asks = n ? [{
    id: `activity:${newest}`, card: "activity", tier: OWED, kind: "",
    title: summary,
    why: unseen.slice(0, 2).map((i) => i.summary).join(" · ")
      + (n > 2 ? ` · +${n - 2} more` : "") + ` · ${age(unseen[n - 1].at)} ago`,
    acts: [{ label: "Review", open: true },
           { label: "Looks fine", run: seen }],
  }] : [];

  return { data, error, setError, reload, busy, items, unseen, seen, level, summary, asks };
}

function Thumb({ f }) {
  const [broken, setBroken] = useState(false);
  const url = `/api/attachments/${f.id}/file`;
  // Messages HEIC comes back as JPEG (routes/attachments.py), so any image
  // from a text can be shown; a file that won't load becomes its name.
  if (broken || !/^image\//.test(f.mime || "")) {
    return <a className="act-file" href={url} target="_blank" rel="noopener noreferrer">
      {f.filename || "file"}</a>;
  }
  return (
    <a className="act-thumb" href={url} target="_blank" rel="noopener noreferrer"
      title={f.filename}>
      <img src={url} alt="" loading="lazy" onError={() => setBroken(true)} />
    </a>
  );
}

function Row({ it, reload }) {
  const [err, setErr] = useState(null);
  const [working, setWorking] = useState(null); // "undo" | "redo"
  const [filing, setFiling] = useState(false);

  function act(kind, url, body) {
    setWorking(kind);
    setErr(null);
    postJSON(url, body)
      .then(() => { setFiling(false); return reload(); })
      .catch((e) => setErr(e.message))
      .finally(() => setWorking(null));
  }

  const undone = Boolean(it.undone_at);
  const origin = it.origin_text;
  const said = (origin?.text || "").trim();
  const files = origin?.files || [];
  const quote = said && !(files.length && PLACEHOLDER.test(said)) ? said : "";
  const canRedo = it.redo_kinds && !undone;
  return (
    <li className={`act-row${it.unseen ? " unseen" : ""}${undone ? " undone" : ""}`}
      {...dragPin(`change · ${it.summary}`, it)}>
      <span className={`act-kind kind-${it.kind}`}>{undone ? "undone" : KIND[it.kind] || it.kind}</span>
      <div className="act-body">
        <div className="act-line">
          <span className="act-summary" title={it.summary}>{tidy(it.summary)}</span>
          <span className="act-age" title={new Date(it.at).toLocaleString()}>{age(it.at)}</span>
        </div>
        <div className="act-from">
          {/* three at most: a text with a dozen photos pushed the rest off the card */}
          {files.slice(0, 3).map((f) => <Thumb key={f.id} f={f} />)}
          {files.length > 3 && <span className="act-more" title={files.slice(3).map((f) => f.filename).join(", ")}>
            +{files.length - 3}</span>}
          <span className="act-src">{SOURCE[it.source] || it.source}</span>
          {quote && <span className="act-quote" title={said}>{shortUrls(quote)}</span>}
        </div>
        {(it.undoable || canRedo || err) && (
          <div className="act-actions">
            {it.undoable && (
              <button disabled={!!working}
                onClick={() => act("undo", `/api/activity/${it.id}/undo`)}>
                {working === "undo" ? "undoing…" : "undo"}
              </button>
            )}
            {canRedo && (
              <button disabled={!!working} aria-expanded={filing}
                className={filing ? "on" : ""} onClick={() => setFiling((v) => !v)}>
                {working === "redo" ? "re-filing…" : "file as…"}
              </button>
            )}
            {filing && !working && (
              <span className="act-kinds">
                {it.redo_kinds.map((k) => (
                  <button key={k} className="act-chip"
                    onClick={() => act("redo", `/api/activity/${it.id}/redo`, { kind: k })}>
                    {k === "keep" ? "just keep" : KIND[k]}
                  </button>
                ))}
              </span>
            )}
            {err && <span className="act-err">{err}</span>}
          </div>
        )}
      </div>
    </li>
  );
}

export default function ActivityWidget({ card }) {
  const { data, error, reload, busy, items, unseen, seen } = card;

  return (
    <section className="widget" {...dragPin("Changes while you were away", items)}>
      <header>
        <h2>While you were away {unseen.length > 0 &&
          <span className="widget-sub">{unseen.length} new</span>}</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={busy} />
          <button className="chip" disabled={!unseen.length} onClick={seen}
            title="Mark everything here as seen">looks fine</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && items.length === 0 && (
        <p className="widget-empty">
          Nothing yet. Texts to yourself, bill tasks and flight check-ins show up here
          when they're filed, each with an undo.
        </p>
      )}
      {items.length > 0 && (
        <ul className="act-list">
          {items.map((it) => <Row key={it.id} it={it} reload={reload} />)}
        </ul>
      )}
    </section>
  );
}
