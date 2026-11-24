import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useVisiblePoll } from "../lib/poll.js";
import { Cap, Meter } from "./ClaudeAccounts.jsx";

// An outside provider's accounts (Settings → Providers, app/delegates/accounts.py), shaped like
// Claude accounts. "Add account" runs the provider's sign-in in a new home and opens the browser
// (agy opens Google's itself); the callback lands on localhost. Device code is a fallback only.

const TAG_LABEL = { personal: "personal", learning: "learning", commercial: "client & commercial" };
const LOGIN_POLL_MS = 2000;

const num = (n) => Math.round(n).toLocaleString();

function Credits({ a, patch }) {
  const c = a.credits;
  const used = a.credits_used;
  let line;
  if (used != null) {
    line = <>{num(used)} of {num(a.monthly_credits)} credits used this month <small>(from the balance ChatGPT reports)</small></>;
  } else if (c?.unlimited) {
    line = "unlimited credits on this plan";
  } else if (c?.balance != null) {
    line = <>{num(c.balance)} credits left{!a.monthly_credits && <small> · set the monthly allowance to see what&apos;s used</small>}</>;
  } else {
    // USC's Edu workspace: the windows come back, the balance is null (the
    // 6,500 are a pool the school manages). Say so, and give Kestrel's count.
    const m = a.month;
    line = (
      <>
        {a.reading_error ? `No usage reading (${a.reading_error}).` : "ChatGPT doesn't report a credit balance for this workspace,"}
        {" "}so {a.monthly_credits ? `use against the ${num(a.monthly_credits)} can't be shown` : "credits can't be shown"}.
        {" "}Kestrel&apos;s own count this month: {m.runs} run{m.runs === 1 ? "" : "s"}
        {m.tokens_in > 0 && <>, {num(m.tokens_in)} tokens in ({num(m.tokens_cached)} cached), {num(m.tokens_out)} out</>}
        {m.models?.length > 0 && <> on {m.models.join(", ")}</>}.
      </>
    );
  }
  const pct = used != null && a.monthly_credits ? Math.min(100, (used / a.monthly_credits) * 100) : null;
  const capPct = a.credit_cap != null && a.monthly_credits ? Math.min(100, (a.credit_cap / a.monthly_credits) * 100) : null;
  return (
    <>
      {pct != null && <Meter w={{ percent: pct, resets_at: null }} label="Month" cap={capPct} />}
      <p className="prov-acct-line">{line}</p>
      <div className="acct-rules">
        <Cap label="Allowance" value={a.monthly_credits} max={1e7} unit=" credits/mo" what="credits a month"
          onSave={(v) => patch({ monthly_credits: v })} />
        <Cap label="Stop at" value={a.credit_cap} max={1e7} unit=" credits" what="credits used this month"
          onSave={(v) => patch({ credit_cap: v })} />
      </div>
      {a.credit_cap_inert && (
        <p className="set-note acct-why-note">The credit stop can&apos;t apply without a balance; the 5h and week windows still stop it (the window ceiling below).</p>
      )}
    </>
  );
}

