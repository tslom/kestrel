// Shaping for console context pills. Every pin button hands the console what the
// widget already had in hand — but "what the widget had" ranges from a 6-field
// health summary to 538KB of messages, and the big end of that is more than the
// window holds. So pinning is a budget, not a copy.
//
// Two rules, both about staying useful rather than merely small:
//   - Plumbing fields go. A row's guid/refreshed_at/logo_url can't be reasoned
//     about or acted on; they're pure token cost next to `text` and `contact_name`.
//   - Long lists become a head sample that *says* it's a sample. `omitted` in the
//     payload is the load-bearing part: a model handed a quietly-truncated array
//     will confidently total the wrong number, where one that can see the cut
//     says "of the 20 shown" or goes and queries the source.
//
// console.py's PILL_LIMIT is the floor under all of this. This module decides
// what's worth keeping; that one guarantees no pill can cost the session.

// Budget for one pill's row list, in serialized characters. Comfortably under
// PILL_LIMIT (20k) so the shaped payload still has room for its wrapper keys.
const ROW_BUDGET = 12_000;

// Identifiers and display-only URLs. `id` is deliberately NOT here — the console
// acts through it (POST /api/email/{id}/action), so it earns its bytes.
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
    // A message row carries its files already decorated; shape them here so a
    // raw provider slice gets the same treatment as a hand-built pin.
    out[k] = k === "attachments" ? pinFiles(v) : v;
  }
  return out;
}

/**
 * Head sample of `rows` that fits the budget, as `{ items, omitted }`.
 * Byte-budgeted rather than a fixed row count because rows differ by an order
 * of magnitude — 200 health readings cost less than 20 messages with bodies.
 */
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

/**
 * Files a message carried, for pins. Mirrors what the message log renders (the
 * name is the actionable part), plus the one thing the chip only implies: text
 * extraction is opt-in (`attachments --extract`), so an unextracted file is a
 * filename and nothing more. Saying which is which stops the console from
 * claiming it read a PDF it can only see the name of.
 */
export function pinFiles(items) {
  if (!Array.isArray(items) || items.length === 0) return undefined;
  return items.map((a) => ({
    id: a.id,                        // addressable: attachments --extract <id>
    filename: a.filename,
    mime: a.mime,
    text: a.status === "ok" ? "extracted" : "not extracted",
  }));
}
