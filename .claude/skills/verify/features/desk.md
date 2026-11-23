# The rooms desk (default layout since 11-22)

## Sub-features
- Sidebar of rooms (Today, Plans, People, Money, Health, Places, Mind, then Map and Notes), badges counting each room's asks. Under 1100px it folds to an icon rail.
- A page per room: its asks inline above its cards (each card the same CardView the map and classic column draw), a card's pill reading "snoozed" when all its asks are snoozed.
- Today: the brief line, the console's own asks (approvals, interrupted or limit-stopped tabs, unmerged branches), every other ask grouped by room, then the room doors.
- Floating glass top bar: the day as a sentence (`src/plan/Rooms.jsx` TodaySentence), the glance chips, and the focus line (`src/plan/FocusLine.jsx`): the most urgent ask, its buttons, Later (snooze menu), › to step past it, "n of m". It reads in Today's order (`todayOrder` in Rooms.jsx): approvals, then cut-off tabs, then branches, then room by room.
- The Map room's map stops above the dock (`--dock-h`); the undo toast floats above the dock too.
- The console folds to a centred dock: its whole bar (tabs, state, action, Yes/No / Answer / Resume, usage, model, mode, ⋯, new tab, full screen, the open/fold chevron) over the composer, laid out in flow so a growing composer pushes the bar up. What doesn't fit folds into ⋯ by priority (`src/lib/barHost.js`). A tab a quit or the usage limit cut off shows "cut off" / "usage limit" with Resume on the bar. ⌘J or the chevron opens it as a right-hand column; under 860px the open console takes the page, and a sidebar room click folds it again. The Map room draws the map full-page.
- Sources and a left-behind branch's review open as a sheet over the page; a click on the scrim or Esc closes it.
- Agent visualizer (`src/plan/AgentLayer.jsx`): orbs (default), cursors or off, from Settings → Appearance or the Look lab.
- Classic, the desk before it, stays reachable: Settings → Appearance → Desk, the Look lab, or ⌥⇧L.

## How to get to it (user POV)
- Open the app: the rooms desk is the default. Click a room in the sidebar, or a door on Today.
- A map node, ⌘K card jump or an ask's "Open" lands on the card's room with the card ringed.
- Esc from a room goes back to Today.

## Driving it with Playwright
- Layout: `<html data-layout="hybrid">` (or `classic`), stored in localStorage `desk_layout` (unset means rooms).
- Sidebar: `.cx-nav`, `.cx-nav-item[data-room="plans"]` (any room id, `today`, `map`; `vault` opens Notes), `.cx-badge`.
- Pages: `.cx-room` (`.hy-today` on Today), `.cx-room-head`, `.cx-cards`, `.cx-card[data-card="tasks"]`, `.cx-card-head`, `.band-pill`.
- Asks: `.cx-asks`, `.cx-ask` (`[data-ask-card]`), `.cx-ask-acts .pbtn`, `.cx-later`; Today's `.hy-group`, doors `.cx-door[data-room-door="money"]`.
- Dock: `.plan-stage.peek > .stage-sheet` over `.prompt-bar`; `.st-fold` (aria-label "Open the conversation" / "Fold the conversation"); `--dock-h` on `.plan` is the dock's height, and a room's page pads its foot by it.
- Top bar: `.plan-top`, `.hy-sentence`, `.hy-focus` (`.blocking` / `.fix`), `.hy-focus-acts .pbtn`, `.hy-later` (role=menu), `[aria-label="Next ask"]`, `.hy-focus-count`.
- Sheet: `.plan.side-wide .plan-side`, `.hy-scrim`.
- Agents: `.agent-layer`, `.cx-agent` (`[data-node]`, `.orbs` / `.cursors`, `.flip` near the right edge), `.ag-tag`.
- Proof a room rendered: every `.cx-card` in `.cx-cards` has a non-zero rect and is visible in a screenshot (see Gotchas).

## Gotchas
- Playwright's WebKit Tabs only to text fields (Safari's default); press `Alt+Tab` to walk every control. The .app turns on `tabFocusesLinks` (`backend/desktop_shell.py`), so plain Tab reaches buttons there.
- WebKit doesn't focus a button on click, so a menu opened by a click takes focus itself (the Later menus do) or its keys go to the page.
- WebKit (the app's engine) paints nothing of a positioned inline box past the first CSS column: a `position` on `.cx-card` or anything inside it made three of Plans' five cards vanish while their rects looked fine. Check room pages in WebKit screenshots, not by DOM rects.
- The hidden Needs-you list's keyboard triage (j/k/Enter/s) is off on this desk; keys there would act on asks you can't see.
- The top bar's height is measured into `--top-h` on `.plan`; a page or console that starts under it uses `--under-top`.
- The focus line's Yes/No sit under the approval modal's scrim for the tab on screen (the modal answers it); they answer approvals from other tabs directly.
- Habit nudge, undo toast and ⌘K palette are the classic desk's own (`features/map.md`).
