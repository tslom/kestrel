"""Basemap tiles, fetched backend-side and served from our own origin.

The map sheet needs something under the dots or it is a scatter plot: a route
across campus reads as a route only if the streets are there. The frontend
cannot fetch them itself — image hosts outside the CDN allowlist are blocked,
and this is a localhost app besides — so the backend is the only place the
request can come from, which is the shape IDEAS called for when it said "a
basemap fetched backend-side and served locally; do not plan on an external
tile server".

That makes us a client of somebody else's tile server, so the manners are part
of the design rather than a nicety:

* **Cached forever, by (z, x, y).** A tile is only ever fetched once. Street
  geometry does not move, and a map you pan back over must not re-ask. The
  cache is derived and droppable like every other provider cache — dropping it
  costs one re-fetch per tile you look at again.
* **Identified.** A real User-Agent naming the app, as the OSM tile usage
  policy requires. An anonymous scraper is what that policy exists to stop.
* **Bounded.** Zoom is clamped to MIN_Z..MAX_Z and each request is one tile
  from one viewport — no prefetching, no walking a bounding box, nothing that
  turns a pan into a download of the county.
* **Soft on failure.** An unreachable tile server returns 204 rather than an
  error: the map draws without a basemap, which is the vector-only fallback
  the same IDEAS entry offered as the other option. It is a degraded map, not
  a broken sheet.
"""

import logging
import sqlite3
import urllib.error
import urllib.request

from fastapi import APIRouter, Response

from . import db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# openstreetmap.org's own tiles: the one basemap that still serves anonymous
# requests. Carto's light_all was the first choice — grey cartography meant to
# sit under data, which is this map's job — and as of 2026-09-14 it returns
# tiles stamped "API KEY REQUIRED" instead of an error, so the map rendered
# with a watermark across it rather than failing loudly. Checked by looking at
# the pixels; a 200 proved nothing.
#
# The standard style is more colourful than the desk's paper palette wants, so
# the sheet desaturates it at draw time (MapSheet's ctx.filter) rather than
# reaching for a styled basemap that needs an account.
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
        # `with con:` and not just the session: session() closes the connection
        # but commits nothing, so without it every tile is re-fetched forever
        # and the cache stays empty — which is what happened the first time.
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
        # 204, not 404: the map asks for tiles it may not get (edges of the
        # world, a dead network), and a console full of red 404s would make a
        # working map look broken.
        return Response(status_code=204)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})
