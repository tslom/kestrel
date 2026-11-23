import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { openLink } from "../lib/plaidLink.js";
import SheetHead from "../ui/SheetHead.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// Status slide-over: every source's auth, last refresh and cache freshness in
// one list, worst first (GET /api/status does the ranking). Doubles as the
// debug view — the last error verbatim, the newest cached day, row counts —
// so "why is Health empty" is a click, not a log grep.
//
// The re-auth buttons are the same flows the widgets use (oauth_routes /
// spotify_routes / Plaid Link); this is just the place you actually notice
// they're needed.

const LABEL = {
  ok: "ok", stale: "stale", broken: "broken", off: "not set up",
};

// What each source actually depends on — the thing to go check when it's red.
// Answers "where does this data come from" without a trip to DESIGN.md.
const ORIGIN = {
  calendar: "Google Calendar API", email: "Gmail API",
  health: "Fitbit (via Google)", weather: "Open-Meteo (no auth)",
  news: "RSS feeds (no auth)",
  spotify: "Spotify Web API", messages: "local chat.db (Full Disk Access)",
  location: "OwnTracks → Cloudflare relay (pulled)", finance: "Plaid",
  features: "derived from the other providers",
};

function Row({ row, onAction, busy }) {
  const [open, setOpen] = useState(false);
  const meta = [];
  if (row.last_ok_ago) meta.push(`synced ${row.last_ok_ago}`);
  else if (row.refreshable) meta.push("never synced this boot");
  if (row.newest_day) meta.push(`newest ${row.newest_day}`);
  if (row.rows != null) meta.push(`${row.rows.toLocaleString()} rows`);
  if (row.duration_ms != null) meta.push(`${row.duration_ms}ms`);

  return (
    <li className={`status-row ${row.state}`} {...dragPin(`source · ${row.source}`, row)}>
      <button className="status-row-head" onClick={() => setOpen((v) => !v)}
        aria-expanded={open} title={ORIGIN[row.source] || undefined}>
        <span className={`dot state-${row.state}`} />
        <span className="status-name">{row.source}</span>
        <span className="status-detail">{row.detail}</span>
        <span className="status-state">{LABEL[row.state] || row.state}</span>
      </button>
      {row.action && (
        <button className="status-action" disabled={busy}
          onClick={() => onAction(row.action)}>
          {busy ? "…" : row.action.kind === "plaid" ? "reconnect" : "connect"}
        </button>
      )}
      {open && (
        <div className="status-more">
          <p className="status-origin">{ORIGIN[row.source] || "—"}</p>
          {meta.length > 0 && <p className="status-meta">{meta.join(" · ")}</p>}
          {row.auth && (
            <p className="status-meta">
              auth: {row.auth.kind}
              {row.auth.account ? `/${row.auth.account}` : ""} — {row.auth.detail}
            </p>
          )}
          {(row.notes || []).map((n) => <p key={n} className="status-note">{n}</p>)}
          {row.error && <pre className="status-error">{row.error}</pre>}
          {row.days_behind != null && row.stale_after_days != null && (
            <p className="status-meta">
              {row.days_behind}d behind (flagged past {row.stale_after_days}d)
            </p>
          )}
          {/* history: the current state can't show a fault that already
              healed, or a sub-step that failed while the refresh succeeded */}
          {(row.events || []).length > 0 && (
            <ul className="status-events">
              {row.events.map((e) => (
                <li key={e.at + e.kind} className={`status-event kind-${e.kind}`}>
                  <span className="status-event-when">{e.ago}</span>
                  <span className="status-event-kind">{e.kind}</span>
                  {e.detail && <span className="status-event-detail">{e.detail}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

// One recorded failure. Distinct from `Row` above: a source has a *state* you
// can act on, a fault is something that already happened and the only actions
// are read it or clear it.
//
// `count` is the number that carries the weight — a fault seen once during a
// migration and a fault firing on every render look identical without it.
function Fault({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="status-row broken">
      <button className="status-row-head" onClick={() => setOpen((v) => !v)}
        aria-expanded={open}>
        <span className="dot state-broken" />
        <span className="status-name">{row.label}</span>
        <span className="status-detail">{row.detail}</span>
        <span className="status-state">
          {row.count > 1 ? `×${row.count} · ` : ""}{row.ago}
        </span>
      </button>
      {open && (
        <div className="status-more">
          <p className="status-meta">
            {/* stored with microseconds so the backend's trim can order by
                it; nobody reads those, so cut back to seconds here */}
            {row.origin} · {row.kind} · first seen {row.first_at.slice(0, 19).replace("T", " ")}
            {row.count > 1 && `, ${row.count} times`}
          </p>
          {row.detail && <pre className="status-error">{row.detail}</pre>}
          {row.trace && <pre className="status-error">{row.trace}</pre>}
        </div>
      )}
    </li>
  );
}

export default function StatusPanel({ onPin }) {
  const [data, setData] = useState(null);
  const [faults, setFaults] = useState(null);
  const held = useHeld();
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);   // action kind+account in flight
  const pollRef = useRef(null);

  const load = useCallback(() => fetchJSON("/api/status")
    .then((d) => { setData(d); setError(null); return d; })
    .catch((e) => { setError(e.message); return null; }), []);

  // Separate from the status read, and never allowed to fail it: the fault log
  // is a debugging convenience and the source list is the panel's actual job.
  const loadFaults = useCallback(() => fetchJSON("/api/failures?limit=25")
    .then(setFaults).catch(() => setFaults(null)), []);

  useEffect(() => { load(); loadFaults(); }, [load, loadFaults]);

  // The consent poll below outlives nothing: it has to stop when the sheet
  // goes, and the sheet going is this component unmounting.
  useEffect(() => () => clearInterval(pollRef.current), []);

  // Consent happens in another tab, so nothing tells us when it's done —
  // poll until the source flips (or we give up), same as AccountsBar did.
  const pollUntilFixed = (source) => {
    const until = Date.now() + 5 * 60 * 1000;
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      const d = await load();
      const row = d?.sources?.find((s) => s.source === source);
      if (!row || row.state === "ok" || Date.now() > until) {
        clearInterval(pollRef.current);
        setBusy(null);
      }
    }, 2000);
  };

  const runAction = async (action, source) => {
    setBusy(source);
    try {
      if (action.kind === "plaid") {
        await openLink(action.item_id, async () => {
          await fetchJSON("/api/plaid/sync", { method: "POST" });
          await load();
          setBusy(null);
        });
        return;
      }
      // OAuth: the backend opens the consent tab. A tab this window opens is
      // a dead end in the desktop shell — window.open returns null there and
      // opens nothing — so the hand-off happens in app/browser.py instead.
      const url = action.kind === "spotify" ? "/api/oauth/spotify/start" : "/api/oauth/start";
      await postJSON(url, action.kind === "spotify"
        ? { open_browser: true }
        : { account: action.account, open_browser: true });
      pollUntilFixed(source);
    } catch (e) {
      setError(e.message);
      setBusy(null);
    }
  };

  const counts = data?.counts || {};
  // Hoisted out of the pin button's onClick so the header can drag the same
  // shape. The event log rides along only for sources that aren't ok — on a
  // healthy day it's 14 sources of noise.
  const pinData = data && {
    source: "status", worst: data.worst, counts: data.counts,
    sources: data.sources.map((s) => ({
      source: s.source, state: s.state, detail: s.detail,
      last_ok_ago: s.last_ok_ago, days_behind: s.days_behind,
      ...(s.state !== "ok" && { fails: s.fails, events: s.events }),
    })),
  };
  return (
    <>
    {/* The pin: "why is this widget empty" is a console question, and the
        answer is always here. */}
    <SheetHead
      refreshedAt={["broken", "stale", "off", "ok"]
        .filter((s) => counts[s])
        .map((s) => `${counts[s]} ${LABEL[s]}`).join(" · ")}
      label="sources · status" data={pinData} onPin={onPin}
      pinTitle="Attach source health to your next console message"
      onRefresh={load} refreshTitle="Re-check now" />

    {error && <p className="status-error">{error}</p>}
    {!data && !error && <p className="widget-empty">Checking…</p>}

    {data && (
      <>
        <ul className="status-list">
          {data.sources.map((row) => (
            <Row key={row.source} row={row} busy={busy === row.source}
              onAction={(a) => runAction(a, row.source)} />
          ))}
        </ul>
        <h3 className="status-group">app</h3>
        <ul className="status-list">
          {data.infra.map((row) => (
            <li key={row.name} className={`status-row ${row.state}`}>
              <div className="status-row-head static">
                <span className={`dot state-${row.state}`} />
                <span className="status-name">{row.name}</span>
                <span className="status-detail">{row.detail}</span>
              </div>
            </li>
          ))}
        </ul>
        {/* The layers the source list can't speak for: a read that 500ed, a
            card that couldn't render, a console turn that came back an error.
            Hidden entirely when empty — an always-present "0 faults" heading
            is one more thing to learn to skim past. */}
        {faults?.failures?.length > 0 && !held.has("faults") && (
          <>
            <h3 className="status-group">
              faults
              {/* held until the undo toast goes (lib/undo.js holdDelete) */}
              <button className="chip" onClick={() => holdDelete({
                label: `Cleared ${faults.failures.length} fault${faults.failures.length === 1 ? "" : "s"}`,
                key: "faults",
                commit: () => fetchJSON("/api/failures", { method: "DELETE" }).then(loadFaults),
              })}
                title="Clear the log — it's derived, and clearing it is how you check whether something is still happening">
                clear
              </button>
            </h3>
            <ul className="status-list">
              {faults.failures.map((f) => <Fault key={f.fingerprint} row={f} />)}
            </ul>
          </>
        )}
        <p className="status-foot">
          Refreshed by the poller every {Math.round(data.refresh_seconds / 60)} min;
          use the sync button in the strip to force a cycle. Checked {data.generated_at.slice(11)}.
        </p>
      </>
    )}
    </>
  );
}
