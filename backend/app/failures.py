"""Failure log — the three layers `providers/heartbeat.py` doesn't cover.

The poller's outcomes have been persisted since v33/v34, so "which source
broke, and when" is a click on the status panel. Everything around the
providers still failed into stderr:

  api      an on-demand read raising inside `providers/api.py`. The poller
           path records every cycle; the *read* path — the twenty-odd registry
           endpoints the desk hits on every load — mapped ValueError to 400
           and let everything else become a 500 that existed only in
           the stderr log (/tmp/vault-dashboard-app.log; it was
           /private/tmp/backend.log when this was written, and a grep
           against the old name now silently finds nothing, which reads
           exactly like a clean log). DESIGN.md already argues that log is not
           a record ("unrotated, and /tmp is periodically cleaned"); v34 acted
           on it for the poller and stopped there.
  desk     a widget throwing during render. There was no error boundary at
           all, so one bad card blanked the whole desk, and the only trace was
           a devtools console nobody had open.
  console  a turn coming back is_error. `console.py` streams that flag to the
           browser and drops it, so the two questions worth asking about the
           agent — how often does a turn fail, and on what — were answerable
           only by replaying transcripts by hand. Both "On reach" lessons in
           DESIGN.md were found exactly that way.

Same contract as `heartbeat.record()`: this is never load-bearing, so it
never raises into its caller. A failure log that can itself fail a request is
worse than no failure log.
"""

import functools
import hashlib
import json
import posixpath
import re
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.exception_handlers import (
    http_exception_handler as _default_http_exception_handler)
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db

router = APIRouter(prefix="/api/failures")

FAILURE_KEEP = 200      # distinct failures retained, most-recent first

# Cap on what one row stores. A React component stack or a Python traceback is
# a few KB; a minified stack from a bundled build occasionally is not, and an
# error message built from a response body has no ceiling at all.
DETAIL_MAX = 2000
TRACE_MAX = 8000

# `poller` is the post-provider steps (alarms/notify/wake/backup): providers
# have heartbeat, but those four had only a print.
ORIGINS = {"api", "desk", "console", "poller"}


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _fingerprint(origin: str, label: str, detail: str | None) -> str:
    """What counts as "the same failure" for the purpose of not writing it
    twice a second.

    Deliberately *not* the trace: the same bug reached through two code paths
    is one thing to fix, and a stack that differs only in a line number would
    otherwise split it into two rows that each look rare. Deliberately *not*
    just the label either — one widget has more than one way to break, and
    collapsing those hides the second one behind the first one's count.
    """
    return hashlib.sha256(
        "\x1f".join((origin, label, detail or "")).encode()).hexdigest()[:16]


def _stable(text: str) -> str:
    """The part of a free-text reason that stays put between occurrences.

    An HTTPException's detail is usually built from the exception that caused
    it — `f"…: {e}"` — and that text carries counters, ports, message ids and
    timeouts that change every time. Fingerprinting the raw text would turn
    one flaky upstream into a row per request; numbers and hex ids are masked
    so they don't, while the words that say *what* failed still split rows.
    """
    text = re.sub(r"0x[0-9a-fA-F]+|\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b|\d+",
                  "#", text)
    return text[:200]


def record(origin: str, kind: str, label: str, *,
           detail: str | None = None, trace: str | None = None,
           key: str | None = None) -> None:
    """Upsert one failure. Never raises.

    `key`, when given, stands in for `detail` in the fingerprint — for a
    detail whose text varies between occurrences of the same failure. The row
    then shows the newest detail, as it shows the newest trace.
    """
    detail = _clip(detail, DETAIL_MAX)
    trace = _clip(trace, TRACE_MAX)
    # Microseconds, not the seconds every other timestamp in app.db carries,
    # because `last_at` is not only displayed — the trim below orders by it.
    # The failure this table exists to survive is a card throwing on every
    # render, which puts dozens of rows in the same second; at second
    # precision that is a tie SQLite breaks by rowid, i.e. by *first* insert,
    # so the trim kept the twelve oldest and dropped what had just happened.
    now = datetime.now().isoformat()
    fp = _fingerprint(origin, label, key if key is not None else detail)
    try:
        with db.session() as con, con:
            con.execute(
                "INSERT INTO failures (fingerprint, origin, kind, label,"
                " detail, trace, first_at, last_at, last_day, count)"
                " VALUES (?,?,?,?,?,?,?,?,?,1)"
                " ON CONFLICT(fingerprint) DO UPDATE SET"
                "   last_at=excluded.last_at,"
                "   last_day=excluded.last_day,"
                # identical by construction unless `key` was given
                "   detail=excluded.detail,"
                # newest trace wins: when you go to read one, the useful
                # question is what the most recent occurrence looked like
                "   trace=COALESCE(excluded.trace, failures.trace),"
                "   count=failures.count+1",
                (fp, origin, kind, label, detail, trace, now, now,
                 db.day_key()))
            # Trim by recency, not by id: a long-standing failure that still
            # fires today is the one you most want kept, and it holds the
            # oldest row in the table.
            con.execute(
                "DELETE FROM failures WHERE fingerprint IN ("
                "  SELECT fingerprint FROM failures"
                "  ORDER BY last_at DESC LIMIT -1 OFFSET ?)", (FAILURE_KEEP,))
    except sqlite3.Error as e:  # logging is never load-bearing
        print(f"[failures] could not record {origin}/{label}: {e!r}")


