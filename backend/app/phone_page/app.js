// The phone door's page (app/phone.py). Plain script, no build: it is served
// as written, under a CSP that allows nothing but this origin's own files.
//
// What it does: pair (a code from Settings on the Mac), list the cards that
// are waiting, Allow / Deny, and turn on notifications. When the Mac can't be
// reached it says since when, shows the last cards it saw with their buttons
// off, and queues nothing.
const TOKEN = "kestrel_token";
const LAST = "kestrel_last";
const POLL_MS = 4000;

const $ = (id) => document.getElementById(id);
const show = (el, on) => { el.hidden = !on; };
let token = localStorage.getItem(TOKEN);
let reachable = true;
let timer = null;
const answered = new Map();   // card id -> "Allowed" | "Denied" | why it failed

const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const linked = () => new URLSearchParams(location.hash.slice(1)).get("card");
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

async function api(path, body) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method: body ? "POST" : "GET", headers, body: body && JSON.stringify(body), cache: "no-store" });
  let data = null;
  try { data = await r.json(); } catch { /* a plain-text refusal from the guard */ }
  if (!r.ok) { const e = new Error(data?.detail || `refused (${r.status})`); e.status = r.status; throw e; }
  return data;
}

// ---- pairing

async function pair(code) {
  show($("pair-error"), false);
  try {
    const got = await api("/api/pair", { code, name: /iPhone|iPad/.test(navigator.userAgent) ? "iPhone" : "Phone" });
    token = got.token;
    localStorage.setItem(TOKEN, token);
    history.replaceState(null, "", "/");
    start();
  } catch (e) {
    $("pair-error").textContent = e.status ? e.message : "Couldn't reach the Mac. Is it awake and on the tailnet?";
    show($("pair-error"), true);
  }
}

function showPairing() {
  clearInterval(timer);
  show($("cards"), false); show($("foot"), false); show($("pair"), true);
  show($("install-hint"), !standalone());
  $("state").textContent = "Not paired";
  $("state").className = "";
}

function unpair() {
  token = null;
  localStorage.removeItem(TOKEN);
  localStorage.removeItem(LAST);
  showPairing();
}

// ---- cards

function cardNode(c, live) {
  const li = document.createElement("li");
  li.className = `card${c.id === linked() ? " linked" : ""}`;
  const add = (cls, text, tag = "p") => {
    if (!text) return null;
    const el = document.createElement(tag);
    el.className = cls; el.textContent = text; li.append(el);
    return el;
  };
  add("card-tab", c.title);
  const what = add("card-what", c.detail || c.tool);
  if (what && c.count > 1) what.textContent += ` (+${c.count - 1} more)`;
  if (c.detail) add("card-tool", c.tool);
  add("card-why", c.why);
  add("card-warn", c.warning);
  const done = answered.get(c.id);
  if (done) { add("card-done", done); return li; }
  const btns = document.createElement("div");
  btns.className = "card-btns";
  for (const [label, allow] of [["Deny", false], ["Allow", true]]) {
    const b = document.createElement("button");
    b.textContent = label;
    if (allow) b.className = "allow";
    b.disabled = !live;
    b.addEventListener("click", () => answer(c, allow, btns));
    btns.append(b);
  }
  li.append(btns);
  return li;
}

async function answer(card, allow, btns) {
  btns.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}`, { allow });
    answered.set(card.id, allow ? "Allowed" : "Denied");
  } catch (e) {
    if (e.status === 401) return unpair();
    // 409: answered at the desk, or the turn ended. Anything without a
    // status never reached the Mac, and is not retried later.
    answered.set(card.id, e.status ? e.message : "Not sent: the Mac can't be reached.");
  }
  refresh();
}

function render(data, live) {
  const list = $("card-list");
  list.replaceChildren(...data.cards.map((c) => cardNode(c, live && data.can_answer)));
  show($("empty"), live && data.cards.length === 0);
  const off = $("unreachable");
  off.textContent = `Mac unreachable since ${clock(data.at)} (asleep or off the tailnet). Nothing is sent until it's back.`;
  show(off, !live);
  $("state").className = live ? "" : "off";
  $("state").textContent = !live ? "Unreachable"
    : data.cards.length ? `${data.cards.length} waiting`
      : data.working ? `${data.working} working` : "All quiet";
}

async function refresh() {
  try {
    const data = await api("/api/status");
    reachable = true;
    localStorage.setItem(LAST, JSON.stringify(data));
    for (const id of answered.keys()) if (!data.cards.some((c) => c.id === id)) answered.delete(id);
    render(data, true);
  } catch (e) {
    if (e.status === 401) return unpair();
    reachable = false;
    let last = null;
    try { last = JSON.parse(localStorage.getItem(LAST)); } catch { /* nothing kept */ }
    // A refusal with a status came from the Mac (a 403 from the guard):
    // reachable, but not letting us in. Say that instead of "asleep".
    if (e.status) { $("state").textContent = e.message; $("state").className = "off"; return; }
    render(last || { cards: [], at: new Date().toISOString(), working: 0 }, false);
  }
}

// ---- notifications

const keyBytes = (b64) => Uint8Array.from(atob(b64.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

async function notifyState() {
  const btn = $("notify"), note = $("notify-note");
  const say = (text) => { note.textContent = text; show(note, !!text); };
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    show(btn, false);
    return say(standalone() ? "This browser can't show notifications."
      : "For notifications: Share → Add to Home Screen, then open Kestrel from the Home Screen and pair there.");
  }
  if (Notification.permission === "denied") {
    show(btn, false);
    return say("Notifications are blocked for Kestrel. Allow them in Settings → Notifications → Kestrel.");
  }
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  show(btn, !sub);
  say(sub ? "Notifications are on: a card that waits while you're away from the Mac reaches this phone." : "");
}

async function enableNotifications() {
  const note = $("notify-note");
  try {
    if (await Notification.requestPermission() !== "granted") return notifyState();
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push/key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    await api("/api/push/subscribe", { subscription: sub.toJSON() });
  } catch (e) {
    note.textContent = `Couldn't turn notifications on: ${e.message}`;
    show(note, true);
    return;
  }
  notifyState();
}

// ---- start

function start() {
  if (!token) return showPairing();
  show($("pair"), false); show($("cards"), true); show($("foot"), true);
  refresh();
  clearInterval(timer);
  timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, POLL_MS);
  notifyState().catch(() => {});
}

$("pair-form").addEventListener("submit", (e) => { e.preventDefault(); pair($("pair-code").value); });
$("notify").addEventListener("click", enableNotifications);
$("forget").addEventListener("click", unpair);
document.addEventListener("visibilitychange", () => { if (token && document.visibilityState === "visible") refresh(); });
window.addEventListener("hashchange", () => { if (token) refresh(); });
window.addEventListener("online", () => { if (token && !reachable) refresh(); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

// A QR from Settings carries the code in the fragment, which no server sees.
const code = new URLSearchParams(location.hash.slice(1)).get("pair");
if (code && !token) pair(code); else start();
