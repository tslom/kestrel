# "Needs you" list and side cards

## Sub-features
- Ask rows, sorted blocking → interrupted → limited → card asks → orphans; per-row actions, a "later" snooze picker (≤4 options), note pill.
- Settle hold: a row you act on stays put marked done ("until …") until the pointer leaves or 6s pass.
- Snoozed fold with "Bring back"; "today" and "quiet" bands (quiet fold remembered in localStorage `needs_quiet_open`).
- All-clear state, "N cleared today", put away / dock the column, a card stacked above the list, the Sources panel.

## How to get to it (user POV)
- The right column of the plan at ≥1200px. Below that, the strip's "need you" chip (`.needs-toggle`) opens it in the map's place.
- Keyboard (focus outside inputs and the console): j/k move, ↵ runs the first action, s then 1–4 snoozes, o opens, Esc backs out.
- A card: click its row (`.row-line[data-card="email"]`, say), or its map node.

## Driving it with Playwright
- Column: `.plan-side`, `.side-list`, `.side-card` (the open card, with `.side-x` to close and `.pbtn` "⤢ Full view" when it has a sheet), `.band-pill`.
- Asks: `.q` (`.blocking` / `.fix` / `.hot` / `.on` / `.kcur` / `.done`) with `[data-ask]` and `[data-card]`, `button.q-title`, `.q-acts .pbtn`, `.q-done`, `.q-later`, `.q-snooze`.
- Cards: `.row-line[data-card="calendar"]` (aria-current when open). The quiet band folds away, so a quiet card's row may not be in the DOM: open `.band-toggle` first.
- Folds and states: `.snz-line`, `.snz-row`, `.band-toggle`, `.all-clear`, `.keys-hint`, `.cleared`, `.count`; `.glance-chip.needs-toggle`.
- `GET/POST /api/snoozes`, `POST /api/snoozes/wake`.
- Proof: clicking `.row-line[data-card="habits"]` shows `.side-card` with `ul.habit-list`; `.side-x` closes it.

## Gotchas
- Rows come from live card data; count and order change between runs. Pick rows by `data-card` / `data-ask`, never by index.
- Action buttons have real effects: console Yes / No answers a live permission request, Resume sends a turn, snoozing writes app.db (Undo in the 8s toast wakes it).
- Blocking rows have no "later". Below 1200px × only closes the column and isn't remembered.
