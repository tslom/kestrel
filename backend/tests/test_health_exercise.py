"""health._auto_exercise — the AZM-driven Exercise auto-check-in leg of
refresh(). Seeds health_days directly (the sync half is Google-API-bound and
tested elsewhere) and asserts both gates (AZM volume + peak HR) and the
one-per-qualifying-day fill."""

from datetime import date

from app import db
from app.providers import health


def _seed_azm(con, rows, hr_max=190):
    """Seed AZM per day, plus an hr_max that clears the peak gate by default.
    Pass `hr_max=None` to seed no peak at all, or a per-row (day, azm, hr)
    triple to vary it."""
    for row in rows:
        day, azm, hr = (*row, hr_max)[:3]
        con.execute(
            "INSERT INTO health_days (day, metric, value, refreshed_at)"
            " VALUES (?, 'active_zone_minutes', ?, '2026-07-24T00:00:00')",
            (day, azm))
        if hr is not None:
            con.execute(
                "INSERT INTO health_days (day, metric, value, refreshed_at)"
                " VALUES (?, 'hr_max', ?, '2026-07-24T00:00:00')", (day, hr))
    con.commit()


def test_auto_exercise_checks_only_days_over_threshold(dbfile, frozen_day,
                                                       monkeypatch):
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 30.0)
    monkeypatch.setattr(health, "EXERCISE_HR_MAX_THRESHOLD", 180.0)
    monkeypatch.setattr(health, "EXERCISE_HABIT_NAME", "Exercise")
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-20", 26), ("2026-07-21", 64), ("2026-07-22", 30)])
    n = health._auto_exercise(con, date(2026, 7, 20), date(2026, 7, 22))
    con.close()
    assert n == 2  # 64 and 30 (inclusive) qualify; 26 doesn't
    checked = {x["day"] for x in db.habit_history(days=14)["habits"][0]["days"]
               if x["value"]}
    assert checked == {"2026-07-21", "2026-07-22"}


def test_auto_exercise_requires_peak_hr(dbfile, frozen_day, monkeypatch):
    """Volume alone is not a workout: a long stretch of merely-elevated HR can
    bank more AZM than a real session (alcohol, illness, stress). Only the day
    that actually peaked counts."""
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 30.0)
    monkeypatch.setattr(health, "EXERCISE_HR_MAX_THRESHOLD", 180.0)
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-30", 315, 170),   # huge volume, never peaked
                    ("2026-07-31", 120, 185)])  # real session
    n = health._auto_exercise(con, date(2026, 7, 30), date(2026, 7, 31))
    con.close()
    assert n == 1
    checked = {x["day"] for x in db.habit_history(days=14)["habits"][0]["days"]
               if x["value"]}
    assert checked == {"2026-07-31"}


def test_auto_exercise_skips_days_missing_peak_hr(dbfile, monkeypatch):
    """The gate is a requirement, not a tiebreak — no hr_max cached means no
    check-in, rather than falling back to AZM alone."""
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 30.0)
    monkeypatch.setattr(health, "EXERCISE_HR_MAX_THRESHOLD", 180.0)
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-21", 400)], hr_max=None)
    assert health._auto_exercise(con, date(2026, 7, 21), date(2026, 7, 21)) == 0
    con.close()


def test_auto_exercise_peak_gate_disabled_at_zero(dbfile, monkeypatch):
    """Zeroing the peak threshold drops the gate back to AZM-only, including
    for days with no hr_max cached at all."""
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 30.0)
    monkeypatch.setattr(health, "EXERCISE_HR_MAX_THRESHOLD", 0.0)
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-21", 400)], hr_max=None)
    assert health._auto_exercise(con, date(2026, 7, 21), date(2026, 7, 21)) == 1
    con.close()


def test_auto_exercise_disabled_at_zero_threshold(dbfile, monkeypatch):
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 0.0)
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-21", 400)])
    assert health._auto_exercise(con, date(2026, 7, 21), date(2026, 7, 21)) == 0
    con.close()


def test_auto_exercise_is_idempotent(dbfile, monkeypatch):
    monkeypatch.setattr(health, "EXERCISE_AZM_THRESHOLD", 30.0)
    monkeypatch.setattr(health, "EXERCISE_HR_MAX_THRESHOLD", 180.0)
    db.create_habit("Exercise")
    con = db.connect()
    _seed_azm(con, [("2026-07-21", 64)])
    span = (date(2026, 7, 21), date(2026, 7, 21))
    assert health._auto_exercise(con, *span) == 1
    assert health._auto_exercise(con, *span) == 0  # second pass writes nothing
    con.close()
