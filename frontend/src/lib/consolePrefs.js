// The console's remembered choices, per tab mode. Here so Settings writes the
// same keys the console reads, rather than a copy that could drift.
export const modeKey = (profile) => (profile === "general" ? "console_mode" : `console_mode:${profile}`);
export const modelKey = (profile) => (profile === "general" ? "console_model" : `console_model:${profile}`);
export const effortKey = (profile) => `console_effort:${profile}`;

// The models the pickers offer. The CLI says which it has (session_info's
// `models`: { value, resolved, label, efforts }), and that list is cached per
// profile so every picker has it before a session starts — Settings, a new
// tab, a queued child. A model the CLI adds turns up with no code change.
// Before any session has ever reported, the static aliases stand in.
const modelsKey = (profile) => `console_models:${profile}`;
// What "default" means for a profile (console.default_model), from the
// socket's `mode` frame, so Settings can say what it resolves to.
const defaultKey = (profile) => `console_default_model:${profile}`;
export const FALLBACK_MODELS = ["sonnet", "opus", "haiku", "fable"]
  .map((value) => ({ value, resolved: null, label: null, efforts: [] }));

// Stats for nerds: time to first token, tok/s and model calls on each turn's
// footer (app/console_stats.py). Off unless Settings turns it on; read when a
// turn ends, so a toggle shows from the next reply.
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
  return FALLBACK_MODELS;
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

// The CLI's own "default" is its default, not the profile's ("default" here
// means DEV_MODEL in a dev tab), so it isn't offered as such. Pinned ids
// ("claude-opus-4-8") are the long tail behind the aliases.
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

// Every picker's choices, as [value, label] pairs: "default" first, then the
// aliases, and the pinned versions apart (an <optgroup>, a fold). A stored
// value the list doesn't have (an alias the CLI dropped) is kept, so a
// select still shows what's chosen.
export function modelChoices(list, defaultAlias = null, current = null) {
  const { aliases, pinned } = pickerModels(list);
  const main = [["default", modelLabel(list, "default", defaultAlias)],
                ...aliases.map((m) => [m.value, modelLabel(list, m.value)])];
  const old = pinned.map((m) => [m.value, modelLabel(list, m.value)]);
  if (current && current !== "default" && ![...main, ...old].some(([v]) => v === current))
    main.push([current, current]);
  return { main, pinned: old };
}

// The effort levels a model takes: [] when it has no effort setting, or
// when the list hasn't said (the fallback rows).
export function effortsFor(list, value, defaultAlias = null) {
  const key = value === "default" ? defaultAlias : value;
  return findModel(list, key)?.efforts || [];
}

// What Enter does while a turn runs (#92): "steer" hands the message to the
// CLI now, read at the next tool boundary (the old behaviour, the default);
// "queue" holds it server-side for its own turn when this one ends
// (app/console_queue.py). ⌥Enter does the other. One setting for every tab,
// read at the moment you press it.
const ENTER_KEY = "console_enter_midturn";
export const ENTER_MODES = ["steer", "queue"];
export function readEnterMode() {
  try { return localStorage.getItem(ENTER_KEY) === "queue" ? "queue" : "steer"; } catch { return "steer"; }
}
export function writeEnterMode(mode) {
  try { localStorage.setItem(ENTER_KEY, mode === "queue" ? "queue" : "steer"); } catch { /* a nicety */ }
}
