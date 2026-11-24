import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import { RankBars, useChartWidth } from "../ui/charts.jsx";
import { useSheetData } from "../lib/sheet.js";
import { capRows } from "../lib/pin.js";
import { fmtAgo, fmtDay, shortDay } from "../lib/time";
import { dragPin } from "../lib/drag.js";
import { fmtMoney } from "../lib/units.js";
import { useEffect, useRef, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData, broadcastRefresh } from "../lib/refresh";

// The Money sheet: bands in the order the question is asked (have, moving, owed, where it went,
// receipts). Reads are SQLite only; `finance.get()` makes no API calls.
// Design: DESIGN.md → Sheets are written to the subject, not the provider.
// Plaid stores money leaving as positive; callers flip the sign before fmtMoney (lib/units).

// Compact signed money for chart labels: +$80, −$1.2k (true minus sign).
function compactMoney(v) {
  const a = Math.abs(v);
  const n = a >= 1000 ? `${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k` : Math.round(a);
  return `${v < 0 ? "−" : "+"}$${n}`;
}


// Plaid categories arrive as FOOD_AND_DRINK.
function prettyCategory(c) {
  if (!c) return "Uncategorized";
  const words = c.replace(/_/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Mirrors SPEND_EXCLUDED_CATEGORIES in providers/finance.py; drift shows as bars that don't sum to the totals.
const NOT_SPENDING = ["TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"];

// Mirrors NET_WORTH_ASSET_TYPES in providers/finance.py; `current` on credit/loan is the amount owed.
const ASSET_TYPES = ["depository", "investment", "brokerage"];


// Net worth over time (--data line); a lone first snapshot renders as a hero stat, not a line.
function NetWorthChart({ data }) {
  const [box, W] = useChartWidth(320);
  if (data.length === 0)
    return <p className="widget-empty fin-empty">No net-worth history in this range yet — it starts building from today.</p>;
  const latest = data[data.length - 1];
  if (data.length === 1) {
    return (
      <figure className="fin-chart">
        <div className="fin-hero-row">
          <span className="fin-hero-lg">{fmtMoney(latest.net_worth, "USD")}</span>
          <span className="fin-hero-sub">net worth</span>
        </div>
        <p className="widget-empty">
          One snapshot so far ({fmtMoney(latest.assets, "USD")} assets ·{" "}
          {fmtMoney(latest.liabilities, "USD")} debts). The trend line fills in as days accrue.
        </p>
      </figure>
    );
  }
  const H = 172, padX = 12, padTop = 16, padBot = 22;
  const plotW = W - padX * 2, plotH = H - padTop - padBot;
  const vals = data.map((d) => d.net_worth);
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const x = (i) => padX + plotW * (i / (data.length - 1));
  const y = (v) => padTop + plotH * (1 - (v - min) / span);
  const line = data.map((d, i) => `${x(i)},${y(d.net_worth)}`).join(" ");
  const area = `${padX},${padTop + plotH} ${line} ${x(data.length - 1)},${padTop + plotH}`;
  return (
    <figure className="fin-chart" ref={box}>
      <div className="fin-hero-row">
        <span className="fin-hero-lg">{fmtMoney(latest.net_worth, "USD")}</span>
        <span className="fin-hero-sub">net worth</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="fin-chart-svg" role="img"
        aria-label="Net worth over time">
        <polygon className="fin-area" points={area} />
        <polyline className="fin-line" points={line} />
        {data.map((d, i) => (
          <circle key={d.day} className="fin-dot" cx={x(i)} cy={y(d.net_worth)} r="3">
            <title>{`${shortDay(d.day)}\nnet ${fmtMoney(d.net_worth, "USD")}`}</title>
          </circle>
        ))}
        <text className="fin-axis-label" textAnchor="start" x={padX} y={H - 6}>{shortDay(data[0].day)}</text>
        <text className="fin-axis-label" textAnchor="end" x={W - padX} y={H - 6}>{shortDay(latest.day)}</text>
      </svg>
    </figure>
  );
}

// Weekly net cashflow as diverging bars; sign and value label carry polarity (colour is not the only cue).
function CashflowChart({ data }) {
  const [box, W] = useChartWidth(320);
  if (data.length === 0) return <p className="widget-empty">No cashflow in this range.</p>;
  const H = 172, padX = 10, padTop = 20, padBot = 28;
  const plotH = H - padTop - padBot;
  const maxAbs = Math.max(1, ...data.map((d) => Math.abs(d.net)));
  const zeroY = padTop + plotH / 2;
  const scale = plotH / 2 / maxAbs;
  const slot = (W - padX * 2) / data.length;
  const barW = Math.min(40, slot * 0.6);
  return (
    <figure className="fin-chart" ref={box}>
      <svg viewBox={`0 0 ${W} ${H}`} className="fin-chart-svg" role="img"
        aria-label="Weekly net cashflow (surplus green, deficit red)">
        <line className="fin-chart-axis" x1={padX} x2={W - padX} y1={zeroY} y2={zeroY} />
        {data.map((d, i) => {
          const cx = padX + slot * i + slot / 2;
          const h = Math.max(1, Math.abs(d.net) * scale);
          const up = d.net >= 0;
          const y = up ? zeroY - h : zeroY;
          return (
            <g key={d.week} className={`fin-bar ${up ? "pos" : "neg"}`}>
              <title>{`Week of ${shortDay(d.week)}\nin $${d.inflow}  ·  out $${d.outflow}\nnet ${compactMoney(d.net)}`}</title>
              <rect x={cx - barW / 2} y={y} width={barW} height={h} rx="3" />
              <text className="fin-bar-label" textAnchor="middle" x={cx}
                y={up ? y - 4 : y + h + 11}>{compactMoney(d.net)}</text>
              <text className="fin-axis-label" textAnchor="middle" x={cx}
                y={H - 9}>{shortDay(d.week)}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

// Daily spending bars (outflow >=0, zero-filled to today) with a dotted daily-average line.
function SpendChart({ data }) {
  const [box, W] = useChartWidth(320);
  if (data.length === 0) return <p className="widget-empty">No spending in this range.</p>;
  const H = 172, padX = 10, padTop = 16, padBot = 28;
  const plotH = H - padTop - padBot;
  const max = Math.max(1, ...data.map((d) => d.value));
  const avg = data.reduce((s, d) => s + d.value, 0) / data.length;
  const baseY = padTop + plotH;
  const scale = plotH / max;
  const slot = (W - padX * 2) / data.length;
  const barW = Math.min(28, slot * 0.6);
  const avgY = baseY - avg * scale;
  const tickEvery = Math.ceil(data.length / 7);
  return (
    <figure className="fin-chart" ref={box}>
      <div className="fin-hero-row">
        <span className="fin-hero-lg">{fmtMoney(avg, "USD")}</span>
        <span className="fin-hero-sub">avg / day</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="fin-chart-svg" role="img"
        aria-label="Daily spending with average line">
        <line className="fin-chart-axis" x1={padX} x2={W - padX} y1={baseY} y2={baseY} />
        {data.map((d, i) => {
          const cx = padX + slot * i + slot / 2;
          const h = d.value > 0 ? Math.max(1, d.value * scale) : 0;
          return (
            <g key={d.day} className="fin-bar spend">
              <title>{`${shortDay(d.day)}\nspent ${fmtMoney(d.value, "USD")}`}</title>
              <rect x={cx - barW / 2} y={baseY - h} width={barW} height={h} rx="2" />
              {i % tickEvery === 0 && (
                <text className="fin-axis-label" textAnchor="middle" x={cx}
                  y={H - 9}>{shortDay(d.day)}</text>
              )}
            </g>
          );
        })}
        <line className="fin-avg-line" x1={padX} x2={W - padX} y1={avgY} y2={avgY} />
        <text className="fin-avg-label" textAnchor="end" x={W - padX} y={avgY - 4}>avg</text>
      </svg>
    </figure>
  );
}

// Upcoming obligations (liabilities due <=14d + merchant recurring outflows
// <=7d), merged server-side. Keyed on due_day, not the day/value shape.
function BillsDueSlice({ data }) {
  if (data.length === 0) return <p className="widget-empty">Nothing due soon.</p>;
  return (
    <ul className="event-list" {...dragPin("bills due", data)}>
      {data.map((b, i) => (
        <li key={`${b.account || b.merchant}-${b.due_day}-${i}`}
          {...dragPin(`bill · ${b.account || b.merchant || b.name || "bill"}`, b)}>
          <span className="event-time">{fmtDay(b.due_day)}</span>
          <span className="event-summary" title={b.type || undefined}>
            {b.account || b.merchant || b.name || "Bill"}
            {b.is_overdue ? " · overdue" : ""}
          </span>
          <span className="event-cal fin-amount">
            {b.amount == null ? "—" : fmtMoney(b.amount, b.currency)}
          </span>
        </li>
      ))}
    </ul>
  );
}

// Balances split as net worth is: yours, then owed, each in the account's own currency.
function Accounts({ data }) {
  if (data.length === 0) return <p className="widget-empty">No accounts connected.</p>;
  const side = (types, want) => data.filter((a) => types.includes(a.type) === want);
  const assets = side(ASSET_TYPES, true);
  const debts = side(ASSET_TYPES, false);
  const total = (rows) => rows.reduce((s, a) => s + (a.current || 0), 0);

  const Group = ({ rows, label, owed }) => rows.length > 0 && (
    <div className="money-accounts" {...dragPin(`${label.toLowerCase()}`, rows)}>
      <div className="money-group-head">
        <span>{label}</span>
        <span className="fin-amount">{fmtMoney(owed ? -total(rows) : total(rows), "USD")}</span>
      </div>
      <ul className="event-list">
        {rows.map((a) => (
          <li key={a.account_id} {...dragPin(`account · ${a.account}`, a)}>
            <span className="event-summary">{a.account}</span>
            <span className="event-cal">{a.subtype || a.type}</span>
            <span className="fin-amount">
              {a.current == null ? "—" : fmtMoney(owed ? -a.current : a.current, a.currency)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );

  return <>
    <Group rows={assets} label="Accounts" />
    <Group rows={debts} label="Owed" owed />
  </>;
}

// Grouped from the transaction rows (no per-category endpoint); bars scale to the largest category.
function Breakdown({ txns }) {
  const spend = txns.filter((t) => t.amount > 0 && !NOT_SPENDING.includes(t.category));
  if (spend.length === 0) return <p className="widget-empty">Nothing spent in this range.</p>;

  const by = new Map();
  for (const t of spend) {
    const k = prettyCategory(t.category);
    by.set(k, (by.get(k) || 0) + t.amount);
  }
  const rows = [...by.entries()].sort((a, b) => b[1] - a[1]);
  // Past eight the bars are hairlines; the tail is counted together.
  const shown = rows.slice(0, 8);
  const tail = rows.slice(8);
  if (tail.length) shown.push([`${tail.length} smaller categories`, tail.reduce((s, r) => s + r[1], 0)]);
  return (
    <RankBars rows={shown.map(([label, amount]) => ({ label, value: amount }))}
      fmt={(v) => fmtMoney(v, "USD")}
      pinLabel={(r) => `spending · ${r.label.toLowerCase()}`}
      empty="Nothing spent in this range." />
  );
}

// The claim control at a row's edge (flow in ExpensesWidget). Only a waiting claim can be taken
// back: a sent one must keep matching the spreadsheet already sent.
function ClaimButton({ t, claim, onClaim, onUnclaim }) {
  if (claim) {
    const part = claim.amount < t.amount ? ` ${fmtMoney(claim.amount, t.currency)}` : "";
    return claim.status === "waiting" ? (
      <button className="txn-claim on" onClick={() => onUnclaim(claim.id)}
        title="Claimed as an expense, not sent yet. Click to take it back">
        Expense{part}
      </button>
    ) : (
      <span className="txn-claim on sent" title={`Sent${claim.status === "paid" ? ", and paid" : ""}`}>
        Expense{part} · {claim.status}
      </span>
    );
  }
  // Pending is fine: the sync moves the claim to the posted charge (expenses.follow_posted).
  if (t.amount <= 0) return null;
  return (
    <button className="txn-claim" onClick={() => onClaim(t.id)}
      title="Claim this as an expense to be paid back (edit the amount on the Expenses card to claim part of it)">
      + Expense
    </button>
  );
}

// The receipts, newest day first, keeping every Plaid enrichment. `marks` draws claims and the
// reviewed line; unreviewed rows under it posted late (Plaid dates by swipe) and get a dot.
// Hovered row is tracked in JS, not CSS :hover: WKWebView leaves :hover stuck on scroll or window
// blur, so we re-ask elementFromPoint (rAF-throttled) on scroll and clear on leave/blur.
function useHoveredRow(ref) {
  const [id, setId] = useState(null);
  useEffect(() => {
    let last = null, frame = 0;
    const check = () => {
      frame = 0;
      const el = last && document.elementFromPoint(last[0], last[1])?.closest("li[data-txn]");
      setId(el && ref.current?.contains(el) ? el.dataset.txn : null);
    };
    const soon = () => { if (!frame) frame = requestAnimationFrame(check); };
    const move = (e) => { last = [e.clientX, e.clientY]; soon(); };
    const clear = () => { last = null; soon(); };
    document.addEventListener("pointermove", move, { passive: true });
    document.addEventListener("scroll", soon, { capture: true, passive: true });
    document.documentElement.addEventListener("mouseleave", clear);
    window.addEventListener("blur", clear);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("scroll", soon, { capture: true });
      document.documentElement.removeEventListener("mouseleave", clear);
      window.removeEventListener("blur", clear);
    };
  }, [ref]);
  return id;
}

function TxnDays({ data, marks, onClaim, onUnclaim }) {
  const box = useRef(null);
  const hovered = useHoveredRow(box);
  if (data.length === 0) return <p className="widget-empty">No transactions in this range.</p>;
  const reviewed = new Set(marks?.reviewed ?? []);
  const byDay = new Map();
  for (const t of data) {
    if (!byDay.has(t.day)) byDay.set(t.day, []);
    byDay.get(t.day).push(t);
  }
  for (const txns of byDay.values())
    txns.sort((a, b) => reviewed.has(a.id) - reviewed.has(b.id));
  let lineAt = null;   // id of the first reviewed row, newest-first
  for (const [, txns] of [...byDay.entries()].reverse()) {
    const hit = txns.find((t) => reviewed.has(t.id));
    if (hit) { lineAt = hit.id; break; }
  }
  let below = false;
  return <div ref={box}>{[...byDay.entries()].reverse().map(([day, txns]) => (
    <section key={day} className="slice-day" {...dragPin(`spend · ${fmtDay(day)}`, capRows(txns))}>
      <h3>{fmtDay(day)}</h3>
      <ul className="event-list">
        {txns.map((t) => {
          const line = t.id === lineAt;
          if (line) below = true;
          const late = below && !reviewed.has(t.id);
          return [
          line && (
            <li key="reviewed-line" className="txn-reviewed-line">
              expenses reviewed through here
            </li>
          ),
          <li key={t.id} data-txn={t.id}
            className={[late && "txn-late", hovered === t.id && "txn-hover"]
              .filter(Boolean).join(" ") || undefined}
            title={late ? "Posted after your last expenses review" : undefined}
            {...dragPin(`txn · ${t.merchant || t.name}`, t)}>
            <span className="event-time">{fmtMoney(-t.amount, t.currency, true)}</span>
            <span className="event-summary" title={t.category ? prettyCategory(t.category) : undefined}>
              {t.logo_url && (
                <img className="fin-logo" src={t.logo_url} alt="" loading="lazy"
                  onError={(e) => { e.currentTarget.style.display = "none"; }} />
              )}
              {t.website ? (
                <a className="fin-merchant-link" title={t.website}
                  href={/^https?:\/\//.test(t.website) ? t.website : `https://${t.website}`}
                  target="_blank" rel="noreferrer noopener">{t.merchant || t.name}</a>
              ) : (t.merchant || t.name)}
              {t.pending ? " · pending" : ""}
              {t.location && <span className="fin-location"> · {t.location}</span>}
              {t.counterparties && (
                <span className="fin-location"> · via {t.counterparties}</span>
              )}
            </span>
            <span className="event-cal">{t.account}</span>
            {marks && <ClaimButton t={t} claim={marks.claimed[t.id]}
              onClaim={onClaim} onUnclaim={onUnclaim} />}
          </li>,
          ];
        })}
      </ul>
    </section>
  ))}</div>;
}

// `balances` and `bills_due` ignore the range (snapshots of now); asked anyway for one code path.
const VIEWS = ["balances", "networth", "cashflow", "spend", "transactions", "bills_due"];

export default function MoneySheet({ onPin }) {
  // A month: rent, statements and paychecks land on it.
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("finance", VIEWS, "this-month");

  // Own read on the "expenses" topic so both cards redraw; failure costs only the claim buttons.
  const { data: marks, reload: reloadMarks } = useWidgetData(
    () => fetchJSON(`/api/expenses/marks?range=${encodeURIComponent(range)}`)
      .catch(() => null),
    "expenses");
  useEffect(() => { reloadMarks(); }, [range, reloadMarks]);
  const [claimErr, setClaimErr] = useState(null);
  const claimed = (p) => p.then(() => { setClaimErr(null); broadcastRefresh("expenses"); },
    (e) => setClaimErr(e.message));
  const onClaim = (id) => claimed(postJSON("/api/expenses", { transaction_id: id }));
  const onUnclaim = (id) => claimed(fetchJSON(`/api/expenses/${id}`, { method: "DELETE" }));

  const cash = rows("cashflow");
  const inflow = cash.reduce((s, w) => s + w.inflow, 0);
  const outflow = cash.reduce((s, w) => s + w.outflow, 0);
  const net = inflow - outflow;
  const txns = rows("transactions");

  // Daily bars up to 31 days, weekly net beyond (a quarter of daily bars is a comb).
  const daily = rows("spend").length > 0 && rows("spend").length <= 31;

  const pinLabel = `money · ${range}`;
  const pinData = payloads && {
    range,
    accounts: rows("balances"),
    cashflow: { in: inflow, out: outflow, net },
    due: rows("bills_due"),
    transactions: capRows(txns),
  };

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (
        <>
          <Band title="Worth" pin={[`net worth · ${range}`, rows("networth")]}>
            <NetWorthChart data={rows("networth")} />
            <Accounts data={rows("balances")} />
          </Band>

          <Band title="Flow" note={compactMoney(net)}
            pin={[`cashflow · ${range}`, cash]}>
            <p className="money-flow">
              <span className="money-in">in {fmtMoney(inflow, "USD")}</span>
              <span className="money-out">out {fmtMoney(outflow, "USD")}</span>
            </p>
            {daily ? <SpendChart data={rows("spend")} /> : <CashflowChart data={cash} />}
          </Band>

          {rows("bills_due").length > 0 && (
            <Band title="Due soon" note={`${rows("bills_due").length}`}
              pin={["bills due", rows("bills_due")]}>
              <BillsDueSlice data={rows("bills_due")} />
            </Band>
          )}

          <Band title="Where it went" note={fmtMoney(outflow, "USD")}>
            <Breakdown txns={txns} />
          </Band>

          <Band title="Transactions" note={`${txns.length}`}
            pin={[`transactions · ${range}`, capRows(txns)]}>
            {claimErr && <p className="widget-note">{claimErr}</p>}
            <TxnDays data={txns} marks={marks}
              onClaim={onClaim} onUnclaim={onUnclaim} />
          </Band>
        </>
      )}
    </>
  );
}
