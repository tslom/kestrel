// The live model lists, what the Claude account in use may run, and the
// defaults (backend app/model_catalog.py, /api/models). One copy for every
// picker, read through useSyncExternalStore; it asks the backend when the
// first reader appears and again whenever something says the account may
// have changed (a console's `mode` frame, a Settings write). The last answer
// is kept in localStorage so a picker has it before the first request lands.
import { useSyncExternalStore } from "react";
import { fetchJSON, postJSON } from "./api.js";

const URL = "/api/models";
const STORE_KEY = "models_info";
const listeners = new Set();
let info = null;
function stored() {
  if (info == null) {
    try { info = JSON.parse(localStorage.getItem(STORE_KEY)) || null; } catch { info = null; }
  }
  return info;
}
let asking = null;
let asked = false;

function take(d) {
  info = d;
  try { localStorage.setItem(STORE_KEY, JSON.stringify(d)); } catch { /* a nicety */ }
  listeners.forEach((f) => f());
  return d;
}

// Ask again (coalesced: one request in flight at a time).
export function refreshModels() {
  asked = true;
  if (!asking) asking = fetchJSON(URL).then(take).catch(() => info).finally(() => { asking = null; });
  return asking;
}

function subscribe(f) {
  listeners.add(f);
  if (!asked) refreshModels();
  return () => listeners.delete(f);
}

export const useModelsInfo = () => useSyncExternalStore(subscribe, stored);
export const modelsInfo = stored;

// Every write answers with the whole state.
export const setModelDefault = (key, value) =>
  postJSON(`${URL}/defaults`, { key, value }, "PUT").then(take);
export const recheckModels = () => postJSON(`${URL}/refresh`, {}).then(take);

// "claude-opus-5-5" → "opus"; an alias is its own family.
export function familyOf(id) {
  if (!id) return null;
  const m = /^claude-([a-z]+)-\d/.exec(id);
  return m ? m[1] : String(id).replace(/\[.*\]$/, "").toLowerCase();
}

// Why the account in use can't run this picker row ({ value, resolved }),
// or null. A tier or "default" is never held back: the backend swaps it.
export function refusedWhy(data, row) {
  const blocked = data?.claude?.blocked;
  if (!blocked || !row || row.value === "default") return null;
  return blocked[familyOf(row.resolved || row.value)] || null;
}
