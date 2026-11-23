import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData, broadcastRefresh } from "../lib/refresh";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { OWED } from "../lib/asks.js";
import { offerUndo } from "../lib/undo.js";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import { AddInput } from "../ui/CheckList.jsx";
import { dragPin } from "../lib/drag.js";
import { fmtMoney } from "../lib/units.js";
import { shortDay } from "../lib/time";

// Expenses to be paid back (backend app/expenses.py): what's waiting
// to go, the Export button that sends it, and each sent batch until it's paid.
//
// Charges are claimed from the Money sheet's transaction list. This card is
// where they're checked before sending, which is why amount, category and
// note can be edited here: claiming part of a charge is an edit, not a
// separate kind of claim. Both views reload on the "expenses" topic, so a
// claim made in one shows up in the other.
const money = (n) => fmtMoney(n, "USD");
const TOPIC = "expenses";

function Editor({ row, categories, onDone }) {
  const [amount, setAmount] = useState(String(row.amount));
  const [category, setCategory] = useState(row.category || "");
  const [note, setNote] = useState(row.note || "");
  const [err, setErr] = useState(null);
  const n = parseFloat(amount);
  const dirty = n !== row.amount || category !== (row.category || "")
    || note !== (row.note || "");

  function save() {
    postJSON(`/api/expenses/${row.id}`, { amount: n, category, note }, "PATCH")
      .then(() => { broadcastRefresh(TOPIC); onDone(); })
      .catch((e) => setErr(e.message));
  }
  return (
    <li className="expense-edit"
      onKeyDown={(e) => {
        if (e.key === "Escape") onDone();
        if (e.key === "Enter" && dirty && n > 0) save();
      }}>
      <label>
        <span>Amount{row.txn_amount != null && ` (of ${money(row.txn_amount)})`}</span>
        <input type="number" step="0.01" min="0.01" max={row.txn_amount ?? undefined}
          value={amount} autoFocus onChange={(e) => setAmount(e.target.value)} />
      </label>
      <label>
        <span>Category</span>
        <input list="expense-categories" value={category}
          onChange={(e) => setCategory(e.target.value)} />
      </label>
      <label className="wide">
        <span>Note</span>
        <input value={note} placeholder="What it was for"
          onChange={(e) => setNote(e.target.value)} />
      </label>
      <datalist id="expense-categories">
        {categories.map((c) => <option key={c} value={c} />)}
      </datalist>
      {err && <p className="widget-note">{err}</p>}
      <div className="expense-edit-actions">
        <button className="form-save-btn" disabled={!dirty || !(n > 0)} onClick={save}>Save</button>
        <button className="widget-footer-btn" onClick={onDone}>Cancel</button>
      </div>
    </li>
  );
}

// A claim made on a pending charge. `pending`: it hasn't posted, so the
// amount can still move (the claim follows it until you export).
// `vanished`: the charge disappeared without posting, usually a released
// hold, so there may be nothing to claim.
function PendingTag({ state }) {
  if (state === "pending") return (
    <span className="check-tag" title="Still pending at the bank. The claim follows the charge when it posts">pending</span>
  );
  if (state === "vanished") return (
    <span className="check-tag missing" title="The bank dropped this pending charge without posting it (a released hold or a cancelled order). Check before sending">cancelled?</span>
  );
  return null;
}

// Rows in a sent batch that no longer match what the bank says: the charge
// settled at a different amount after the sheet went out, or it was a
// pending charge that never posted.
function batchDrift(items) {
  const settled = items.filter((r) => r.posted_amount != null);
  const vanished = items.filter((r) => r.pending_state === "vanished");
  const out = [];
  if (settled.length) out.push({
    text: `${settled.length} charge${settled.length === 1 ? "" : "s"} settled at a different amount`,
    title: settled.map((r) => `${r.merchant}: sent ${money(r.amount)}, charge posted at ${money(r.posted_amount)}`).join("\n"),
  });
  if (vanished.length) out.push({
    text: `${vanished.length} pending charge${vanished.length === 1 ? "" : "s"} never posted`,
    title: vanished.map((r) => `${r.merchant} ${money(r.amount)}`).join("\n"),
  });
  return out;
}

// Mark a batch paid (by that deposit, or none), undoable: undo marks it
// unpaid again. The broadcast re-reads this card and the Money sheet's tags.
function payBatch(b, txn) {
  const url = `/api/expenses/batches/${b.id}/paid`;
  return postJSON(url, { paid: true, transaction_id: txn ?? null }).then(() => {
    broadcastRefresh(TOPIC);
    offerUndo({
      label: `Marked the ${shortDay(b.sent_day)} batch paid (${money(b.total)})`,
      askId: txn ? `expenses:wire:${b.id}` : undefined,
      undo: () => postJSON(url, { paid: false }).then(() => broadcastRefresh(TOPIC)),
    });
  });
}

