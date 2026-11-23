"""Which timezone the user is *in* on a given date.

Every timestamp on this dashboard is unambiguous as an instant — Google stores
each event with its own UTC offset — but that is not the same as being readable.
A calendar slice mixes offsets freely: a club call sits at -04:00 and a USC
orientation block at -07:00, so a list of events rendered in each event's own
zone silently changes frame halfway down. Asked to lay that out, a console did
exactly what the data said and produced a table where 9:00 AM and 8:30 PM were
three hours apart in different zones with nothing marking the switch.

So rendering picks one zone per event *date* and says which it is. The zone is
where the user will be, not where the event was filed, because that is the frame
a person plans in: while they're in North Carolina a Los Angeles event at 9am
local is something they'd have to be awake at noon for, and after they move the
same relationship inverts.

The machine clock nearly solves this by itself — macOS follows you, so
`datetime.now().astimezone()` is right for today. It is wrong for every *other*
day, which is most of what a calendar shows: sitting in NC in August, move-in
day renders in Eastern time for an event that will happen in Pacific.

TIMELINE is therefore explicit rather than derived from flights. Deriving it was
the obvious idea and the data doesn't support it: the RDU→LAX flight that starts
all of this is in the calendar but not in the `trips` table, so a flight-driven
rule would have missed the one transition that matters most.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

# (first day in this zone, IANA zone). Sorted; the first entry's zone also
# covers everything before it. Add a line when a move is booked — the comment
# should say which flight, so a wrong entry is traceable to a real itinerary.
TIMELINE: list[tuple[str, str]] = [
    ("2026-08-17", "America/Los_Angeles"),  # RDU→LAX, AA 2299 (USC move-in 8/19)
    ("2026-11-25", "America/New_York"),     # LAX→RDU, AA 2308 (Thanksgiving)
    ("2026-11-29", "America/Los_Angeles"),  # RDU→LAX, AA 2308
]

HOME_ZONE = "America/New_York"      # before the first transition

# Airport -> IANA zone. A different axis from TIMELINE: that one says where the
# *user* is on a date, this says what frame an airline's clock is printed in.
# `trips.dep_local` / `arr_local` are each local to their own airport, which is
# checkable against the data — AA 2299 leaves RDU at 18:00 and reaches LAX at
# 20:19 for a 5h19m block, and AA 2308 confirms it eastbound. So on 2026-08-17
# zone_on() says Los_Angeles (the user lands there) while dep_local's 18:00 is
# Eastern; reading a departure through TIMELINE puts it three hours out.
#
# Explicit for TIMELINE's reason: derivation needs an airport database, and a
# wrong guess here is silent. Unknown codes fall back to the user's zone on the
# day and say so via `airport_zone_known()` — a caller that must not guess can
# then abstain rather than be quietly wrong.
AIRPORT_ZONES: dict[str, str] = {
    "RDU": "America/New_York",
    "LAX": "America/Los_Angeles",
}

# What to print. %Z gives EDT/EST/PDT/PST, which is accurate but noisier than
# useful — the distinction a reader needs is coast, not whether DST is on.
_SHORT = {"America/New_York": "ET", "America/Los_Angeles": "PT"}


def zone_on(day: str | date) -> ZoneInfo:
    """The zone the user is in on `day` (a YYYY-MM-DD string or a date)."""
    key = day if isinstance(day, str) else day.isoformat()
    name = HOME_ZONE
    for start, zone in TIMELINE:
        if key >= start:
            name = zone
        else:
            break
    return ZoneInfo(name)


def label_for_zone(zone: ZoneInfo) -> str:
    """Short name for a zone already in hand — 'ET', 'PT', else %Z.

    Split from label() for callers holding a zone that is *not* the user's on
    that day: a departure airport's, say, where going back through zone_on()
    would relabel it as the zone the reader is in and undo the point.
    """
    return _SHORT.get(str(zone)) or datetime.now(zone).strftime("%Z")


def label(day: str | date) -> str:
    """Short zone name for `day` — 'ET', 'PT', else the %Z abbreviation."""
    return label_for_zone(zone_on(day))


def airport_zone_known(code: str | None) -> bool:
    """Is `code` in AIRPORT_ZONES? Lets a caller that must not guess abstain."""
    return bool(code) and code.upper() in AIRPORT_ZONES


def airport_zone(code: str | None, day: str | date) -> ZoneInfo:
    """The zone an airline clock at `code` is printed in.

    Falls back to the user's zone on `day` for an unknown airport, which is
    right far more often than UTC would be (most flights leave from where you
    are) and wrong in exactly the case worth checking — see AIRPORT_ZONES.
    """
    if code and code.upper() in AIRPORT_ZONES:
        return ZoneInfo(AIRPORT_ZONES[code.upper()])
    return zone_on(day)


def to_local(value: str, day: str | date) -> str:
    """Re-express an ISO timestamp in the zone for `day`, keeping the instant.

    All-day values (bare YYYY-MM-DD) have no instant to move and pass through.
    """
    if len(value) <= 10:
        return value
    return datetime.fromisoformat(value).astimezone(zone_on(day)).isoformat()
