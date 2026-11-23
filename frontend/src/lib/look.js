// Which look lab variants are on (looks/index.js), and the A/B pair being
// compared. Per browser, like the theme (lib/theme.js), as attributes on
// <html>: `data-look` (colour), `data-layout` (the desk: rooms, or classic),
// `data-map` (the map's ground) and `data-agents` (the agent visualizer).
// ⌥⇧L flips the A/B pair of layouts; ⌥⇧M steps the map grounds, ⌥⇧A the
// agent styles.
import { useEffect, useState } from "react";
import { fetchJSON, postJSON } from "./api.js";
import { AGENT_STYLES, GROUNDS, LAYOUTS, LOOKS, agentsById, groundById, layoutById,
         lookById } from "../looks/index.js";

const KEY = "look";
const PAIR = "look_pair_r6";   // a new key per round, so last round's pair doesn't hide this one's
// A new key when the default changed (rooms over classic): the old one
// still held whichever round-4/5 layout was last flipped to.
const LAYOUT = "desk_layout";
const GROUND = "map_ground";
const AGENTS = "look_agents";
export const LOOK_EVENT = "kestrel:look";

const read = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const write = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private mode */ } };
const announce = (detail) => window.dispatchEvent(new CustomEvent(LOOK_EVENT, { detail }));

// One attribute per axis; the first entry of each list is the default and
// leaves its attribute off, so the baseline CSS applies untouched. Except
// the layout, which is always set: the default (rooms) is the one with CSS.
function setAttr(name, id, dflt) {
  const root = document.documentElement;
  if (id === dflt) delete root.dataset[name]; else root.dataset[name] = id;
}

// Themes: colour looks Kestrel made when asked (backend app/looks.py, the
// console's theme_set tool). Data, unlike the looks above: role -> #RRGGBB
// per scheme, fetched from /api/looks/themes and written here as CSS on the
// role's token. Which one is on is the server's to say (`active`), so the one
// Kestrel just set is the one showing; the last answer is kept per browser so
// the next launch paints in it before the fetch lands.
const THEMES = "look_themes";
const HEX = /^#[0-9A-Fa-f]{6}$/;
const THEME_ID = /^theme-[a-z0-9-]+$/;
const TOKEN = /^--[a-z0-9-]+$/;
const NONE = { themes: [], active: null, roles: {} };
let themeState = NONE;

// The server's words are still checked before they become CSS: a value that
// isn't a hex, or a name that isn't a token, is dropped, not written.
function themeRules(t, roles) {
  const decls = (scheme) => Object.entries(t[scheme] || {})
    .filter(([role, v]) => TOKEN.test(roles[role] || "") && HEX.test(v))
    .map(([role, v]) => `${roles[role]}:${v};`).join("");
  // :root twice, so a theme outranks the base palettes whatever order the
  // sheets load in; each scheme scoped the way styles.css scopes its own.
  const sel = `:root:root[data-look="${t.id}"]`;
  const [dark, light] = [decls("dark"), decls("light")];
  return [
    dark && `${sel}[data-theme="dark"]{${dark}}`,
    dark && `@media (prefers-color-scheme: dark){${sel}:not([data-theme="light"]){${dark}}}`,
    light && `${sel}[data-theme="light"]{${light}}`,
    light && `@media (prefers-color-scheme: light){${sel}:not([data-theme="dark"]){${light}}}`,
  ].filter(Boolean).join("\n");
}

export function writeThemes(state) {
  const themes = (Array.isArray(state?.themes) ? state.themes : []).filter((t) => THEME_ID.test(t?.id || ""));
  themeState = { themes, roles: state?.roles || {},
                 active: themes.some((t) => t.id === state?.active) ? state.active : null };
  let sheet = document.getElementById("look-themes");
  if (!sheet) {
    sheet = document.createElement("style");
    sheet.id = "look-themes";
    document.head.appendChild(sheet);
  }
  sheet.textContent = themes.map((t) => themeRules(t, themeState.roles)).join("\n");
}

export const lookThemes = () => themeState.themes;
const themeById = (id) => themeState.themes.find((t) => t.id === id);
// A theme reads like a look everywhere a look is named (the lab, a vote).
export const anyLookById = (id) => {
  const t = themeById(id);
  return t ? { id: t.id, name: t.name, question: t.note || "A theme Kestrel made.", theme: true } : lookById(id);
};

function storeThemes() { write(THEMES, themeState.themes.length ? JSON.stringify(themeState) : null); }

// Before first paint, from what the last fetch said.
function restoreThemes() {
  try { writeThemes(JSON.parse(read(THEMES)) || NONE); } catch { writeThemes(NONE); }
}

