// Shaping for console context pills: pinning is a budget, not a copy (a pin ranges from 6 fields to 538KB of
// messages). Plumbing fields go; long lists become a head sample that says it's a sample (`omitted`), so the
// model never totals a quietly-truncated array. console.py's PILL_LIMIT is the floor under this.
// Design: DESIGN.md → `frontend/src/lib/pin.js`.

// Serialized chars per pill's row list; under PILL_LIMIT (20k) to leave room for wrapper keys.
const ROW_BUDGET = 12_000;

// Plumbing. `id` is deliberately NOT here: the console acts through it (POST /api/email/{id}/action).
const NOISE = new Set([
  "refreshed_at",   // every cached row carries it; the pill's own label dates it
  "guid",           // chat.db row identity, not addressable over the API
  "track_id", "artist_id", "artist_key",  // Spotify ids; `track`/`artist` are the readable half
  "logo_url",       // merchant art
  "continued",      // calendar render flag for multi-day spans
]);

/** Drop plumbing fields and empty values from one row. */
function slim(row) {
  if (!row || typeof row !== "object" || Array.isArray(row)) return row;
  const out = {};
  for (const [k, v] of Object.entries(row)) {
    if (NOISE.has(k)) continue;
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v) && v.length === 0) continue;
    // Shape message files here so a raw provider slice matches a hand-built pin.
    out[k] = k === "attachments" ? pinFiles(v) : v;
  }
  return out;
}

/** Head sample of `rows` fitting the budget, as `{ items, omitted }`. Byte-budgeted: row sizes differ by 10x. */
export function capRows(rows, budget = ROW_BUDGET) {
  if (!Array.isArray(rows)) return { items: rows, omitted: 0 };
  const items = [];
  let used = 0;
  for (const row of rows) {
    const shaped = slim(row);
    const cost = JSON.stringify(shaped)?.length ?? 0;
    if (items.length && used + cost > budget) break;
    items.push(shaped);
    used += cost;
  }
  return { items, omitted: rows.length - items.length };
}

/** A message's files for pins: name plus whether text was extracted (opt-in, `attachments --extract`), so the console never claims to have read a PDF it only sees the name of. */
export function pinFiles(items) {
  if (!Array.isArray(items) || items.length === 0) return undefined;
  return items.map((a) => ({
    id: a.id,                        // addressable: attachments --extract <id>
    filename: a.filename,
    mime: a.mime,
    text: a.status === "ok" ? "extracted" : "not extracted",
  }));
}
