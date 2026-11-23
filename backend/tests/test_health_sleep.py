"""health._auto_sleep — the sleep_hours-driven Sleep auto-check-in leg of
refresh(). Seeds health_days directly (the sync half is Google-API-bound and
tested elsewhere) and asserts the threshold gate + one-per-qualifying-day fill.
sleep_hours already lands on the wake-up day, so the check-in inherits that."""

from datetime import date

from app import db
from app.providers import health


def _seed_sleep(con, rows):
    for day, hours in rows:
        con.execute(
            "INSERT INTO health_days (day, metric, value, refreshed_at)"
            " VALUES (?, 'sleep_hours', ?, '2026-07-24T00:00:00')",
            (day, hours))
    con.commit()


def test_auto_sleep_checks_only_nights_over_threshold(dbfile, frozen_day,
                                                      monkeypatch):
    monkeypatch.setattr(health, "SLEEP_HOURS_THRESHOLD", 9.0)
    monkeypatch.setattr(health, "SLEEP_HABIT_NAME", "Sleep")
    db.create_habit("Sleep")
    con = db.connect()
    _seed_sleep(con, [("2026-07-20", 6.07), ("2026-07-21", 9.3),
                      ("2026-07-22", 9.0)])
    n = health._auto_sleep(con, date(2026, 7, 20), date(2026, 7, 22))
    con.close()
    assert n == 2  # 9.3 and 9.0 (inclusive) qualify; 6.07 doesn't
    checked = {x["day"] for x in db.habit_history(days=14)["habits"][0]["days"]
               if x["value"]}
    assert checked == {"2026-07-21", "2026-07-22"}


def test_auto_sleep_disabled_at_zero_threshold(dbfile, monkeypatch):
    monkeypatch.setattr(health, "SLEEP_HOURS_THRESHOLD", 0.0)
    db.create_habit("Sleep")
    con = db.connect()
    _seed_sleep(con, [("2026-07-21", 10.5)])
    assert health._auto_sleep(con, date(2026, 7, 21), date(2026, 7, 21)) == 0
    con.close()


def test_auto_sleep_is_idempotent(dbfile, monkeypatch):
    monkeypatch.setattr(health, "SLEEP_HOURS_THRESHOLD", 9.0)
    db.create_habit("Sleep")
    con = db.connect()
    _seed_sleep(con, [("2026-07-21", 9.3)])
    span = (date(2026, 7, 21), date(2026, 7, 21))
    assert health._auto_sleep(con, *span) == 1
    assert health._auto_sleep(con, *span) == 0  # second pass writes nothing
    con.close()


def test_auto_sleep_never_overwrites_manual_uncheck(dbfile, frozen_day,
                                                    monkeypatch):
    """A deliberately cleared night (value=0 tombstone) stays cleared even when
    the hours qualify — the auto pass must not resurrect it."""
    monkeypatch.setattr(health, "SLEEP_HOURS_THRESHOLD", 9.0)
    monkeypatch.setattr(health, "SLEEP_HABIT_NAME", "Sleep")
    habit = db.create_habit("Sleep")
    db.checkin(habit["id"], "2026-07-21", value=0)  # manual un-check tombstone
    con = db.connect()
    _seed_sleep(con, [("2026-07-21", 9.3)])
    assert health._auto_sleep(con, date(2026, 7, 21), date(2026, 7, 21)) == 0
    con.close()
    checked = {x["day"] for x in db.habit_history(days=14)["habits"][0]["days"]
               if x["value"]}
    assert checked == set()
