# Morning brief (sheet `brief`), Weather (`weather`), USC sports (`athletics`), Deliveries & travel (card `travel`)

## Sub-features
- Brief: the day's generated summary, regenerate, a one-line `.brief-line` above the plan.
- Weather: day strip (past / today / forecast), hi / lo / rain, hero row; no card.
- USC sports: next games, per-season W/L strip and record; always `this-year`, no range picker.
- Travel card: packages in transit and trips, mark delivered, remove.

## How to get to it (user POV)
- `⌘K Morning brief`, or the "morning brief" `.side-link` on `.brief-line`.
- `⌘K Weather`, `⌘K USC sports`.
- `⌘K Deliveries & travel` jumps to `.row-line[data-card="travel"]` (no sheet).

## Driving it with Playwright
- Brief: `section.widget.brief`, `.ai-badge`, `.brief-body`, `.brief-cols h3`, `.brief-failed`; regenerate button has title "Regenerate today's brief". `/api/brief` (polled every 2.5s while generating).
- Weather: `.wx-days li` (`.today` / `.past`), `.wx-day`, `.wx-lo`, `.wx-hi`, `.wx-rain`, `.hero-row`. `/api/weather` with metrics, so it's the easy way to exercise metric chips.
- Sports: `.event-list`, `.sp-season`, `.sp-name`, `.sp-strip` (its `i` children are classed sp-w / sp-l from the result, built in a template string), `.sp-record`. `/api/athletics`.
- Travel: `.travel-list li` (`.soon`), `.travel-key`, `.travel-alert`, `.travel-drop`. `/api/packages`, `/api/trips`.
- Proof: brief shows `.brief-body`; weather shows `.wx-days li.today`.

## Gotchas
- First open of a day shows "Writing today's brief…" (`.widget-empty.pulse`) while Claude writes it. Regenerate spends model usage and overwrites the day's cached brief; don't click it to verify.
- Weather is live Open-Meteo (no auth); sports refetch the Sidearm iCal feed after a 2h TTL; off-season empty states are normal.
- Travel's delivered / remove buttons write app.db immediately; remove has no confirm.
