import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh.js";
import "../console/delegates.css";
import ProviderAccounts from "./ProviderAccounts.jsx";

// Settings → Providers (app/delegates/registry.py): each row says whether a CLI is installed, signed
// in and on, and what gets it there. Codex and Antigravity sign in per account (ProviderAccounts).
// `login` is the alternative sign-in ways, with `code` in backticks.
function tokens(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

function withCode(text) {
  return text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

function SignIn({ ways }) {
  if (ways.length === 1) return <>Sign in: {withCode(ways[0])}</>;
  return (
    <>
      Sign in, either way:
      <ul className="prov-ways">
        {ways.map((w) => <li key={w}>{withCode(w)}</li>)}
      </ul>
    </>
  );
}

function Needs({ p }) {
  const steps = [
    { done: p.installed, text: <>Install it: <code>{p.install}</code></> },
    { done: p.signed_in, text: <SignIn ways={p.login} /> },
  ];
  return (
    <div className="prov-need">
      <ol className="prov-steps">
        {steps.map((s, i) => (
          <li key={i} className={s.done ? "done" : ""}>
            <span className="prov-step-n">{i + 1}</span><span>{s.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Provider({ p, onSwitch, saving, tags, act, onLogin }) {
  const state = p.ready ? "Ready" : !p.installed ? "Not installed"
    : !p.signed_in ? "Not signed in" : "Off";
  const used = p.usage;
  const facts = [
    p.version && `v${p.version.replace(/^[^\d]*/, "")}`,
    p.installed && p.auth_note,
    used && `${used.runs} run${used.runs === 1 ? "" : "s"} in 30 days`,
    used?.tokens_in + used?.tokens_out > 0 && `${tokens(used.tokens_in + used.tokens_out)} tokens`,
    used?.failed > 0 && `${used.failed} failed`,
  ].filter(Boolean);
  return (
    <div className="prov" data-provider={p.id}>
      <div className="prov-top">
        <span className="prov-name">{p.label}</span>
        <span className="prov-vendor">{p.vendor}</span>
        <span className={`prov-state${p.ready ? " ready" : ""}`}>
          <span className="prov-dot" aria-hidden="true" />{state}
        </span>
        {p.outside && p.installed && p.signed_in && (
          <div className="set-seg" role="radiogroup" aria-label={`${p.label} on or off`}>
            {[[true, "On"], [false, "Off"]].map(([value, label]) => (
              <button key={label} role="radio" aria-checked={p.enabled === value} disabled={saving}
                className={p.enabled === value ? "on" : ""}
                onClick={() => { if (p.enabled !== value) onSwitch(p.id, value); }}>{label}</button>
            ))}
          </div>
        )}
      </div>
      <p className="prov-facts">{facts.join(" · ")}</p>
      <ul className="prov-good">
        {p.good_for && <li><span className="prov-good-k">Good for</span> {p.good_for}</li>}
        {(p.best_at || []).map((b) => <li key={b}><span className="prov-good-k best">Better than Claude at</span> {b}</li>)}
      </ul>
      {p.outside && !p.installed && <Needs p={p} />}
      {p.outside && p.installed && !p.signed_in && !p.has_accounts && <Needs p={p} />}
      {p.has_accounts && <ProviderAccounts p={p} tags={tags} act={act} onLogin={onLogin} />}
    </div>
  );
}

export default function Providers() {
  const { data, setData, error, setError } = useWidgetData("/api/delegates/providers");
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  // `fresh`: past the backend's short cache, since you've just changed
  // something in a terminal.
  const recheck = () => {
    setBusy(true);
    fetchJSON("/api/delegates/providers?fresh=true")
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  const onSwitch = (id, enabled) => {
    setSaving(true);
    postJSON(`/api/delegates/providers/${id}`, { enabled })
      .then(setData).catch((e) => setError(e.message)).finally(() => setSaving(false));
  };
  // Account writes answer with the whole page, so the view never re-asks.
  const act = (url, body, method = "POST") =>
    postJSON(url, body ?? {}, method).then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message));
  // A sign-in's progress is polled per provider; once it lands, the account
  // list itself has changed, so the page is read again past the cache.
  const onLogin = (id, login) => {
    setData((d) => d && { ...d, providers: d.providers.map((p) => (p.id === id ? { ...p, signing_in: login } : p)) });
    if (login?.state === "done") recheck();
  };
  return (
    <>
      <p className="set-note">
        Claude runs everything and is the only model that sees your vault, messages, email or money.
        The others get a written brief and an empty folder, nothing else, and only two kinds of work:
        generic grunt work that would waste Claude&apos;s limits, and what they&apos;re plainly better at.
        Coursework, anything personal and a project&apos;s own ideas stay with Claude; the gate refuses them.
      </p>
      {error && <p className="set-note bad">{error}</p>}
      {(data?.providers || []).map((p) => (
        <Provider key={p.id} p={p} onSwitch={onSwitch} saving={saving}
          tags={data.tags || []} act={act} onLogin={onLogin} />
      ))}
      <div className="set-row">
        <div className="set-what"><small>Installed or signed in just now?</small></div>
        <div className="set-how">
          <button className="pbtn" disabled={busy} onClick={recheck}>{busy ? "Checking…" : "Check again"}</button>
        </div>
      </div>
    </>
  );
}
