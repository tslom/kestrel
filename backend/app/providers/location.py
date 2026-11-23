"""Location provider (archive/ROUND34_PLAN.md §10). OwnTracks (iOS) posts fixes to
the location relay (relay/, a Cloudflare Worker); refresh() pulls them into
`location_points` through location_relay.py — this module never talks to the
phone itself. `location_points` is the raw
source of truth (a drop loses history, like Spotify's `plays`);
`location_days` is a derived/droppable cache this module (re)computes.

Day is **day_key** (4am rollover) — a behavioral source like screen/spotify/
messages: a 1am fix belongs to yesterday.

Places = **stays**, not grid cells: contiguous runs of fixes that never leave
STAY_RADIUS_M of their own running centroid, grouped across the range by
proximity, and counted only when they clear PLACE_MIN_DWELL_SEC *and*
PLACE_MIN_FIXES. See the block above STAY_RADIUS_M for why the grid had to go
and what it cost. A place the vault names (app/places.py) outranks clustering
entirely, because at campus density the distance between two real places is
smaller than the GPS error that would have to separate them.

Entropy = Shannon entropy (bits) over time-per-place (all groups, unfiltered):
a routine day (all time in 1-2 places) is low, a novel day (time spread across
many) is high. Dwell time per fix = the gap to the next fix, capped at
MAX_GAP_MIN so an offline stretch (dead zone, phone off) doesn't inflate
whichever place the phone was last seen in by hours.

**Home is dated, not constant** (app/places.py): `at_home`, `time_at_home_min`,
`first_leave` and `last_return` are all scored against the home in effect on
*that day*. A day whose home the vault doesn't know produces none of the four —
an absence, never a zero, because a zero there is indistinguishable from a real
day spent out and reaches the correlation matrix looking like a measurement.
first_leave/last_return are minutes-since-4am, and are absent when every fix
that day was inside the home radius (never left).
"""

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median

from .. import db, notify, places
from ..config import DAY_ROLLOVER_HOUR, DESK_BASELINE_DAYS
from . import location_relay, offline
from .ranges import Range, days_between

NAME = "location"
METRICS = ("time_at_home_min", "places", "entropy", "km_traveled", "first_leave", "last_return")
DEFAULT_METRIC = "time_at_home_min"
ALLOW_FUTURE = False  # no next-week location

EARTH_RADIUS_M = 6_371_000
GRID_DECIMALS = 3          # ~110m grid cells for place clustering
MAX_GAP_MIN = 20
MAX_GAP_SECONDS = MAX_GAP_MIN * 60
# A grid cell only counts as a "place" if its dwell exceeds this. Without it,
# a driving day inflates `places`: every ~110m cell the car passes through
# gets its ~1min gap-to-next-fix booked as dwell, so transit reads as dozens
# of "places". The 2026-07-24 walk test (a ~100km driving day) showed the
# transit tail sits at <=~2min/cell while genuine stops were >=~4min, so 3min
# cleanly separates them (42 raw clusters -> 15 real places). entropy stays
# over all clusters (already dwell-weighted, so transit barely moves it).
PLACE_MIN_DWELL_SEC = 180


_UNSET = object()   # "caller didn't say", distinct from "there is no home"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _cluster_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, GRID_DECIMALS), round(lon, GRID_DECIMALS))


# ------------------------------------------------------------------ places
#
# Places used to be grid cells: lat/lon rounded to GRID_DECIMALS, ~110m square.
# Cheap, and wrong in a way that only shows up once you draw it. A building
# does not line up with the grid, so a stationary hour near a cell boundary is
# filed as two or three places — measured on this vault, September 2026 held
# **55 places, 12 of them inside the home radius** (one flat, twelve cells),
# and the three largest non-home places sat 57m, 64m and 93m apart: one campus
# building, read as three, with its 677 minutes split between them.
#
# A stay fixes that at the source. Rather than asking which square a fix landed
# in, walk the fixes in time order and cut a new stay only when one leaves
# STAY_RADIUS_M of the run's running centroid — so a continuous sit-down is one
# stay wherever the grid lines happen to fall. Stays then group into places by
# proximity, greedily from the longest, which keeps a campus from chaining into
# one blob the way single-linkage would.
#
# Same numbers, this vault, September: **55 places -> 23, home 12 -> 5.**
#
# What this deliberately does NOT try to fix: two places closer together than
# the GPS error between them. The gym at the foot of the dorm is ~15m away and
# accuracy near home runs p50 17m / p90 47m, so no radius separates them —
# that is what declared places in app/places.py are for, and why a declaration
# that overlaps another says so instead of pretending.
STAY_RADIUS_M = 100
PLACE_MERGE_M = 130
# A merged place may not sprawl further than this from its own centroid.
# Greedy grouping alone left two halves of one building 77m apart but in
# separate groups — whether a stay merged depended on where the group's
# centroid happened to sit when that stay was processed, which is an ordering
# artifact and not a fact about the world. So grouping is followed by a
# convergence pass that merges the closest eligible pair until none qualify.
# The span cap is what stops that pass from chaining across a campus: at USC
# scale 175m holds one building and its forecourt without reaching the next.
PLACE_SPAN_MAX_M = 175
# A place needs to have been *observed* twice. Dwell is booked as the gap to
# the next fix (capped at MAX_GAP_MIN), so a lone fix followed by silence books
# up to 20 minutes on the strength of one observation — which is a sighting,
# not a stay. On a sparse travel day that inflated everything: 2026-08-17, the
# cross-country move, reported **43 places from 58 fixes**, nearly all of them
# single fixes 20 minutes apart. Requiring two drops that day to 7, the daily
# mean from 10.8 to 5.4, and the `places ~ coverage_min` correlation that
# IDEAS flags as a known bias from +0.73 to +0.63. Three would be tighter
# still and is wrong: it zeroes that travel day outright, discarding stops that
# genuinely happened.
PLACE_MIN_FIXES = 2