def recent(limit: int = 50, origin: str | None = None) -> list[dict]:
    """Newest first. `ago` is precomputed for the same reason /api/status
    precomputes it: the panel should not be doing clock arithmetic."""
    from .providers import heartbeat   # local: heartbeat imports db, not us

    where, args = ("WHERE origin=?", [origin]) if origin else ("", [])
    try:
        with db.session() as con:
            rows = con.execute(
                f"SELECT * FROM failures {where} ORDER BY last_at DESC LIMIT ?",
                [*args, limit]).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) | {"ago": heartbeat.since(r["last_at"])} for r in rows]


def clear() -> int:
    """Drop the log. It is derived — the button exists so that "is this still
    happening" has an answer that isn't reading timestamps."""
    try:
        with db.session() as con, con:
            return con.execute("DELETE FROM failures").rowcount
    except sqlite3.Error:
        return 0


async def middleware(request, call_next):
    """Persist what a 500 used to leave only in stderr. Registered by main.py.

    Lives here rather than in main.py so the test exercises *this* function
    instead of a copy of it — a middleware the suite reimplements is a stub,
    and a stub stops testing its subject the moment the two drift.

    The poller records its own outcomes, so this is here for the read path:
    `providers/api.py` maps ValueError to 400 and lets anything else — a
    sqlite error, a KeyError in a summary builder, a provider raising on a
    shape it didn't expect — become an unhandled 500. The desk shows that as
    "Internal Server Error" in one card, and the traceback lived in the
    stderr log (/tmp/vault-dashboard-app.log) until /tmp was next cleaned.

    Re-raises always: Starlette's handler still returns the 500 and the
    traceback still reaches stderr. This only adds a durable copy.
    """
    try:
        response = await call_next(request)
    except Exception as e:
        record("api", "unhandled", _route_label(request),
               detail=repr(e), trace=traceback.format_exc())
        raise
    label = _route_label(request)
    # A route raising HTTPException(5xx) by hand never reaches the block above
    # — FastAPI turns it into a response — but it is the same failure. A 4xx is
    # the caller being wrong, and logging those would bury the real ones.
    if response.status_code >= 500:
        status = f"HTTP {response.status_code}"
        # The reason, when the response came from an HTTPException:
        # `http_exception_handler` below left it on the request. Without it a
        # row read "HTTP 502" and the cause (a health-API timeout behind
        # /api/trends) had to be dug out of the stderr log.
        reason, trace = getattr(request.state, "failure_reason", (None, None))
        if reason:
            record("api", "unhandled", label, detail=f"{status}: {reason}",
                   trace=trace, key=f"{status}: {_stable(reason)}")
        else:
            record("api", "unhandled", label, detail=status)
    return response


