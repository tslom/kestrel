# Trends (card `trends`, sheet `trends`)

## Sub-features
- Card: an energy lede.
- Sheet modes: **series** (range picker, metric chips, "+ N more" / "fewer", one sparkline per picked metric; defaults sleep hours, steps, energy), **patterns** (correlation pairs, window 90d / 1y / all, listening-only, include same-source), **sleep** (stage chart for this week / last week / this month).
- This is where the metric chips are: use it to exercise `metric=`.

## How to get to it (user POV)
- `⌘K Trends`; card is `.row-line[data-card="trends"]`.

## Driving it with Playwright
- Modes: `.mode-row .chip` (text series / patterns / sleep, `.active`).
- Metric chips: `.chip-row.metrics .chip` (text drops `health:` and turns `_` into spaces, e.g. "sleep hours"), `.chip-more`.
- Rows: `.trend-row`, `.trend-label`, `svg.spark` (aria-label "sparkline, min …"), `.spark-empty`.
- Patterns: `.patterns`, `.patterns-intro`, `.corr-row`, `.corr-feat`, `.corr-dir`, `.corr-r`, `.corr-tent`.
- Sleep: `.sleep-stages`, `rect.sleep-seg`, `.sleep-legend`.
- `/api/trends/metrics`, `/api/trends?range=&metric=`, `/api/trends/correlations?window=…`.
- Proof: three `.trend-row` by default; toggling a metric chip changes the count.

## Gotchas
- Own state, not `useSheetData`; the head's pin and refresh appear only in series mode.
- `health:*` metrics can trigger the live Fitbit backfill (health.md).
- Empty: "Pick a metric above.", "No significant patterns in this window yet…".
