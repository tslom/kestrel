"""Pulls OwnTracks fixes from the location relay (relay/ — a Cloudflare Worker).

The phone posts to the relay, which is always up; this pulls whatever has
queued there into `location_points` on each poller cycle, then acknowledges it
so the relay deletes it. It replaced the phone → Tailscale Funnel → Mac
listener push chain, which needed the Mac awake, Tailscale connected, Funnel
configured and the listener running all at once — and went silently dark for
days whenever one of them wasn't. Now the Mac being asleep or offline only
delays a pull, and nothing on this machine is reachable from the internet.

Pull → insert → ack, in that order. If the ack is lost the next pull hands the
same points back and `INSERT OR IGNORE` on the ts primary key drops them, so
at-least-once is enough and no watermark is kept here.

`row()` is the one place OwnTracks JSON becomes a location_points row. The
relay stores messages verbatim so this file, not the Worker, owns the parsing
and the unit normalisation.
"""

from datetime import datetime, timezone

from .. import db
from ..config import LOCATION_RELAY_TOKEN, LOCATION_RELAY_URL
from . import http

PAGE = 500
# The relay's `last_contact` (epoch ms of the phone's newest request, any
# message type), kept so health() can tell a phone that's alive but still from
# one that has stopped talking.
CONTACT_KEY = "location_relay_last_contact"


def configured() -> bool:
    return bool(LOCATION_RELAY_URL and LOCATION_RELAY_TOKEN)


# Cloudflare's edge answers urllib's default "Python-urllib/3.x" with a 403
# (error 1010, a banned-client signature) before the Worker ever runs.
_HEADERS = {"User-Agent": "vault-dashboard/1.0 (+localhost, personal use)"}


def _request(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    return http.json_request(f"{LOCATION_RELAY_URL.rstrip('/')}{path}",
                             label="location relay", token=LOCATION_RELAY_TOKEN,
                             method=method, body=body, headers=_HEADERS, timeout=20)


def _scaled(value, factor: float):
    """Numeric field → normalised float, or None if absent or a sentinel.

    OwnTracks omits fields it has no reading for, but some builds send -1
    instead; both mean "unknown", and a -1 stored as m/s would look like real
    backwards motion to anyone who later plots it.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return value * factor


def row(msg: dict) -> tuple | None:
    """One OwnTracks message → a location_points row, or None to skip it.

    Only `_type: location` carries a fix. A `transition` has coordinates but
    describes a waypoint crossing, so storing it would double-count the fix
    that triggered it. Units match what the Overland-era rows already mean:
    `speed` m/s (OwnTracks sends km/h), `battery` 0..1 (OwnTracks sends %).
    """
    if not isinstance(msg, dict) or msg.get("_type") != "location":
        return None
    lat, lon, tst = msg.get("lat"), msg.get("lon"), msg.get("tst")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    try:
        when = datetime.fromtimestamp(int(tst), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    # Same string shape as the Overland rows already on disk, so the ts
    # PRIMARY KEY keeps de-duplicating across the seam.
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
    """Drain the relay into location_points. Returns the points stored and
    the day_keys they landed on, so the caller recomputes those days — a
    backlog flushed after a week offline touches more than today.

    Raises on a relay error (bad token, Worker down) so the poller's heartbeat
    records it; a network failure passes through for offline.py to name."""
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
        # Only after the rows are committed: a crash between the two re-pulls
        # the same page next cycle, which the ts primary key absorbs.
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
