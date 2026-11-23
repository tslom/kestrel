"""Daily brief (IDEAS round 2): one short morning brief per day_key,
generated lazily on the first GET of the day and cached in app.db
(`daily_brief`). DB-only — never written into the vault. The row is also the
generation lock: an atomic upsert claims it by flipping status to
'generating', which works across backend restarts and terminal-skill callers
(WAL + busy_timeout). The /daily-brief skill is a thin wrapper over these
routes.

Not a provider: no range/metric axis, no external source, nothing for the
poller to refresh. Model call mirrors providers/email_triage.py — the Claude
Code CLI on the subscription login, never the raw API.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from . import agent, db, food, leave, people, vault_views
from .routes import email as email_routes
from . import providers
from .providers import email_extract, features, news, weather
from .config import BRIEF_MODEL, BRIEF_STALE_GENERATING_SECONDS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/brief")

SYSTEM = (
    "You write a short morning brief for a personal dashboard, from the JSON "
    "context provided and nothing else. Reply in markdown using only ## "
    "headings, - bullets and **bold** (no tables, no links, no code). Open "
    "with one bold sentence sizing up the day, folding in today's weather "
    "(temperature range in °F, rain chance) if available — weather gets a "
    "mention in the opener, not its own section; if weather_today.uv_index is "
    "6 or higher, add a short sunscreen reminder. Then ## sections, chosen "
    "and ordered by what actually matters today, drawn from: "
    "Calendar, Tasks, Habits, Inbox, Travel, People, Health, Spending, World. "
    # Every section every day, each with a filler line when it had nothing,
    # made the brief a wall to skim past instead of read. Sections are earned
    # now: a quiet day should produce a short brief.
    "Include a section only when it has something the reader would act on or "
    "be glad to know; drop the rest entirely rather than writing a "
    "'nothing to report' line, and never pad to reach a section count. "
    "Four strong sections beat nine thin ones — treat six as a ceiling. "
    "tasks_today is already triaged into overdue/due_today/available/"
    "waiting_stale groups (subtasks flattened out) — list overdue first, "
    "then due today, then recommend at most 3 from 'available' to focus on; "
    "if waiting_stale isn't empty, add one line naming the stalest item. "
    "leave_by is a list of {event, leave_by, location, drive_min} for today's "
    "located events with a computed departure time — when an event in the "
    "Calendar section has a matching non-null leave_by, append 'leave by "
    "H:MM' to its line (skip when leave_by is null); never add a separate "
    "section for it. "
    "Travel comes from packages_arriving (packages with an ETA today/"
    "tomorrow, plus anything delivered in the last 3 days as a return-window "
    "reminder) and trips_soon (flights departing within 48h, with their "
    "current status — call out anything not 'confirmed' or 'On Time', e.g. "
    "delays/gate changes/cancellations); omit the section when both are "
    "empty. People comes from people_signals: birthdays (name, "
    "days_until) within a week, and watch_due (name, days_since, cadence) "
    "for contacts overdue per their own watch cadence — one line each, "
    "suggestions only, never phrase as a task; omit the section when both "
    "lists are empty. World comes from "
    "world_news, a short list of {category, title, summary} news clusters — "
    "render it as '5 things to know', one line per item, title only (skip "
    "summary/category), most important first. features_14d is a "
    "{feature: {day: value}} matrix of the last 14 days across every passive "
    "signal (sleep, steps, screen time, spending, messages, etc.); if — and "
    "only if — one pattern is genuinely striking (a clear trend, spike or "
    "drop), add a single **Signals** line naming it, otherwise omit it "
    "entirely — never force it, never restate a section already covered. "
    "correlations is a ranked list of {a, b, r, n, p} cross-source "
    "relationships over the past year (r = Pearson correlation, negative = "
    "moves opposite, n = days compared, p = significance; cross_basis: true "
    "means the two features count a day differently, so treat that pair as "
    "weaker evidence); if the top one is "
    "genuinely interesting and non-obvious, add a single **Patterns** line "
    "stating it as an association not a cause (e.g. 'you tend to sleep less "
    "on heavy-listening days'), noting nothing else — skip it entirely "
    "otherwise, and never dress a correlation up as certainty. "
    "If a source appears under 'failed', say it was "
    "unavailable — never invent data. Keep the whole brief under 250 words."
)


# The plan view's brief bar (the line): not the morning brief above — that
# stays whole for the /daily-brief skill and the "Morning brief" view — but one
# line over a "needs you" list the reader is already looking at. So it may
# only say what the list can't.
LINE_SYSTEM = (
    "You write one line for the top of a personal dashboard. The reader is "
    "already looking at `asks`, the rows of their 'needs you' list (what, "
    "why, which card), so never restate a row or list them. Say only how the "
    "asks bear on each other — an order that matters, one that unblocks or "
    "informs another, a shared cause or a shared window of time — and then "
    "tomorrow's first thing from `tomorrow_first`, as 'Tomorrow: …'. State a "
    "relation only when the rows themselves plainly support it — no 'may', no "
    "guessing at what an inbox might contain. If no two asks clearly bear on "
    "each other, give only the tomorrow part. If there is "
    "nothing to join and no tomorrow either, reply with nothing at all. "
    "Plain text, one line, under 35 words, no markdown, no preamble. Never "
    "invent anything that isn't in the JSON."
)

# (day, signature of the asks) -> line. Derived and in-memory: a restart
# costs one regeneration, and yesterday's entries are dropped as today's land.
_lines: dict[tuple[str, str], str] = {}


def _tomorrow_first() -> dict | None:
    """The first timed event of the next day key, or None."""
    events = providers.get("calendar", "next-day")["data"]
    timed = [e for e in events if not e.get("all_day")]
    return ({"summary": timed[0].get("summary"), "start": timed[0].get("start")}
            if timed else None)


def _event_weather(events: list[dict], day: str) -> list[dict]:
    """Today's events with a location string -> {event, forecast}, skipping
    ones that name no physical place, don't geocode, or whose forecast fails —
    degrade, never fail.

    The is_virtual() guard is §14's, reused rather than recopied: geocoders
    resolve a junk location confidently, so a "TBD" event was quoting the
    forecast at (2.77, -77.67) — open Pacific off Colombia, where the day is
    always a thunderstorm at 100% — and a Zoom URL was quoting the campus
    centroid, which reads plausible enough never to be questioned.
    """
    out = []
    for ev in events:
        loc = ev.get("location")
        if not loc or leave.is_virtual(loc):
            continue
        coords = weather.geocode(loc)
        if coords is None:
            continue
        forecast = weather.forecast_at(*coords, day)
        if forecast is not None:
            out.append({"event": ev.get("summary"), "forecast": forecast})
    return out


def _gather(day: str) -> dict:
    """Each source independently, in-process; a failure degrades to a note in
    'failed' — the brief still generates from whatever loaded."""
    ctx: dict = {}
    failed: dict[str, str] = {}
    sources = (
        ("calendar", lambda: providers.get("calendar", "today")["data"]),
        ("leave_by", lambda: leave.compute_leaves(day)),
        ("tasks_today", lambda: db.tasks_today()),
        ("habits", lambda: db.list_habits()),
        ("sleep_last_night",
         lambda: providers.get("health", "today", "sleep_hours")["data"]),
        ("steps_yesterday",
         lambda: providers.get("health", "yesterday", "steps")["data"]),
        ("email_queue",
         lambda: {k: email_routes.get_queue()[k] for k in ("count", "unread")}),
        ("spend_yesterday",
         lambda: providers.get("finance", "yesterday", "spend")["data"]),
        # §25: yesterday's food vs targets, one line; None when nothing was logged
        ("food_yesterday", lambda: food.brief_context(day)),
        ("packages_arriving", lambda: email_extract.packages_for_brief(day)),
        ("trips_soon", lambda: email_extract.upcoming_trips()),
        ("people_signals", lambda: people.brief_signals(day)),
        ("weather_today", lambda: weather.day_summary(day)),
        ("event_weather", lambda: _event_weather(ctx.get("calendar", []), day)),
        ("features_14d", lambda: features.matrix(day)),
        ("correlations", lambda: features.correlations(day, cross_domain=True)),
        ("world_news", lambda: news.top_headlines(day)),
        ("generated_notes", lambda: vault_views.list_generated_unreviewed()),
    )
    for name, fn in sources:
        try:
            ctx[name] = fn()
        except Exception as e:  # degrade, never fail the brief
            failed[name] = str(e)
    return {"day": day, "context": ctx, "failed": failed}


def _generate(day: str) -> None:
    """Sync worker; only ever call via _spawn (asyncio.run inside would blow
    up on the event-loop thread). Same shape as email_triage under the
    poller."""
    if (why := agent.blocked()) is not None:
        # Usage window spent: the call would be refused, so don't gather two
        # dozen sources for it. Same error row a refused call would leave.
        log.warning("brief generation failed for %s: %s", day, why)
        with db.session() as con, con:
            con.execute("UPDATE daily_brief SET status='error', error=?"
                        " WHERE day=?", (why[:500], day))
        return
    gathered = _gather(day)
    sources = json.dumps({"loaded": sorted(gathered["context"]),
                          "failed": gathered["failed"]})
    # A connection per write, not one held open across the multi-second model
    # call.
    try:
        body = asyncio.run(agent.ask(
            json.dumps(gathered, default=str),
            model=BRIEF_MODEL, system=SYSTEM)).strip()
        if not body:
            raise RuntimeError("empty reply from model")
        with db.session() as con, con:
            con.execute(
                "UPDATE daily_brief SET status='ready', body=?,"
                " error=NULL, model=?, sources=?, generated_at=?"
                " WHERE day=?",
                (body, BRIEF_MODEL, sources,
                 datetime.now().isoformat(timespec="seconds"), day))
        log.info("brief ready for %s", day)
    except Exception as e:  # SDK/CLI failure -> retryable row
        log.warning("brief generation failed for %s: %s", day, e)
        with db.session() as con, con:
            con.execute(
                "UPDATE daily_brief SET status='error', error=?, sources=?"
                " WHERE day=?", (str(e)[:500], sources, day))


def _claim(day: str, force: bool) -> bool:
    """Atomically move today's row to 'generating'. True = we own generation.
    Without force, only a missing row or a stale 'generating' row (backend
    died mid-run) is claimed — ready/error rows return as-is, so a failed
    brief never retries on its own dime."""
    now = datetime.now()
    stale = (now - timedelta(seconds=BRIEF_STALE_GENERATING_SECONDS)
             ).isoformat(timespec="seconds")
    with db.session() as con, con:
        cur = con.execute(
            "INSERT INTO daily_brief (day, status, started_at)"
            " VALUES (?, 'generating', ?)"
            " ON CONFLICT(day) DO UPDATE SET status='generating',"
            "  started_at=excluded.started_at, body=NULL, error=NULL,"
            "  generated_at=NULL"
            " WHERE ? OR (daily_brief.status='generating'"
            "             AND daily_brief.started_at < ?)",
            (day, now.isoformat(timespec="seconds"), int(force), stale))
        return cur.rowcount == 1


_tasks: set[asyncio.Task] = set()  # keep refs so tasks aren't GC'd mid-run


def _spawn(day: str) -> None:
    t = asyncio.create_task(asyncio.to_thread(_generate, day))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _row(day: str) -> dict:
    with db.session() as con:
        r = con.execute("SELECT * FROM daily_brief WHERE day=?", (day,)).fetchone()
    d = dict(r)
    d["sources"] = json.loads(d["sources"] or "{}")
    return d


# Routes are async def deliberately: asyncio.create_task needs the event-loop
# thread (a plain def route runs in the threadpool). They only do fast reads.

@router.get("")
async def get_brief():
    """Today's brief; kicks off generation on the first open of the day.
    Always returns the row immediately — the widget polls while
    status='generating'. An 'error' row returns as-is (retry is explicit,
    via /regenerate)."""
    day = db.day_key()
    if _claim(day, force=False):
        _spawn(day)
    return _row(day)


@router.post("/regenerate")
async def regenerate():
    """Force a fresh brief for today, even over a ready/error/live row.
    Asked for, so it runs past the budget gate the first-open brief stops at
    (the task copies this context, interactive flag included)."""
    day = db.day_key()
    if _claim(day, force=True):
        with agent.interactive():
            _spawn(day)
    return _row(day)


def _clip(n: int):
    return lambda v: v[:n] if isinstance(v, str) else v


class Ask(BaseModel):
    # Clipped, not rejected: a 422 here hid the whole bar (8 times in the
    # log), and a row's `why` grows on its own (the Messages-broken suffix).
    title: Annotated[str, BeforeValidator(_clip(300))]
    why: Annotated[str, BeforeValidator(_clip(300))] = ""
    card: Annotated[str | None, BeforeValidator(_clip(40))] = None


class LineIn(BaseModel):
    asks: Annotated[list[Ask], BeforeValidator(
        lambda v: v[:40] if isinstance(v, list) else v)] = Field(default_factory=list)


@router.post("/line")
async def post_line(body: LineIn):
    """The plan view's brief bar for today's asks: how they bear on each other,
    plus tomorrow's first thing — or "" when there's nothing to add.

    Cached per day and per set of asks, so the model is asked again only when
    the list changes (the page posts on every change, debounced). A failure is
    reported and not cached, so the next post tries again."""
    day = db.day_key()
    asks = [a.model_dump() for a in body.asks]
    sig = hashlib.sha1(json.dumps(asks, sort_keys=True).encode()).hexdigest()
    if (day, sig) in _lines:
        return {"day": day, "line": _lines[(day, sig)], "cached": True}
    try:
        tomorrow = await asyncio.to_thread(_tomorrow_first)
    except Exception:  # no calendar: the line just has no tomorrow
        tomorrow = None
    if not asks and not tomorrow:
        line = ""
    else:
        try:
            line = (await agent.ask(json.dumps({"asks": asks, "tomorrow_first": tomorrow}),
                                    model=BRIEF_MODEL, system=LINE_SYSTEM)).strip()
        except Exception as e:  # SDK/CLI failure: say so, retry next time
            return {"day": day, "line": None, "error": str(e)[:300]}
        line = " ".join(line.split())   # one line, whatever the model did
    for k in [k for k in _lines if k[0] != day]:
        del _lines[k]
    _lines[(day, sig)] = line
    return {"day": day, "line": line, "cached": False}
