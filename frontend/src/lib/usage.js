// Which cards actually get looked at. Buffered client for /api/usage.
//
// Two rules make this cheap enough to leave on forever:
//
//   * **Nothing sends on its own.** Events land in a buffer and go out on a
//     timer or when the page hides — a desk that re-renders on every websocket
//     tick would otherwise POST hundreds of times an hour to say the same
//     seventeen cards are still there.
//   * **`seen` is deduped per (label, band) for the life of the tab.** It is an
//     impression, not a counter: React re-renders a card whenever anything
//     upstream changes, so counting renders would measure the refresh loop and
//     call it attention. Recording the band separately means a card that goes
//     quiet → needs during the day still registers the change.
//
// Like report.js this uses bare `fetch` and swallows everything. Telemetry
// that can break the desk is worse than no telemetry.
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
      // Most flushes happen precisely when the page is going away.
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* the desk does not care whether this worked */
  }
}

function push(kind, label, band = "") {
  if (!label) return;
  buffer.push({ kind, label: String(label).slice(0, 100), band, count: 1 });
  // A flood means something is wrong upstream; send early rather than sit on
  // a buffer that grows past what the endpoint will accept.
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
