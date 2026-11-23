import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useLoad } from "../lib/refresh";
import { openLink } from "../lib/plaidLink.js";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import { BROKEN, age } from "../lib/asks.js";
import { lead, MIN_BASELINE } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { fmtMoney } from "../lib/units.js";

// Compact finance card (PLAID_PLAN.md phase 4): per-account balances + week
// spend from the finance provider, gated on /api/plaid/status. Connect and
// Reconnect drive Plaid Link (loaded lazily by plaidLink.js). Display flips
// Plaid's sign convention — a stored amount of 12.50 is $12.50 spent.

export function useFinanceCard() {
  const [status, setStatus] = useState(null);   // /api/plaid/status payload
  const [balances, setBalances] = useState(null);
  const [spend, setSpend] = useState(null);   // [{day, value}] over the baseline window
  const [error, setError] = useState(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;   // StrictMode remounts after running the cleanup
    return () => { alive.current = false; };
  }, []);

  const load = useCallback(async () => {
    try {
      const st = await fetchJSON("/api/plaid/status");
      if (!alive.current) return;
      setStatus(st);
      if (!st.configured || st.items.length === 0) {
        setBalances(null); setSpend(null); setError(null);
        return;
      }
      // A month rather than a week, because the card now says whether today's
      // outflow is unusual and seven days can't support that claim. Same
      // this-month/last-month fallback the trends card uses, and for the same
      // reason: it's the only anchored window long enough, and on the 3rd it
      // is three days long. Both spans resolve server-side from db.day_key().
      const [bal, cur] = await Promise.all([
        fetchJSON("/api/finance?metric=balances"),
        fetchJSON("/api/finance?range=this-month&metric=spend"),
      ]);
      if (!alive.current) return;
      let series = cur.data;
      if (series.length < MIN_BASELINE + 1) {
        const prev = await fetchJSON("/api/finance?range=last-month&metric=spend");
        if (!alive.current) return;
        series = [...prev.data, ...series];
      }
      setBalances(bal.data);
      setSpend(series);
      setError(null);
    } catch (e) {
      if (alive.current) setError(e.message);
    }
  }, []);

  const [reload, refreshing] = useLoad(load);

  const items = status?.items || [];
  // needs_consent = §18: item works fine but was linked before liabilities
  // was added — same Link update-mode reconnect fixes both cases.
  const reconnect = items.find(
    (it) => it.status === "login_required" || it.status === "needs_consent");
  const connected = status?.configured && items.length > 0;

  const currency = balances?.[0]?.currency;
  // finance.py's spend metric is zero-filled up to today, so the last day of
  // the series is today's outflow and a zero in it is a real zero — a day you
  // didn't spend, not a day the bank didn't answer. That's what makes this
  // series safe to use as a baseline where health's would not be.
  const spendToday = spend?.length ? spend[spend.length - 1].value : null;
  const spendWeek = spend?.length
    ? spend.slice(-7).reduce((sum, d) => sum + d.value, 0) : null;
  const CARD_METRICS = [
    // Money is skewed hard by construction — most days near zero, rent once a
    // month — which is exactly the distribution the median/MAD scorer and the
    // rarity gate were chosen for. A mean-and-stdev test here would call the
    // week after payday normal and the rest of the month a drought.
    { key: "spend", label: "Spent today", worse: "up", floor: 150,
      fmt: (v) => fmtMoney(v, currency) },
  ];
  const readings = spendToday != null
    ? CARD_METRICS.map((m) => ({
        ...m, day: spend[spend.length - 1].day, value: spendToday, series: spend }))
    : [];
  const hit = spendToday != null ? lead(readings) : null;

  // Balances don't change on a day you didn't spend, and they're one click
  // away in the quiet line — a bank that needs reconnecting is the only thing
  // here that wants you, and now an unusual day's spending is the only other
  // thing that earns the "today" band.
  const level = !status ? null
    : reconnect ? NEEDS
    : !connected ? QUIET
    : !balances ? null
    : hit ? HAS : QUIET;
  const summary = !status?.configured ? "Plaid not set up"
    : !connected ? "no bank connected"
    : reconnect ? `${reconnect.institution || "Bank"} `
      + (reconnect.status === "needs_consent"
        ? "needs an extra permission" : "needs a reconnect")
    : spendToday ? `${fmtMoney(spendToday, currency)} today`
      + (spendWeek != null ? ` · ${fmtMoney(spendWeek, currency)} this week` : "")
    : "no spend today";

  // A bank that needs you to log in again is a broken source. Plaid Link runs
  // from the card, so the row opens it rather than reconnecting from here.
  const asks = reconnect ? [{
    id: `finance:reconnect:${reconnect.item_id ?? reconnect.institution}`, card: "finance",
    tier: BROKEN, kind: "fix", title: summary,
    why: reconnect.last_synced_at ? `last synced ${age(reconnect.last_synced_at)} ago` : "",
    acts: [{ label: "Reconnect", style: "fixb", open: true }],
  }] : [];

  return { status, balances, spend, error, setError, alive, load, reload, refreshing,
           items, reconnect, connected, currency, spendToday, spendWeek,
           readings, hit, level, summary, asks };
}