def _is_place(group: dict) -> bool:
    """Whether a group of stays counts as somewhere you went, for both the
    `places` metric and the map — one predicate so the two cannot drift."""
    return (group["seconds"] > PLACE_MIN_DWELL_SEC
            and group["n"] >= PLACE_MIN_FIXES)


def _stays(pts: list[dict]) -> list[dict]:
    """Contiguous runs of fixes that never leave STAY_RADIUS_M of their own
    running centroid.

    Dwell is booked by the same gap rule the day metrics use — each fix is
    worth the gap to the *next* fix, capped at MAX_GAP_SECONDS — including the
    last fix of a stay, whose gap points at the first fix of the next one. That
    keeps total booked time equal to total covered time, so a stay-based
    `entropy` stays comparable to the grid-based one it replaces."""
    stays = []
    i, n = 0, len(pts)
    while i < n:
        lat_sum, lon_sum, count = pts[i]["lat"], pts[i]["lon"], 1
        j = i + 1
        while j < n:
            if _haversine_m(pts[j]["lat"], pts[j]["lon"],
                            lat_sum / count, lon_sum / count) > STAY_RADIUS_M:
                break
            lat_sum += pts[j]["lat"]
            lon_sum += pts[j]["lon"]
            count += 1
            j += 1
        seconds = 0.0
        for k in range(i, j):
            if k + 1 < n:
                seconds += min((pts[k + 1]["ts"] - pts[k]["ts"]).total_seconds(),
                               MAX_GAP_SECONDS)
        stays.append({"lat": lat_sum / count, "lon": lon_sum / count,
                      "seconds": seconds, "n": count,
                      "first": pts[i]["ts"], "last": pts[j - 1]["ts"],
                      "day": pts[i].get("day")})
        i = j
    return stays


def _group_stays(stays: list[dict], known=None) -> list[dict]:
    """Stays -> places. A declared place claims every stay inside it; the rest
    group greedily, longest first, within PLACE_MERGE_M.

    Declared first and not merely as a label: naming is the only thing that can
    hold one flat together when its own fixes scatter further than the distance
    to the next building. Two visits to one place are two `visits` and one
    place, which is the distinction that makes the map a map of a routine."""
    groups: list[dict] = []
    by_name: dict[str, dict] = {}

    def _new(stay, name=None, named=None):
        g = {"lat": stay["lat"], "lon": stay["lon"], "seconds": 0.0, "n": 0,
             "visits": 0, "days": set(), "name": name,
             "ambiguous_with": list(named.ambiguous_with) if named else [],
             "first": stay["first"], "last": stay["last"], "members": [],
             # The stays themselves, kept alongside the (lat, lon) `members`
             # that _span measures. Nothing in this module reads them —
             # app/place_suggest.py does, to ask which calendar event recurs
             # across a place's visits, which needs their *times* and so
             # cannot be answered from a centroid.
             "stays": []}
        groups.append(g)
        return g

    def _absorb(g, stay):
        total = g["n"] + stay["n"]
        if g["name"] is None:          # a declared place keeps its own centroid
            g["lat"] = (g["lat"] * g["n"] + stay["lat"] * stay["n"]) / total
            g["lon"] = (g["lon"] * g["n"] + stay["lon"] * stay["n"]) / total
        g["n"] = total
        g["seconds"] += stay["seconds"]
        g["visits"] += 1
        g["members"].append((stay["lat"], stay["lon"]))
        g["stays"].append(stay)
        if stay.get("day"):
            g["days"].add(stay["day"])
        g["first"] = min(g["first"], stay["first"])
        g["last"] = max(g["last"], stay["last"])

    def _span(g) -> float:
        return max((_haversine_m(mlat, mlon, g["lat"], g["lon"])
                    for mlat, mlon in g["members"]), default=0.0)

    for stay in sorted(stays, key=lambda s: -s["seconds"]):
        named = (places.resolve(stay["lat"], stay["lon"], stay["day"], known)
                 if known is not None and stay.get("day") else None)
        if named is not None:
            g = by_name.get(named.name)
            if g is None:
                g = by_name[named.name] = _new(stay, named.name, named)
                g["lat"], g["lon"] = named.lat, named.lon
            _absorb(g, stay)
            continue
        best, best_d = None, PLACE_MERGE_M
        for g in groups:
            if g["name"] is not None:
                continue
            d = _haversine_m(stay["lat"], stay["lon"], g["lat"], g["lon"])
            if d <= best_d:
                best, best_d = g, d
        _absorb(best if best is not None else _new(stay), stay)

    _converge(groups, _span)
    return groups


