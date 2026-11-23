# Settings (sheet `settings`), Look lab, theme

## Sub-features
- Settings: Claude account (sign in / switch / cancel), Claude Code version and update, theme (System / Light / Dark), desk layout (Rooms / Classic), agents on the desk (Orbs / Cursors / Off), model and permission selects for new tabs; Usage (auto-resume, Stats for nerds, Model usage → Open); Scheduled prompts (list, Pause / Resume, Delete, New scheduled prompt form); link to Sources.
- Providers: Claude, Gemini, ChatGPT (Codex) with installed / signed in / on-off, the install and sign-in commands when one isn't ready, "Check again".
- Stats for nerds: on, each reply's footer gains "Ns to first token · N tok/s · N calls" (from the next reply).
- Scheduled prompts: a due one opens as a new tab on its own (lib/schedules.js, once a minute and on load), starting on a brief that names the prompt and says if it's late.
- Look lab: choose a layout, pin A/B and flip, agent style and a session replay, map ground, colour, vote ("Like it" / "Not it" / "Just the note").
- Themes Kestrel made (console tool theme_set): each is a chip after the built-in colours in the lab's Colour row, with "Specimen" and "Remove it" under the row while one is on. Which one is on is the server's (`GET /api/looks/themes` → `active`), not localStorage.
- Theme specimen: `/#specimen/dark/<theme id or baseline>` renders `src/ui/ThemeSpecimen.jsx` instead of the desk: every colour role in use, no console, no cards. It is what theme_set screenshots.
- Palette theme toggle.

## How to get to it (user POV)
- `⌘K Settings`, `⌘K Look lab`, `⌘K Use the dark theme` (reads `⌘K Use the light theme` when already dark).
- On the classic desk `.look-chip` shows. ⌥⇧L flips A/B, ⌥⇧M steps the map ground, ⌥⇧A the agent style.

## Driving it with Playwright
- Settings: `.settings`, `section.set-sec`, `.set-row`, `.set-what`, `.set-how`, `.set-seg` (role=radiogroup, buttons role=radio, `.on`), `select.set-select`, `.set-note` (`.good` / `.bad`).
- Look lab: `.look-lab` (role=dialog), `.ll-x`, `.ll-list`, `button.ll-pick` (aria-checked), `.ll-ab button` (aria-pressed), `.ll-seg`, `.ll-axis`, `.ll-btns button`, `.ll-saved`.
- Theme: localStorage `theme` = `"dark"`, which sets `data-theme` on `<html>`. Look-lab state is `data-look` / `data-layout` / `data-map` / `data-agents` on `<html>`.
- Themes: `.ll-chips[aria-label="Colour"] button` (a theme's has `.ll-theme-swatch`), `button.ll-link` removes the one that's on. `data-look` is the theme's id (`theme-warm-dusk`), its CSS is in `style#look-themes`. Specimen: `.spec`, `.spec-swatches`; ready when `<html>` has `data-specimen="ready"`. The tool's own picture: `python -m app.webshot <loopback url> out.png` (off-screen WKWebView). To drive the tool without a model, call `console_tools.theme_set.handler({...})` under `DASHBOARD_DATA_DIR=/tmp/x BACKEND_PORT=<a scratch backend serving a fresh dist>`.
- Providers: `.prov` (one per provider, `data-provider`), `.prov-state` (`.ready`), `.prov-need` with `.prov-steps`; its on/off is a `.set-seg`. `GET /api/delegates/providers`.
- `GET /api/console/account`, `GET /api/console/cli`, `GET /api/looks`.
- Scheduled prompts: `.sched`, `.sched-row` (`.off` when paused), `.sched-mode`, `.sched-btns`, `form.sched-new`, `.sched-prompt`, `.sched-when`, `.sched-days` (buttons aria-pressed), `.set-input`. `GET/POST /api/schedules`, `GET /api/schedules/due`, `POST /api/schedules/{id}/claim|ran|enabled`, `DELETE /api/schedules/{id}`.
- Stats for nerds: localStorage `console_nerd_stats` = `"1"`; the footer text matches /tok\/s/.
- Proof for the runner: put a schedule's `next_at` in the past (in a DB copy), load the page, and watch `runs` go to 1 and a new `.st-tab` start on the brief.

## Gotchas
- Screenshots are judged in dark mode (CLAUDE.md, "Dark mode first"). Use `colorScheme: "dark"` on the context **and** `localStorage.theme = "dark"` before load: `setTheme` removes the key when the choice matches the system, so neither alone is reliable.
- A scheduled prompt that comes due opens a real tab and runs a real turn (model usage). Creating one in the rig writes app.db: use a DB copy, or delete it after.
- Picking any Colour chip while a theme is on writes `look_themes.active` in app.db, and "Remove it" deletes the theme's row. The specimen page never runs `requestAnimationFrame` work in the tool's screenshot (the window is never shown), so don't gate its ready flag on a frame.
- Votes are real app.db rows. "Continue in browser" changes the CLI login machine-wide. "Update to X" runs a real Claude Code update. Selects write localStorage at once. A provider's On/Off writes `delegate_providers` in app.db; put it back.
