"""Location provider: OwnTracks fixes arrive via the relay (location_relay.py);
refresh() pulls them into `location_points` (raw, never droppable) and
recomputes `location_days` (derived). Day = `day_key`.

Traps: places are stays, not grid cells, and a declared place (app/places.py)
outranks clustering; home-relative metrics are absent, never 0, on a day with
no known home.

Design: DESIGN.md → `app/providers/location.py`: stays, places, coverage.
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
# Transit tail is <=~2min/place, genuine stops >=~4min (100km driving day: 42 clusters -> 15).
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
# Stay detection and grouping: DESIGN.md → `app/providers/location.py`.
STAY_RADIUS_M = 100
PLACE_MERGE_M = 130
# Cap on a merged place's distance from its own centroid; stops _converge chaining across a campus.
PLACE_SPAN_MAX_M = 175
# A lone fix followed by silence books up to MAX_GAP_MIN: a sighting, not a stay. 3 would zero real travel days.
PLACE_MIN_FIXES = 2


def _is_place(group: dict) -> bool:
    """Whether a group counts as somewhere you went; shared by the `places` metric and the map."""
    return (group["seconds"] > PLACE_MIN_DWELL_SEC
            and group["n"] >= PLACE_MIN_FIXES)


def _stays(pts: list[dict]) -> list[dict]:
    """Contiguous runs of fixes within STAY_RADIUS_M of their running centroid.
    Each fix books the gap to the *next* fix (capped), including a stay's last fix."""
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
    group greedily, longest first, within PLACE_MERGE_M."""
    groups: list[dict] = []
    by_name: dict[str, dict] = {}

    def _new(stay, name=None, named=None):
        g = {"lat": stay["lat"], "lon": stay["lon"], "seconds": 0.0, "n": 0,
             "visits": 0, "days": set(), "name": name,
             "ambiguous_with": list(named.ambiguous_with) if named else [],
             "first": stay["first"], "last": stay["last"], "members": [],
             # read by place_suggest (needs the stays' times, not centroids)
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
    """Merge the closest eligible pair of undeclared groups until none qualify
    (centroids within PLACE_MERGE_M, merged span within PLACE_SPAN_MAX_M).
    Declared places never merge."""
    while True:
        open_groups = [g for g in groups if g["name"] is None]
        pairs = []
        for i, a in enumerate(open_groups):
            for b in open_groups[i + 1:]:
                d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if d <= PLACE_MERGE_M:
                    pairs.append((d, i, a, b))
        # A pair rejected for sprawl disqualifies only itself (span isn't monotone in distance).
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
    """day_key -> [start, end) as local-aware datetimes (4am rollover)."""
    d = date.fromisoformat(day)
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(d, time(hour=DAY_ROLLOVER_HOUR), tzinfo=tz)
    return start, start + timedelta(days=1)


def _minutes_since(dt: datetime, start: datetime) -> float:
    return round((dt - start).total_seconds() / 60, 1)


def latest_fix() -> dict | None:
    """Newest fix ever ingested (not day-scoped), or None before any arrive."""
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


# Must outrun a day's worth of driving fixes.
HOME_STRETCH_LOOKBACK = 500


def home_stretch() -> dict | None:
    """When the current stay at home began and how long it has run; None if the
    newest fix is away. Silence at home is "still home", not a dead phone."""
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
    """A stored fix timestamp (UTC, Z suffix) as local time."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()


def _load_points(con, day: str, home=_UNSET) -> list[dict]:
    """The day's fixes, each tagged `at_home` and grid cell. `home` is the home
    in effect *that day*; None (no known home) makes every `at_home` None, which
    callers must not collapse to False."""
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
    """Recompute location_days for each day from location_points; local only, no I/O."""
    known = places.load()
    with con:
        for day in days:
            home = places.home_on(day, known)
            pts = _load_points(con, day, home)
            con.execute("DELETE FROM location_days WHERE day=?", (day,))
            # Marked even with no points: an empty day is computed, to nothing.
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
                # Over every group, unfiltered: filtering first would make a quiet day look varied.
                "entropy": _shannon_entropy(secs),
                "km_traveled": round(km, 2),
            }
            # Home-relative metrics exist only when the day's home is known (absent, never 0.0).
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
    """Compute any of `days` never computed (or whose fix count moved since,
    per location_sync), and return which. Future days are skipped.
    Every reader backfills its own window; safe only because this is local."""
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
# At-home ceiling: a stay at home produces no fixes, so only a week of stillness is worth flagging.
HOME_QUIET_MAX_MIN = 7 * 24 * 60


