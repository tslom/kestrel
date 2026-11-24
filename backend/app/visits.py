"""Which place, on which visit: user labels plus a similarity-weighted vote.

At campus density a cluster is not a place, and position, time of day and the
calendar-as-location all failed. The label comes from the user; `predict` only
learns the shape of labelled visits. A calendar event is identity, never position.
Design: DESIGN.md → `app/visits.py`: which place, on which visit.
"""

import math
import re
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, places
from .providers import location, ranges

router = APIRouter(prefix="/api")

# Fewer fixes is a sighting: shown, never evidence. Not applied to dwell totals (home phone goes quiet).
EVIDENCE_MIN_FIXES = 2

HOUR_SIGMA = 2.5              # circular: 23:00 and 01:00 are 2h apart
DURATION_SIGMA = 0.7          # natural-log units, so e^0.7 ~ a 2x ratio
WEEKEND_MISMATCH = 0.5
EVENT_AGREEMENT = 0.5         # pull toward 1 when both visits share a recurring series
EVENT_MIN_RECURRENCE = 3      # excludes one-offs
MIN_LABELS_TO_PREDICT = 3     # below this no prediction is offered
MIN_SUPPORT = 0.05            # absolute resemblance floor; a genuine match scores 0.91, a 03:00 stay 0.0001
LABEL_MATCH_TOLERANCE_MIN = location.MAX_GAP_MIN


def _key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 4), round(lon, 4)


def _parse(ts: str) -> datetime:
    return location.parse_ts(ts)


# -------------------------------------------------------------------- storing

def label(started_at: str, lat: float, lon: float, name: str) -> dict:
    """Assert which place one visit was. An empty name clears the label."""
    key_lat, key_lon = _key(lat, lon)
    started = _parse(started_at).isoformat()
    name = (name or "").strip()

    with db.session() as con:
        if not name:
            con.execute("DELETE FROM visit_labels WHERE started_at = ?", (started,))
        else:
            con.execute(
                "INSERT OR REPLACE INTO visit_labels"
                " (started_at, lat, lon, name, at) VALUES (?,?,?,?,?)",
                (started, key_lat, key_lon, name,
                 datetime.now().isoformat(timespec="seconds")))
        con.commit()
    return {"started_at": started, "name": name or None}


def labels_at(lat: float, lon: float) -> list[dict]:
    """Every labelled visit at this cluster, whenever it happened (not range-limited)."""
    key_lat, key_lon = _key(lat, lon)
    with db.session() as con:
        rows = con.execute(
            "SELECT started_at, name FROM visit_labels"
            " WHERE lat = ? AND lon = ? ORDER BY started_at",
            (key_lat, key_lon)).fetchall()
    return [{"started_at": r["started_at"], "name": r["name"]} for r in rows]


# ------------------------------------------------------------------ predicting

_INSTANCE_NOISE = re.compile(r"\s*\((Lecture|Discussion|Lab|Quiz|Section)\)\s*$",
                             re.IGNORECASE)


def _series(summary: str | None) -> str | None:
    """A recurring event's identity: "CSCI 103 (Lecture)" and "(Discussion)" are one series."""
    if not summary:
        return None
    return _INSTANCE_NOISE.sub("", summary.strip())[:40] or None


def _events_by_day(days: list[str]) -> dict[str, list]:
    if not days:
        return {}
    with db.session() as con:
        rows = con.execute(
            "SELECT day, summary, location, start_at, end_at FROM events"
            " WHERE day BETWEEN ? AND ? AND all_day = 0",
            (min(days), max(days))).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["day"], []).append(r)
    return out


def _event_for(stay: dict, by_day: dict) -> dict | None:
    """The timed event this visit overlapped, if one did. Virtual locations are skipped: "standup" is not a place."""
    from .leave import is_virtual
    best = None
    for e in by_day.get(stay["day"], ()):
        try:
            start = location.parse_ts(e["start_at"])
            end = location.parse_ts(e["end_at"])
        except (ValueError, TypeError):
            continue
        if not (stay["first"] < end and start < stay["last"]):
            continue
        if e["location"] and is_virtual(e["location"]):
            continue
        # shortest overlap: an all-afternoon block says less than the 50 minutes
        span = (end - start).total_seconds()
        if best is None or span < best[0]:
            best = (span, e)
    if best is None:
        return None

    e = best[1]
    from .place_suggest import event_place
    return {"series": _series(e["summary"]),
            "summary": (e["summary"] or "").strip() or None,
            # The place part of the location string, for the picker chip —
            # "KAP 267, 3620 S Vermont Ave, ..." -> "KAP 267".
            "where": event_place(e["location"] or "")}


def _signature(stay: dict, series: str | None = None) -> tuple:
    """What could identify a visit. `series` is event identity only; never the event's position."""
    start = stay["first"]
    hours = start.hour + start.minute / 60
    minutes = max(1.0, stay["seconds"] / 60)
    return hours, math.log(minutes), start.weekday() >= 5, series


def _hour_gap(a: float, b: float) -> float:
    d = abs(a - b) % 24
    return min(d, 24 - d)


def _similarity(a: tuple, b: tuple) -> float:
    """How much one labelled visit says about another, in [0, 1]."""
    hour = math.exp(-0.5 * (_hour_gap(a[0], b[0]) / HOUR_SIGMA) ** 2)
    dur = math.exp(-0.5 * ((a[1] - b[1]) / DURATION_SIGMA) ** 2)
    weekend = 1.0 if a[2] == b[2] else WEEKEND_MISMATCH
    base = hour * dur * weekend

    # same recurring event pulls toward 1 (only ever raises); different events change nothing
    if len(a) > 3 and len(b) > 3 and a[3] and a[3] == b[3]:
        return base + (1.0 - base) * EVENT_AGREEMENT
    return base