function Batch({ b, onChange }) {
  const [err, setErr] = useState(null);
  const act = (p) => p.then(onChange).catch((e) => setErr(e.message));
  const paid = (txn) => payBatch(b, txn).catch((e) => setErr(e.message));
  const n = b.items.length;
  return (
    <li className={`expense-batch${b.paid_at ? " is-paid" : ""}`}
      {...dragPin(`expenses · sent ${shortDay(b.sent_day)}`, b)}>
      <div className="expense-batch-head">
        <span>Sent {shortDay(b.sent_day)} · {n} item{n === 1 ? "" : "s"}</span>
        <span className="fin-amount">{money(b.total)}</span>
      </div>
      <div className="expense-batch-foot">
        {b.paid_at ? (
          <span className="check-meta">
            paid {shortDay(b.paid_at.slice(0, 10))}
            {b.paid_amount != null && b.paid_amount !== b.total && ` · ${money(b.paid_amount)} came in`}
          </span>
        ) : b.wire ? (
          <>
            <span className="expense-wire" title={b.wire.name}>
              {money(b.wire.amount)} came in {shortDay(b.wire.day)}. Is this it?
            </span>
            <button className="debt-nudge" onClick={() => paid(b.wire.transaction_id)}
              title={`Mark this batch paid by the ${b.wire.name} deposit`}>mark paid</button>
          </>
        ) : (
          <>
            <span className="check-meta">awaiting payment</span>
            <button className="debt-nudge waiting" onClick={() => paid(null)}
              title="Mark paid without naming the deposit">mark paid</button>
          </>
        )}
        <button className="check-drop" title="Show the spreadsheet in Finder"
          onClick={() => act(postJSON(`/api/expenses/batches/${b.id}/reveal`, {}))}>
          <Icon name="external" size={11} />
        </button>
        {b.paid_at ? (
          <button className="check-drop" title="Not paid after all — reopen"
            onClick={() => act(postJSON(`/api/expenses/batches/${b.id}/paid`, { paid: false }))}>
            <Icon name="undo" size={11} />
          </button>
        ) : (
          // Labelled, not a hover icon: "he hasn't paid the last one, send
          // it again with more on it" is the normal way this gets used.
          <button className="debt-nudge waiting expense-reopen"
            title="Put these back with the waiting expenses so the next export sends them all in one sheet (the old file stays in Downloads)"
            onClick={() => act(fetchJSON(`/api/expenses/batches/${b.id}`, { method: "DELETE" }))}>
            reopen to add more
          </button>
        )}
      </div>
      {!b.paid_at && batchDrift(b.items).map((d) => (
        <p key={d.text} className="expense-drift" title={d.title}>{d.text}</p>
      ))}
      {err && <p className="widget-note">{err}</p>}
    </li>
  );
}

export function useExpensesCard() {
  const { data, error, busy } = useWidgetData("/api/expenses", TOPIC);

  const waiting = data?.waiting ?? [];
  const batches = data?.batches ?? [];
  const unpaid = batches.filter((b) => !b.paid_at);
  // A deposit that looks like Dad's wire is the one thing here that wants an
  // answer today. Anything waiting to send, or sent and unpaid, is real
  // content but nothing is owed yet.
  const level = !data ? null : data.wires > 0 ? NEEDS : waiting.length || unpaid.length ? HAS : QUIET;
  const summary = !data ? "" : [
    data.wires > 0 && `${data.wires} wire${data.wires === 1 ? "" : "s"} to confirm`,
    waiting.length > 0 && `${money(data.waiting_total)} to send`,
    unpaid.length > 0 && `${money(data.outstanding)} awaiting`,
  ].filter(Boolean).join(" · ") || "nothing outstanding";

  // One row per deposit that looks like a batch being paid back. Marking it
  // is the same POST the batch's own button makes.
  const markPaid = (b) => payBatch(b, b.wire.transaction_id);
  const asks = batches.filter((b) => !b.paid_at && b.wire).map((b) => ({
    id: `expenses:wire:${b.id}`, card: "expenses", tier: OWED, kind: "",
    title: `${money(b.wire.amount)} came in ${shortDay(b.wire.day)} — is it the ${shortDay(b.sent_day)} batch?`,
    why: `${b.wire.name} · the batch was ${money(b.total)}, ${b.items.length} item${b.items.length === 1 ? "" : "s"}`,
    acts: [{ label: "Mark paid", style: "go", run: () => markPaid(b) },
           { label: "Open", open: true }],
  }));

  return { data, error, busy, waiting, batches, level, summary, asks };
}

