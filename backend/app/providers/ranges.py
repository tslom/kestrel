"""Range vocabulary: the ONE place range tokens become day spans.

Anchored on db.day_key() (4am rollover). Weeks are ISO (Mon–Sun); spans are inclusive
[start, end] day-keys. Explicit dates parse too: `2026-06-15` or `2026-06-01..2026-06-15`
(double-dot keeps it one shell-safe word), capped at 366 days.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .. import db

# next-* ranges exist for calendar-like sources only; providers opt in via
# ALLOW_FUTURE. The parser accepts them all — the gate lives in providers.get().
PAST_RANGES = (
    "today", "yesterday",
    "this-week", "last-week",
    "this-month", "last-month",
    "this-year", "last-year",
)
FUTURE_RANGES = ("next-day", "next-week", "next-month", "next-year")
ALL_RANGES = PAST_RANGES + FUTURE_RANGES

# "tomorrow" must be a token: hand-computed dates land a day-key off between midnight and 4am.
# Normalized away on parse, so Range.token and cache keys stay canonical.
ALIASES = {"tomorrow": "next-day"}

# What to advertise to a caller choosing a range (tool schemas, CLI help).
VOCAB = ALL_RANGES + tuple(ALIASES)

CUSTOM_GRAMMAR = "YYYY-MM-DD[..YYYY-MM-DD]"
MAX_SPAN_DAYS = 366  # a typo'd year must not become a giant API backfill
_CUSTOM = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?$")


@dataclass(frozen=True)
class Range:
    token: str
    start: date  # inclusive
    end: date    # inclusive
    future: bool

    @property
    def start_key(self) -> str:
        return self.start.isoformat()

    @property
    def end_key(self) -> str:
        return self.end.isoformat()

    def days(self) -> list[str]:
        return days_between(self.start, self.end)


def days_between(start: str | date, end: str | date) -> list[str]:
    """Inclusive [start, end] as day-keys; the one expander (private copies drift)."""
    d = date.fromisoformat(start) if isinstance(start, str) else start
    last = date.fromisoformat(end) if isinstance(end, str) else end
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _month_span(anchor: date) -> tuple[date, date]:
    start = anchor.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def _parse_custom(token: str, today: date) -> Range:
    m = _CUSTOM.fullmatch(token)
    if m is None:
        raise ValueError(
            f"unknown range {token!r}; expected one of: {', '.join(ALL_RANGES)}"
            f" or {CUSTOM_GRAMMAR}")
    try:
        start = date.fromisoformat(m.group(1))
        end = date.fromisoformat(m.group(2)) if m.group(2) else start
    except ValueError:
        raise ValueError(f"invalid date in range {token!r} ({CUSTOM_GRAMMAR})")
    if end < start:
        raise ValueError(f"reversed span {token!r}: end is before start")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ValueError(f"span {token!r} exceeds {MAX_SPAN_DAYS} days")
    # Future is gated by date, not by name — same rule next-* uses.
    return Range(token=token, start=start, end=end, future=end > today)


def parse_range(token: str, today: date | None = None) -> Range:
    """token -> Range. Raises ValueError on unknown tokens."""
    token = ALIASES.get(token, token)
    t = today or date.fromisoformat(db.day_key())
    monday = t - timedelta(days=t.weekday())
    spans: dict[str, tuple[date, date]] = {
        "today": (t, t),
        "yesterday": (t - timedelta(days=1), t - timedelta(days=1)),
        "this-week": (monday, monday + timedelta(days=6)),
        "last-week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "this-month": _month_span(t),
        "last-month": _month_span(t.replace(day=1) - timedelta(days=1)),
        "this-year": (date(t.year, 1, 1), date(t.year, 12, 31)),
        "last-year": (date(t.year - 1, 1, 1), date(t.year - 1, 12, 31)),
        "next-day": (t + timedelta(days=1), t + timedelta(days=1)),
        "next-week": (monday + timedelta(days=7), monday + timedelta(days=13)),
        "next-month": _month_span((_month_span(t)[1]) + timedelta(days=1)),
        "next-year": (date(t.year + 1, 1, 1), date(t.year + 1, 12, 31)),
    }
    if token not in spans:
        return _parse_custom(token, t)
    start, end = spans[token]
    return Range(token=token, start=start, end=end, future=token in FUTURE_RANGES)
