import { useEffect, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import VaultLink from "../ui/VaultLink.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// The Brain (DESIGN.md → The Brain): facts live in app.db; this card is where you rule on them.
// Unconfirmed facts are already used, hedged, until ✓; ✗ makes a tombstone ingest never relearns.
// Review goes a page at a time (the backfill brings hundreds).

const TOPIC = "brain";
const BRAIN_DIR = "07-Brain";

export function useBrainCard() {
  const { data, error, reload, busy } = useWidgetData("/api/brain", TOPIC);
  const pages = data?.pages ?? [];
  const pending = data?.pending ?? 0;
  const known = pages.reduce((n, p) => n + p.active + p.pending, 0);

  // To-confirm is "today", never "needs you": Kestrel already uses the facts, labelled.
  const level = !data ? null : pending > 0 ? HAS : QUIET;
  const summary = !data ? "" : known === 0 ? (data.ingesting ? "learning…" : "nothing learned yet")
    : (pending ? `${pending} to confirm · ` : "") + `${known} fact${known === 1 ? "" : "s"}`
      + ` on ${pages.length} page${pages.length === 1 ? "" : "s"}`
      + (data.ingesting ? " · learning…" : "");

  return { data, error, reload, refreshing: busy, level, summary, pages, pending };
}

async function rule(ids, verdict) {
  return postJSON("/api/brain/verdict", { ids, verdict });
}

function FactMeta({ f }) {
  return (
    <span className="brain-meta">
      {f.as_of}{f.source_label ? ` · ${f.source_label}` : ""}
    </span>
  );
}

function FactRow({ f, onChanged, editable = false }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(f.claim);
  const [err, setErr] = useState(null);

  const act = (p) => p.then(onChanged).catch((e) => setErr(e.message));
  // ✗ is held for the undo toast: rejecting can also revive what the fact
  // superseded, so it's sent when the toast goes rather than reversed after.
  const gone = useHeld().has(`fact:${f.id}`);
  const forget = () => holdDelete({
    label: `Forgot “${f.claim.length > 50 ? `${f.claim.slice(0, 50)}…` : f.claim}”`,
    key: `fact:${f.id}`,
    commit: () => rule([f.id], "rejected").then(onChanged),
  });
  if (gone) return null;
  const save = () => {
    const claim = text.trim();
    if (!claim || claim === f.claim) { setEditing(false); return; }
    act(postJSON(`/api/brain/fact/${f.id}/edit`, { claim }).then(() => setEditing(false)));
  };

  return (
    <li className={`brain-fact ${f.status}`} {...dragPin(`brain fact · ${f.claim}`, f)}>
      {editing ? (
        <input className="brain-edit" value={text} autoFocus
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEditing(false); }}
          onBlur={save} />
      ) : (
        <span className="brain-claim">
          {f.claim}
          {f.core ? <span className="brain-core" title="In the profile every session starts with">profile</span> : null}
        </span>
      )}
      <FactMeta f={f} />
      <span className="gen-actions">
        {f.status === "pending" && (
          <button onClick={() => act(rule([f.id], "confirmed"))} title="That's right">✓</button>
        )}
        {editable && !editing && (
          <button onClick={() => setEditing(true)} title="Reword it — your wording becomes the fact">edit</button>
        )}
        <button onClick={forget} aria-label="Forget this fact"
          title="Wrong — forget it, and never learn it again">✗</button>
      </span>
      {err && <span className="brain-err">{err}</span>}
    </li>
  );
}

// One page's facts, refetched when the page changes (useLoad reloads only on mount and the bus).
function usePage(slug) {
  const got = useWidgetData(
    () => (slug ? fetchJSON(`/api/brain/page/${slug}`) : Promise.resolve(null)), TOPIC);
  const { reload } = got;
  useEffect(() => { reload(); }, [slug, reload]);
  return { ...got, page: got.data && got.data.slug === slug ? got.data : null };
}

