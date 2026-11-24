import { modelsInfo, refusedWhy } from "./models.js";

// The console's remembered choices per tab mode, here so Settings writes the keys the console reads.
export const modeKey = (profile) => (profile === "general" ? "console_mode" : `console_mode:${profile}`);
export const modelKey = (profile) => (profile === "general" ? "console_model" : `console_model:${profile}`);
export const effortKey = (profile) => `console_effort:${profile}`;

// Models the pickers offer: the CLI's list (session_info `models`) cached per profile so every picker has it
// before a session starts. Before any session has reported, the live catalog stands in (lib/models.js).
const modelsKey = (profile) => `console_models:${profile}`;
// What "default" resolves to per profile (console.default_model), from the socket's `mode` frame.
const defaultKey = (profile) => `console_default_model:${profile}`;

// Stats for nerds (app/console_stats.py): off unless Settings turns it on; read at turn end.
export const NERD_KEY = "console_nerd_stats";
export const nerdStats = () => {
  try { return localStorage.getItem(NERD_KEY) === "1"; } catch { return false; }
};
export function nerdBits(s) {
  const bits = [];
  if (s.ttft_ms != null) bits.push(`${(s.ttft_ms / 1000).toFixed(1)}s to first token`);
  if (s.tok_s != null) bits.push(`${Math.round(s.tok_s)} tok/s`);
  if (s.calls) bits.push(`${s.calls} call${s.calls === 1 ? "" : "s"}`);
  return bits;
}

function read(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}

export function cachedModels(profile) {
  // A profile no session has run in yet borrows another's: the CLI is the same.
  for (const p of [profile, "general", "dev", "study"]) {
    const list = read(modelsKey(p));
    if (Array.isArray(list) && list.length) return list;
  }
  return modelsInfo()?.claude?.choices || [];
}

export function rememberModels(profile, list) {
  if (!Array.isArray(list) || !list.length) return;
  try { localStorage.setItem(modelsKey(profile), JSON.stringify(list)); } catch { /* a nicety */ }
}

export function cachedDefault(profile) {
  try { return localStorage.getItem(defaultKey(profile)); } catch { return null; }
}

export function rememberDefault(profile, alias) {
  if (!alias) return;
  try { localStorage.setItem(defaultKey(profile), alias); } catch { /* a nicety */ }
}

// The CLI's "default" is its own, not the profile's (DEV_MODEL in a dev tab), so it isn't offered; pinned ids are the long tail.
export const isPinned = (m) => /^claude-/.test(m.value);

export function pickerModels(list) {
  const rows = (list || []).filter((m) => m.value && m.value !== "default");
  return { aliases: rows.filter((m) => !isPinned(m)), pinned: rows.filter(isPinned) };
}

// "claude-fable-5-2" → "Fable 5.2", for an id the list doesn't name.
export function prettyModel(id) {
  if (!id) return null;
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id);
  if (!m) return id;
  return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ""}`;
}

// The row for an alias or a resolved id, if the list has one.
export function findModel(list, key) {
  if (!key) return null;
  return (list || []).find((m) => m.value === key && m.value !== "default")
    || (list || []).find((m) => m.resolved === key && m.value !== "default") || null;
}

// What a choice is called in a picker: "fable · Fable 5.2", and "default"
// as what it resolves to for this profile ("default · Opus 5.5").
export function modelLabel(list, value, defaultAlias = null) {
  if (value === "default") {
    const d = defaultAlias && findModel(list, defaultAlias);
    return `default${d?.label ? ` · ${d.label}` : defaultAlias ? ` · ${defaultAlias}` : ""}`;
  }
  const m = findModel(list, value);
  if (!m) return value;
  if (isPinned(m)) return m.label || prettyModel(m.value);
  return m.label ? `${m.value} · ${m.label}` : m.value;
}

// Picker choices as [value, label, refused]: "default", aliases, pinned apart. `refused` is why the account can't
// run it (lib/models.js; shown disabled). A stored value the list lacks is kept so a select shows what's chosen.
export function modelChoices(list, defaultAlias = null, current = null, models = null) {
  const { aliases, pinned } = pickerModels(list);
  const row = (m) => [m.value, modelLabel(list, m.value), refusedWhy(models, m)];
  const main = [["default", modelLabel(list, "default", defaultAlias), null], ...aliases.map(row)];
  const old = pinned.map(row);
  if (current && current !== "default" && ![...main, ...old].some(([v]) => v === current))
    main.push([current, current]);
  return { main, pinned: old };
}

// Effort levels a model takes; [] when none or the list hasn't said.
export function effortsFor(list, value, defaultAlias = null) {
  const key = value === "default" ? defaultAlias : value;
  return findModel(list, key)?.efforts || [];
}

// Enter mid-turn (#92): "steer" (default) hands it to the CLI at the next tool boundary; "queue" holds it server-side (app/console_queue.py). ⌥Enter does the other. One setting for all tabs.
const ENTER_KEY = "console_enter_midturn";
export const ENTER_MODES = ["steer", "queue"];
export function readEnterMode() {
  try { return localStorage.getItem(ENTER_KEY) === "queue" ? "queue" : "steer"; } catch { return "steer"; }
}
export function writeEnterMode(mode) {
  try { localStorage.setItem(ENTER_KEY, mode === "queue" ? "queue" : "steer"); } catch { /* a nicety */ }
}
