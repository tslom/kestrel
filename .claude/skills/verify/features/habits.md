# Habits (card `habits`, sheet `habits`)

## Sub-features
- Card: toggle today, a 7-day dot strip (click a past day to mark it), streak, add.
- Sheet: 7-day grid with clickable cells, rename, archive, delete, add.

## How to get to it (user POV)
- `⌘K Habits` opens the sheet; card is `.row-line[data-card="habits"]`.

## Driving it with Playwright
- Card: `ul.habit-list`, `button.habit-toggle` (`.done` when logged), `.habit-name`, `.habit-week`, `button.habit-dot` (`.done` / `.miss` / `.na`, `.is-today`; a 24px target around an 11px `i` mark), `.habit-streak` (`.hot` at ≥7).
- Sheet: `table.habit-grid`, `th.habit-grid-today`, `button.habit-grid-rename`, `button.habit-cell`, `form.habit-add`.
- `GET /api/habits/history?days=7`, `POST /api/habits/{id}/checkin`.
- Proof: the dot / cell class flips between `done` and `miss`.

## Gotchas
- Check-ins are real data. Card toggles get an undo toast; sheet toggles, renames and adds don't. Prefer reading over toggling; if you must toggle, toggle back and confirm the row in app.db.
- Archive and Delete use `window.confirm`; a dialog blocks Playwright until handled, and Delete erases history.
- Day keys are `db.day_key()` (4am rollover): near midnight "today" is yesterday on purpose.
- Closing the Food day and weighing in tick habits too ("Log food", "Weigh in").