def _converge(groups: list[dict], span) -> None:
    """Merge the closest eligible pair of undeclared groups until none qualify.

    Eligible means centroids within PLACE_MERGE_M *and* a merged span still
    within PLACE_SPAN_MAX_M. Closest-pair-first rather than first-found so the
    result doesn't depend on list order either; the span cap is what keeps
    repeated merging from walking across a campus one building at a time.
    Declared places never merge — a name is an assertion that this is one
    place, and two names are an assertion that these are two."""
    while True:
        open_groups = [g for g in groups if g["name"] is None]
        pairs = []
        for i, a in enumerate(open_groups):
            for b in open_groups[i + 1:]:
                d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if d <= PLACE_MERGE_M:
                    pairs.append((d, i, a, b))
        # Closest first, but a pair rejected for sprawl only disqualifies
        # itself: span is not monotone in centroid distance, so a wider pair
        # can still merge cleanly and must still be offered the chance.
        merged = False
        for _, _, a, b in sorted(pairs, key=lambda p: (p[0], p[1])):
            total = a["n"] + b["n"]
            merged_lat = (a["lat"] * a["n"] + b["lat"] * b["n"]) / total
            merged_lon = (a["lon"] * a["n"] + b["lon"] * b["n"]) / total
            if span({"lat": merged_lat, "lon": merged_lon,
                     "members": a["members"] + b["members"]}) > PLACE_SPAN_MAX_M:
                continue
            merged = True
            break
        if not merged:
            return
        a["lat"], a["lon"], a["n"] = merged_lat, merged_lon, total
        a["seconds"] += b["seconds"]
        a["visits"] += b["visits"]
        a["days"] |= b["days"]
        a["members"] += b["members"]
        a["stays"] += b["stays"]
        a["first"] = min(a["first"], b["first"])
        a["last"] = max(a["last"], b["last"])
        groups.remove(b)


