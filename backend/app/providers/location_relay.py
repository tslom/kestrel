"""Pulls OwnTracks fixes from the location relay (relay/, a Cloudflare Worker).

Pull -> insert -> ack; `INSERT OR IGNORE` on the ts primary key makes a lost ack
harmless, so no watermark. `row()` is the one place OwnTracks JSON becomes a
location_points row (the relay stores messages verbatim).
Design: DESIGN.md → `app/providers/location_relay.py`: pull, ack, parse.
"""

from datetime import datetime, timezone

from .. import db
from ..config import LOCATION_RELAY_TOKEN, LOCATION_RELAY_URL
from . import http

PAGE = 500
# The relay's `last_contact` (epoch ms), so health() can tell a still phone from a silent one.
CONTACT_KEY = "location_relay_last_contact"


def configured() -> bool:
    return bool(LOCATION_RELAY_URL and LOCATION_RELAY_TOKEN)


# Cloudflare 403s urllib's default User-Agent (error 1010) before the Worker runs.
_HEADERS = {"User-Agent": "vault-dashboard/1.0 (+localhost, personal use)"}


def _request(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    return http.json_request(f"{LOCATION_RELAY_URL.rstrip('/')}{path}",
                             label="location relay", token=LOCATION_RELAY_TOKEN,
                             method=method, body=body, headers=_HEADERS, timeout=20)


def _scaled(value, factor: float):
    """Numeric field -> normalised float; None if absent or negative (some builds send -1 for unknown)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return value * factor


def row(msg: dict) -> tuple | None:
    """One OwnTracks message -> a location_points row, or None. Only `_type: location` is a fix; speed m/s, battery 0..1."""
    if not isinstance(msg, dict) or msg.get("_type") != "location":
        return None
    lat, lon, tst = msg.get("lat"), msg.get("lon"), msg.get("tst")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    try:
        when = datetime.fromtimestamp(int(tst), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    # Overland-era string shape, so the ts PRIMARY KEY still de-duplicates
    ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (ts, db.day_key(when.astimezone()), float(lat), float(lon),
            _scaled(msg.get("vel"), 1 / 3.6),      # km/h → m/s
            None,                                  # motion: no OwnTracks counterpart
            _scaled(msg.get("batt"), 1 / 100),     # percent → 0..1
            _scaled(msg.get("acc"), 1))


def insert(con, rows: list[tuple]) -> None:
    """Caller owns the transaction."""
    con.executemany(
        "INSERT OR IGNORE INTO location_points"
        " (ts, day, lat, lon, speed, motion, battery, accuracy)"
        " VALUES (?,?,?,?,?,?,?,?)", rows)


def pull() -> dict:
    """Drain the relay into location_points. Returns {stored, days}; the caller recomputes those days.

    Raises on a relay error so the poller's heartbeat records it; network failures pass through to offline.py."""
    stored, days, contact = 0, set(), None
    while True:
        page = _request(f"/pull?after=0&limit={PAGE}")
        points = page.get("points") or []
        contact = page.get("last_contact") or contact
        rows = [r for r in (row(p.get("msg")) for p in points) if r]
        with db.session() as con, con:
            if rows:
                insert(con, rows)
            if contact:
                con.execute(
                    "INSERT INTO sync_state (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (CONTACT_KEY, str(contact)))
        if not points:
            break
        # ack only after commit; a crash re-pulls the page, which the ts key absorbs
        _request("/ack", method="POST", body={"upto": max(p["id"] for p in points)})
        stored += len(rows)
        days.update(r[1] for r in rows)
        if len(points) < PAGE:
            break
    return {"stored": stored, "days": sorted(days)}


def last_contact_age_min() -> float | None:
    """Minutes since the phone last reached the relay, or None if unknown."""
    with db.session() as con:
        r = con.execute("SELECT value FROM sync_state WHERE key=?",
                        (CONTACT_KEY,)).fetchone()
    if r is None:
        return None
    then = datetime.fromtimestamp(int(r["value"]) / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60
