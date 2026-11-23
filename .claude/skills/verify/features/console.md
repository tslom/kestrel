# Console (tabs, composer, status bar, ⌘K palette)

## Sub-features
- Tabbed sessions; every tab stays mounted, so a hidden tab keeps its socket.
- Tab menu (right-click a tab): pin, move, Later (snooze), settle, regenerate title; drag to reorder; snoozed/settled tabs behind the ☾ N button.
- `@` in the composer lists other tabs, files and past sessions; a tab or session attaches as a digest pill (also by ⌘-dragging a `.session-row`).
- ⌘Enter sends the prompt to a new background tab; typing at an approval card declines it with that text.
- Composer: send, Steer/Queue and Stop while busy (the ⋯ menu's Enter mid-turn setting picks; ⌥Enter does the other), the held list, `!` shell mode, history (Ctrl+R), formulas (Ctrl+M).
- Status bar: state, current tool, elapsed, "N failed", usage chips, view, timeline, full screen. Items fold into ⋯ by priority when room runs out.
- ⋯ menu (docs / resume / new / mcp), model + effort picker, mode picker for new tabs (general / dev + project / study), sessions panel.
- Ask and permission cards as a centred modal; limit banner with Resume.
- ⌘K command palette.
- Dev tab: `.kestrel/project.json` scripts as bar buttons (run in the tab's worktree on its own port), a preview button, and the watched PR's chip; ⋯ has watch PR / unwatch PR. PR news lands as a PR entry and wakes the tab. The view pane's Preview has back / forward / reload and recent addresses; its Tasks lists the script runs.
- Other models in the log: a delegate / fan-out row that opens to its brief, the shortlist card (framed previews, "use this one"), the "model changed" rule, and "retry with…" on a turn's last reply.

## How to get to it (user POV)
- It's the left of the plan view. ⌘J toggles read ↔ peek (folded to its bar), ⌘F full; clicking the bar toggles, double-click goes full, Esc leaves full.
- ⌥T new tab, ⌥W close, ⌥1–9 switch (match on `e.code`).
- ⌘K / Ctrl+K opens the palette; 2+ characters adds a Notes group.

## Driving it with Playwright
- Bar: `.status-bar` (`.waiting` / `.writing` / `.working` / `.cutoff` / `.error` / `.unread` / `.idle`), `.st-fold`, `.st-word`, `.st-link`, `.st-btn`, `[aria-label="Full screen"]`.
- Tabs: `.st-tabs`, `.st-tab` (role=tab, aria-selected), `.st-tab-pick`, `.st-tab-x`, `[aria-label="New tab"]`; panes `.console-pane` (`.tab-hidden` when not shown).
- Tab menu: right-click a `.st-tab` → `.st-tab-menu` with `.st-tm-item` rows (role=menuitem); Later opens `.st-tm-later` (`.pbtn` choices). Rest list: `.st-rest` → `.st-rest-menu`, rows `.st-rest-row` (click `.st-kid-pick` to bring one back). Menus portal to body and open upward when the bar is low; wait ~300ms for the fade before a screenshot.
- Log: `.console` (`.busy`), `.console-bar .dot` (`.on` = socket open), `.bar-more` (⋯; items role=menuitem), `.console-log`, `.turn`, `.entry.user`, `.entry.assistant`.
- Composer: `.composer textarea`, `[aria-label="Send"]` (disabled until connected and non-empty), `.btn.stop`, `[aria-label="New chat"]`; modal `.card-modal`.
- Pickers: `.model-chip`, `.mode-picker`, `.mp-mode`, `.mp-project`, `.sessions-panel`, `.session-search`, `.session-row`, `.usage-chip`, `.limit-banner`, `.limit-resume`.
- Palette: `.cmdk` (role=dialog), `.cmdk-input`, `.cmdk-item` (role=option; **runs on mousedown**, so press Enter rather than `click()`), `.cmdk-empty`.
- Other models: `.delegate-row` (`.dg-head` toggles `.dg-body`), `.shortlist` with `.sl-pick` (its `iframe`, `.sl-go`), `.model-mark`, `.retry-btn` (shown on `.turn` hover) opening `.retry-menu`. Seed them with a faked `/api/sessions/<id>/transcript`: text entries carrying `model`, tool entries named `mcp__vault__fan_out` / `mcp__vault__shortlist` / `mcp__vault__delegate` with `raw` and `result`. A real batch for the card comes from `runs.fan_out` with stand-in agents against a scratch `DASHBOARD_DATA_DIR`.
- `WS /ws/console`, `/api/console/usage`, `/api/sessions`.
- Dev bar: `.pb-row`, `.pb-script` (`.on` = running; pick by `title`, since labels hide under the bar's compaction), `.pb-preview`, `.pr-chip`, `.pr-note` in the log; ⋯ copy `.pb-menu`. Preview: `.vp-bar`, `.vp-nav`, `iframe.vp-frame`, `.vp-servers`; Tasks: `.vp-task`. `/api/console/project`, `/api/console/project/run`, `/api/console/project/stop`, `/api/console/pr`.
- Proof without spending a turn: `.console-bar .dot.on`, and ⌘K opens `.cmdk` with `.cmdk-item` rows matching the typed text.

## Gotchas
- Chip text is clipped ("fix the tab…"): match on its first words. A seeded tab with no `console_session:<id>` loses its title (see the scratch rig memory). Regenerate title calls Haiku: route `/api/sessions/*/title` to a fake.
- `@` rows: `.cmd-popup` `.cmd-row` (a tab or session row also carries an at- class by kind); pills `.pill`. The digest route is `/api/sessions/<id>/digest`; fake it while :8788 runs older code. A drop can be simulated by dispatching `drop` on `.composer` with the `application/x-vault-pin` payload.
- Held list: `.held-list`, `.held-row` (`.held-text` click to edit into `.held-edit`, `.held-btn` verbs, `.held-btn.word` = steer/send now; they show on hover). Steering ones are `.queued-row`. Setting: `select[aria-label="Enter mid-turn"]` in `.bar-more`. A fake socket has to answer `held_*` frames with `{type: "held", items}` as app/console_queue.py does.
- The approval modal's scrim covers the composer: click `.card-modal-later` before typing a redirect there.
- **Any send runs a real CLI turn and spends usage**; a stored session auto-resumes on load, and the limit banner can auto-resume (also a send). Verify with a scratch rig (memory: scratch console rig) or don't send.
- Inside a dev-tab turn :8788 is the old backend code; a 200 proves nothing about your change.
- A bar button may be folded into ⋯ (`data-cut`) at narrow widths; open `.bar-more` before concluding it's gone.
- Script buttons start real processes on the backend (they live until stopped or the tab's tree is released): stop what you start. `/api/console/pr` calls GitHub through `gh`; in a rig, `page.route` it with a fake snapshot (match `/api/console/pr(\?|$)`: a bare prefix match also catches `/api/console/project`). A reload polls a watched PR at once, which is how to test a wake without waiting 2 minutes.
- Esc inside `.console-pane` belongs to the console, not the plan.
- "retry with…" and the shortlist's "use this one" both send a message: a real turn outside a faked socket. A retry also sets the tab's own model.
