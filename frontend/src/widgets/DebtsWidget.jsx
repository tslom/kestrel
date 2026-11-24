import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { OWED, age } from "../lib/asks.js";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { CheckList, CheckRow, AddInput } from "../ui/CheckList.jsx";
import { dragPin } from "../lib/drag.js";
import { holdDelete, useHeld } from "../lib/undo.js";

// Personal IOUs — who owes you and who you owe. Deliberately NOT the
// fin_liabilities table, which holds Plaid's institutional debt (loans,
// cards); a beach-week $24 has nothing to do with a credit line.
const money = (n) => `$${n.toFixed(2)}`;

// Owed beers ride along in the same load: their own table and their own
// section, never in the dollar totals or the chase list.
const loadDebts = () => Promise.all([fetchJSON("/api/debts"), fetchJSON("/api/beers")])
  .then(([debts, beers]) => ({ ...debts, beer: beers }));

export function useDebtsCard() {
  const { data, error, reload } = useWidgetData(loadDebts, "debts");

  function settle(id) {
    postJSON(`/api/debts/${id}/settled`, { settled: true }).then(reload);
  }

  // held until the undo toast goes (lib/undo.js holdDelete)
  function drop(d) {
    holdDelete({ label: `Removed ${d.person}'s ${money(d.amount)}`, key: `debt:${d.id}`,
                 commit: () => fetchJSON(`/api/debts/${d.id}`, { method: "DELETE" }).then(reload) });
  }

  // A row is "Theo ×2": the check settles the oldest, the × drops the newest.
  function settleBeer(g) {
    postJSON("/api/beers/settle", { person: g.person, direction: g.direction }).then(reload);
  }

  function dropBeer(g) {
    fetchJSON(`/api/beers/${g.ids[g.ids.length - 1]}`, { method: "DELETE" }).then(reload);
  }

  // Buys a week of quiet; settling is the only exit. Day arithmetic is the server's (`nudge`,
  // `reminded_days_ago`): the browser's midnight disagrees with day_key for four hours nightly.
  function remind(id) {
    postJSON(`/api/debts/${id}/reminded`, {}).then(reload);
  }

  const debts = data?.debts ?? [];
  // NEEDS is your move, not the money (a standing balance took 44 impressions, 3 opens): a debt
  // never chased, or chased a week ago, is due; once all are chased it drops to HAS.
  const chase = data?.to_chase ?? 0;
  const beers = data?.beer?.beers ?? [];
  const beerCount = (data?.beer?.owed_to_me ?? 0) + (data?.beer?.i_owe ?? 0);
  // Beers alone never raise the card past HAS: nothing about one is due.
  const level = !data ? null : chase > 0 ? NEEDS
    : debts.length || beers.length ? HAS : QUIET;
  const beerNote = beerCount ? `${beerCount} beer${beerCount === 1 ? "" : "s"}` : "";
  const summary = !data ? "" : debts.length === 0 ? (beerNote || "all settled")
    : (chase > 0 ? `${chase} to chase · ` : "")
      + `${money(data.owed_to_me)} owed to you`
      + (data.i_owe > 0 ? ` · ${money(data.i_owe)} you owe` : "")
      + ` · ${debts.length} open`
      + (beerNote ? ` · ${beerNote}` : "");

  // One row per debt that's your move: never asked about, or asked a week
  // ago and still open — the same test `to_chase` counts.
  const asks = debts.filter((d) => d.nudge === "never" || d.nudge === "overdue").map((d) => ({
    id: `debts:${d.id}`, card: "debts", tier: OWED, kind: "",
    title: d.direction === "i_owe" ? `You owe ${d.person} ${money(d.amount)}`
      : `${d.person} owes you ${money(d.amount)}`,
    why: [d.reason, d.nudge === "never" ? `never asked · open ${age(d.created_at)}`
      : `asked ${d.reminded_days_ago} d ago`].filter(Boolean).join(" · "),
    // A nudge goes out under your name: drafted to read and send yourself; "ask" records it.
    acts: [{
      label: "Draft nudge", style: "go",
      draft: {
        text: `Draft a short, friendly nudge to ${d.person} about the ${money(d.amount)}`
          + `${d.reason ? ` for ${d.reason}` : ""} — don't send it.`,
        pin: [`debt · ${d.person}`, d],
      },
    }],
  }));

  return { data, error, reload, settle, drop, remind, debts, level, summary, asks,
           beers, settleBeer, dropBeer };
}

