# Lists (card `lists`), Watchlist (card `watchlist`), While you were away (card `activity`)

## Sub-features
- Lists: a chip per list, + list, rename, delete, add items (`Category: item` files a category), tick, remove.
- Watchlist: all / movies / shows filter, add as "Title (YYYY) — Service", mark watched, remove.
- Activity: what the backend filed on its own, each with undo and "file as…" re-file; "looks fine" marks all seen.

## How to get to it (user POV)
- No sheets. `⌘K Lists`, `⌘K Watchlist` and `⌘K While you were away` jump to `.row-line[data-card="lists"]`, `.row-line[data-card="watchlist"]`, `.row-line[data-card="activity"]`.
- Unseen activity also shows as an ask row (`.q[data-ask]`) with "Review" / "Looks fine".
- The New Features list is the Kestrel list here (app.db `lists` / `list_items`), not IDEAS.md.

## Driving it with Playwright
- Lists: `.list-chips .chip` (`.active`), `.chip-n`, `.chip.list-new`, `.widget-note`, `li.check-row`, `.check-name`, `.check-tag`, `button.habit-toggle` (tick), `button.check-drop` (remove), `.widget-add input`.
- Watchlist: `.widget-actions .chip` (all / movies / shows), `button.habit-toggle` (title "Mark watched").
- Activity: `ul.act-list`, `li.act-row` (`.unseen`, `.undone`), `.act-kind`, `.act-summary`, `.act-actions button`, `.act-kinds .act-chip`, `.act-err`.
- `GET /api/lists`, `GET /api/watchlist`, `GET /api/activity`.
- Proof: clicking a `.list-chips .chip` swaps the `li.check-row` items.

## Gotchas
- Remove (a list row, a watchlist row) and deleting a list are held for the undo toast (`src/lib/undo.js` holdDelete): the row or list leaves the screen at once and the DELETE is sent only when the toast expires, about 8s later. Undo sends nothing. Deleting a list cascades to its items. Ticking and mark-watched write at once with no undo. In a rig, `page.route` the non-GET `/api` calls, or the expiry deletes real data.
- **Activity "file as…" calls the model** (except "just keep"), and its undo reverts a real write already applied.