export default function FinanceWidget({ card, onPin }) {
  const { status, balances, spend, error, setError, alive, load, reload, refreshing,
          items, reconnect, connected, currency, spendToday, spendWeek,
          readings, hit } = card;
  const [busy, setBusy] = useState(false);      // Link flow / post-link sync
  const [setup, setSetup] = useState(null);     // {client_id, secret, env} while the form is open

  // The exchange/sync POST answers once its sync has run (routes/plaid.py),
  // so what it wrote is there to load.
  const synced = () => { if (alive.current) { setBusy(false); load(); } };

  // itemId set = Link update mode (reconnect after ITEM_LOGIN_REQUIRED).
  const connect = async (itemId) => {
    setBusy(true);
    try {
      await openLink(itemId, async (publicToken, metadata) => {
        if (itemId) {
          await fetchJSON("/api/plaid/sync", { method: "POST" });
        } else {
          await postJSON("/api/plaid/exchange", {
            public_token: publicToken,
            institution_name: metadata?.institution?.name,
            institution_id: metadata?.institution?.institution_id,
          });
        }
        synced();
      });
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  // Setup form: the Plaid client_id/secret go straight to the backend and
  // are never kept or echoed — the form clears itself on success.
  const saveSetup = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await postJSON("/api/plaid/setup", setup);
      setSetup(null);
      setError(null);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="widget"
      {...dragPin("finance · balances + month spend",
                  balances && { source: "finance", balances, spend })}>
      <header>
        <h2>Finance <span className="widget-sub">
          {status?.env === "sandbox" ? "sandbox" : "this week"}</span></h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!balances}
            onClick={() => onPin("finance · balances + month spend",
              { source: "finance", balances, spend })}
            title="Attach balances + this month's daily spend to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!status && !error && <><div className="skel" /><div className="skel w60" /></>}
      {status && !status.configured && !setup && (
        <p className="widget-empty">
          Plaid not set up.{" "}
          <button className="chip"
            onClick={() => setSetup({ client_id: "", secret: "", env: "sandbox" })}>
            Set up
          </button>
        </p>
      )}
      {status && !status.configured && setup && (
        <form className="fin-setup" onSubmit={saveSetup}>
          <input value={setup.client_id} placeholder="client_id" required
            autoComplete="off" spellCheck={false}
            onChange={(e) => setSetup({ ...setup, client_id: e.target.value })} />
          <input type="password" value={setup.secret} required
            placeholder={`${setup.env} secret`} autoComplete="off"
            onChange={(e) => setSetup({ ...setup, secret: e.target.value })} />
          <div className="fin-setup-row">
            {["sandbox", "production"].map((env) => (
              <button type="button" key={env}
                className={`chip${setup.env === env ? " active" : ""}`}
                onClick={() => setSetup({ ...setup, env })}>
                {env}
              </button>
            ))}
            <span className="fin-setup-spacer" />
            <button type="button" className="chip" disabled={busy}
              onClick={() => setSetup(null)}>cancel</button>
            <button type="submit" className="chip" disabled={busy}>
              {busy ? "saving…" : "save"}
            </button>
          </div>
          <p className="fin-setup-hint">
            From the Plaid dashboard (Team settings → Keys). Stored in app.db,
            backend-side only.
          </p>
        </form>
      )}
      {status?.configured && items.length === 0 && (
        <p className="widget-empty">
          No bank connected.{" "}
          <button className="chip" disabled={busy} onClick={() => connect(null)}>
            {busy ? "connecting…" : "Connect bank"}
          </button>
        </p>
      )}
      {connected && (
        <>
          {reconnect && (
            <p className="fin-reconnect">
              <span className="dot attention" />
              {reconnect.institution || "Bank"}{" "}
              {reconnect.status === "needs_consent"
                ? "needs an extra permission"
                : "needs a reconnect"}
              <button className="chip" disabled={busy}
                onClick={() => connect(reconnect.item_id)}>
                {busy ? "reconnecting…" : "Reconnect"}
              </button>
            </p>
          )}
          {!balances && !error && <><div className="skel" /><div className="skel w60" /></>}
          {balances && balances.length === 0 && (
            <p className="widget-empty">Accounts syncing — refresh in a moment.</p>
          )}
          {balances && balances.length > 0 && (
            <ul className="fin-accounts" {...dragPin("finance · balances", balances)}>
              {balances.map((a) => (
                <li key={a.account_id} {...dragPin(`account · ${a.account}`, a)}>
                  <span className="fin-name" title={a.subtype || a.type || undefined}>
                    {a.account}
                  </span>
                  <span className="fin-amount">
                    {a.current == null ? "—" : fmtMoney(a.current, a.currency)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {/* Was a bare "spent this week $X" footer — a number with nothing
              to read it against, which is the whole complaint this rebuild
              answers. Now the card says whether today's outflow is unusual
              for you, and stays silent when it isn't. */}
          {spendToday != null && balances?.length > 0 && (
            // The week total rides the quiet line rather than sitting under it
            // in its own paragraph: on an ordinary day the card said "spent
            // today $0.00" and "spent this week $285.94" on two separate
            // lines, which is two lines spent on one unremarkable fact.
            <Lede hit={hit} readings={readings} label="money"
              tail={spendWeek != null ? `${fmtMoney(spendWeek, currency)} this week` : null} />
          )}
        </>
      )}
    </section>
  );
}
