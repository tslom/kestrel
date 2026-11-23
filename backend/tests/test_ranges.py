"""providers/ranges.py — the one place range tokens become day spans. Pure
logic (takes an explicit `today`), so no DB fixture needed. Anchor day is
2026-07-22, a Wednesday, so week/month boundaries are non-trivial."""

from datetime import date

import pytest

from app.providers.ranges import MAX_SPAN_DAYS, Range, parse_range

TODAY = date(2026, 7, 22)  # Wednesday; ISO week Mon 07-20 .. Sun 07-26


def span(token):
    r = parse_range(token, today=TODAY)
    return (r.start_key, r.end_key)


@pytest.mark.parametrize("token,start,end", [
    ("today",      "2026-07-22", "2026-07-22"),
    ("yesterday",  "2026-07-21", "2026-07-21"),
    ("this-week",  "2026-07-20", "2026-07-26"),
    ("last-week",  "2026-07-13", "2026-07-19"),
    ("this-month", "2026-07-01", "2026-07-31"),
    ("last-month", "2026-06-01", "2026-06-30"),
    ("this-year",  "2026-01-01", "2026-12-31"),
    ("last-year",  "2025-01-01", "2025-12-31"),
    ("next-day",   "2026-07-23", "2026-07-23"),
    ("next-week",  "2026-07-27", "2026-08-02"),
    ("next-month", "2026-08-01", "2026-08-31"),
    ("next-year",  "2027-01-01", "2027-12-31"),
])
def test_named_spans(token, start, end):
    assert span(token) == (start, end)


def test_named_future_flag():
    assert parse_range("today", today=TODAY).future is False
    assert parse_range("yesterday", today=TODAY).future is False
    for t in ("next-day", "next-week", "next-month", "next-year"):
        assert parse_range(t, today=TODAY).future is True


def test_month_span_rolls_over_year():
    # December's "next-month" is next January.
    assert span("next-month") == ("2026-08-01", "2026-08-31")
    dec = date(2026, 12, 15)
    r = parse_range("next-month", today=dec)
    assert (r.start_key, r.end_key) == ("2027-01-01", "2027-01-31")


def test_custom_single_day():
    r = parse_range("2026-06-15", today=TODAY)
    assert (r.start_key, r.end_key) == ("2026-06-15", "2026-06-15")
    assert r.future is False


def test_custom_span():
    r = parse_range("2026-06-01..2026-06-15", today=TODAY)
    assert (r.start_key, r.end_key) == ("2026-06-01", "2026-06-15")


def test_custom_future_gated_by_date_not_name():
    # A custom range whose end is past `today` is flagged future.
    r = parse_range("2026-08-01..2026-08-05", today=TODAY)
    assert r.future is True


def test_custom_reversed_span_rejected():
    with pytest.raises(ValueError, match="reversed"):
        parse_range("2026-06-15..2026-06-01", today=TODAY)


def test_custom_span_cap():
    with pytest.raises(ValueError, match="exceeds"):
        parse_range("2020-01-01..2026-01-01", today=TODAY)
    # Exactly MAX_SPAN_DAYS is allowed.
    start = date(2026, 1, 1)
    end = date.fromordinal(start.toordinal() + MAX_SPAN_DAYS - 1)
    r = parse_range(f"{start.isoformat()}..{end.isoformat()}", today=TODAY)
    assert len(r.days()) == MAX_SPAN_DAYS


def test_unknown_token_rejected():
    with pytest.raises(ValueError, match="unknown range"):
        parse_range("this-fortnight", today=TODAY)


def test_invalid_date_rejected():
    with pytest.raises(ValueError, match="invalid date"):
        parse_range("2026-13-40", today=TODAY)


def test_days_is_inclusive():
    r = parse_range("2026-07-20..2026-07-22", today=TODAY)
    assert r.days() == ["2026-07-20", "2026-07-21", "2026-07-22"]


def test_days_single():
    assert parse_range("today", today=TODAY).days() == ["2026-07-22"]


def test_range_keys():
    r = Range(token="x", start=date(2026, 1, 2), end=date(2026, 1, 3), future=False)
    assert r.start_key == "2026-01-02"
    assert r.end_key == "2026-01-03"


# ---------- aliases ----------

def test_tomorrow_is_next_day():
    """A console asked "what's on my calendar tomorrow?" at 02:30 found no
    `tomorrow` token, computed the date off the wall clock, and queried two
    day-keys out — the clock and the day-key disagree until 4am."""
    assert span("tomorrow") == span("next-day")


def test_an_alias_resolves_to_its_canonical_token():
    """Range.token stays canonical so nothing keyed on it double-caches."""
    assert parse_range("tomorrow", TODAY).token == "next-day"


def test_an_alias_keeps_the_future_flag():
    assert parse_range("tomorrow", TODAY).future is True


def test_aliases_are_advertised_but_not_duplicated_in_all_ranges():
    from app.providers.ranges import ALL_RANGES, VOCAB
    assert "tomorrow" in VOCAB and "tomorrow" not in ALL_RANGES
    assert set(ALL_RANGES) <= set(VOCAB)
