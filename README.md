# Kestrel — v2

Localhost dashboard over a (fresh) Obsidian vault with an embedded Claude Code
console, DB-backed tasks & habits, session resume, and a generated-notes inbox.

## What it is

Kestrel is a personal operating system I built for myself: one local macOS app
that pulls my email, calendar, coursework, messages, money, location, health
and music into a single desk, with a Claude agent embedded in it that can read
all of it and act on it — triage mail, draft replies, move calendar events,
file notes, track who I owe a reply, plan around class.

- **Backend:** Python, FastAPI, SQLite (WAL) — about 30 provider modules
  (Gmail and Google Calendar over OAuth with PKCE, Plaid, Spotify, iMessage's
  local database, Brightspace, OwnTracks location) behind one cache-and-poll
  layer, plus a background "brain" that distils a fact store from the data.
- **Agent:** the Claude Agent SDK runs a multi-tab console. Tabs work in their
  own git worktrees, hand work to each other, and call ~80 custom tools.
  Writes go through a permission layer with a review card and an undo log.
  A tool-choice eval keeps the tool descriptions honest.
- **Frontend:** React + Vite, served into a native WKWebView window
  (pywebview). Includes a terminal (xterm.js), PDF viewer (pdf.js) and
  KaTeX/MathLive math.
