"""Which timezone the user is *in* on a given date.

Rendering picks one zone per event date (where the user will be, not where the
event was filed) and says which. The machine clock is right only for today.
TIMELINE is explicit, not derived from flights.
Design: DESIGN.md → `app/tz.py`: which timezone the user is in.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

# (first day in this zone, IANA zone), sorted. Name the flight in the comment.
TIMELINE: list[tuple[str, str]] = [
    ("2026-08-17", "America/Los_Angeles"),  # RDU→LAX, AA 2299 (USC move-in 8/19)
    ("2026-11-25", "America/New_York"),     # LAX→RDU, AA 2308 (Thanksgiving)
    ("2026-11-29", "America/Los_Angeles"),  # RDU→LAX, AA 2308
]

HOME_ZONE = "America/New_York"      # before the first transition

# Airport -> IANA zone: the frame an airline's clock (`trips.dep_local`/`arr_local`) is
# printed in, not where the user is. Unknown codes fall back to zone_on(); see airport_zone_known().
AIRPORT_ZONES: dict[str, str] = {
    "RDU": "America/New_York",
    "LAX": "America/Los_Angeles",
}

# Coast, not %Z: DST state is noise to a reader.
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
    """Short name for a zone already in hand ('ET', 'PT', else %Z); for zones that aren't the user's that day."""
    return _SHORT.get(str(zone)) or datetime.now(zone).strftime("%Z")


def label(day: str | date) -> str:
    """Short zone name for `day` — 'ET', 'PT', else the %Z abbreviation."""
    return label_for_zone(zone_on(day))


def airport_zone_known(code: str | None) -> bool:
    """Is `code` in AIRPORT_ZONES? Lets a caller that must not guess abstain."""
    return bool(code) and code.upper() in AIRPORT_ZONES


def airport_zone(code: str | None, day: str | date) -> ZoneInfo:
    """The zone an airline clock at `code` is printed in; unknown airports fall back to the user's zone on `day`."""
    if code and code.upper() in AIRPORT_ZONES:
        return ZoneInfo(AIRPORT_ZONES[code.upper()])
    return zone_on(day)


def to_local(value: str, day: str | date) -> str:
    """Re-express an ISO timestamp in the zone for `day`, keeping the instant; all-day dates pass through."""
    if len(value) <= 10:
        return value
    return datetime.fromisoformat(value).astimezone(zone_on(day)).isoformat()
