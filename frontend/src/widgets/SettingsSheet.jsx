import { useCallback, useEffect, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { cachedDefault, cachedModels, modelChoices, modeKey, modelKey, NERD_KEY } from "../lib/consolePrefs.js";
import Schedules from "./Schedules.jsx";
import { ModelOptions } from "../console/ModelPicker.jsx";
import { AUTO_KEY } from "../lib/autoResume.js";
import Providers from "./Providers.jsx";
import PhoneAccess from "./PhoneAccess.jsx";
import { setThemeChoice, themeChoice } from "../lib/theme";
import { setAxis, setLayout, useLook } from "../lib/look.js";
import { AGENT_STYLES, LAYOUTS } from "../looks/index.js";

// Settings: the choices that are yours to make once and leave. Only things that
// really change something belong here, so it holds no config you could only
// break, and nothing a card already does better: Sources keeps the connections
// and the console bar keeps per-conversation controls.
//
// Preferences stay in localStorage as they always have, per browser; the Claude
// login belongs to the CLI and is only asked about (app/claude_account.py).

const POLL_MS = 1500;
const PLAN = { pro: "Pro", max: "Max", team: "Team", enterprise: "Enterprise" };
const SEGMENTS = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];
// "never ask" is left out on purpose: the console bar is the one place it can
// be turned on, because that's where its warning is.
const MODES = [["auto", "auto"], ["default", "ask"], ["plan", "plan"]];

const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });

function Section({ title, children }) {
  return <section className="set-sec"><h3>{title}</h3>{children}</section>;
}

function Row({ label, hint, children }) {
  return (
    <div className="set-row">
      <div className="set-what"><span>{label}</span>{hint && <small>{hint}</small>}</div>
      <div className="set-how">{children}</div>
    </div>
  );
}

