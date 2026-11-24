"""Basemap tiles, fetched backend-side and served from our own origin.

We are a client of someone else's tile server: cached forever by (z, x, y)
(tile_cache, droppable), a real User-Agent, zoom clamped to MIN_Z..MAX_Z, one
tile per request (no prefetch), and 204 on failure (map draws without basemap).
Design: DESIGN.md → `app/tiles.py`.
"""

import logging
import sqlite3
import urllib.error
import urllib.request

from fastapi import APIRouter, Response

from . import db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# OSM's own tiles: the one basemap still serving anonymous requests (Carto
# returns watermarked 200s). MapSheet desaturates at draw time (ctx.filter).
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "© OpenStreetMap contributors"
USER_AGENT = "vault-dashboard/1.0 (personal localhost dashboard)"

MIN_Z, MAX_Z = 3, 19
TIMEOUT_S = 8


def _fetch(z: int, x: int, y: int) -> bytes | None:
    url = TILE_URL.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("tiles: %s/%s/%s unavailable: %s", z, x, y, e)
        return None


def tile(z: int, x: int, y: int) -> bytes | None:
    """One PNG tile, from cache or from upstream once."""
    if not (MIN_Z <= z <= MAX_Z):
        return None
    span = 1 << z
    if not (0 <= x < span and 0 <= y < span):
        return None

    with db.session() as con:
        row = con.execute(
            "SELECT png FROM tile_cache WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
    if row:
        return row["png"]

    png = _fetch(z, x, y)
    if png is None:
        return None
    try:
        # `with con:` commits; session() alone doesn't, so the cache stayed empty.
        with db.session() as con, con:
            con.execute(
                "INSERT OR REPLACE INTO tile_cache (z, x, y, png, fetched_at)"
                " VALUES (?,?,?,?, datetime('now'))", (z, x, y, png))
    except sqlite3.Error as e:            # a cache miss is not a failed map
        log.warning("tiles: could not cache %s/%s/%s: %s", z, x, y, e)
    return png


@router.get("/tiles/{z}/{x}/{y}.png")
def get_tile(z: int, x: int, y: int):
    png = tile(z, x, y)
    if png is None:
        # 204, not 404: expected misses (world edge, dead network) shouldn't flood red 404s.
        return Response(status_code=204)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})