def predict(stay: dict, examples: list[tuple[tuple, str]],
            sig: tuple | None = None) -> dict | None:
    """Which place this visit probably was: a similarity-weighted vote over labelled visits.

    The result carries its example counts so a guess off three visits is visible as one."""
    if len(examples) < MIN_LABELS_TO_PREDICT:
        return None
    if sig is None:
        sig = _signature(stay)

    votes: dict[str, float] = {}
    counts: dict[str, int] = {}
    support = 0.0
    for other, name in examples:
        w = _similarity(sig, other)
        support = max(support, w)
        votes[name] = votes.get(name, 0.0) + w
        counts[name] = counts.get(name, 0) + 1

    total = sum(votes.values())
    # Nothing labelled looks like this visit; the top share would be an artefact.
    if total <= 0 or support < MIN_SUPPORT:
        return None

    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    best, best_w = ranked[0]
    runner_up = ranked[1][1] / total if len(ranked) > 1 else 0.0
    return {
        "name": best,
        # vote share, not a probability; `support` is what share cannot say
        "share": round(best_w / total, 3),
        "support": round(support, 3),
        "from_labels": counts[best],
        "total_labels": len(examples),
        "runner_up": round(runner_up, 3),
    }


# ------------------------------------------------------------------- the list

def _match_labels(stays: list[dict], by_start: dict[str, str]) -> list[str | None]:
    """Assign each stored label to at most one stay, nearest to its span first.

    Not containment: a narrower range yields a stay that begins after its label."""
    names: list[str | None] = [None] * len(stays)
    tol = LABEL_MATCH_TOLERANCE_MIN * 60
    taken = set()

    for started, name in by_start.items():
        try:
            when = _parse(started)
        except ValueError:
            continue
        best, best_gap = None, None
        for i, s in enumerate(stays):
            if i in taken:
                continue
            if s["first"] <= when <= s["last"]:
                gap = 0.0
            else:
                gap = min(abs((when - s["first"]).total_seconds()),
                          abs((when - s["last"]).total_seconds()))
            if gap <= tol and (best_gap is None or gap < best_gap):
                best, best_gap = i, gap
        if best is not None:
            names[best] = name
            taken.add(best)
    return names


def over(lat: float, lon: float, rng) -> dict:
    """Every visit to this cluster in the range, labelled or predicted."""
    group = location.cluster_at(lat, lon, rng)
    known = labels_at(group["lat"], group["lon"])
    by_start = {r["started_at"]: r["name"] for r in known}

    stays = sorted(group["stays"], key=lambda s: s["first"])
    names = _match_labels(stays, by_start)

    by_day = _events_by_day([s["day"] for s in stays if s.get("day")])
    events = [_event_for(s, by_day) for s in stays]

    # Only a series that recurs here counts as identity; a one-off would glue unrelated visits.
    seen = Counter(e["series"] for e in events if e and e["series"])
    recurring = {s for s, n in seen.items() if n >= EVENT_MIN_RECURRENCE}

    def sig(i):
        e = events[i]
        series = e["series"] if e and e["series"] in recurring else None
        return _signature(stays[i], series)

    # Learner examples: labelled and not a sighting.
    examples = [(sig(i), names[i]) for i, s in enumerate(stays)
                if names[i] and s["n"] >= EVIDENCE_MIN_FIXES]

    out = []
    for i, s in enumerate(stays):
        name = names[i]
        sighting = s["n"] < EVIDENCE_MIN_FIXES
        guess = None
        if not name and not sighting:
            guess = predict(s, examples, sig(i))
        ev = events[i]
        out.append({
            # a picker candidate, never an answer
            "event": ev and {"summary": ev["summary"], "where": ev["where"],
                             "recurring": ev["series"] in recurring},
            "started_at": s["first"].isoformat(),
            "day": s["day"],
            "minutes": round(s["seconds"] / 60, 1),
            "fixes": s["n"],
            # one fix then silence: shown, but not claimed as a 20-minute visit
            "sighting": sighting,
            "off_centre_m": round(places.haversine_m(
                s["lat"], s["lon"], group["lat"], group["lon"])),
            "name": name,
            "predicted": guess,
        })

    names = sorted({r["name"] for r in known})
    return {
        "place": {"lat": round(group["lat"], 6), "lon": round(group["lon"], 6),
                  "name": group["name"], "span_m": round(group["span_m"]),
                  "visits": len(stays),
                  "sightings": sum(1 for r in out if r["sighting"])},
        "visits": list(reversed(out)),          # newest first, like the sheet
        "used_before": names,
        "labelled": len(examples),
        "min_to_predict": MIN_LABELS_TO_PREDICT,
    }


# -------------------------------------------------------------------- routes

class VisitLabel(BaseModel):
    started_at: str
    lat: float
    lon: float
    name: str = ""          # empty clears it


@router.get("/location/place/visits")
def place_visits(lat: float, lon: float, range: str = "this-week"):
    try:
        return over(lat, lon, ranges.parse_range(range))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.post("/location/visits/label")
def set_visit_label(body: VisitLabel):
    try:
        return label(body.started_at, body.lat, body.lon, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
