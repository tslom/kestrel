# Tasks (card `tasks`, sheet `tasks`)

## Sub-features
- Card: overdue, due today and a capped "available" pick, a stale-waiting line, check-off, quick-add (parses `@ 5pm` and shows a due hint).
- Sheet ("All tasks"): open / waiting / someday, sort-by-category and show-deferred toggles, click a title for details, done / drop / pin per row.

## How to get to it (user POV)
- `⌘K Tasks` opens the sheet (its tab reads "All tasks"). The card is `.row-line[data-card="tasks"]`; its `.widget-footer-btn` "N more in all tasks" opens the sheet too.

## Driving it with Playwright
- Card: `ul.task-list`, `button.check` (title "Mark done"), `.dot.overdue`, `.task-title`, `.pin-mini`, `.widget-add input`, `.task-add-hint`, `.task-stale`.
- Sheet: `.chip-row .chip` (toggles), `h3.widget-sub` (sections), `button.task-title-btn` (aria-expanded) → `.task-detail`, `.task-detail-fields`, `.task-meta`.
- `GET /api/tasks/today` (card), `GET /api/tasks?status=open|waiting|someday` (sheet), `POST /api/tasks`, `POST /api/tasks/{id}/status`.
- Proof (read-only): typing `foo @ 5pm` in the add box shows `.task-add-hint` without pressing Enter.

## Gotchas
- Tasks are real data in app.db. A card check-off has an 8s undo toast (`.undo-toast`) — but reopening also reopens the subtasks the close swept up. Done and Drop in the sheet, and adding a task, have no undo.
- To exercise a write, add a task titled e.g. "verify-skill scratch", then delete it with `sqlite3 -cmd '.timeout 5000' backend/data/app.db` (see CLAUDE.md), and say so in the report.
- `tasks.md` in the vault is generated from app.db; never check it as the source.
