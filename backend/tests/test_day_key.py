"""db.day_key — the ONLY way "today" is computed anywhere in the app. Local
time with a 4am rollover: 00:00–03:59 belongs to the previous calendar day."""

from datetime import datetime

from app import db


def test_afternoon_is_same_day():
    assert db.day_key(datetime(2026, 7, 22, 14, 30)) == "2026-07-22"


def test_just_after_rollover_is_same_day():
    assert db.day_key(datetime(2026, 7, 22, 4, 0)) == "2026-07-22"


def test_just_before_rollover_is_previous_day():
    assert db.day_key(datetime(2026, 7, 22, 3, 59)) == "2026-07-21"


def test_midnight_is_previous_day():
    assert db.day_key(datetime(2026, 7, 22, 0, 0)) == "2026-07-21"


def test_rollover_across_month_boundary():
    assert db.day_key(datetime(2026, 8, 1, 2, 0)) == "2026-07-31"


def test_default_uses_now():
    # No dt passed → uses datetime.now(); just assert it returns a valid ISO day.
    key = db.day_key()
    datetime.strptime(key, "%Y-%m-%d")  # raises if malformed