function ClaudeAccount() {
  const [data, setData] = useState(null);
  const [asking, setAsking] = useState(false);   // the step before the browser opens
  const [email, setEmail] = useState("");
  const [error, setError] = useState(null);
  const load = useCallback(() => fetchJSON("/api/console/account").then(setData)
    .catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const state = data?.login.state;
  useEffect(() => {
    if (state !== "waiting") return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [state, load]);

  const start = () => {
    setError(null);
    postJSON("/api/console/account/login", { email: email.trim() || null })
      .then(() => { setAsking(false); setEmail(""); return load(); })
      .catch((e) => setError(e.message));
  };
  const cancel = () => postJSON("/api/console/account/login/cancel", {}).then(load);

  const a = data?.account;
  const failed = state === "failed" ? data.login.error : null;
  const note = error || failed || a?.error;
  return (
    <>
      <Row label={!data ? "Checking…" : a?.logged_in ? a.email || "Signed in" : "Not signed in"}
        hint={a?.logged_in ? [PLAN[a.plan] || a.plan, a.expires_at && `until ${fmtDay(a.expires_at)}`]
          .filter(Boolean).join(" · ") : undefined}>
        {state === "waiting" ? <button className="pbtn" onClick={cancel}>Cancel</button>
          : !asking && <button className="pbtn" disabled={!data} onClick={() => setAsking(true)}>
              {a?.logged_in ? "Switch account" : "Sign in"}
            </button>}
      </Row>
      {state === "waiting" && <p className="set-note">Waiting on the browser. Finish signing in there.</p>}
      {state === "done" && <p className="set-note good">Signed in. New tabs use this account; a conversation already open may carry on as the old one until its tab reconnects.</p>}
      {note && <p className="set-note bad">{note}</p>}
      {asking && (
        <div className="set-ask">
          <p>This is the login for all of Claude Code on this Mac, your terminal included, and everything then shares that account's usage limits.</p>
          <div className="set-ask-row">
            <input type="email" value={email} placeholder="Email, to fill in the sign-in page (optional)"
              aria-label="Email for the sign-in page" onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") start(); }} autoFocus />
            <button className="pbtn go" onClick={start}>Continue in browser</button>
            <button className="pbtn" onClick={() => { setAsking(false); setError(null); }}>Cancel</button>
          </div>
        </div>
      )}
    </>
  );
}

function ClaudeCode() {
  const [cli, setCli] = useState(null);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { fetchJSON("/api/console/cli").then(setCli).catch(() => setCli({})); }, []);
  const update = () => {
    setUpdating(true); setError(null);
    postJSON("/api/console/cli/update", {})
      .then((r) => { setCli(r); if (!r.ok) setError(r.output || "The update failed."); })
      .catch((e) => setError(e.message))
      .finally(() => setUpdating(false));
  };
  return (
    <>
      <Row label="Claude Code" hint={cli?.installed ? `${cli.installed} · ${cli.channel}` : "Not found"}>
        {cli?.update_available
          ? <button className="pbtn go" disabled={updating} onClick={update}>
              {updating ? "Updating…" : `Update to ${cli.latest}`}
            </button>
          : cli?.installed && <span className="set-value">Up to date</span>}
      </Row>
      {error && <p className="set-note bad">{error}</p>}
    </>
  );
}

function Stored({ storageKey, fallback, options }) {
  const [value, setValue] = useState(() => localStorage.getItem(storageKey) || fallback);
  const known = options.some(([v]) => v === value);
  return (
    <select className="set-select" value={value} onChange={(e) => {
      setValue(e.target.value);
      localStorage.setItem(storageKey, e.target.value);
    }}>
      {/* A choice made elsewhere, like "never ask", shows as what it is. */}
      {!known && <option value={value} disabled>{value === "bypass" ? "never ask" : value}</option>}
      {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

// A profile's model: what the CLI last reported for it (cached by the
// console), "default" named by what it resolves to there.
function StoredModel({ profile }) {
  const [value, setValue] = useState(() => localStorage.getItem(modelKey(profile)) || "default");
  const choices = modelChoices(cachedModels(profile), cachedDefault(profile), value);
  return (
    <select className="set-select" value={value} onChange={(e) => {
      setValue(e.target.value);
      localStorage.setItem(modelKey(profile), e.target.value);
    }}>
      <ModelOptions choices={choices} />
    </select>
  );
}

export default function SettingsSheet({ onOpen }) {
  const [theme, setTheme] = useState(themeChoice);
  const look = useLook();
  return (
    <div className="settings">
      <Section title="Claude account">
        <ClaudeAccount />
        <ClaudeCode />
      </Section>

      <Section title="Providers">
        <Providers />
      </Section>

      <Section title="Appearance">
        <Row label="Theme">
          <div className="set-seg" role="radiogroup" aria-label="Theme">
            {SEGMENTS.map(([value, label]) => (
              <button key={value} role="radio" aria-checked={theme === value}
                className={theme === value ? "on" : ""}
                onClick={() => { setThemeChoice(value); setTheme(value); }}>{label}</button>
            ))}
          </div>
        </Row>
        <Row label="Desk" hint="Rooms is the desk; Classic is the one before it (⌥⇧L flips)">
          <div className="set-seg" role="radiogroup" aria-label="Desk layout">
            {LAYOUTS.map((l) => (
              <button key={l.id} role="radio" aria-checked={look.layout === l.id}
                className={look.layout === l.id ? "on" : ""}
                onClick={() => setLayout(l.id)}>{l.name.split(" (")[0]}</button>
            ))}
          </div>
        </Row>
        <Row label="Agents on the desk" hint="What a working tab looks like as it moves to what it's touching">
          <div className="set-seg" role="radiogroup" aria-label="Agent visualizer">
            {AGENT_STYLES.map((t) => (
              <button key={t.id} role="radio" aria-checked={look.agents === t.id}
                className={look.agents === t.id ? "on" : ""} title={t.question}
                onClick={() => setAxis("agents", t.id)}>{t.name}</button>
            ))}
          </div>
        </Row>
      </Section>

      <Section title="New console tabs">
        <p className="set-note">Tabs already open keep what they have until they reconnect.</p>
        <Row label="Vault model"><StoredModel profile="general" /></Row>
        <Row label="Dev model"><StoredModel profile="dev" /></Row>
        <Row label="Vault permissions" hint="Set “never ask” from the console bar, where it warns you">
          <Stored storageKey={modeKey("general")} fallback="auto" options={MODES} />
        </Row>
      </Section>

      <Section title="Usage">
        <Row label="Resume tabs automatically when the limit resets"
          hint="A tab the limit cut off carries on a minute or two after the window reopens; a tab you stopped never does">
          <Stored storageKey={AUTO_KEY} fallback="1" options={[["1", "On"], ["0", "Off"]]} />
        </Row>
        <Row label="Stats for nerds"
          hint="Time to first token, tok/s and model calls on each reply's footer, from the next reply on">
          <Stored storageKey={NERD_KEY} fallback="0" options={[["1", "On"], ["0", "Off"]]} />
        </Row>
        <Row label="Model usage" hint="Tokens and API-equivalent cost by tab, model, mode and day; who spent the 5h window">
          <button className="pbtn" onClick={() => onOpen("tokens")}>Open</button>
        </Row>
      </Section>

      <Section title="Scheduled prompts">
        <Schedules />
      </Section>

      <Section title="Phone">
        <PhoneAccess />
      </Section>

      <Section title="Connections">
        <Row label="Sources" hint="Accounts, sync status and errors">
          <button className="pbtn" onClick={() => onOpen("status")}>Open</button>
        </Row>
      </Section>
    </div>
  );
}
