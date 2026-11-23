# CLAUDE.md

Solo, localhost-only dashboard over an Obsidian vault with an embedded Claude
Code console. Architecture and rules: `DESIGN.md`. Current roadmap and build
order: `IDEAS.md` (update it as sections get done).

## Rules (violating these breaks the system)

- **One source of truth per datum.** Tasks/habits/checkins = `backend/data/app.db`.
  Notes = vault markdown. `tasks.md` and `index.db` are generated/derived —
  read-only, regenerable; never parse them back in or hand-edit them.
- **"Today" = `db.day_key()`** (local time, 4am rollover), in code and in
  skills.
- **sqlite3 CLI against app.db:** `sqlite3 -cmd '.timeout 5000'
  backend/data/app.db '…'` (WAL; the DB is shared with skills running in
  terminal Claude Code). `-cmd` covers dot-commands too, while a leading
  `PRAGMA busy_timeout=5000;` works only in front of SQL.
- **Localhost-only.** Backend binds 127.0.0.1, CORS allows only the Vite
  origin. OAuth tokens/secrets live backend-side (app.db) — never in frontend
  code or responses. **One exception, the phone door** (`app/phone.py`,
  DESIGN.md → Security posture): a second listener, still on 127.0.0.1
  (:8791), that the user's own `tailscale serve` proxies to. It serves its
  allowlist (`phone.DOOR_ROUTES`) and nothing else. Never add a route to it
  for convenience, never point Serve or anything else at :8788, and never
  run `tailscale serve`/`funnel` from a session: that command is the user's.
- **External data goes through `app/providers/`** — skills/console run
  `python -m app.providers <source> --range this-week`, never raw API calls.
  For reads, always try the provider CLI first (`--query foo` substring
  search, `--format brief` token-cheap output); claude.ai MCP connectors are
  only for writes (e.g. moving a calendar event) or data no provider covers.
  Provider cache tables in app.db are derived (droppable); range tokens parse
  only in `providers/ranges.py`.
- Agent-created vault notes go in `06-Generated/` (a PostToolUse hook stamps
  provenance frontmatter).

## Tool use

Measured over the 20 sessions before 2026-08-31: half the context spend and
19 of 59 failed tool calls came from these four habits.

- **Absolute paths in every Bash command.** The cwd between calls is not
  dependable — `cd backend && …` fails when cwd is already `backend/`, and a
  bare `app/providers/x.py` fails when it isn't. This was the #1 tool failure.
- **Edit/Write tool for project files, not `python - <<'PY'` patching or
  `sed -i`** — including under auto mode, whose "do the work through Bash"
  nudge this overrides. Heredoc patches carry ~2x the context per edit and are
  what the auto-mode classifier blocks.
- **Read the region, not the file.** `sed -n`/offset-limit for anything over
  ~300 lines — `DESIGN.md` (2.5k), `styles.css` (3.4k), `db.py` (2.8k),
  `console_tools.py` (2.4k), `console.py` (1.1k), `IDEAS.md` (730).
  Whole-file is fine below that. Don't re-read a file you
  just edited; the tools error if an edit didn't land.
- **Scratch node/jsdom/SDK scripts must exit**: end them with
  `process.exit(0)`. Otherwise the event loop stays alive and the call dies
  at the 2-minute wall after the assertions already printed. To bound a command, `perl -e 'alarm shift; exec @ARGV' 30 node x.mjs`.

## Run

- Backend: `cd backend && source .venv/bin/activate && VAULT_PATH=/abs/path/to/vault uvicorn app.main:app --reload --port 8788`
- Frontend: `cd frontend && npm run dev` → http://localhost:5173
- `ANTHROPIC_API_KEY` must be **unset** in the backend shell (it shadows the
  CLI subscription login).
- **A Kestrel dev tab works in its own worktree** (`.claude/worktrees/tab-…`,
  branch `kestrel/tab-…`; the start line says which). The .app serves the
  main checkout, so your edits aren't live until `worktree_merge` lands them
  on `main`; verify with pytest in your tree (the venv, `.env`,
  `backend/data` and `node_modules` are symlinked, so app.db is the real one;
  `.claude/hooks/session_start.py` links them in any worktree, however it was
  made). Once merged, the
  reloader (`launch_app.Backend`) holds the restart while any console turn
  is in flight: within that turn :8788 still serves the old code, so curl in
  a later turn. (A terminal `uvicorn --reload` has no such hold, so an edit
  there drops every console tab mid-turn. Tabs reconnect on their own.)
- §21's scheduled wake needs `/etc/sudoers.d/kestrel-wake` installed once per
  machine (`backend/scripts/kestrel-wake.sudoers`, README → Prerequisites). It
  fails soft when absent, so a missing rule looks like nothing at all — check
  it before debugging a stale alarm.

## Checks