export default function ExpensesWidget({ card, onPin }) {
  const { data, error, busy, waiting, batches } = card;
  const [editing, setEditing] = useState(null);
  const [saved, setSaved] = useState(null);     // the last export's result
  const [err, setErr] = useState(null);

  const changed = () => { setErr(null); broadcastRefresh(TOPIC); };
  const fail = (e) => setErr(e.message);

  function drop(id) {
    fetchJSON(`/api/expenses/${id}`, { method: "DELETE" }).then(changed, fail);
  }

  // "Venmo 24.50 haircut": a trailing number is the amount and whatever
  // follows is the note, the same grammar the debts box uses.
  function add(text) {
    const m = text.match(/^(.+?)\s+\$?(\d+(?:\.\d{1,2})?)\s*(.*)$/);
    if (!m) { setErr("Write it as “Venmo 24.50 haircut”: who, how much, what for."); return; }
    postJSON("/api/expenses", {
      merchant: m[1], amount: parseFloat(m[2]), note: m[3].trim() || null,
    }).then(changed, fail);
  }

  function exportSheet() {
    postJSON("/api/expenses/export", {})
      .then((r) => { setSaved(r); changed(); }, fail);
  }

  function markReviewed() {
    postJSON("/api/expenses/review", {}).then(changed, fail);
  }

  return (
    <section className="widget" {...dragPin("Expenses", data)}>
      <header>
        <h2>Expenses {data && (waiting.length > 0 || data.outstanding > 0) && (
          <span className="widget-sub">
            {waiting.length > 0 && `${money(data.waiting_total)} to send`}
            {waiting.length > 0 && data.outstanding > 0 && " · "}
            {data.outstanding > 0 && `${money(data.outstanding)} awaiting`}
          </span>
        )}</h2>
        <div className="widget-actions">
          {/* A broadcast rather than this card's own reload, so the Money
              sheet's claim tags and reviewed line re-read too. */}
          <RefreshButton onClick={() => broadcastRefresh(TOPIC)} busy={busy} />
          <button className="pin" disabled={!data}
            onClick={() => onPin("Expenses", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}

      {data && waiting.length === 0 && (
        <p className="widget-empty">
          Nothing waiting. Click “+ Expense” on a charge in the Money sheet’s
          transactions, or add cash below.
        </p>
      )}
      {waiting.length > 0 && (
        <ul className="check-list" {...dragPin("expenses · waiting", waiting)}>
          {waiting.map((r) => editing === r.id ? (
            <Editor key={r.id} row={r} categories={data.categories}
              onDone={() => setEditing(null)} />
          ) : (
            <li key={r.id} className="check-row expense-row"
              {...dragPin(`expense · ${r.merchant}`, r)}>
              <span className="expense-day">{shortDay(r.day)}</span>
              <button className="check-body expense-open" onClick={() => setEditing(r.id)}
                title="Edit amount, category or note">
                <span className="check-name">{r.merchant}</span>
                {r.category
                  ? <span className="check-tag">{r.category}</span>
                  : <span className="check-tag missing">no category</span>}
                <PendingTag state={r.pending_state} />
                {r.note && <span className="check-meta">{r.note}</span>}
              </button>
              <span className="debt-amount" title={r.txn_amount != null && r.amount < r.txn_amount
                ? `part of a ${money(r.txn_amount)} charge` : undefined}>
                {money(r.amount)}{r.txn_amount != null && r.amount < r.txn_amount && "*"}
              </span>
              <button className="check-drop" onClick={() => drop(r.id)} title="Not an expense after all">
                <Icon name="close" size={11} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="widget-note">{err}</p>}
      {waiting.length > 0 && (
        <button className="form-save-btn expense-export" onClick={exportSheet}>
          Export {money(data.waiting_total)} (.xlsx)
        </button>
      )}
      {saved && (
        <p className="expense-saved">
          Saved <b>{saved.name}</b> to Downloads.{" "}
          <button className="expense-reveal" onClick={() =>
            postJSON(`/api/expenses/batches/${saved.batch_id}/reveal`, {}).catch(fail)}>
            Show in Finder
          </button>
        </p>
      )}
      {/* Not straight after an export, which just did exactly this. */}
      {data && waiting.length === 0 && !saved && (
        <button className="widget-footer-btn" onClick={markReviewed}
          title="Moves the reviewed line in the Money sheet up to now without sending anything">
          No expenses lately: mark everything reviewed
        </button>
      )}

      {batches.length > 0 && (
        <ul className="expense-batches">
          {batches.map((b) => <Batch key={b.id} b={b} onChange={changed} />)}
        </ul>
      )}
      <AddInput placeholder="Add cash or Venmo: “Venmo 24.50 haircut”" onAdd={add} />
    </section>
  );
}
