import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh.js";
import "../console/delegates.css";

// Settings → Providers: the models Claude can send work to (backend
// app/delegates/registry.py). Each is a CLI on this machine; a row says
// whether it is installed, signed in and switched on, and when it isn't,
// the one command that gets it there. Signing in happens in a terminal: it
// is a browser flow the backend can't run for you.
function tokens(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

function Needs({ p }) {
  const steps = [
    { done: p.installed, text: <>Install it: <code>{p.install}</code></> },
    { done: p.signed_in, text: <>Sign in: {p.login.split("`").map((part, i) =>
      (i % 2 ? <code key={i}>{part}</code> : part))}</> },
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

function Provider({ p, onSwitch, saving }) {
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
      <p className="prov-facts">{[p.good_for, ...facts].filter(Boolean).join(" · ")}</p>
      {p.outside && !(p.installed && p.signed_in) && <Needs p={p} />}
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
  return (
    <>
      <p className="set-note">
        Claude runs everything and is the only model that sees your vault, messages, email or money.
        The others get a written brief and an empty folder, nothing else.
      </p>
      {error && <p className="set-note bad">{error}</p>}
      {(data?.providers || []).map((p) => (
        <Provider key={p.id} p={p} onSwitch={onSwitch} saving={saving} />
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
