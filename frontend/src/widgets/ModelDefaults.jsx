import { useState } from "react";
import { modelKey } from "../lib/consolePrefs.js";
import { recheckModels, setModelDefault, useModelsInfo } from "../lib/models.js";

// Settings → Models (app/model_catalog.py): per-mode/tier/provider defaults from the live lists. A
// default that's gone or unrunnable says what runs instead, here, rather than breaking a tab.

const MODE_NAMES = { general: "Vault tabs", study: "Study tabs", dev: "Dev tabs" };
const TIER_NAMES = { plan: "Plan", decide: "Decide", grunt: "Grunt" };
const PLAN = { pro: "Pro", max: "Max", team: "Team", enterprise: "Enterprise" };

function ago(at) {
  if (!at) return "never";
  const m = Math.round((Date.now() / 1000 - at) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m}m ago` : m < 48 * 60 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
}

const nameOf = (info, id) => (info.claude.models.find((m) => m.id === id)?.label) || id;

function ClaudeSelect({ row, info, onSet }) {
  const { models, families, tiers, blocked } = info.claude;
  const newest = (f) => models.find((m) => m.family === f);
  const value = row.value || "";
  const known = !value || [...tiers, ...families, ...models.map((m) => m.id)].includes(value);
  return (
    <select className="set-select" value={value} aria-label={`${row.label} model`}
      onChange={(e) => onSet(row, e.target.value)}>
      <option value="">built in ({row.builtin})</option>
      {!known && <option value={value}>{value} (gone)</option>}
      {row.kind === "mode" && (
        <optgroup label="tiers">
          {tiers.map((t) => <option key={t} value={t}>{t}</option>)}
        </optgroup>
      )}
      <optgroup label="newest of">
        {families.map((f) => (
          <option key={f} value={f}>
            {f} · {newest(f)?.label}{blocked[f] ? " (not on this account)" : ""}
          </option>
        ))}
      </optgroup>
      <optgroup label="pinned versions">
        {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
      </optgroup>
    </select>
  );
}

function ProviderSelect({ row, info, onSet }) {
  const list = info.providers[row.name]?.models || [];
  const [draft, setDraft] = useState(row.value || "");
  if (!list.length) {
    return (
      <input className="set-select" value={draft} placeholder="its own default" aria-label={`${row.label} model`}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { if (draft !== (row.value || "")) onSet(row, draft); }}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
    );
  }
  const value = row.value || "";
  return (
    <select className="set-select" value={value} aria-label={`${row.label} model`}
      onChange={(e) => onSet(row, e.target.value)}>
      <option value="">its own default</option>
      {value && !list.some((m) => m.id === value) && <option value={value}>{value} (gone)</option>}
      {list.map((m) => <option key={m.id} value={m.id}>{m.label && m.label !== m.id ? `${m.label} · ${m.id}` : m.id}</option>)}
    </select>
  );
}

function DefaultRow({ row, info, onSet }) {
  const label = row.kind === "mode" ? MODE_NAMES[row.name] || row.name
    : row.kind === "tier" ? TIER_NAMES[row.name] || row.name : row.label;
  const runs = row.runs ? (row.kind === "provider" ? row.runs : nameOf(info, row.runs)) : "its CLI's default";
  return (
    <>
      <div className="set-row">
        <div className="set-what">
          <span>{label}</span>
          <small>{row.hint ? `${row.hint} · ` : ""}runs {runs}</small>
        </div>
        <div className="set-how">
          {row.kind === "provider"
            ? <ProviderSelect key={row.value || ""} row={row} info={info} onSet={onSet} />
            : <ClaudeSelect row={row} info={info} onSet={onSet} />}
        </div>
      </div>
      {row.notes.map((n) => <p key={n} className="set-note warn">{n}</p>)}
    </>
  );
}

function Access({ info }) {
  const { accounts, families, active } = info.claude;
  if (!accounts.length) return null;
  return (
    <ul className="model-access" aria-label="What each Claude account may run">
      {accounts.map((a) => (
        <li key={a.uuid} className={a.uuid === active ? "on" : ""}>
          <span className="ma-who">{a.label.split("@")[0]}{a.plan ? ` · ${PLAN[a.plan] || a.plan}` : ""}</span>
          {families.map((f) => {
            const got = a.access[f];
            const state = !got ? "unknown" : got.ok ? "yes" : "no";
            return (
              <span key={f} className={`ma-fam ${state}`}
                title={!got ? "not checked yet" : got.ok ? `runs (${got.source}, ${ago(got.checked_at)})`
                  : `${got.reason} (${got.source === "turn" ? "a turn was refused" : "probe"}, ${ago(got.checked_at)})`}>
                {state === "yes" ? "✓" : state === "no" ? "✕" : "?"} {f}
              </span>
            );
          })}
        </li>
      ))}
    </ul>
  );
}

export default function ModelDefaults() {
  const info = useModelsInfo();
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);
  if (!info?.claude) return <p className="set-note">Loading the model lists…</p>;

  const onSet = (row, value) => {
    setError(null);
    setModelDefault(row.key, value)
      .then(() => {
        // The mode's default is what new tabs start on, so a pick this
        // browser remembered from a tab's model chip stops overriding it.
        if (row.kind === "mode") localStorage.setItem(modelKey(row.name), "default");
      })
      .catch((e) => setError(e.message));
  };
  const recheck = () => {
    setChecking(true); setError(null);
    recheckModels().catch((e) => setError(e.message)).finally(() => setChecking(false));
  };
  const group = (kind) => info.defaults.filter((r) => r.kind === kind)
    .map((r) => <DefaultRow key={r.key} row={r} info={info} onSet={onSet} />);
  const lists = [["Claude", info.claude.models.length, info.claude.fetched],
    ...Object.values(info.providers).map((p) => [p.label, p.models.length, p.fetched])];

  return (
    <>
      <Row label="What each account may run"
        hint="Found by asking each model for one token; rechecked weekly and when the account switches">
        <button className="pbtn" disabled={checking} onClick={recheck}>{checking ? "Checking…" : "Recheck now"}</button>
      </Row>
      <Access info={info} />
      {error && <p className="set-note bad">{error}</p>}
      <h4 className="set-sub">New tabs, by mode</h4>
      {group("mode")}
      <h4 className="set-sub">Tiers</h4>
      {group("tier")}
      <h4 className="set-sub">Other providers</h4>
      {group("provider")}
      <p className="set-note">
        Lists: {lists.map(([name, n, f]) => (f?.ok === 0 && !n ? `${name}: ${f.note}`
          : `${name}: ${n} model${n === 1 ? "" : "s"}, ${ago(f?.at)}`)).join(" · ")}.
        A tab's model chip still picks for that tab.
      </p>
    </>
  );
}

function Row({ label, hint, children }) {
  return (
    <div className="set-row">
      <div className="set-what"><span>{label}</span>{hint && <small>{hint}</small>}</div>
      <div className="set-how">{children}</div>
    </div>
  );
}
