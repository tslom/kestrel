"""Failure log: the layers `providers/heartbeat.py` doesn't cover (api, desk, console, poller).

One row per distinct failure, keyed by fingerprint. Never load-bearing: like
`heartbeat.record()` it never raises into its caller. Derived and droppable.
Design: DESIGN.md → Faults.
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

# Cap on what one row stores; an error built from a response body has no ceiling.
DETAIL_MAX = 2000
TRACE_MAX = 8000

# `poller` = post-provider steps (alarms/notify/wake/backup); providers use heartbeat.
ORIGINS = {"api", "desk", "console", "poller"}


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _fingerprint(origin: str, label: str, detail: str | None) -> str:
    """What counts as "the same failure". Deliberately not the trace, and not the label alone."""
    return hashlib.sha256(
        "\x1f".join((origin, label, detail or "")).encode()).hexdigest()[:16]


def _stable(text: str) -> str:
    """The part of a free-text reason that stays put between occurrences (numbers and hex ids masked)."""
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
    # Microseconds: the trim orders by `last_at`, and second-level ties keep the oldest rows.
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
                "   detail=excluded.detail,"
                # newest trace wins
                "   trace=COALESCE(excluded.trace, failures.trace),"
                "   count=failures.count+1",
                (fp, origin, kind, label, detail, trace, now, now,
                 db.day_key()))
            # Trim by recency, not id: a long-standing failure still firing holds the oldest row.
            con.execute(
                "DELETE FROM failures WHERE fingerprint IN ("
                "  SELECT fingerprint FROM failures"
                "  ORDER BY last_at DESC LIMIT -1 OFFSET ?)", (FAILURE_KEEP,))
    except sqlite3.Error as e:  # logging is never load-bearing
        print(f"[failures] could not record {origin}/{label}: {e!r}")


def recent(limit: int = 50, origin: str | None = None) -> list[dict]:
    """Newest first, with `ago` precomputed (as /api/status does)."""
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
    """Drop the log (it is derived)."""
    try:
        with db.session() as con, con:
            return con.execute("DELETE FROM failures").rowcount
    except sqlite3.Error:
        return 0


async def middleware(request, call_next):
    """Persist 5xx failures on the read path. Re-raises always: this is a copy, not a handler.

    Lives here (not main.py) so the test exercises this function, not a copy.
    """
    try:
        response = await call_next(request)
    except Exception as e:
        record("api", "unhandled", _route_label(request),
               detail=repr(e), trace=traceback.format_exc())
        raise
    label = _route_label(request)
    # A hand-raised HTTPException(5xx) becomes a response, not an exception. 4xx is not recorded.
    if response.status_code >= 500:
        status = f"HTTP {response.status_code}"
        # Left on the request by `http_exception_handler` below.
        reason, trace = getattr(request.state, "failure_reason", (None, None))
        if reason:
            record("api", "unhandled", label, detail=f"{status}: {reason}",
                   trace=trace, key=f"{status}: {_stable(reason)}")
        else:
            record("api", "unhandled", label, detail=status)
    return response


def _route_label(request) -> str:
    """"GET /api/email/{msg_id}/body": the route template, not the raw path (else one row per id)."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


async def http_exception_handler(request, exc):
    """FastAPI's HTTPException handler, plus: a 5xx's detail and traceback go on `request.state` for `middleware`."""
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
    """Wire the failure log into an app (main.py and the tests both call this)."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.middleware("http")(middleware)


# --- desk traces: minified frame -> source line ------------------------------
#
# Frames are resolved when the report arrives (hidden .map files are pruned after a
# week, vite.config.js); a frame whose map is gone is left as it came.

DIST_ASSETS = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "assets"
_FRAME = re.compile(r"https?://[^\s()@]+?/assets/([\w.-]+\.js):(\d+):(\d+)")
_B64 = {c: i for i, c in enumerate(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


def _vlq(segment: str) -> list[int]:
    """One sourcemap segment: base64 VLQ (5 data bits per digit; low bit is the sign)."""
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
    """Just enough of Source Map v3 to map a frame to a source line.

    Fields are running deltas, so `__init__` walks `mappings` once and keeps the state at each line start.
    """

    def __init__(self, raw: dict):
        root = raw.get("sourceRoot") or ""
        # Report paths from the repo root, not relative to dist/assets.
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
    # mtime in the key so a hand-copied dist doesn't serve a stale parse.
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
    """What the desk posts; `origin` is always "desk", never read from the body."""

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
        "distinct": len(rows),
        "occurrences": sum(r["count"] for r in rows),
    }


@router.delete("")
def clear_failures() -> dict:
    return {"cleared": clear()}
