---
name: verify
description: Build/launch/drive recipe for verifying vault-dashboard changes end-to-end (frontend GUI + backend API). Core harness here; per-sheet/card recipes in features/.
---

# Verifying vault-dashboard

This file is the harness: Launch, Doctor, Drive, Evidence, Cleanup. What each
sheet or card is, how a user reaches it and how to drive it lives in
`features/<name>.md` (index: `features/README.md`). Read the one for the
feature you changed before driving it.

Every selector, `data-card` id, palette title (written `⌘K Calendar`) and
`src/…` path written in a code span in these files is checked against
`frontend/src` by `backend/tests/test_verify_skill.py`. A class built in a
template string can't be checked, so name it in prose. When you rename a class, the test names
the feature file to fix. To mention something that no longer exists, leave it
out of a code span.

## Launch

- **From a dev-tab worktree** (the usual case): :8788 serves the *main*
  checkout's built `dist`, so it shows none of your frontend edits. Start Vite
  from your worktree on a spare port; it proxies `/api` and `/ws` to :8788:

  ```bash
  cd <worktree>/frontend && npx vite --port 5199 --strictPort > /tmp/kv-rig/vite.log 2>&1 &
  ```

  Writes work from the rig: the proxy keeps the page's Host, and
  `localguard.origin_ok` lets a page same-origin with a loopback proxy write.
  A 403 on a POST from :5199 is a regression, not the rig.

  Backend changes aren't live at all until merged; verify those with pytest,
  or start a backend from the worktree and point the rig at it:

  ```bash
  cd <worktree>/backend && env -u ANTHROPIC_API_KEY NOTIFY_ENABLED=0 ALARMS_DRY_RUN=1 WAKE_ENABLED=0 \
    .venv/bin/python -m uvicorn app.main:app --port 8799 --lifespan off > /tmp/kv-rig/be.log 2>&1 &
  cd <worktree>/frontend && KESTREL_BACKEND_PORT=8799 npx vite --port 5199 --strictPort > /tmp/kv-rig/vite.log 2>&1 &
  ```

  `--lifespan off` matters: it skips the pollers, the email send loop and
  the orphan-worktree sweep, which would otherwise run twice beside the
  .app's. It uses the real app.db, so undo any write you make.
- From the main checkout: backend (:8788) and Vite (:5173) may already be
  up. Cold-start commands are in CLAUDE.md ("Run"): `VAULT_PATH` set,
  `ANTHROPIC_API_KEY` unset.
- Ready means the plan rendered, not that the port answered: wait for
  `.plan` and `svg.map`, about 5s on a cold load.

## Doctor

One read-only pass before the first drive and after any failed one:

```bash
lsof -nP -iTCP:8788 -iTCP:5173 -iTCP:5199 -sTCP:LISTEN   # who owns which port
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5199/api/providers  # proxy → backend
curl -s http://localhost:8788/api/status | head -c 400  # sources: auth / stale
```

A `broken` source in `/api/status` (expired Google token, chat.db without
Full Disk Access, aw-server down after a reboot) explains an empty sheet
better than your change does. Check it before debugging.

## Drive

No Playwright in the repo. Install `playwright-core` in a scratch dir and
launch the installed Chrome (no browser download). Pixel and canvas work must
also be checked in WebKit (the .app is WKWebView; `webkit` is in
`~/Library/Caches/ms-playwright`).

```bash
mkdir -p /tmp/kv-rig && cd /tmp/kv-rig && npm init -y && npm i playwright-core
```

```js
import { chromium } from "playwright-core";
const browser = await chromium.launch({ channel: "chrome", headless: true });
const ctx = await browser.newContext({ colorScheme: "dark", viewport: { width: 1440, height: 900 } });
await ctx.addInitScript(() => localStorage.setItem("theme", "dark"));
const page = await ctx.newPage();
await page.goto("http://localhost:5199");
await page.waitForSelector("svg.map");
// open a sheet the way a user does: ⌘K, its palette title, Enter
await page.keyboard.press("Meta+k");
await page.fill(".cmdk-input", "Calendar");
await page.keyboard.press("Enter");          // items run on mousedown, so Enter, not click()
await page.waitForSelector('.fv-body[data-sheet="calendar"]');
await page.screenshot({ path: "/tmp/kv-rig/calendar.png" });
await browser.close();
process.exit(0);                              // or the call hangs to the 2-minute wall
```

Ways in (details per feature file):
- A sheet: ⌘K and its palette title. Titles are `DESK` in
  `src/widgets/cards.js` plus `LOOSE_PANELS` in `src/lib/palette.js`, not
  the sheet's tab title. `⌘K Tasks` opens a tab reading "All tasks", and
  `⌘K Sources & status` one reading "Sources". `.fv-back` closes them all.
- The default desk is the rooms desk (`features/desk.md`): a card is `.cx-card[data-card="email"]` on its room's page. The side-column journeys below are the classic desk's (localStorage `desk_layout` = `classic`).
- A card in the side column: click `.row-line[data-card="email"]` (any id
  in `DESK`). `.side-card .side-x` closes it, and `.side-card .pbtn` is its
  "Full view". Asks are `button.q-title`.
- Console: ⌘J read ↔ peek, ⌘F full (`features/console.md`).
- Under 1200px the side column is closed; `.needs-toggle` opens it.
  Breakpoints are in `features/map.md`.

Dark mode first (CLAUDE.md): `colorScheme: "dark"` and `theme=dark` in
localStorage, as above. `setTheme` drops the key when it matches the system,
so set both.

## Evidence

- Use the real user path (palette, clicks, keys), not a test-only setter or a
  direct fetch. A `.fv-body` that mounted proves less than the content in it.
  Each feature file names its proof.
- Check side effects where the change has them: the app.db row (`sqlite3
  -cmd '.timeout 5000' backend/data/app.db '…'`), the vault file, the API
  response. Data is real (symlinked from the main checkout), so prefer
  read-only journeys. Each feature file's Gotchas lists which buttons write,
  spend model usage, or reach Gmail, Messages or Obsidian.
- To review many screenshots cheaply, montage them into one page (base64
  `<img>`s in `page.setContent`; `file://` is blocked from about:blank). Judge
  them in dark mode.
- If a precondition is missing (no data, expired auth), the journey failed.
  Say why in the report; don't weaken the journey until it passes.

## Cleanup

- Kill what you started, by PID (`lsof -ti tcp:5199 | xargs kill`). Never
  kill by process name: :8788 and :5173 belong to the running app.
- Undo any write you made to exercise a feature (feature files say how), and
  say so in the report.
- Keep the screenshots; cleanup removes servers and scratch state, not
  evidence.

## Keeping the map true

- Changed a feature? Update its file in the same commit: sub-features, the way
  in, handles, gotchas. A new sheet or card gets a file and a line in
  `features/README.md`. `test_verify_skill.py` fails on a file missing from the
  index or missing one of the four sections.
- A drive that fails because the map is wrong (a renamed title, a moved
  button) is drift. Fix the file. A drive that fails because the app is
  broken is a product bug. Report it, don't paper over it in the map.