def _ago(minutes: float) -> str:
    return f"{round(minutes)}m ago" if minutes < 120 else f"{round(minutes / 60)}h ago"


def health() -> dict:
    """Source health from the age of the newest fix and where it was: silence at
    home is ok, silence away is stale. The relay's last contact (any request
    from the phone) proves the app is alive and overrides."""
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
    """Poller entrypoint: drain the relay, recompute today, yesterday and any day
    the backlog landed on, and notify if location has gone quiet."""
    today = db.day_key()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    pulled = {"stored": 0, "days": []}
    if location_relay.configured():
        try:
            pulled = location_relay.pull()
        except Exception as e:
            if not offline.is_network_error(e):
                # A refusal won't fix itself (once a day); offline is just the lid closed.
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
        # Keyed by the newest fix: one notice per silence.
        notify.send("location", "stale", fix["ts"] if fix else "none",
                    title="Location has gone quiet", body=h["detail"])
    return {"days": days, "pulled": pulled["stored"]}


def brief_line(it: dict) -> str:
    return f"{it['day']} {it['value']}"


SUMMARY_DAYS = DESK_BASELINE_DAYS
# Minutes of day observed below which a day's numbers are noise (absolute, not a fraction of the day).
# Gate on coverage, not fix count; 35 days of history separated at 180.
COVERAGE_FLOOR_MIN = 180
# Not time_at_home_min: coverage gaps bias it hardest.
TREND_METRIC = "km_traveled"
# Observed entropy spread 0.97 (one errand) .. 4.49 (travel day); a normal day lands near 2.5.
ENTROPY_BANDS = ((1.5, "routine"), (3.0, "mixed"))


def _entropy_label(bits: float | None) -> str | None:
    if bits is None:
        return None
    for cut, label in ENTROPY_BANDS:
        if bits < cut:
            return label
    return "varied"


def _clock(day: str, minutes: float | None) -> str | None:
    """Minutes-since-4am -> wall clock; formatted here because the rollover is the backend's."""
    if minutes is None:
        return None
    start, _ = _day_bounds(day)
    return (start + timedelta(minutes=minutes)).strftime("%-I:%M %p")


def _covered_minutes(times: list[datetime]) -> float:
    """Minutes accounted for by these time-ordered fixes, each gap capped at MAX_GAP_MIN.
    The one coverage definition: the card shows it and coverage_minutes gates on it."""
    return round(sum(min((b - a).total_seconds(), MAX_GAP_SECONDS)
                     for a, b in zip(times, times[1:])) / 60, 1)


def is_thin(coverage_min: float | None) -> bool:
    """Was this day watched too little for its numbers to mean anything?
    Absence (None) reads as thin, the opposite of screen.is_thin on purpose."""
    return coverage_min is None or coverage_min < COVERAGE_FLOOR_MIN


def coverage_minutes(con, days: list[str]) -> dict[str, float]:
    """Covered minutes per day. Public: features.py gates on it too. Callers go
    through is_thin, not COVERAGE_FLOOR_MIN directly."""
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
    """Fix count, coverage, and whether the departure and homecoming were *seen*
    (a first_leave is a real departure only if a home fix preceded it; likewise last_return)."""
    pts = _load_points(con, day)
    if not pts:
        return {"fixes": 0, "coverage_min": 0.0,
                "leave_observed": False, "return_observed": False}
    # bool(): at_home is None with no known home, and those flags only gate metrics not written then.
    return {"fixes": len(pts),
            "coverage_min": _covered_minutes([p["ts"] for p in pts]),
            "leave_observed": bool(pts[0]["at_home"]),
            "return_observed": bool(pts[-1]["at_home"])}


def summary() -> dict:
    """One payload for the desk card, centered on the newest day with real
    coverage rather than today."""
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
    nothing — so the 8 days the move to a coverage gate should have
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
# The known bias travels with it (`places` correlates with coverage at +0.76),
# which is why a place carries `minutes` and `visits` rather than
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
