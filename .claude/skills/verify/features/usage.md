# Desk usage (sheet `usage`)

## Sub-features
- Range chips week / month / all.
- Per-card table: active days / seen days, touches, last touch; idle rows greyed.
- A ⌘K command-runs table.

## How to get to it (user POV)
- `⌘K Desk usage`. No card.

## Driving it with Playwright
- `.usage-sheet`, `.chip-row .chip` (`.active`), `p.usage-lede`, `table.usage-table`, `tr.usage-idle`, `.usage-bands`, `.usage-active`, `.usage-seen`, `.usage-touch`, `.usage-last`, `h4.usage-head`.
- `GET /api/usage/summary?days=7|30|365`.
- Proof: switching chips changes the `table.usage-table` rows.

## Gotchas
- Read-only, but a verifier's own ⌘K runs and card opens are counted as telemetry and land in these numbers. Say so if you report usage figures.