- **Voice:** wake word, local speech-to-text (Parakeet on Apple's Metal) and
  local text-to-speech. Audio never leaves the machine.
- **Location relay:** a Cloudflare Worker + D1 queue the phone posts to, so
  the laptop never accepts inbound connections.
- **Engineering:** ~2,400 backend tests, lint hooks that block bad edits,
  and design docs (`DESIGN.md`) that record each decision and the evidence for
  it.

**Security model.** The server binds 127.0.0.1 only. A Host allowlist blocks
DNS rebinding, an Origin check blocks cross-site writes and WebSocket
hijacking, and CORS is limited to the app's own origins. OAuth tokens and
secrets live server-side in SQLite and never reach the browser. Attachments
are parsed in a resource-limited subprocess, and untrusted text reaching the
model is fenced.

It's built around one person's accounts and a Mac, so it isn't a turnkey
install. The sections below are the real setup notes.

**Which file to read.** `DESIGN.md` is the architecture and the rules — how it
works and why, kept current. `IDEAS.md` is what's open and a one-line log of
what shipped. `CLAUDE.md` is the short list of rules that break things when
violated. `docs/` holds how-to recipes you follow when doing a specific job
(`wake-word.md`); `archive/` holds the implementation specs for work that has
shipped — historical, deliberately not maintained, and cited only for the
evidence in them. Nothing in `archive/` should be treated as current.

## Prerequisites

- Python 3.10+, Node 18+
- Claude Code CLI installed and logged in with your subscription:
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude login
  ```
- **Unset `ANTHROPIC_API_KEY`** in the backend's shell — it silently overrides
  your subscription login (the console warns if it's set).
- **Usage chips need no setup, on macOS.** The backend asks
  `/api/oauth/usage` every five minutes using the credential Claude Code keeps
  in your login keychain (`security find-generic-password -s "Claude Code-
  credentials"`). It reads, never writes: the CLI owns that credential and
  rotates it every few hours, and because the token is re-read on each poll
  rather than cached, rotation is picked up automatically.

  Off macOS, or with the CLI logged out, there is nothing to read and the chips
  fall back to `~/.claude.json` — which only the interactive TUI refreshes, and
  which can sit frozen for days. Past a window's `resets_at` the percentage is
  void rather than merely old, so the chip renders blank instead of lying. A
  blank chip is the honest state, not a bug.

  `CLAUDE_CODE_OAUTH_TOKEN` overrides the keychain if you set it. **Do not
  point it at `claude setup-token` output** — those tokens lack the
  `user:profile` scope this endpoint requires and it answers

  ```
  403  OAuth token does not meet scope requirement user:profile
       required_scopes: ["user:profile"]
  ```

  which is exactly what the keychain fallback exists to avoid. The endpoint
  also rate-limits hard: test it with single requests, never a loop.
- **Scheduled wake (§21 alarms), once per machine.** Alarms reach your phone
  through iCloud and need nothing here, but keeping one *true* overnight means
  waking the laptop for a reconcile cycle — and `pmset schedule` requires root
  even for an admin user. Install the scoped rule:
  ```bash
  sudo install -m 0440 -o root -g wheel \
    backend/scripts/kestrel-wake.sudoers /etc/sudoers.d/kestrel-wake
  ```
  It grants exactly `/usr/bin/pmset schedule wake …` and `schedule cancel wake
  …` and nothing else. Replace `tslom` in it with your macOS username first,
  then check with `visudo -c -f`. Verify with
  `sudo -n -l | grep pmset`; remove with `sudo rm /etc/sudoers.d/kestrel-wake`.
  **This does not survive a migration to a new Mac** — `/etc/sudoers.d` isn't
  part of the repo and Migration Assistant won't bring it. Skipping it is safe
  and silent by design: wake fails soft, logs, and retries, so alarms still
  fire on time — they just stop being re-checked while the lid is closed. If
  you're chasing "why did my alarm fire for a flight that moved", start here.
**ActivityWatch is no longer a prerequisite** — §7 (screen time) was removed
2026-09-13; why, and the cleanup for an older checkout, are in
`archive/SCREEN_TIME.md`.

## Run

Terminal 1:
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
VAULT_PATH=/absolute/path/to/NEW/vault uvicorn app.main:app --reload --port 8788
```

Terminal 2:
```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:5173.

**Where errors go.** Run this way and tracebacks land in the terminal. Launched
via the desktop app (below), stdout/stderr append to
`/tmp/vault-dashboard-app.log` — that is the file to grep for a 500, plus
`[providers]`/`[alarms]`/`[wake]` cycle lines and `Started server process`
pairs that reveal a `--reload` storm. It is unrotated and `/tmp` gets cleaned,
so it is not a record: anything worth keeping is in the `failures` table and
`provider_events` in app.db, which survive both. Note the log used to be
`/private/tmp/backend.log`; that path no longer exists, and grepping it
returns nothing in a way that looks exactly like a clean log.

## Desktop app

The two terminals above are the *dev* setup (Vite HMR). For normal use there's
a double-clickable bundle — a native window, no browser, no terminal:

```bash
./scripts/make-app.sh          # generates "Kestrel.app"
```

`backend/launch_app.py` runs the backend in-process and serves the built
frontend, so the app owns the whole lifecycle:

- **Frontend rebuilds itself on launch** if anything under `frontend/src`
  (or `index.html` / `vite.config.js` / `package.json`) is newer than
  `frontend/dist`. You never have to remember to `npm run build` — a git pull,
  a branch switch and a hand edit all get picked up the same way. ~300ms when
  stale, a stat walk when it isn't.
- **Closing the window shuts down everything it started** — uvicorn, the
  poller, and the `claude` CLI sessions the console spawns (grandchildren that
  used to survive the window; the launcher takes its own process group and
  sweeps it).
- **If the port is already busy it attaches instead of racing.** Running the
  app while a dev backend is up used to fail the bind *silently* and ride on
  the terminal's server. Now it says so, and closing the window leaves that
  backend alone.
- **It's a real app, not a Python process wearing an icon.** Menu bar with
  ⌘R / ⌘, / ⌘⇧L / ⌘Q (which runs the full shutdown, not `terminate:`), working
  ⌘C/⌘V on the page, and a window that reopens where you left it
  (`backend/data/window.json`).

Re-run `make-app.sh` when the repo moves, when node/claude change prefix, or
after `brew upgrade python@3.12`. It bakes in absolute paths and a PATH (a
Finder launch inherits neither), plus a copy of the Python framework's launcher
stub — macOS takes an app's identity from the executable that's actually
running, and the venv's `python` symlink resolves back into `Python.framework`,
which is why the app used to show up as "Python" with the generic rocket icon.
The stub is linked against an exact Homebrew Cellar path, so a Python upgrade
strands the copy; the run script probes it and falls back to the venv, logging
what happened, so the worst case is a launch that's named wrong rather than one
that doesn't happen.

The location listener is deliberately outside this: it's a `KeepAlive` launchd
agent, because location has to keep recording whether the window is open.
Logs: `/tmp/vault-dashboard-app.log`.

### Location ingest (§10)

OwnTracks (iOS, HTTP mode) → **location relay** (`relay/`, a Cloudflare
Worker) ← pulled each poller cycle by `providers/location_relay.py`. The phone
never needs the Mac awake or reachable; points queue at the relay until the
next pull, and nothing on the Mac is exposed. One-time setup:

1. `cd relay && wrangler login`, then `wrangler d1 create vault-location-relay`
   and paste the printed `database_id` into `relay/wrangler.toml`.
2. `wrangler d1 execute vault-location-relay --remote --file schema.sql`
3. Two secrets — generate each with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`:
   `wrangler secret put INGEST_TOKEN` (the phone's; can only add points) and
   `wrangler secret put PULL_TOKEN` (the backend's; reads and deletes).
4. `wrangler deploy` — note the `https://vault-location-relay.<you>.workers.dev` URL.
5. `backend/.env`: `LOCATION_RELAY_URL=<that URL>` and
   `LOCATION_RELAY_TOKEN=<PULL_TOKEN>`.
6. OwnTracks: Mode `HTTP`, URL `<that URL>/ingest`, authentication on, any
   UserID, password = `INGEST_TOKEN`. Locator mode **significant changes** —
   the mode iOS keeps alive across background eviction.

If location goes quiet, `location.refresh()` posts a macOS notification: once
per silence (6h away from home, a week at home — a stationary phone reports
nothing), and once a day if the relay refuses the pull. The relay's
last-contact time (any request from the phone) counts as the phone being
alive.

Two deploy-time traps. `wrangler dev` refuses a `compatibility_date` newer
than its bundled runtime, so the date in `wrangler.toml` trails today's. And
Cloudflare's edge 403s urllib's default `Python-urllib` User-Agent (error
1010) before the Worker runs — invisible locally, which is why
`location_relay` sends its own.

This replaced OwnTracks → Tailscale Funnel → a listener on :8790 (retired
2026-09-22 after going silently dark for four days); DESIGN.md → Security posture has
the history.

## What to try first

- Type a message → streamed output, tool chips, per-turn cost.
- `/daily-brief` or any of your slash commands — they load via setting_sources.
- Add a habit ("Exercise"), check it off — 4am rollover applies automatically.
- Add a task → watch `tasks.md` regenerate in the vault (read-only view).
- Ask the console to draft a note → it lands in `06-Generated/` with
  provenance frontmatter → appears in the Generated inbox widget → ✓ (reviewed)
  or ★ (adopt as curated).
- Refresh the page — the session resumes. "resume ▾" lists the same sessions
  terminal `/resume` sees.
- Switch the mode picker to "ask" and request a bash command — an Allow/Deny
  card appears. Switch to "plan" and ask for something multi-step — approve
  the plan card to start execution.

## Context providers

One pattern for external data (13 sources — calendar, health, email, finance,
weather, news, reddit, spotify, lastfm, messages, location,
attachments, features), exposed three ways over the same code; a background
poller keeps an app.db cache fresh:

```bash
curl "localhost:8788/api/weather?range=next-week&metric=tmax"   # widgets
python -m app.providers weather --range next-week               # skills/console
python -m app.providers --list                                  # sources & ranges
```

Ranges (`today` … `last-year`, `next-*` where a source has future data) all
resolve through the 4am rollover. Google OAuth is per account (`school` for
calendar + Gmail, `personal` for health); first-ever auth for an account runs
from the terminal with the downloaded client secret, and later re-auth
(Testing-mode refresh tokens expire after ~7 days) goes through the dashboard's
Connect button:

```bash
python -m app.providers.oauth authorize --account school --client-json client_secret.json
```

## Notes

- `backend/data/app.db` is real data (tasks/habits/OAuth tokens). The poller
  snapshots it into `backend/data/backups/` once a month and keeps twelve —
  nothing to schedule. For a copy right now (before a migration, say):
  `python scripts/backup_app_db.py --force`. Backups hold OAuth tokens, so
  they stay on this machine. `index.db` is a disposable cache.
- Skills writing to app.db from bash: `sqlite3 backend/data/app.db "PRAGMA busy_timeout=5000; ..."`
- Permission mode: `CONSOLE_PERMISSION_MODE` (default `auto` — writes run
  without a card, anything that can't be undone still asks, and every turn
  ends with a list of what it changed). `default` asks about everything;
  `plan` proposes first; `bypassPermissions` ("never ask" in the picker) asks
  about nothing at all, summary only.
