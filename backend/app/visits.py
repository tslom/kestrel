"""Which place, on which visit — and learning your signature for the rest.

§24 named a cluster. This is the correction to the assumption underneath that:
at campus density **a cluster is not a place**. The busiest unnamed cluster in
this vault spans 168m and contains Leavey Library, McCarthy Quad, Everybody's
Kitchen, Birnkrant and the SSCI building, and which one a visit was is a
different answer each time.

Everything automatic was measured first, and all of it failed:

* **Coordinates.** p50 17m / p90 47m of GPS error against ~15m of building
  separation — the measurement `app/places.py` is founded on. No radius
  separates what the sensor never resolved.
* **Time of day alone.** The hub cluster's visits run 08:00 to 02:00 with
  peaks at 11:00 and 19:00 that do not split cleanly by building.
* **The calendar, as a claim about where you were.** Of 71 visit/event
  overlaps in seven weeks, **8** were spatially usable: 48% geocode only to
  the campus centroid, so their position is unknown, and 41% are more than
  150m away and merely coincide in time. The KAP/DMC/ACC labels that show up
  against mid-campus visits are events happening elsewhere while you stood in
  the middle of campus.

The calendar does come back below, but doing a different job. An event is used
here only for its **identity**, never its position: two visits that coincided
with the same recurring lecture are probably the same place, whichever place
that is. That is safe where the position claim was not — at the home dorm, 4
visits overlap CSCI 103 because you were home while a lecture happened, and
saying "these four are alike" is harmless when all four are labelled home,
where saying "these four were at SAL" was a lie.

It is worth the little it is worth, and no more. Measured over 261 real
visits: 28% coincide with any timed event, which is the ceiling for offering
one as a one-click chip, and 9% coincide with one that recurs at least three
times at the same cluster, which is the ceiling for it as a learning signal.
And it is strongest exactly where it is least needed — 6 of 8 visits to the
SAL cluster, 5 of 13 to the THH one, both already single buildings — while the
168m hub that motivated this module has 40 real visits, 9 with any event, and
nothing recurring but a one-off "Bid Day".

So the label comes from you. What can be learned is the *shape* of your
visits: once a few are labelled, "evening and long" and "midday and short" are
genuinely different, and that is what `predict` scores. It is a weighted vote
over your own labelled visits — not a model, because with a few dozen labels
per cluster a model would be a way of hiding how little evidence there is.
Every prediction carries the count that produced it for the same reason.

One caution the numbers force. A large minority of "visits" are not visits:
25 of the hub's 60 are a single fix followed by `MAX_GAP_MIN` of booked
silence — a sighting, which `location.py` already argues about at the place
level under PLACE_MIN_FIXES and nobody applied to stays within a place. They
are still listed, because a sighting is a real thing that happened and hiding
it would be its own lie, but they are marked and never used as evidence for a
prediction. You should not be labelling phantoms, and the learner should not
be learning from them.
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

# A visit has to have been seen twice to be evidence. Same rule and same
# reasoning as PLACE_MIN_FIXES, applied one level down: dwell is booked as the
# gap to the next fix, so one fix followed by silence books up to MAX_GAP_MIN
# on the strength of a single observation.
#
# Deliberately NOT applied to the dwell totals on the card. At home the phone
# genuinely stops reporting while you sit still — `HOME_QUIET_MAX_MIN` in the
# provider says so — and 1111 of the home dorm's 2636 minutes are on single-fix
# stays, so dropping them from the metric would gut `time_at_home_min` to fix
# a problem it doesn't have. Here it only decides what may be *learned from*.
EVIDENCE_MIN_FIXES = 2

# How far apart two visits can be in the day and still count as the same kind
# of visit. Circular: 23:00 and 01:00 are two hours apart, not twenty-two.
HOUR_SIGMA = 2.5
# Duration is compared as a ratio rather than a difference — 20m against 40m
# is the same gap as 40m against 80m, and a lecture is not a coffee.
DURATION_SIGMA = 0.7          # in natural log units, so e^0.7 ~ a 2x ratio
# A weekday visit is weak evidence about a weekend one.
WEEKEND_MISMATCH = 0.5
# Two visits that coincided with the same recurring event are pulled this far
# toward "the same kind of visit", whatever the clock says. Applied as a pull
# toward 1 rather than a multiplier so similarity stays in [0, 1], and only
# when both visits have an event and the events agree — a *disagreement* says
# nothing, since two different lectures can happen at one building.
EVENT_AGREEMENT = 0.5
# How often a series must recur at a cluster before its identity counts as a
# signal rather than a coincidence. Three is where "Bid Day" (a one-off that
# ran long) stops qualifying and a weekly lecture starts.
EVENT_MIN_RECURRENCE = 3

# Below this many labelled visits at a cluster, no prediction is offered at
# all. Two labels can only ever say "one of the two things you have named",
# which is not knowledge, and a confident-looking guess off two examples is
# worse than an empty field.
MIN_LABELS_TO_PREDICT = 3

# How much a visit has to resemble *something* you have labelled before a guess
# is offered at all. The vote share is relative — it says which label won, not
# whether any of them fit — so on its own it will hand a 03:00 four-minute stay
# to "Everybody's Kitchen" at 90% for no better reason than that lunch is also
# short. This is the absolute number that catches that. Measured on the two
# ends: a genuine match (20:00/100m against 19:00/90m) scores 0.91, and that
# 03:00 stay scores 0.0001 against everything labelled.
MIN_SUPPORT = 0.05

# How far outside a stay's span a label may sit and still belong to it. Stay
# boundaries are recomputed from whatever fixes the asked-for range contains,
# so a range starting mid-visit yields a stay that begins *after* the instant
# the label was written against. One gap's tolerance re-attaches it; beyond
# that it is a different visit.
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
    """Every labelled visit at this cluster, whenever it happened.

    Not limited to the range being viewed: the point of learning a signature is
    that last month's labels tell you about this week's visits."""
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
    """A recurring event's identity, stripped of per-instance noise so that
    "CSCI 103 (Lecture)" and "CSCI 103 (Discussion)" count as one series."""
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
    """The timed event this visit overlapped, if one did.

    Virtual locations are skipped for the reason §14 records: a Zoom URL
    geocodes, confidently and wrongly. Here it would not mislead about
    position — position is never read — but "standup" is not a place and
    offering it as a name would waste the click."""
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
        # The shortest overlapping event: an all-afternoon block that happens
        # to span the visit says less than the 50 minutes that matches it.
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
    """The things about a visit that could plausibly identify it.

    `series` is the recurring event it coincided with, carried as identity
    only — this function never sees where the event was, because that is the
    reading the measurements ruled out."""
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

    # Same recurring event, so probably the same errand — pulled toward 1
    # rather than multiplied, so this can only ever raise a similarity. Two
    # *different* events change nothing: one building holds many lectures.
    if len(a) > 3 and len(b) > 3 and a[3] and a[3] == b[3]:
        return base + (1.0 - base) * EVENT_AGREEMENT
    return base


def predict(stay: dict, examples: list[tuple[tuple, str]],
            sig: tuple | None = None) -> dict | None:
    """Which place this visit probably was, from your own labelled visits.

    A weighted vote: every labelled visit at this cluster gets a say, weighted
    by how much it looks like this one. Transparent on purpose — the result
    carries how many examples backed it and how strong the runner-up was, so a
    guess resting on three visits cannot be mistaken for one resting on thirty.
    """
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
    # Nothing you have labelled looks like this visit. The share would still
    # name a winner — something is always top — and that winner would be an
    # artefact of which label happens to be marginally less unlike it.
    if total <= 0 or support < MIN_SUPPORT:
        return None

    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    best, best_w = ranked[0]
    runner_up = ranked[1][1] / total if len(ranked) > 1 else 0.0
    return {
        "name": best,
        # Share of the vote, not a probability of being right. Named `share`
        # rather than `confidence` so nobody reads it as the latter — and
        # paired with `support`, which is the part `share` cannot say: how
        # much this visit resembles the closest thing you have labelled.
        "share": round(best_w / total, 3),
        "support": round(support, 3),
        "from_labels": counts[best],
        "total_labels": len(examples),
        "runner_up": round(runner_up, 3),
    }


# ------------------------------------------------------------------- the list

def _match_labels(stays: list[dict], by_start: dict[str, str]) -> list[str | None]:
    """Assign each stored label to at most one stay, nearest first.

    Not a containment test, which was the first attempt and is wrong in the
    direction that actually happens: stay boundaries are recomputed from the
    fixes inside the range asked for, so viewing a narrower range yields a stay
    that *begins after* the instant its label was written against, and the
    label falls outside the span rather than inside it.

    So: distance from the label to the stay's span, nearest stay wins, and one
    label can never label two visits."""
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

    # Only a series that actually recurs *here* counts as a signal. A one-off
    # that happened to run long — "Bid Day" spanned three visits to the hub —
    # is a coincidence of the clock, and treating it as identity would glue
    # three unrelated visits together.
    seen = Counter(e["series"] for e in events if e and e["series"])
    recurring = {s for s, n in seen.items() if n >= EVENT_MIN_RECURRENCE}

    def sig(i):
        e = events[i]
        series = e["series"] if e and e["series"] in recurring else None
        return _signature(stays[i], series)

    # Examples for the learner: labelled, and seen more than once. A sighting
    # is a real event but it is not evidence about what a visit looks like.
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
            # What was on the calendar during this visit. A candidate for the
            # picker, never an answer: it says what you were doing, and only
            # sometimes does that name where you were.
            "event": ev and {"summary": ev["summary"], "where": ev["where"],
                             "recurring": ev["series"] in recurring},
            "started_at": s["first"].isoformat(),
            "day": s["day"],
            "minutes": round(s["seconds"] / 60, 1),
            "fixes": s["n"],
            # One fix and then silence. Shown, because it happened; marked,
            # because calling it a 20-minute visit would be a claim the data
            # does not support.
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