export default function DebtsWidget({ card, onPin }) {
  const { data, error, reload, settle, drop, remind, debts,
          beers, settleBeer, dropBeer } = card;
  const held = useHeld();

  // "Rowan 24 [pizza]": trailing number is the amount. Beers ("Theo 2 beers") are checked first so "2 beers" isn't $2.
  function add(text) {
    const b = text.match(/^(.+?)\s+(?:(\d+)\s+)?beers?\b\s*(.*)$/i);
    if (b) {
      postJSON("/api/beers", {
        person: b[1], count: b[2] ? parseInt(b[2], 10) : 1,
        reason: b[3].trim() || null, direction: "owed_to_me",
      }).then(reload);
      return;
    }
    const m = text.match(/^(.+?)\s+\$?(\d+(?:\.\d{1,2})?)\s*(.*)$/);
    if (!m) return;
    postJSON("/api/debts", {
      person: m[1], amount: parseFloat(m[2]),
      reason: m[3].trim() || null, direction: "owed_to_me",
    }).then(reload);
  }

  return (
    <section className="widget" {...dragPin("Debts", data)}>
      <header>
        <h2>Debts {data && debts.length > 0 && (
          <span className="widget-sub">net {money(data.net)}</span>
        )}</h2>
        <div className="widget-actions">
          <button className="pin" disabled={!data}
            onClick={() => onPin("Debts", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && debts.length === 0 && beers.length === 0 && (
        <p className="widget-empty">Nothing outstanding — add one as “Rowan 24 pizza night” or “Theo beer”.</p>
      )}
      {data && debts.length > 0 && (
        <CheckList drag={dragPin("debts · open", debts)}>
          {debts.filter((d) => !held.has(`debt:${d.id}`)).map((d) => (
            <CheckRow key={d.id} done={false}
              toggleTitle="Mark settled"
              drag={dragPin(`debt · ${d.person}`, d)}
              onToggle={() => settle(d.id)} onDelete={() => drop(d)}>
              <span className="check-name">{d.person}</span>
              <span className={`debt-amount${d.direction === "i_owe" ? " owed" : ""}`}>
                {d.direction === "i_owe" ? "−" : ""}{money(d.amount)}
              </span>
              {d.reason && <span className="check-meta">{d.reason}</span>}
              {d.nudge && (
                <button className={`debt-nudge${d.nudge === "waiting" ? " waiting" : ""}`}
                  onClick={() => remind(d.id)}
                  title={d.nudge === "never"
                    ? `Mark that you asked ${d.person} about this`
                    : d.nudge === "overdue"
                      ? `Asked ${d.reminded_days_ago}d ago and still open — mark that you asked again`
                      : `Asked ${d.reminded_days_ago === 0 ? "today" : `${d.reminded_days_ago}d ago`}`
                        + ` · back in ${(data.nudge_days ?? 7) - d.reminded_days_ago}d`}>
                  {d.nudge === "never" ? "ask"
                    : d.nudge === "overdue" ? `ask again · ${d.reminded_days_ago}d`
                    : `asked ${d.reminded_days_ago === 0 ? "today" : `${d.reminded_days_ago}d`}`}
                </button>
              )}
            </CheckRow>
          ))}
        </CheckList>
      )}
      {data && beers.length > 0 && (
        <>
          <p className="beer-head">Beers</p>
          <CheckList drag={dragPin("beers · open", beers)}>
            {beers.map((g) => (
              <CheckRow key={`${g.direction}:${g.person}`} done={false}
                toggleTitle={g.direction === "i_owe" ? `You bought ${g.person} one`
                  : `${g.person} bought you one`}
                drag={dragPin(`beers · ${g.person}`, g)}
                onToggle={() => settleBeer(g)} onDelete={() => dropBeer(g)}>
                <span className="check-name">{g.person}</span>
                <span className={`debt-amount${g.direction === "i_owe" ? " owed" : ""}`}>
                  {g.direction === "i_owe" ? "you owe " : ""}
                  {g.count === 1 ? "1 beer" : `${g.count} beers`}
                </span>
                {g.reasons.length > 0 && <span className="check-meta">{g.reasons.join(" · ")}</span>}
              </CheckRow>
            ))}
          </CheckList>
        </>
      )}
      <AddInput placeholder="Add a debt — “Rowan 24 pizza night” or “Theo beer”…" onAdd={add} />
    </section>
  );
}
