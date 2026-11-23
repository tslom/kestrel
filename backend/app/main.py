"""FastAPI app. Run with:  uvicorn app.main:app --reload --port 8788"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .delegates import api as delegates_api
from . import (activity, brain, brief, claude_account, console, console_project, console_terminal, db,
               email_schedule, expenses,
               failures, files, indexer,
               leave, lists, people,
               looks, phone, schedules, snoozes, tiles, token_usage, trends, usage, visits,
               voice, watcher, widgets)
from .config import VAULT_PATH
from .localguard import LocalGuard
from .providers import api as providers_api
from .providers import poller
from .routes import attachments as attachment_routes
from .routes import calendar as calendar_routes
from .routes import cite as cite_routes
from .routes import email as email_routes
from .routes import extract as extract_routes
from .routes import oauth as oauth_routes
from .routes import places as places_routes
from .routes import plaid as plaid_routes
from .routes import spotify as spotify_routes
from .routes import status as status_routes
from .routes import food as food_routes
from .routes import study as study_routes

# Nothing configured logging before this, so the root logger had no handler
# and Python's last-resort one printed WARNING and up, bare: every log.info in
# app/ (brief's "brief ready", …) went nowhere. INFO for our own `app.*`
# loggers only — root stays at WARNING so third-party libraries (the voice
# stack's model downloads, httpx) don't start narrating every request.
# uvicorn's own loggers don't propagate to root, so nothing prints twice.
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")
logging.getLogger("app").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    watch_task = None
    try:
        info = indexer.rebuild()
        print(f"[index] {info['indexed_notes']} notes indexed from {VAULT_PATH}")
        watch_task = asyncio.create_task(watcher.watch_loop())
    except FileNotFoundError as e:
        print(f"[index] SKIPPED: {e}\n        Set VAULT_PATH and POST /api/reindex.")
    refresh_task = asyncio.create_task(poller.refresh_loop())
    send_task = asyncio.create_task(email_schedule.send_loop())
    # Fully merged branches that closed tabs and finished --worktree sessions
    # left behind, gone (console_worktree.sweep); in the background, since
    # it's a few git calls per branch per project.
    sweep_task = asyncio.create_task(asyncio.to_thread(console.sweep_orphans))
    def _swept(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        if t.exception():
            print(f"[worktrees] orphan sweep failed: {t.exception()!r}")
        elif t.result():
            print(f"[worktrees] swept merged orphans: {t.result()}")
    sweep_task.add_done_callback(_swept)
    # The phone door (app/phone.py): its own listener on 127.0.0.1:8791, and
    # only if Settings > Phone has it switched on.
    try:
        await phone.sync()
    except Exception as e:   # the door never keeps the app from starting
        print(f"[phone] door not opened: {e!r}")
    yield
    await phone.stop()
    send_task.cancel()
    refresh_task.cancel()
    if watch_task:
        watch_task.cancel()


app = FastAPI(title="Personal Dashboard", lifespan=lifespan)

# Vite dev server origin. Localhost-only app; tighten later if you deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: DNS-rebinding and cross-site simple requests (app/localguard.py).
app.add_middleware(LocalGuard)

# Durable record of any 500, which otherwise exists only in a /tmp log that
# gets cleaned. Body lives in failures.py so the suite tests this exact
# function rather than a copy of it. Also installs the HTTPException handler
# that lets a hand-raised 502 be logged with its reason, not just "HTTP 502".
failures.install(app)

app.include_router(failures.router)  # /api/failures: the desk reports here, the status panel reads
app.include_router(usage.router)  # /api/usage: which cards get looked at, so the ranking can be checked
app.include_router(token_usage.router)  # /api/token-usage: tokens and API-equivalent cost, off the transcripts
app.include_router(schedules.router)  # /api/schedules: prompts that open a tab at a time, once or on repeat
app.include_router(widgets.router)
app.include_router(activity.router)  # what the backend changed on its own, with undo
app.include_router(snoozes.router)  # plan view: an ask pushed out of "needs you" until a time
app.include_router(looks.router)  # look lab: verdicts on UI design variants (redesign rounds)
app.include_router(phone.router)  # Settings > Phone only; the phone itself talks to phone.door on :8791
app.include_router(console.router)
app.include_router(delegates_api.router)  # other models on errands: providers page, fan-out batches
app.include_router(console_terminal.router)
app.include_router(console_project.router)  # dev tabs: project.json scripts, a port per worktree, PR watch
app.include_router(claude_account.router)  # Settings: which Claude login, via the CLI; no tokens cross
app.include_router(voice.router)  # mic in / speech out for the console; audio never leaves the machine
app.include_router(email_routes.router)  # write actions; GET /api/email stays provider-generated
app.include_router(calendar_routes.router)  # ditto: writes only, reads are the calendar provider's
app.include_router(trends.router)  # internal series (mood/energy/habits); health series stay on /api/health
app.include_router(brief.router)  # daily brief: lazy per-day generation, DB-only
app.include_router(brain.router)  # what Kestrel knows about you: facts in app.db, pages in the vault
app.include_router(oauth_routes.router)  # Connect button; token values never leave the backend
app.include_router(plaid_routes.router)  # bank connect/reconnect; finance data stays on /api/finance
app.include_router(spotify_routes.router)  # PKCE connect; listening data stays on /api/spotify
app.include_router(spotify_routes.data_router)  # /api/spotify/summary for the MusicWidget
app.include_router(extract_routes.router)  # deliveries + travel; extraction runs inside email.refresh()
app.include_router(people.router)  # CRM-lite: merged emails/messages/events view; PeopleWidget/PeopleExpand dashboard
app.include_router(leave.router)  # §14 time-to-leave: live traffic-aware leave_by per located event
app.include_router(lists.router)  # debts / user lists / watchlist — app.db is truth, not the old markdown
app.include_router(expenses.router)  # claims to Dad: mark, export an .xlsx, see the wire land
app.include_router(status_routes.router)  # /api/status: auth + last refresh + freshness per source
app.include_router(food_routes.router)  # §25 food tracker: log, targets, USC menus, suggestions
app.include_router(study_routes.router)  # quiz/card answers posted by StudyBlock; coverage reads them
app.include_router(tiles.router)  # basemap PNGs, fetched once and cached; the map sheet's only image source
app.include_router(places_routes.router)  # naming a cluster the map drew; writes a vault note, which stays the truth
app.include_router(visits.router)  # which place each visit was — a cluster is not one place at campus density
app.include_router(cite_routes.router)   # /api/cite/{ref}: what a console sentence was read off
app.include_router(attachment_routes.router)  # a file chip's bytes, on demand; never stored
app.include_router(files.router)  # paths a reply names: resolve under ~, view in-app, or `open`/reveal
app.include_router(providers_api.router)


# /api/health belongs to the health provider (§3); the liveness probe is
# the conventional /api/healthz.
@app.get("/api/healthz")
def healthz(db_check: bool = Query(False, alias="db")):
    # `day` is the server's day_key — the frontend polls this on wake to
    # notice a rollover (laptop reopened the next morning) and reload its
    # widgets. It must come from db.day_key(), never the client's clock:
    # the 4am rollover is a backend rule and the caches are keyed by it.
    # `?db=1` also opens app.db, which runs any pending migration: the merge
    # guard's probe (merge_guard.py), since a server can answer while its
    # first connection would still fail.
    if db_check:
        try:
            with db.session() as con:
                con.execute("SELECT count(*) FROM schema_applied").fetchone()
        except Exception as e:
            raise HTTPException(503, f"app.db: {e!r}"[:400])
    return {"ok": True, "vault": str(VAULT_PATH), "day": db.day_key()}


# Serve the built frontend (npm run build -> frontend/dist) so the whole app
# is one process on one port for the desktop launcher (launch_app.py). Mounted
# last so every /api and /ws/* route above wins; the SPA has no client-side
# routing, so StaticFiles(html=True) serving index.html at "/" is enough.
# Skipped in dev, where Vite serves :5173 and proxies /api back here.
_DIST = Path(__file__).resolve().parents[1].parent / "frontend" / "dist"


class _Dist(StaticFiles):
    """dist minus the sourcemaps. The build writes them (hidden) for
    failures.py to read off disk; the page never references them, so
    there's no reason to hand them to anything that asks."""

    def lookup_path(self, path):
        if path.endswith(".map"):
            return "", None
        return super().lookup_path(path)


if _DIST.is_dir():
    app.mount("/", _Dist(directory=_DIST, html=True), name="frontend")
