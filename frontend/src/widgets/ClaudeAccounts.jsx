import { useState } from "react";
import { formatResetIn } from "../console/UsageChips.jsx";
import { pctOf, useAccounts, windowOf } from "../lib/claudeAccounts.js";

// Settings → Claude accounts (app/claude_accounts.py): usage, rules, one-click switch; the login
// stays in the Keychain, never here.

const PLAN = { pro: "Pro", max: "Max", team: "Team", enterprise: "Enterprise" };
const WINDOWS = [["five_hour", "5h"], ["seven_day", "Week"]];

export function Meter({ w, label, cap }) {
  const live = w && !(w.resets_at != null && w.resets_at * 1000 <= Date.now());
  const pct = live ? Math.min(100, w.percent) : 0;
  const over = cap != null && pct >= cap;
  return (
    <div className={`acct-meter${over ? " over" : ""}`}>
      <span className="acct-m-label">{label}</span>
      <span className="acct-m-track" aria-hidden="true">
        <span className="acct-m-fill" style={{ width: `${pct}%` }} />
        {cap != null && <span className="acct-m-cap" style={{ left: `${cap}%` }} title={`stops at ${cap}%`} />}
      </span>
      <span className="acct-m-pct">{w ? pctOf(w) : "—"}</span>
      <span className="acct-m-reset">{!w ? "no reading yet" : live ? formatResetIn(w.resets_at) || "" : "window reset"}</span>
    </div>
  );
}

// A number box that saves on blur or Enter; empty means none. A percent
// (1–100) unless `max` and `unit` say otherwise (credits for Codex accounts).
export function Cap({ value, onSave, label, max = 100, unit = "%", what = "ceiling, percent" }) {
  const [draft, setDraft] = useState(null);
  const shown = draft ?? (value == null ? "" : String(value));
  const save = () => {
    if (draft == null) return;
    const v = draft.trim() === "" ? null : Number(draft);
    setDraft(null);
    if (v !== value && (v == null || (v >= (max === 100 ? 1 : 0) && v <= max))) onSave(v);
  };
  return (
    <label className={`acct-cap${unit === "%" ? "" : " wide"}`}>
      {label}
      <input type="number" min={max === 100 ? 1 : 0} max={max} inputMode="numeric" placeholder="—" value={shown}
        aria-label={`${label} ${what}`}
        onChange={(e) => setDraft(e.target.value)} onBlur={save}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />{unit}
    </label>
  );
}

function Account({ a, first, last, onMove, act }) {
  const [forget, setForget] = useState(false);
  const [name, setName] = useState(null);
  const patch = (body) => act(`/${a.uuid}`, body, "PATCH");
  const saveName = () => {
    if (name != null && name.trim() && name.trim() !== a.label) patch({ label: name.trim() });
    setName(null);
  };
  return (
    <li className={`acct${a.active ? " active" : ""}${a.enabled ? "" : " off"}`}>
      <div className="acct-top">
        <input className="acct-name-in" value={name ?? a.label} aria-label="Account name"
          size={Math.min(28, Math.max(6, (name ?? a.label).length + 1))}
          onChange={(e) => setName(e.target.value)} onBlur={saveName}
          onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
        <small className="acct-sub">
          {[a.email !== a.label && a.email, PLAN[a.plan] || a.plan].filter(Boolean).join(" · ")}
        </small>
        <span className="acct-actions">
          <button className="pbtn icon" disabled={first} onClick={() => onMove(-1)}
            title="Higher priority" aria-label="Move up">↑</button>
          <button className="pbtn icon" disabled={last} onClick={() => onMove(1)}
            title="Lower priority" aria-label="Move down">↓</button>
          {a.active ? <span className="acct-badge">In use</span>
            : <button className="pbtn go" disabled={!!a.needs_login} onClick={() => act(`/${a.uuid}/use`)}
                title="Switch now; the rules stay off until you go back to auto">Use</button>}
        </span>
      </div>
      <div className="acct-meters">
        {WINDOWS.map(([k, label]) => (
          <Meter key={k} w={windowOf(a, k)} label={label}
            cap={k === "five_hour" ? a.five_hour_cap : a.weekly_cap} />
        ))}
      </div>
      <div className="acct-rules">
        <span className="acct-r-head">Stop at</span>
        <Cap label="5h" value={a.five_hour_cap} onSave={(v) => patch({ five_hour_cap: v })} />
        <Cap label="week" value={a.weekly_cap} onSave={(v) => patch({ weekly_cap: v })} />
        <label className="acct-check" title="Only take it when nobody else has used it in the current 5h window">
          <input type="checkbox" checked={a.idle_only} onChange={(e) => patch({ idle_only: e.target.checked })} />
          only when its 5h window is idle
        </label>
        <label className="acct-check">
          <input type="checkbox" checked={a.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
          on
        </label>
        {!a.active && (forget
          ? <button className="pbtn fixb" onClick={() => act(`/${a.uuid}`, {}, "DELETE")}
              onBlur={() => setForget(false)} autoFocus>Forget it?</button>
          : <button className="pbtn quiet" onClick={() => setForget(true)}>Forget</button>)}
      </div>
      {a.needs_login && <p className="set-note bad">Needs signing in again: {a.needs_login}.</p>}
      {!a.needs_login && a.why && <p className="set-note acct-why-note">Outside its rules: {a.why}.</p>}
    </li>
  );
}

export default function ClaudeAccounts() {
  const { data, error, act } = useAccounts();
  const accounts = data?.accounts || [];
  const move = (i, d) => {
    const order = accounts.map((a) => a.uuid);
    [order[i], order[i + d]] = [order[i + d], order[i]];
    act("/order", { uuids: order });
  };
  if (!data) return error ? <p className="set-note bad">{error}</p> : null;
  if (!accounts.length) return null;   // the sign-in row below saves the first one
  const held = data.mode === "hold";
  return (
    <div className="accts">
      <div className="acct-mode">
        <span>{held
          ? <>Held on <b>{data.active?.label}</b>, picked by hand. The rules are off.</>
          : "Auto: Kestrel uses the first account in this order that's inside its rules."}</span>
        {held && <button className="pbtn" onClick={() => act("/auto")}>Back to auto</button>}
      </div>
      {data.refusal && <p className="set-note bad">Stopped: {data.refusal}. Background work is paused and the console won't send until one frees up.</p>}
      {error && <p className="set-note bad">{error}</p>}
      <ol className="acct-list">
        {accounts.map((a, i) => (
          <Account key={a.uuid} a={a} first={i === 0} last={i === accounts.length - 1}
            onMove={(d) => move(i, d)} act={act} />
        ))}
      </ol>
      {data.events.length > 0 && (
        <details className="acct-log">
          <summary>Recent switches</summary>
          <ul>
            {data.events.map((e, i) => (
              <li key={i}><time>{new Date(e.at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</time> {e.text}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
