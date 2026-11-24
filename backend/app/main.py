"""FastAPI app. Run with:  uvicorn app.main:app --reload --port 8788"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .delegates import api as delegates_api
from . import (activity, brain, brief, claude_account, claude_accounts, console, console_project, console_terminal, db,
               email_schedule, expenses,
               failures, files, indexer,
               leave, lists, model_catalog, people,
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

# INFO for `app.*` only; root stays WARNING so third-party libraries (httpx,
# voice model downloads) stay quiet. Without this, log.info in app/ went nowhere.
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
    # Every saved Claude account's usage, and the rules that pick one.
    accounts_task = asyncio.create_task(claude_accounts.loop())
    # Each provider's live model list, and what each Claude account may run.
    models_task = asyncio.create_task(model_catalog.loop())
    # Sweep merged branches left by closed tabs (console_worktree.sweep); off-thread: git calls per branch.
    sweep_task = asyncio.create_task(asyncio.to_thread(console.sweep_orphans))
    def _swept(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        if t.exception():
            print(f"[worktrees] orphan sweep failed: {t.exception()!r}")
        elif t.result():
            print(f"[worktrees] swept merged orphans: {t.result()}")
    sweep_task.add_done_callback(_swept)
    # The phone door (app/phone.py): :8791, only if Settings > Phone is on.
    try:
        await phone.sync()
    except Exception as e:   # the door never keeps the app from starting
        print(f"[phone] door not opened: {e!r}")
    yield
    await phone.stop()
    send_task.cancel()
    accounts_task.cancel()
    models_task.cancel()
    refresh_task.cancel()
    if watch_task:
        watch_task.cancel()


app = FastAPI(title="Personal Dashboard", lifespan=lifespan)

# Vite dev server origin only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: DNS-rebinding and cross-site simple requests (app/localguard.py).
app.add_middleware(LocalGuard)

# Durable record of any 500, plus the HTTPException handler that logs a raised 502's reason.
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
app.include_router(claude_accounts.router)  # saved Claude accounts: switch, rules, usage for each
app.include_router(model_catalog.router)  # live model lists, per-account access, Settings → Models
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
app.include_router(leave.router)  # time-to-leave: live traffic-aware leave_by per located event
app.include_router(lists.router)  # debts / user lists / watchlist — app.db is truth, not the old markdown
app.include_router(expenses.router)  # claims to Dad: mark, export an .xlsx, see the wire land
app.include_router(status_routes.router)  # /api/status: auth + last refresh + freshness per source
app.include_router(food_routes.router)  # food tracker: log, targets, USC menus, suggestions
app.include_router(study_routes.router)  # quiz/card answers posted by StudyBlock; coverage reads them
app.include_router(tiles.router)  # basemap PNGs, fetched once and cached; the map sheet's only image source
app.include_router(places_routes.router)  # naming a cluster the map drew; writes a vault note, which stays the truth
app.include_router(visits.router)  # which place each visit was — a cluster is not one place at campus density
app.include_router(cite_routes.router)   # /api/cite/{ref}: what a console sentence was read off
app.include_router(attachment_routes.router)  # a file chip's bytes, on demand; never stored
app.include_router(files.router)  # paths a reply names: resolve under ~, view in-app, or `open`/reveal
app.include_router(providers_api.router)


# /api/health belongs to the health provider; liveness is /api/healthz.
@app.get("/api/healthz")
def healthz(db_check: bool = Query(False, alias="db")):
    # `day` must be db.day_key(), never the client's clock: the frontend polls it to notice
    # the 4am rollover. `?db=1` opens app.db (runs pending migrations): the merge guard's probe.
    if db_check:
        try:
            with db.session() as con:
                con.execute("SELECT count(*) FROM schema_applied").fetchone()
        except Exception as e:
            raise HTTPException(503, f"app.db: {e!r}"[:400])
    return {"ok": True, "vault": str(VAULT_PATH), "day": db.day_key()}


# Built frontend for the desktop launcher; mounted last so every /api and /ws/* route
# wins. Skipped in dev (Vite serves :5173).
_DIST = Path(__file__).resolve().parents[1].parent / "frontend" / "dist"


class _Dist(StaticFiles):
    """dist minus sourcemaps (kept on disk for failures.py, never served)."""

    def lookup_path(self, path):
        if path.endswith(".map"):
            return "", None
        return super().lookup_path(path)


if _DIST.is_dir():
    app.mount("/", _Dist(directory=_DIST, html=True), name="frontend")
