# Health (card `health`, sheet `health`) and the day drill-down (`day:YYYY-MM-DD`)

## Sub-features
- Card: today's lede over Steps, Sleep, Resting HR, Zone min.
- Sheet bands: Latest (five heroes with ↑/↓% vs the rest of the range), Sleep (stacked deep / REM / light / awake), Steps, Steps this year (heatmap), Body (resting HR, HRV, active minutes).
- Clicking a sleep or steps bar opens that day as its own sheet (night + day detail).
- No metric chips here any more; they live in Trends (trends.md).

## How to get to it (user POV)
- `⌘K Health`; card is `.row-line[data-card="health"]`. Day sheets open only from a bar.

## Driving it with Playwright
- `g.bar.pickable` (role=button, aria-label "Open <day>"; Enter / Space work too), `.bar-seg.seg-deep`, `.seg-rem`, `.seg-light`, `.seg-awake`, `.seg-steps`.
- Heatmap: `.heat svg`, `rect[data-day]` (classes heat-0 … heat-4).
- Body: `.series-row`, `.series-name`, `.series-now`, `.series-avg`. Card: `.lede`, `.lede-label`, `.lede-value`.
- `/api/health/summary`, `/api/health?range=…&metric=steps|sleep_hours|resting_hr|hrv|…` (default this week), `/api/health/night/{day}`, `/api/health/day/{day}`.
- Proof: Latest has `.hero` elements; clicking a `g.bar.pickable` adds a `.fv-tab` with a date title and a `.fv-body` whose data-sheet starts with "day:".

## Gotchas
- Fitbit via the Google Health API. A GET backfills never-fetched days live and writes `health_days` / `health_sync`: wide or custom ranges are slow and write the DB.
- Empty: "Nothing synced from Fitbit in this range." Sleep is filed under the wake-up date.
