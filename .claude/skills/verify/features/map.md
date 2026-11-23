# Plan shell, map and full views

## Sub-features
- Map: one node per card (level, badges, sync ring). Click opens the card in the side column, double-click its full view. Kestrel hub focuses the console; agent dots switch tabs; key legend.
- Full view: a tab strip of open sheets with ← Back and a × per tab.
- View pane (Claude's view): seen / preview / terminal / files / plan / tasks.
- Timeline, orphan worktrees (merge / discard), undo toast (⌘Z, 8s), brief line, habit nudge.

## How to get to it (user POV)
- Open `http://localhost:<port>`: console, map, then "Needs you" on the right. Map nodes take Enter / Space.
- Status-bar "view" and "timeline" buttons.
- Esc unwinds: full console → newest full view → card.
- The default desk is the rooms desk (`features/desk.md`); everything here about the side column assumes classic (localStorage `desk_layout` = `classic`, or Settings → Appearance → Desk). The map, full views, view pane, undo toast and habit nudge are the same on both.

## Driving it with Playwright
- Root: `.plan` (`.full` / `.side-wide` / `.side-open` / `.side-hidden` / `.console-full`, plus `[data-cview]` in concept layouts), `.plan-stage` (`.read` / `.peek` / `.full`), `.stage-sheet`, `.prompt-bar`.
- Map: `.map-wrap`, `svg.map`, `g.node` (`.needs` / `.has` / `.quiet` / `.machine`), `g.node[data-card="calendar"]` etc., `.map-hub`, `.agent-dot`, `.key-btn`.
- Full view: `.full-view`, `.fv-back`, `.fv-tab` (role=tab, aria-selected), `.fv-x`, `.fv-body[data-sheet="calendar"]` (data-sheet is the sheet key; hidden ones carry `hidden`).
- Sheet chrome: `.expand-head`, `.expand-refreshed`, `button.pin`, `.range-tab` (aria-pressed), `section.band`, `.band-note`; states `.widget-empty`, `.widget-error`, `.skel`.
- Others: `.view-pane`, `.vp-tab`, `.timeline`, `.tl-row`, `.undo-toast`, `.undo-label`, `.undo-x`, `.brief-line`, `.brief-text`, `.habit-nudge`, `.orph`.
- Proof: after ⌘K → Enter, `.fv-body[data-sheet]` for that key is visible and `.fv-tab[aria-selected="true"]` reads its title.

## Gotchas
- Widths: under 1200px the side column is closed and the map is hidden while it's open. The map sits beside the console only when the work column is ≥800px (a container query): with the column closed that's a window ≥ ~832px; with a card open (column `min(520px, 42%)`) it stacks until ~1364px. Below 760px the column sits above the console.
- Brief line calls the model when the asks change (cached per day + ask set) and renders nothing until it answers.
- Habit nudge pops 1 min after load (at most every 3h, never 23:00–09:00) and can cover clicks; saving it writes check-ins. Dismiss it, don't save.
- Orphan merge / discard and view-pane Save change real git branches and files.
- On the rooms desk a card opens in its room (`.cx-card`) instead of the side column.