// From the server: at load, and whenever a console turn sets a theme (the
// "looks" topic, wired in main.jsx).
export async function loadThemes() {
  const r = await fetchJSON("/api/looks/themes").catch(() => null);
  if (!r) return;   // offline or an old server: keep what's painted
  writeThemes(r);
  storeThemes();
  applyLook();
  announce(currentLook());
}

async function sendActive(id) {
  await postJSON("/api/looks/themes/active", { id }, "PUT").catch(() => {});
}

export async function removeTheme(id) {
  await fetchJSON(`/api/looks/themes/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
  await loadThemes();
}

export function currentLook() { return themeState.active || lookById(read(KEY) || "baseline").id; }
export function currentLayout() { return layoutById(read(LAYOUT) || LAYOUTS[0].id).id; }
export function currentGround() { return groundById(read(GROUND) || GROUNDS[0].id).id; }
export function currentAgents() { return agentsById(read(AGENTS) || AGENT_STYLES[0].id).id; }

// The stepped axes share one shape: a list, a key, an attribute.
const STEPPED = {
  agents: { list: AGENT_STYLES, key: AGENTS, current: currentAgents },
};
export function setAxis(axis, id) {
  const { list, key } = STEPPED[axis];
  const v = list.find((x) => x.id === id) || list[0];
  write(key, v.id === list[0].id ? null : v.id);
  applyLook();
  announce(v.id);
}
export function stepAxis(axis, by = 1) {
  const { list, current } = STEPPED[axis];
  const i = list.findIndex((x) => x.id === current());
  setAxis(axis, list[(i + by + list.length) % list.length].id);
}

export function applyLook() {
  if (themeState === NONE) restoreThemes();
  setAttr("look", currentLook(), "baseline");
  document.documentElement.dataset.layout = currentLayout();
  setAttr("map", currentGround(), GROUNDS[0].id);
  delete document.documentElement.dataset.needs;   // round 5's Needs-you takes, retired
  setAttr("agents", currentAgents(), AGENT_STYLES[0].id);
  delete document.documentElement.dataset.lookApple;   // round 1 only; clears a stale attribute
}

export function setLook(id) {
  // A theme goes on over whichever built-in look was picked, and comes off
  // back to it; picking a built-in look takes any theme off.
  const theme = themeById(id);
  if (theme || themeState.active) {
    themeState = { ...themeState, active: theme ? theme.id : null };
    storeThemes();
    sendActive(themeState.active);
  }
  if (!theme) {
    const look = lookById(id);
    write(KEY, look.id === "baseline" ? null : look.id);
  }
  applyLook();
  announce(currentLook());
}

export function setLayout(id) {
  const l = layoutById(id);
  write(LAYOUT, l.id === LAYOUTS[0].id ? null : l.id);
  applyLook();
  announce(l.id);
}

export function setGround(id) {
  const g = groundById(id);
  write(GROUND, g.id === GROUNDS[0].id ? null : g.id);
  applyLook();
  announce(g.id);
}

export function stepGround(by = 1) {
  const i = GROUNDS.findIndex((g) => g.id === currentGround());
  setGround(GROUNDS[(i + by + GROUNDS.length) % GROUNDS.length].id);
}

export function lookPair() {
  // A pair saved in an earlier round can name a retired variant; fall back
  // rather than flip between two things that both resolve to the default.
  const known = (id) => LAYOUTS.some((l) => l.id === id);
  try { const p = JSON.parse(read(PAIR)); if (Array.isArray(p) && p.length === 2 && p.every(known)) return p; } catch { /* unset */ }
  return ["hybrid", "classic"];   // the desk vs the one before it
}
export function setLookPair(pair) { write(PAIR, JSON.stringify(pair)); announce(); }

// A ⇄ B: whichever of the pair isn't showing; from outside the pair, A.
export function flipLook() {
  const [a, b] = lookPair();
  setLayout(currentLayout() === a ? b : a);
}

// Re-renders on any change to any axis.
export function useLook() {
  const [, bump] = useState(0);
  useEffect(() => {
    const on = () => bump((n) => n + 1);
    window.addEventListener(LOOK_EVENT, on);
    return () => window.removeEventListener(LOOK_EVENT, on);
  }, []);
  return { look: currentLook(), layout: currentLayout(), ground: currentGround(), pair: lookPair(),
           agents: currentAgents(), themes: lookThemes() };
}

export function installLookKeys() {
  window.addEventListener("keydown", (e) => {
    if (!e.altKey || !e.shiftKey || e.metaKey || e.ctrlKey) return;
    if (e.code === "KeyL") { e.preventDefault(); flipLook(); }
    else if (e.code === "KeyM") { e.preventDefault(); stepGround(1); }
    else if (e.code === "KeyA") { e.preventDefault(); stepAxis("agents", 1); }
  });
}

export { LOOKS };