def _shannon_entropy(weights: list[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    ent = 0.0
    for w in weights:
        if w <= 0:
            continue
        p = w / total
        ent -= p * math.log2(p)
    return round(ent, 3)


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    """day_key -> [start, end) as local-aware datetimes spanning the 4am
    rollover window, same convention as screen.py."""
    d = date.fromisoformat(day)
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(d, time(hour=DAY_ROLLOVER_HOUR), tzinfo=tz)
    return start, start + timedelta(days=1)


def _minutes_since(dt: datetime, start: datetime) -> float:
    return round((dt - start).total_seconds() / 60, 1)


def latest_fix() -> dict | None:
    """Most recent point ever ingested, for weather's origin / §14 — not
    day-scoped. None if no points have arrived yet."""
    with db.session() as con:
        row = con.execute(
            "SELECT ts, lat, lon FROM location_points ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return None
    ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).astimezone()
    age_min = (datetime.now().astimezone() - ts).total_seconds() / 60
    home = places.home_now()
    return {"lat": row["lat"], "lon": row["lon"], "ts": row["ts"],
            "age_min": round(age_min, 1),
            "at_home": home.contains(row["lat"], row["lon"])}


# How far back to walk looking for the start of a stay at home. A stay is a
# run of consecutive fixes, and arriving home takes a handful of them, so this
# only has to outrun a day's worth of driving.
HOME_STRETCH_LOOKBACK = 500


def home_stretch() -> dict | None:
    """If the newest fixes are at home, when that stay began and how long it
    has run so far. None if the newest fix is away from home.

    This exists because **silence from OwnTracks is a signal, not a failure**.
    It reports movement; a phone sitting on a desk at home simply stops
    producing fixes, so a stretch at home looks exactly like the app having
    died — and the first version of the card said so out loud, telling a user
    who had been home for 40 hours that their phone had stopped posting. The
    two are genuinely indistinguishable from the fix stream alone; what breaks
    the tie is *where the last fix was*. Home, and the honest reading is
    "still there". Away, and we really have lost the thread.
    """
    with db.session() as con:
        rows = con.execute(
            "SELECT ts, lat, lon FROM location_points ORDER BY ts DESC LIMIT ?",
            (HOME_STRETCH_LOOKBACK,)).fetchall()
    home = places.home_now()
    since = None
    for r in rows:
        if not home.contains(r["lat"], r["lon"]):
            break
        since = r["ts"]
    if since is None:
        return None
    ts = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone()
    return {"since": ts.isoformat(),
            "minutes": round((datetime.now().astimezone() - ts).total_seconds() / 60, 1)}


def parse_ts(raw: str) -> datetime:
    """A stored fix timestamp as local time. The listener writes UTC with a Z
    suffix, which fromisoformat took only from 3.11, so the replace stays."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()


def _load_points(con, day: str, home=_UNSET) -> list[dict]:
    """The day's fixes, each tagged with whether it was at home and which grid
    cell it fell in. Shared by _compute_days and summary() so the two can never
    drift on what "at home" means.

    `home` is the place in effect **on that day** (app/places.py), not a
    constant: scoring an old day against today's address is what made a month
    of days read `time_at_home_min = 0` after a move. None means the vault
    knows of no home then, and every fix's `at_home` is None — unknown, which
    the caller must not collapse to False."""
    if home is _UNSET:
        home = places.home_on(day)
    pts = []
    for p in con.execute(
            "SELECT ts, lat, lon FROM location_points WHERE day=? ORDER BY ts", (day,)):
        ts = parse_ts(p["ts"])
        at_home = home.contains(p["lat"], p["lon"]) if home else None
        pts.append({"ts": ts, "lat": p["lat"], "lon": p["lon"], "at_home": at_home,
                    "cluster": _cluster_key(p["lat"], p["lon"])})
    return pts


def _compute_days(con, days: list[str], now: str) -> None:
    """Recompute location_days for each day from location_points already on
    disk — pure local computation, no I/O (unlike other providers' refresh(),
    the data itself only ever arrives via the listener)."""
    known = places.load()
    with con:
        for day in days:
            home = places.home_on(day, known)
            pts = _load_points(con, day, home)
            con.execute("DELETE FROM location_days WHERE day=?", (day,))
            # Marked even with no points: an empty day is computed, just to
            # nothing (a gap, not zeros). See location_sync in db.py.
            con.execute(
                "INSERT OR REPLACE INTO location_sync (day, points, refreshed_at)"
                " VALUES (?,?,?)", (day, len(pts), now))
            if not pts:
                continue
            day_start, _ = _day_bounds(day)

            home_secs, km = 0.0, 0.0
            for i, p in enumerate(pts):
                if i + 1 < len(pts):
                    gap = min((pts[i + 1]["ts"] - p["ts"]).total_seconds(), MAX_GAP_SECONDS)
                    km += _haversine_m(p["lat"], p["lon"],
                                       pts[i + 1]["lat"], pts[i + 1]["lon"]) / 1000
                else:
                    gap = 0.0
                if p["at_home"]:
                    home_secs += gap

            for p in pts:
                p.setdefault("day", day)
            grouped = _group_stays(_stays(pts), known)
            secs = [g["seconds"] for g in grouped]
            metrics = {
                "places": float(sum(1 for g in grouped if _is_place(g))),
                # Entropy stays over *every* group, unfiltered: it is already
                # dwell-weighted, so transit barely moves it, and filtering
                # first would make a quiet day look more varied than it was.
                "entropy": _shannon_entropy(secs),
                "km_traveled": round(km, 2),
            }
            # The three home-relative metrics exist only for a day whose home
            # is known. Writing 0.0 for a day spent at an address the vault
            # doesn't record would be a measurement of the config, not of the
            # day — and it reaches the correlation matrix as if it were real.
            if home is not None:
                away = [p for p in pts if not p["at_home"]]
                at_home = [p for p in pts if p["at_home"]]
                metrics["time_at_home_min"] = round(home_secs / 60, 1)
                if away:
                    metrics["first_leave"] = _minutes_since(away[0]["ts"], day_start)
                    if at_home:
                        metrics["last_return"] = _minutes_since(at_home[-1]["ts"], day_start)
            for metric, value in metrics.items():
                con.execute(
                    "INSERT INTO location_days (day, metric, value, refreshed_at)"
                    " VALUES (?,?,?,?)", (day, metric, value, now))


def _ensure_days(con, days) -> list[str]:
    """Compute any of `days` that has never been computed, and say which.

    refresh() only ever touches today and yesterday, so a day the backend was
    down for is never computed by the poller — it stays absent until something
    asks for a range covering it. Every reader therefore backfills its own
    window; that is safe here and nowhere else, because computing a location
    day is pure local arithmetic over points the listener already wrote (no
    external pull, so no card render can reach the network).

    Future days are skipped: they have no points to compute from, so they'd be
    recomputed on every single call.

    "Computed" is location_sync, not location_days: a day with no fixes writes
    no metric rows, and judged by those it read as new on every call. A marked
    day is redone only when its fix count has moved since — a late-flushed
    OwnTracks queue landing on a past day, or today still accumulating — so a
    read recomputes at the rate fixes arrive, not the rate it's called.
    """
    today = db.day_key()
    wanted = {d for d in days if d <= today}
    if not wanted:
        return []
    lo, hi = min(wanted), max(wanted)
    counts = {r["day"]: r["n"] for r in con.execute(
        "SELECT day, COUNT(*) AS n FROM location_points"
        " WHERE day BETWEEN ? AND ? GROUP BY day", (lo, hi))}
    marked = {r["day"]: r["points"] for r in con.execute(
        "SELECT day, points FROM location_sync WHERE day BETWEEN ? AND ?", (lo, hi))}
    missing = sorted(d for d in wanted if marked.get(d) != counts.get(d, 0))
    if missing:
        _compute_days(con, missing, datetime.now().isoformat(timespec="seconds"))
    return missing


STALE_FIX_MIN = 6 * 60
# The at-home ceiling. OwnTracks reports movement, so a stay at home produces
# no fixes at all and silence there means nothing is wrong — six hours of it
# is an ordinary evening in. Only a week of total stillness is worth a look,
# and until then the dot must stay quiet: a source that cries broken every
# time you spend a weekend at home teaches you to ignore the panel.
HOME_QUIET_MAX_MIN = 7 * 24 * 60


def _ago(minutes: float) -> str:
    return f"{round(minutes)}m ago" if minutes < 120 else f"{round(minutes / 60)}h ago"


def health() -> dict:
    """Location is the one source the phone *pushes* — to the relay, which
    refresh() pulls from. A pull that succeeds says the relay is fine, not that
    the phone is still sending, so what matters is the age of the newest fix,
    and what that age means depends on **where** that fix was.

    OwnTracks evicted from the background and a rotated token look identical
    from here (silent) — but so does staying home, because a stationary phone
    has no movement to report. The newest fix's location is the only thing
    that tells them apart: at home, silence is the expected reading of "still
    here"; away, we really have lost the thread and the dot should say so.
    The relay's last-contact time sharpens that: any request from the phone,
    fix or not, proves the app is alive.
    """
    if not location_relay.configured():
        return {"state": "off",
                "detail": "no ingest path — set LOCATION_RELAY_URL/_TOKEN (see relay/)"}
    fix = latest_fix()
    if fix is None:
        return {"state": "off", "detail": "no fixes ever ingested — point OwnTracks at the relay"}
    age = fix["age_min"]
    ago = _ago(age)
    contact = location_relay.last_contact_age_min()
    if contact is not None and contact < age and contact <= STALE_FIX_MIN:
        return {"state": "ok",
                "detail": f"newest fix {ago}; phone checked in {_ago(contact)}"}
    if fix["at_home"]:
        if age > HOME_QUIET_MAX_MIN:
            return {"state": "stale",
                    "detail": f"newest fix {ago}, at home — a week without a single fix;"
                              " is OwnTracks still running?"}
        return {"state": "ok",
                "detail": f"newest fix {ago}, at home — OwnTracks reports movement,"
                          " so a stay at home is silent"}
    if age > STALE_FIX_MIN:
        return {"state": "stale",
                "detail": f"newest fix {ago}, away from home — is OwnTracks still running?"}
    return {"state": "ok", "detail": f"newest fix {ago}"}


def refresh() -> dict:
    """Poller entrypoint: drain the relay, then recompute today, yesterday and
    any day the pulled backlog landed on. Then say so out loud if location has
    gone quiet — the dot alone let a four-day outage pass unnoticed."""
    today = db.day_key()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    pulled = {"stored": 0, "days": []}
    if location_relay.configured():
        try:
            pulled = location_relay.pull()
        except Exception as e:
            if not offline.is_network_error(e):
                # A refusal (bad token, Worker gone) won't fix itself; once a
                # day is enough to be heard. Offline is just the lid closed.
                notify.send("location", "relay-error", today,
                            title="Location relay is refusing the pull",
                            body=str(e)[:200])
            raise
    days = sorted({yesterday, today, *pulled["days"]})
    with db.session() as con:
        _compute_days(con, days, datetime.now().isoformat(timespec="seconds"))
    h = health()
    if h["state"] == "stale":
        fix = latest_fix()
        # Keyed by the newest fix: one notice per silence, and a new one only
        # after a fix has arrived and it goes quiet again.
        notify.send("location", "stale", fix["ts"] if fix else "none",
                    title="Location has gone quiet", body=h["detail"])
    return {"days": days, "pulled": pulled["stored"]}


def brief_line(it: dict) -> str:
    return f"{it['day']} {it['value']}"


# The desk card's window. Wider than screen's 7 because OwnTracks is
# best-effort where aw-server is always-on: over the last fortnight only 8
# days cleared the fix floor, so a 7-day sparkline would be mostly gaps.
# Widened from 14 with the lede rebuild, to the one window every card reads
# against. Location loses more days than most sources — the phone reports
# movement, so a week spent at home is a week of holes — which makes the
# longer window matter more here, not less.
SUMMARY_DAYS = DESK_BASELINE_DAYS
# Below this much of the day observed, a day's numbers are noise, not a quiet
# day: a day with one fix computes 0km / 0 places / 0 min at home, which
# renders identically to "never left the house" and is the opposite of the
# truth. Such a day is a gap in the trend, and never the day the card centers
# on.
#
# This gated on fix *count* until 2026-08-30, which measured the wrong axis.
# Every metric here (km_traveled, places, time_at_home_min, entropy) is a
# whole-day quantity, so what makes them trustworthy is how much of the day
# was watched, not how many rows arrived — and the two come apart in both
# directions on real data. 2026-07-29 has the highest fix count in the whole
# series (121) inside 2.4h of coverage, a dense burst on one drive that the
# count gate waved through; 2026-08-23 (4 fixes, 49 min) was refused while
# 2026-07-28 (7 fixes, 25 min) passed, so the old gate ordered days wrong,
# not merely loosely. Coverage subsumes the old floor rather than sitting
# beside it: with gaps capped at MAX_GAP_MIN, clearing 180 min takes at least
# 9 fixes, so there is one gate again, on the axis that means something.
#
# An absolute floor, like screen's, and for a different reason: 24h is never
# in reach because a stationary phone on wifi stops reporting, so a fraction
# of the day would refuse every honest day spent at home. 180 min is where
# the observed days separate — everything below it in 35 days of history is a
# burst inside one errand or a day still accruing, and everything above has
# fixes actually spread across the day.
COVERAGE_FLOOR_MIN = 180
# The sparkline's metric. Not DEFAULT_METRIC (time_at_home_min): time at home
# is the one number the coverage gaps bias hardest, since a stationary phone
# on wifi is exactly when OwnTracks goes quiet. Distance only ever accrues from
# fixes that did arrive.
TREND_METRIC = "km_traveled"
# Shannon bits over dwell-per-cluster -> a word. The observed spread is
# 0.97 (one errand) .. 4.49 (a travel day); the cuts sit either side of the
# ~2.5 that a normal day of a few places lands on.
ENTROPY_BANDS = ((1.5, "routine"), (3.0, "mixed"))


def _entropy_label(bits: float | None) -> str | None:
    if bits is None:
        return None
    for cut, label in ENTROPY_BANDS:
        if bits < cut:
            return label
    return "varied"


def _clock(day: str, minutes: float | None) -> str | None:
    """Minutes-since-4am -> a wall clock time. Formatted backend-side because
    the rollover the offset is measured from is the backend's call; a late
    enough value lands after midnight and reads as the small hours, which is
    correct for a day that runs 4am to 4am."""
    if minutes is None:
        return None
    start, _ = _day_bounds(day)
    return (start + timedelta(minutes=minutes)).strftime("%-I:%M %p")


def _covered_minutes(times: list[datetime]) -> float:
    """Minutes of a day actually accounted for by these fixes, in order.

    Each gap counts for what it spans, capped at MAX_GAP_MIN so an offline
    stretch doesn't credit the day with hours nothing was watching — the same
    cap, for the same reason, that keeps a dead zone from inflating whichever
    cluster the phone was last seen in. One function because _day_shape shows
    this number on the card and coverage_minutes gates on it, and a card that
    displays one coverage while the gate reads another is two answers to one
    question.
    """
    return round(sum(min((b - a).total_seconds(), MAX_GAP_SECONDS)
                     for a, b in zip(times, times[1:])) / 60, 1)


def is_thin(coverage_min: float | None) -> bool:
    """Was this day watched too little for its numbers to mean anything?

    Absence reads as thin here, which is the opposite of screen.is_thin, and
    the difference is real rather than an oversight: this coverage is derived
    from location_points, the raw source of truth that is never dropped, so a
    day missing from the map is a day with no fixes at all. Screen's absence
    means a day synced before its coverage_min existed, which is a hole in
    the record of an honest day, not a thin day. Nothing rests on the choice
    today — a day with no points has no location_days rows to gate either —
    but the two functions now sit in parallel, so the reason they disagree
    belongs next to them.
    """
    return coverage_min is None or coverage_min < COVERAGE_FLOOR_MIN


def coverage_minutes(con, days: list[str]) -> dict[str, float]:
    """How much of each day the fixes account for — the denominator behind
    COVERAGE_FLOOR_MIN. Public because features.py gates on it too: the matrix
    has to refuse a thin day for the same reason the card does, and one query
    with one meaning is how the two thresholds stay the same threshold.

    Every caller should go through is_thin rather than comparing to
    COVERAGE_FLOOR_MIN directly, so the absence rule above stays in one place.
    """
    if not days:
        return {}
    marks = ",".join("?" * len(days))
    by_day: dict[str, list[datetime]] = defaultdict(list)
    for r in con.execute(
            f"SELECT day, ts FROM location_points"
            f" WHERE day IN ({marks}) ORDER BY day, ts", days):
        by_day[r["day"]].append(parse_ts(r["ts"]))
    return {d: _covered_minutes(ts) for d, ts in by_day.items()}


def _day_shape(con, day: str) -> dict:
    """What the day's numbers rest on: how many fixes, how much of the day
    they account for, and whether the leaving and the coming home were
    actually *seen*.

    first_leave is the first fix that was away from home — a real departure
    only if a fix at home came before it. On 2026-08-03 the phone's first fix
    of any kind was 2:50 PM and away, so the stored first_leave of 2:57 PM
    would render as "left home at 2:57 PM" for a day whose morning simply
    wasn't recorded. Likewise last_return is the last fix seen at home, which
    is a homecoming only if the day's last fix is that one; otherwise the day
    ended out and the number is just when they were last spotted in.
    """
    pts = _load_points(con, day)
    if not pts:
        return {"fixes": 0, "coverage_min": 0.0,
                "leave_observed": False, "return_observed": False}
    # bool() because at_home is None on a day with no known home; the two
    # flags only ever gate first_leave/last_return, which _compute_days does
    # not write for such a day, so there is nothing for them to mislabel.
    return {"fixes": len(pts),
            "coverage_min": _covered_minutes([p["ts"] for p in pts]),
            "leave_observed": bool(pts[0]["at_home"]),
            "return_observed": bool(pts[-1]["at_home"])}


def summary() -> dict:
    """One payload for the desk card, in one connection.

    Not get(): that shape is one metric per call, so a card wanting six of
    them plus the day's coverage would cost seven round trips. Centered on the
    newest day with real coverage rather than on today, because at 6am today
    has a fix or two at most — the same reason screen.summary() centers on the
    newest day with data, one threshold stricter.
    """
    today = db.day_key()
    start = (date.fromisoformat(today) - timedelta(days=SUMMARY_DAYS - 1)).isoformat()
    with db.session() as con:
        _ensure_days(con, days_between(start, today))
        rows = con.execute(
            "SELECT day, metric, value, refreshed_at FROM location_days"
            " WHERE day BETWEEN ? AND ? ORDER BY day", (start, today)).fetchall()
        by_day: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            by_day[r["day"]][r["metric"]] = r["value"]
        covered = coverage_minutes(con, days_between(start, today))
        covered_days = {d for d in by_day if not is_thin(covered.get(d))}
        day = max(covered_days, default=None)
        shape = _day_shape(con, day) if day else {}

    # Absent day = absent point, never 0 — and a thin day counts as absent
    # here, since its 0km is an artifact of the phone being quiet.
    trend = [{"day": d,
              "value": by_day.get(d, {}).get(TREND_METRIC) if d in covered_days else None}
             for d in days_between(start, today)]
    seen = [p["value"] for p in trend if p["value"] is not None]

    # Per-metric history for the card's lede, on the same covered-days rule
    # `trend` uses: a day without enough fixes is a hole in every one of these,
    # because the phone being quiet is not the same as you not having moved.
    series = {
        m: [{"day": d,
             "value": by_day.get(d, {}).get(m) if d in covered_days else None}
            for d in days_between(start, today)]
        for m in ("km_traveled", "places", "time_at_home_min")}

    fix = latest_fix()
    # Why the day on the card isn't today, in the two forms that can take.
    # `home` is the ordinary one and is not a fault: no fixes since arriving
    # home means still home. `fix_stale` is the other one — old fixes that end
    # away from home, where the silence really is unexplained.
    home = home_stretch() if fix else None
    out = {
        "day": day,
        "series": series,
        "is_today": day == today,
        "trend": trend,
        "days_with_data": len(seen),
        "median_km": round(median(seen), 2) if seen else None,
        "fix_age_min": fix["age_min"] if fix else None,
        "home_since": home["since"] if home else None,
        "home_for_min": home["minutes"] if home else None,
        "fix_stale": bool(fix and not fix["at_home"]
                          and fix["age_min"] > STALE_FIX_MIN),
        "refreshed_at": max((r["refreshed_at"] for r in rows), default=None),
    }
    out.update({m: by_day.get(day, {}).get(m) for m in METRICS})
    out.update(shape)
    out["entropy_label"] = _entropy_label(out.get("entropy"))
    out["first_leave_at"] = _clock(day, out.get("first_leave")) if day else None
    out["last_return_at"] = _clock(day, out.get("last_return")) if day else None
    return out


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache; recompute any never-computed days first
    (local-only, no external pull — unlike other providers' get())."""
    metric = metric or DEFAULT_METRIC
    with db.session() as con:
        _ensure_days(con, rng.days())
        rows = con.execute(
            "SELECT day, metric, value, refreshed_at FROM location_days"
            " WHERE day BETWEEN ? AND ?", (rng.start_key, rng.end_key)).fetchall()
        data = [{"day": r["day"], "value": r["value"]} for r in rows if r["metric"] == metric]
        refreshed = max((r["refreshed_at"] for r in rows if r["metric"] == metric), default=None)
    return {"metric": metric, "data": data, "refreshed_at": refreshed}


def recompute_history() -> int:
    """Recompute every day the raw points cover; returns the count.

    Run after changing COVERAGE_FLOOR_MIN, because a threshold move reaches
    nothing on its own. features._stale_days triggers on a *source* writing
    after a day's features were computed, and a constant changing writes
    nothing — so the 8 days the 2026-08-30 move to a coverage gate should have
    dropped would have kept their location features indefinitely, which is the
    same confidently-wrong reading the gate was built to stop, arrived at by
    doing nothing. Re-stamping location_days makes those days stale, and the
    next matrix read rebuilds them through the new gate.

    Safe to run any time: it is pure local arithmetic over location_points,
    the source of truth, and _compute_days rebuilds a day from scratch rather
    than amending it. The features rebuild it triggers is safe for the same
    reason the retention comment in features.py gives — location rewrites the
    day it refreshes, so a past day stays answerable.

        python -m app.providers.location --recompute-history
    """
    with db.session() as con:
        days = [r["day"] for r in con.execute(
            "SELECT DISTINCT day FROM location_points ORDER BY day")]
        _compute_days(con, days, datetime.now().isoformat(timespec="seconds"))
        return len(days)


# ---------------------------------------------------------------------- map
#
# Everything above reduces a day of fixes to six numbers, which is the right
# shape for a card and the wrong one for "where was I on Tuesday". trace()
# hands back the fixes themselves plus the places they cluster into, and the
# map sheet does the rest.
#
# Deliberately the same clustering the `places` metric uses — the same grid
# cell, the same 3-minute dwell floor — rather than a second, prettier one. A
# map that drew its own idea of where you stopped would disagree with the
# number on the card, and then there would be two answers to one question.
# The known bias travels with it (`places` correlates with coverage at +0.76;
# see IDEAS), which is why a place carries `minutes` and `visits` rather than
# any claim about importance.

MAP_MAX_POINTS = 6000          # ~4 months of ordinary fixes; a cap, not a target


def trace(rng: Range) -> dict:
    """Every fix in the range, grouped by day, plus the dwelt places.

    Local-only, like the rest of this provider: the listener is the source and
    there is nothing to pull. Coordinates are rounded to 5dp (~1m), which is
    finer than the fixes themselves and cuts the payload by a third."""
    with db.session() as con:
        _ensure_days(con, rng.days())
        rows = con.execute(
            "SELECT ts, day, lat, lon, accuracy FROM location_points"
            " WHERE day BETWEEN ? AND ? ORDER BY ts",
            (rng.start_key, rng.end_key)).fetchall()

    known = places.load()
    home = places.home_on(rng.end_key, known) or places.home_now(known)

    total = len(rows)
    # Thinning keeps every nth fix rather than a sampled window, so the path
    # keeps its shape and its ends; the places below are always computed over
    # the *full* set, because dropping fixes would drop dwell time.
    step = max(1, -(-total // MAP_MAX_POINTS))
    days: dict[str, list] = defaultdict(list)
    for i, r in enumerate(rows):
        if i % step == 0:
            days[r["day"]].append([round(r["lat"], 5), round(r["lon"], 5),
                                   int(parse_ts(r["ts"]).timestamp())])

    return {
        "start": rng.start_key,
        "end": rng.end_key,
        "days": [{"day": d, "points": pts} for d, pts in sorted(days.items())],
        "places": _places_over(rows, home, known),
        "bounds": _bounds(rows),
        # The home in effect at the end of the range. A range that straddles a
        # move draws the address you moved to, which is the one the recent path
        # is relative to; `named` carries every declared place either way.
        "home": {"lat": home.lat, "lon": home.lon, "radius_m": home.radius_m,
                 "name": home.name},
        "named": [p.as_dict() for p in places.named_on(rng.end_key, known)],
        "fixes": total,
        "shown": sum(len(p) for p in days.values()),
        "gap_min": MAX_GAP_MIN,
    }


def cluster_at(lat: float, lon: float, rng: Range) -> dict:
    """The clustered place nearest (lat, lon) over `rng`, recomputed here.

    Shared by everything that answers a question *about one place on the map* —
    the name suggester and the per-visit labeller both — so that all of them
    are talking about the place the map drew and not each their own idea of it.
    Raises LookupError when the point isn't near a cluster, which callers turn
    into a 404.

    `span_m` comes back with it because half the callers want it and computing
    it twice from `members` is how the two drift apart."""
    with db.session() as con:
        rows = con.execute(
            "SELECT ts, day, lat, lon FROM location_points"
            " WHERE day BETWEEN ? AND ? ORDER BY ts",
            (rng.start_key, rng.end_key)).fetchall()

    pts = [{"ts": parse_ts(r["ts"]), "lat": r["lat"], "lon": r["lon"],
            "day": r["day"]} for r in rows]
    groups = [g for g in _group_stays(_stays(pts), places.load()) if _is_place(g)]
    if not groups:
        raise LookupError("no clustered place in this range")

    group = min(groups, key=lambda g: places.haversine_m(lat, lon, g["lat"], g["lon"]))
    if places.haversine_m(lat, lon, group["lat"], group["lon"]) > PLACE_MERGE_M:
        raise LookupError("no clustered place near that point")

    group["span_m"] = max(
        (places.haversine_m(m[0], m[1], group["lat"], group["lon"])
         for m in group["members"]), default=0.0)
    return group


def _places_over(rows, home=None, known=None) -> list[dict]:
    """Dwelt places across the whole range, by the same stay detection and the
    same dwell floor the `places` metric uses.

    Deliberately not a second, prettier clustering: a map that drew its own
    idea of where you stopped would disagree with the number on the card, and
    then there would be two answers to one question."""
    pts = [{"ts": parse_ts(r["ts"]), "lat": r["lat"], "lon": r["lon"],
            "day": r["day"]} for r in rows]
    out = []
    for g in _group_stays(_stays(pts), known):
        if not _is_place(g):
            continue
        out.append({
            # A stable handle for the row beside the map. The name when there
            # is one, else the centroid — never the clustering unit, which is
            # an implementation detail the payload used to leak as `cell`.
            "id": g["name"] or f"{g['lat']:.5f},{g['lon']:.5f}",
            "lat": round(g["lat"], 5), "lon": round(g["lon"], 5),
            "minutes": round(g["seconds"] / 60, 1),
            "visits": g["visits"],
            "days": len(g["days"]),
            "first": g["first"].isoformat(), "last": g["last"].isoformat(),
            "at_home": bool(home and home.contains(g["lat"], g["lon"])),
            "name": g["name"],
            "ambiguous_with": g["ambiguous_with"],
        })
    return sorted(out, key=lambda p: -p["minutes"])


def _bounds(rows) -> dict | None:
    if not rows:
        return None
    lats = [r["lat"] for r in rows]
    lons = [r["lon"] for r in rows]
    return {"min_lat": min(lats), "max_lat": max(lats),
            "min_lon": min(lons), "max_lon": max(lons)}


# Repair entry point, not the provider CLI — that one is `python -m
# app.providers location`, which reads and never rewrites history.
if __name__ == "__main__":                                  # pragma: no cover
    import sys
    if "--recompute-history" in sys.argv:
        print(f"recomputed {recompute_history()} day(s) of location history")
    else:
        print("usage: python -m app.providers.location --recompute-history")