Each enforced rule and what enforces it. A hook's blocking message means fix
it now; `pytest` runs the rest. An exception is a `noqa` / `eslint-disable`
with its reason, never a bare one. A rule elsewhere in this file that isn't
here is prose, so when a correction repeats one, make it a check and add the
row (the global `correct` skill does this). Destructive git from an agent
(force-push, `reset --hard`, `clean -f`, `branch -D`, checkout/restore
discards) is blocked by a machine-level hook outside this repo,
`~/.claude/hooks/git_guard.py` (self-test: `--test`).

| Rule | Enforced by |
|---|---|
| Lint every edit: pyflakes, the `date.today()` ban, ESLint errors, the comment rules below | `.claude/hooks/lint_on_edit.py`, `backend/tests/test_lint.py` |
| No cross-module `_private` use (allowlist is empty): make the name public in its module, or move the helper to a shared one | `backend/tests/test_private_imports.py` |
| No unnecessary effects: you-might-not-need-an-effect and exhaustive-deps are errors (derive in render, act in the handler, key the component, lift the state; a real sync says why in its disable). The useEffect count only shrinks: lower the baseline when you remove one | `frontend/eslint.config.js`, `frontend/lint-baseline.json` |
| No raw `setTimeout`/`clearTimeout` outside `lib/timers.js`: fix the ordering a delay papers over; a real wall-clock wait uses a named helper | `frontend/eslint.config.js` |
| The verify skill's selectors, card ids and palette titles exist in frontend/src; each feature file has its four sections | `backend/tests/test_verify_skill.py` |
| Every `# noqa` names a code that fires there and says why; every `eslint-disable` says `-- why` and disables something | `backend/ruff.toml` (PGH004, RUF100), `frontend/eslint.config.js`, `.claude/hooks/comment_checks.py`, `backend/tests/test_comments.py` |
| No new dated comments ("(MM-DD)", "MM-DD:"): history goes in the commit or DESIGN.md. Existing ones are a ratchet; the hook refuses an edit that adds one | `.claude/hooks/comment_checks.py`, `backend/tests/comments_baseline.json`, `backend/tests/test_comments.py` |
| No `timeout`/`gtimeout` (macOS has neither); no sqlite3 dot-command after SQL in one argument | `.claude/hooks/bash_check.py`, `backend/tests/test_bash_check.py` |
| Every worktree gets the venv, `.env`, data and node_modules | `.claude/hooks/session_start.py`, `backend/tests/test_session_start.py` |
| Only Claude sees personal data: a non-Claude agent gets a scrubbed brief, an allowlisted env and the seatbelt sandbox, through `delegates/boundary.launch` and nowhere else; the package imports nothing personal | `backend/tests/test_delegate_boundary.py` |
| The phone door binds 127.0.0.1, serves only `DOOR_ROUTES`, lets in only the owner's tailnet login plus a paired token, and refuses a card id that isn't open | `backend/tests/test_phone.py` |
| The public snapshot carries no real name, handle, home path, place or project/client name: names match across `_`/`-`/digits and in any case | `scripts/publish_public.py` (the gate, run before every sync), `backend/tests/test_publish_public.py` |
| Every enforcer named in this table exists | `backend/tests/test_claude_md.py` |
| Console tool descriptions stay in budget | `backend/tests/test_tool_eval.py` |

## Evals

- **Changed a console tool description, or added a tool?** Run
  `cd backend && .venv/bin/python -m evals.tool_choice.run --repeat 2` (Haiku,
  about 100k tokens a pass) and add a case for any new tool. It measures which tool
  gets picked, not how it's used, so don't cut a rule just because the score held.
  Baseline 11-22: 90 of 94, varying a point or two by run. `test_tool_eval.py` holds the size budget for free.
- **UI looks:** judging any look uses the global `ui-review`, with `/ui-pass`
  as Kestrel's layer. The reviewer loop runs only when asked, on a named view,
  three rounds at most.
- **Dark mode first (the user, 11-20):** render, screenshot and judge every UI change
  and Look lab variant in dark mode (Playwright `colorScheme: "dark"`); that's
  where he works. Design new looks dark-first; don't polish light variants until
  the app ships to other people.

## Git

- **Commit and push straight to `main`.** Solo repo, linear history — don't
  open a PR, don't ask which branch. This overrides the default "if on the
  default branch, branch first". In a Kestrel dev tab's worktree, "straight
  to main" means: commit on the tab's `kestrel/` branch, then call
  `worktree_merge` with `push: true` (rebase onto main, fast-forward, push).
  Never `git checkout main` or merge by hand in the main checkout.
- **Commit and push when the work is done and verified — don't ask first.**
  The bar is *verified*, not *written*: the backend suite passes (`cd backend
  && python -m pytest`), a frontend change was actually rendered (DESIGN.md,
  "On verification" — a passing vite build proves nothing), and the change was
  exercised against real data where that's possible.
- **Anything off means stop and report instead** — a failing or skipped test, a
  piece of the task left undone, a call you're unsure of, or an approach still
  under discussion. Hand it back rather than committing a half-answer. Same for
  destructive or credential-touching work, which still gets confirmed.
