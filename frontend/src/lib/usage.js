// Which cards actually get looked at: buffered client for /api/usage. Nothing sends on its own (a buffer flushes
// on a timer or page hide), and `seen` is an impression deduped per (label, band) per tab, not a render count.
// Bare `fetch` that swallows everything, like report.js: telemetry must never break the desk.
import { batch } from "./timers.js";

const ENDPOINT = "/api/usage";
const FLUSH_MS = 15000;

let buffer = [];
const impressed = new Set();

const schedule = batch(flush, FLUSH_MS);

export function flush() {
  if (!buffer.length) return;
  const events = buffer;
  buffer = [];
  try {
    // eslint-disable-next-line no-restricted-syntax -- like report.js: telemetry must never break the desk (above)
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
      // Most flushes happen as the page goes away.
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* the desk does not care whether this worked */
  }
}

function push(kind, label, band = "") {
  if (!label) return;
  buffer.push({ kind, label: String(label).slice(0, 100), band, count: 1 });
  // A flood means something is wrong upstream: send early.
  if (buffer.length >= 100) flush();
  else schedule();
}

/** A card rendered on the desk in `band`. Once per band per tab. */
export function seen(label, band) {
  const key = `${label}${band}`;
  if (impressed.has(key)) return;
  impressed.add(key);
  push("seen", label, band);
}

/** You unfolded a card the desk had folded — the ranking being told it's wrong. */
export function opened(label) {
  push("open", label);
}

// Sheets are addressed by provider name, desk cards by slot id, and for one
// pair those differ. Without this the Music card and the sheet it opens land
// in two rows and the card reads as never touched.
const SHEET_ALIAS = { spotify: "music" };

/** A full sheet was opened, from the dock, a card, or ⌘K. */
export function sheetOpened(label) {
  push("sheet", SHEET_ALIAS[label] || label);
}

/** A ⌘K command was run. */
export function commandRun(id) {
  push("command", id);
}

export function installUsage() {
  // `pagehide` over `beforeunload`: it fires for bfcache navigations and on
  // iOS, where beforeunload frequently never runs at all.
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
}
