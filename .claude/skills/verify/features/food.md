# Food (card `food`, sheet `food`)

## Sub-features
- Card: energy and protein bars plus the 3 biggest nutrient gaps, "Next plate" with "ate it", quick-add search (also takes an 8–14 digit barcode), usuals chips, weigh-in, portion questions, close the day.
- Sheet tabs: today (± servings, delete, suggestions), menus (today / tomorrow, hall and meal chips, + to log), week (bars, coverage, weights), setup (profile form).

## How to get to it (user POV)
- `⌘K Food` opens the sheet; card is `.row-line[data-card="food"]`. The card's "set up" link opens the sheet too.

## Driving it with Playwright
- Card: `section.widget.food`, `ul.food-bars`, `li.food-bar`, `.food-bar-value`, `.food-bar-fill`, `.food-add input`, `ul.food-hits` (results select on **mousedown**), `.food-usuals .chip`, `.food-weigh input`, `.food-next .chip`, `.food-count`, `.food-foot .chip`, `ul.food-asks .chip`.
- Sheet: `.chip-row .chip` (today / menus / week / setup), `ul.food-entries`, `.food-step button`, `button.food-x`, `ul.food-menu-items`, `ul.food-plates .chip`, `ul.food-week-bars`, `label.food-field`, `.food-note`.
- `GET /api/food/day`, `/api/food/search?q=`, `/api/food/menus?day=`, `/api/food/week`.
- Proof (read-only): typing in `.food-add input` lists `ul.food-hits li`; the week tab draws `ul.food-week-bars`.

## Gotchas
- **Opening the menus tab can spend model usage**: `GET /api/food/menus` estimates any unestimated menu item via `agent.ask`, and the tab re-polls every 20s. Don't leave it open.
- Log / restore / repeat / plates get an undo toast; servings ±, close-day (also writes a habit check-in), weigh-in, portion answers and setup save don't.
- The sheet jumps to setup by itself when setup is needed and nothing is logged.
- Barcode lookup hits Open Food Facts.