export default function BrainWidget({ card, onPin, onExpand }) {
  const { data, error, reload, refreshing, pages, pending } = card;
  // The page the queue is on: the first one with something to confirm.
  const next = pages.find((p) => p.pending > 0);
  const { page, reload: reloadPage } = usePage(next?.slug);
  const shown = page ? page.facts.filter((f) => f.status === "pending").slice(0, 8) : [];
  const changed = () => Promise.all([reload(), reloadPage()]);

  return (
    <section className="widget" {...dragPin("Brain — what Kestrel knows", data?.profile)}>
      <header>
        <h2>Brain</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!data}
            onClick={() => onPin("Brain — what Kestrel knows", data?.profile)}
            title="Attach the profile to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <div className="skel w80" />}
      {data && pages.length === 0 && (
        <p className="widget-empty">
          {data.ingesting ? "Learning from your texts, chats and email…"
            : "Nothing yet — Kestrel records what it learns about you as you talk."}
        </p>
      )}
      {next && (
        <div className="brain-queue">
          <div className="brain-queue-head">
            <span>To confirm · <b>{next.title}</b> <span className="brain-meta">{next.pending} of {pending}</span></span>
            <span className="gen-actions">
              <button onClick={() => postJSON(`/api/brain/page/${next.slug}/confirm`, {}).then(changed)}
                title={`Everything unconfirmed on ${next.title} is right`}>✓ all {next.pending}</button>
            </span>
          </div>
          <ul className="brain-facts">
            {shown.map((f) => <FactRow key={f.id} f={f} onChanged={changed} />)}
          </ul>
          {onExpand && (
            <button className="widget-footer-btn" onClick={onExpand}>
              review every page <Icon name="arrowRight" size={11} />
            </button>
          )}
        </div>
      )}
      {pages.length > 0 && (
        <ul className="gen-list brain-pages">
          {pages.slice(0, 12).map((p) => (
            <li key={p.slug}>
              <span className="gen-title">
                <VaultLink target={`${BRAIN_DIR}/${p.slug}.md`}>{p.title}</VaultLink>
              </span>
              <span className="gen-by">{p.active + p.pending}{p.pending ? ` · ${p.pending} ?` : ""}</span>
            </li>
          ))}
          {pages.length > 12 && <li className="brain-meta">+{pages.length - 12} more pages</li>}
        </ul>
      )}
    </section>
  );
}

export function BrainSheet() {
  const { data, reload } = useWidgetData("/api/brain", TOPIC);
  const pages = data?.pages ?? [];
  const [slug, setSlug] = useState(null);
  const current = slug ?? pages.find((p) => p.pending > 0)?.slug ?? pages[0]?.slug ?? null;
  const { page, error, reload: reloadPage } = usePage(current);
  const changed = () => Promise.all([reload(), reloadPage()]);
  const loaded = Boolean(page);
  const pendingHere = loaded ? page.facts.filter((f) => f.status === "pending") : [];
  const nextPending = pages.find((p) => p.pending > 0 && p.slug !== current);

  if (!data) return <div className="skel w80" />;
  if (!pages.length) return <p className="widget-empty">Nothing learned yet.</p>;

  return (
    <div className="brain-sheet">
      <nav className="brain-nav">
        {pages.map((p) => (
          <button key={p.slug} className={p.slug === current ? "on" : ""}
            onClick={() => setSlug(p.slug)}>
            <span>{p.title}</span>
            {p.pending > 0 && <span className="brain-count">{p.pending}</span>}
          </button>
        ))}
      </nav>
      <div className="brain-page">
        {error && <WidgetError error={error} />}
        {loaded && (
          <>
            <div className="expand-head">
              <h3 className="brain-title">
                <VaultLink target={`${BRAIN_DIR}/${page.slug}.md`}>{page.title}</VaultLink>
              </h3>
              <span className="expand-spacer" />
              {pendingHere.length > 0 && (
                <span className="gen-actions">
                  <button onClick={() => postJSON(`/api/brain/page/${page.slug}/confirm`, {}).then(changed)}>
                    ✓ {pendingHere.length === page.facts.length ? "all" : "the other"} {pendingHere.length}
                  </button>
                </span>
              )}
              {nextPending && (
                <button className="widget-footer-btn" onClick={() => setSlug(nextPending.slug)}>
                  next: {nextPending.title} <Icon name="arrowRight" size={11} />
                </button>
              )}
            </div>
            {page.summary && <p className="brain-summary">{page.summary}</p>}
            <ul className="brain-facts">
              {page.facts.map((f) => <FactRow key={f.id} f={f} onChanged={changed} editable />)}
            </ul>
            {page.history.length > 0 && (
              <>
                <h4 className="brain-sub">Earlier</h4>
                <ul className="brain-facts">
                  {page.history.map((f) => (
                    <li key={f.id} className="brain-fact superseded">
                      <span className="brain-claim">{f.claim}</span>
                      <FactMeta f={f} />
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