def _route_label(request) -> str:
    """"GET /api/email/{msg_id}/body", not the path with the id in it.

    Labelled by raw path, one failing route split into a row per message —
    `GET /api/email/1a0ea66b…/body` and `…/1a0ea722…/body` were two rows for
    one Gmail outage. The router writes the matched route into the shared
    scope, so it is there by the time call_next returns or raises; an
    unmatched path (a 404 never gets here, but a mount can) keeps the raw one."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


async def http_exception_handler(request, exc):
    """FastAPI's own HTTPException handler, plus a note for the middleware.

    The exception is gone by the time the middleware sees the response, and
    the response body is a stream it would have to drain and rebuild to read.
    So for a 5xx the detail — and the traceback, which for `raise
    HTTPException(502, …) from e` carries the upstream error itself — is left
    on `request.state` (scope-backed, so the middleware's Request sees it) for
    `middleware` to record. Recording stays in one place; this only adds
    what that place couldn't see.
    """
    if exc.status_code >= 500:
        try:
            detail = exc.detail if isinstance(exc.detail, str) \
                else json.dumps(exc.detail, default=str)
            request.state.failure_reason = (
                detail, "".join(traceback.format_exception(exc)))
        except Exception:  # never load-bearing
            pass
    return await _default_http_exception_handler(request, exc)


def install(app) -> None:
    """Wire the failure log into an app: what main.py calls, and what the
    suite calls, so neither tests a copy of the other."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.middleware("http")(middleware)


# --- desk traces: minified frame -> source line ------------------------------
#
# The built desk is one minified bundle, so a desk trace reads
# `gk@http://127.0.0.1:8788/assets/index-D2c-yNSi.js:55:48155`. vite.config.js
# builds with `sourcemap: "hidden"` — a .map beside each bundle, not referenced
# from it — and frames are resolved here, when the report arrives, because
# that is the one moment the matching map is certain to exist: builds prune
# assets after a week (vite.config.js), and a trace kept for its hash alone is
# unmappable forever. A frame whose map
# is gone — a window running a bundle older than that — is left as it came.

DIST_ASSETS = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "assets"
_FRAME = re.compile(r"https?://[^\s()@]+?/assets/([\w.-]+\.js):(\d+):(\d+)")
_B64 = {c: i for i, c in enumerate(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


def _vlq(segment: str) -> list[int]:
    """One sourcemap segment: base64 VLQ, 5 data bits per digit, low bit of
    the assembled value is the sign."""
    out, value, shift = [], 0, 0
    for ch in segment:
        digit = _B64[ch]
        value += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        out.append(-(value >> 1) if value & 1 else value >> 1)
        value, shift = 0, 0
    return out


class _SourceMap:
    """Just enough of Source Map v3 to answer "which source line is this".

    Every field but the generated column is a running delta across the whole
    `mappings` string, so finding line N means walking lines 0..N-1. That walk
    happens once per map, keeping only the running state at the start of each
    line (a minified bundle has ~100 lines); a lookup then decodes one line.
    """

    def __init__(self, raw: dict):
        root = raw.get("sourceRoot") or ""
        # Paths are relative to the map (dist/assets); report them from the
        # repo root, which is where you go to open them.
        self.sources = [posixpath.normpath(posixpath.join(
            "frontend/dist/assets", root, s or "")) for s in raw["sources"]]
        self.lines = raw["mappings"].split(";")
        self.starts = []
        src = oline = ocol = name = 0
        for line in self.lines:
            self.starts.append((src, oline, ocol))
            for seg in filter(None, line.split(",")):
                v = _vlq(seg)
                if len(v) >= 4:
                    src += v[1]; oline += v[2]; ocol += v[3]
                if len(v) >= 5:
                    name += v[4]

    def lookup(self, line: int, col: int) -> tuple[str, int, int] | None:
        """1-based line/column in, as browsers print them; 1-based out."""
        if not 0 < line <= len(self.lines):
            return None
        src, oline, ocol = self.starts[line - 1]
        gcol, best = 0, None
        for seg in filter(None, self.lines[line - 1].split(",")):
            v = _vlq(seg)
            gcol += v[0]
            if gcol > col - 1:
                break
            if len(v) >= 4:
                src += v[1]; oline += v[2]; ocol += v[3]
                best = (src, oline, ocol)
            else:
                best = None   # an explicitly unmapped span
        if best is None or not 0 <= best[0] < len(self.sources):
            return None
        return self.sources[best[0]], best[1] + 1, best[2] + 1


@functools.lru_cache(maxsize=4)
def _load_map(path: str, mtime_ns: int) -> _SourceMap:
    # mtime in the key: a rebuild that reuses a hash (same content) is the
    # same map anyway, but a hand-copied dist should not serve a stale parse.
    return _SourceMap(json.loads(Path(path).read_text()))


def resolve_trace(trace: str | None) -> str | None:
    """Rewrite each bundle frame as `frontend/src/…:line:col`, keeping the
    minified original underneath. Unchanged if nothing resolved. Never raises:
    a trace that can't be mapped is still worth recording as it came."""
    if not trace:
        return trace
    try:
        def sub(m: re.Match) -> str:
            path = DIST_ASSETS / (m[1] + ".map")
            try:
                smap = _load_map(str(path), path.stat().st_mtime_ns)
            except (OSError, ValueError, KeyError):
                return m[0]
            hit = smap.lookup(int(m[2]), int(m[3]))
            return f"{hit[0]}:{hit[1]}:{hit[2]}" if hit else m[0]

        resolved = _FRAME.sub(sub, trace)
    except Exception:  # never load-bearing
        return trace
    if resolved == trace:
        return trace
    return f"{resolved}\n\n— minified —\n{trace}"


class ReportIn(BaseModel):
    """What the desk posts. `origin` is not taken from the body — anything
    arriving here came from the browser by definition."""

    kind: str = Field(pattern="^(render|promise|unhandled|fetch)$")
    label: str = Field(min_length=1, max_length=200)
    detail: str | None = None
    trace: str | None = None


@router.post("")
def report(body: ReportIn) -> dict:
    record("desk", body.kind, body.label,
           detail=body.detail, trace=resolve_trace(body.trace))
    return {"ok": True}


@router.get("")
def list_failures(limit: int = 50, origin: str | None = None) -> dict:
    rows = recent(limit, origin if origin in ORIGINS else None)
    return {
        "failures": rows,
        # Distinct failures, and the occurrences behind them — the second
        # number is the one that says whether something is on fire right now.
        "distinct": len(rows),
        "occurrences": sum(r["count"] for r in rows),
    }


@router.delete("")
def clear_failures() -> dict:
    return {"cleared": clear()}