function Account({ a, tags, first, last, onMove, act }) {
  const [forget, setForget] = useState(false);
  const [name, setName] = useState(null);
  const patch = (body) => act(`/api/delegates/accounts/${a.id}`, body, "PATCH");
  const saveName = () => {
    if (name != null && name.trim() && name.trim() !== a.label) patch({ label: name.trim() });
    setName(null);
  };
  const toggleTag = (t, on) => patch({ allowed_for: tags.filter((x) => (x === t ? on : a.allowed_for.includes(x))) });
  const sub = [a.email !== a.label && a.email, a.plan, a.school && "school account"].filter(Boolean).join(" · ");
  return (
    <li className={`acct${a.enabled ? "" : " off"}`} data-account={a.id}>
      <div className="acct-top">
        <input className="acct-name-in" value={name ?? a.label} aria-label="Account name"
          size={Math.min(28, Math.max(6, (name ?? a.label).length + 1))}
          onChange={(e) => setName(e.target.value)} onBlur={saveName}
          onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
        <small className="acct-sub">{sub}</small>
        <span className="acct-actions">
          <button className="pbtn icon" disabled={first} onClick={() => onMove(-1)}
            title="Tried first" aria-label="Move up">↑</button>
          <button className="pbtn icon" disabled={last} onClick={() => onMove(1)}
            title="Tried later" aria-label="Move down">↓</button>
          <span className={`prov-state${a.signed_in ? " ready" : ""}`}>
            <span className="prov-dot" aria-hidden="true" />{a.signed_in ? "Signed in" : "Signed out"}
          </span>
        </span>
      </div>
      {a.windows.length > 0 && (
        <div className="acct-meters">
          {a.windows.map((w) => <Meter key={w.key} w={w} label={w.label} cap={a.window_cap} />)}
        </div>
      )}
      {a.provider === "codex" && <Credits a={a} patch={patch} />}
      {a.provider !== "codex" && (
        <p className="prov-acct-line">
          {a.month.runs} run{a.month.runs === 1 ? "" : "s"} this month
          {a.month.tokens > 0 && `, ${num(a.month.tokens)} tokens`} · Google reports no usage, so this is Kestrel&apos;s own count
        </p>
      )}
      <div className="acct-rules">
        <span className="acct-r-head">Allowed for</span>
        {tags.map((t) => (
          <label key={t} className="acct-check">
            <input type="checkbox" checked={a.allowed_for.includes(t)} onChange={(e) => toggleTag(t, e.target.checked)} />
            {TAG_LABEL[t] || t}
          </label>
        ))}
      </div>
      <div className="acct-rules">
        {a.provider === "codex" && <>
          <span className="acct-r-head">Stop at</span>
          <Cap label="window" value={a.window_cap} onSave={(v) => patch({ window_cap: v })} />
        </>}
        <label className="acct-check">
          <input type="checkbox" checked={a.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
          on
        </label>
        {forget
          ? <button className="pbtn fixb" onClick={() => act(`/api/delegates/accounts/${a.id}`, {}, "DELETE")}
              onBlur={() => setForget(false)} autoFocus>Forget it and its sign-in?</button>
          : <button className="pbtn quiet" onClick={() => setForget(true)}>Forget</button>}
      </div>
      {a.school && !a.allowed_for.includes("commercial") && (
        <p className="set-note acct-why-note">School account: personal and learning work only, never client or commercial work. Coursework never goes to any outside model.</p>
      )}
      {a.why && <p className="set-note acct-why-note">Not taking work: {a.why}.</p>}
    </li>
  );
}

function SignIn({ p, onChange }) {
  const login = p.signing_in;
  const live = login && ["starting", "waiting", "checking"].includes(login.state);
  const [label, setLabel] = useState("");
  const [err, setErr] = useState(null);
  const [copied, setCopied] = useState(false);
  const base = `/api/delegates/providers/${p.id}/login`;
  const site = p.id === "codex" ? "ChatGPT" : "Google";
  const fallback = (p.sign_in_ways || []).slice(1);
  const refresh = () => fetchJSON(base).then((d) => onChange(d.login)).catch((e) => setErr(e.message));
  useVisiblePoll(refresh, live ? LOGIN_POLL_MS : null, { catchUp: true });
  const start = (way) => {
    setErr(null);
    setCopied(false);
    postJSON(base, { label: label.trim() || null, way: way || null }).then(onChange).catch((e) => setErr(e.message));
  };
  const reopen = () => postJSON(`${base}/reopen`, {}).then(onChange).catch((e) => setErr(e.message));
  const cancel = () => postJSON(base, {}, "DELETE").then(() => onChange(null)).catch((e) => setErr(e.message));
  const copy = () => navigator.clipboard?.writeText(login.code).then(() => setCopied(true));
  // Re-rendered by the 2s poll, which is what moves this count.
  const left = live ? Math.max(0, Math.round(login.limit_s - (Date.now() / 1000 - login.started))) : 0;
  const mins = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;

  if (!live) {
    return (
      <div className="prov-add">
        <input className="prov-add-name" placeholder="Name (optional)" value={label}
          aria-label={`New ${p.label} account's name`} onChange={(e) => setLabel(e.target.value)} />
        <button className="pbtn go" onClick={() => start()}>Add account</button>
        {fallback.map((w) => (
          <button key={w.id} className="pbtn quiet" onClick={() => start(w.id)}>{w.label}</button>
        ))}
        {login?.state === "done" && <small className="prov-ok">Signed in and saved.</small>}
        {login?.state === "failed" && login.error !== "cancelled" && (
          <small className="set-note bad">Didn&apos;t sign in: {login.error}</small>
        )}
        {err && <small className="set-note bad">{err}</small>}
      </div>
    );
  }
  const device = login.way === "device";
  return (
    <div className="prov-login" role="group" aria-label={`${p.label} sign-in`}>
      {!device && !login.opened && <p>Starting {p.label}&apos;s sign-in in a home of its own…</p>}
      {!device && login.opened && login.state !== "checking" && (
        <>
          <p className="prov-turn"><span className="prov-turn-dot" aria-hidden="true" />Your browser needs you now</p>
          <p>
            {site}&apos;s sign-in opened in your browser. Pick the account to add and allow access;
            this saves itself when {site} sends you back.
            {login.way === "browser" && login.url && (
              <> No tab? <button className="pbtn quiet" onClick={reopen}>Open it again</button>{" "}
                or <a href={login.url} target="_blank" rel="noreferrer">use this link</a>.</>
            )}
            {login.way === "acp" && <> Closed the tab? Cancel and add the account again.</>}
          </p>
        </>
      )}
      {login.state === "checking" && <p>Signed in. Checking the account&apos;s home…</p>}
      {device && (
        <ol className="prov-steps">
          <li><span className="prov-step-n">1</span><span>{login.url
            ? <>Open <a href={login.url} target="_blank" rel="noreferrer">{login.url.replace(/^https:\/\//, "")}</a> and sign in with the ChatGPT account to add.</>
            : "Starting…"}</span></li>
          <li><span className="prov-step-n">2</span><span>Enter this code there:{" "}
            <code className="prov-code">{login.code || "…"}</code>{" "}
            {login.code && <button className="pbtn quiet" onClick={copy}>{copied ? "Copied" : "Copy"}</button>}</span></li>
        </ol>
      )}
      {login.error && <p className="set-note bad">{login.error}</p>}
      <div className="prov-login-foot">
        <button className="pbtn quiet" onClick={cancel}>Cancel</button>
        <small className="prov-left">{mins} left</small>
        {err && <small className="set-note bad">{err}</small>}
      </div>
    </div>
  );
}

export default function ProviderAccounts({ p, tags, act, onLogin }) {
  const accounts = p.accounts || [];
  const move = (i, d) => {
    const ids = accounts.map((a) => a.id);
    [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
    act(`/api/delegates/providers/${p.id}/order`, { ids });
  };
  return (
    <div className="prov-accts">
      {accounts.length > 0 && (
        <ol className="acct-list">
          {accounts.map((a, i) => (
            <Account key={a.id} a={a} tags={tags} first={i === 0} last={i === accounts.length - 1}
              onMove={(d) => move(i, d)} act={act} />
          ))}
        </ol>
      )}
      {accounts.length > 1 && <p className="set-note">Each errand goes to the first account in this order that&apos;s on, signed in, allowed that kind of work and under its ceilings.</p>}
      {p.installed && <SignIn p={p} onChange={(login) => onLogin(p.id, login)} />}
    </div>
  );
}
