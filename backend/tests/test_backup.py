"""app/backup.py, the monthly snapshot: assertions are about when it runs and the two ways a
due-check lies (month key from the wall clock not day_key(); a half-written file named for the month)."""

import sqlite3
from pathlib import Path

import pytest

from app import backup, db


def _seed(path):
    """A database worth copying, so a truncated copy is distinguishable."""
    con = db.connect()
    with con:
        con.execute("INSERT INTO stub_days (day, metric, value, refreshed_at)"
                    " VALUES ('2026-08-01', 'a', 1.0, '2026-08-01T00:00:00')")
    con.close()


def test_takes_one_then_skips_the_rest_of_the_month(dbfile, frozen_day):
    _seed(dbfile)
    first = backup.run()
    assert first["month"] == "2026-08"
    assert first["bytes"] > 0

    again = backup.run()
    assert again["skipped"] == "already backed up"
    assert again["file"] == first["file"]
    # One cycle every five minutes all month: the check must not accumulate.
    for _ in range(5):
        backup.run()
    assert len(list((dbfile.parent / "backups").glob("app-*.db"))) == 1


def test_force_takes_another(dbfile, frozen_day):
    _seed(dbfile)
    backup.run()
    forced = backup.run(force=True)
    assert "skipped" not in forced
    assert len(list((dbfile.parent / "backups").glob("app-*.db"))) == 2


def test_new_month_is_due_again(dbfile, frozen_day, freeze_day):
    _seed(dbfile)
    backup.run()
    freeze_day("2026-09-01")
    fresh = backup.run()
    assert fresh["month"] == "2026-09"
    assert len(list((dbfile.parent / "backups").glob("app-*.db"))) == 2


def test_filename_follows_day_key_not_the_wall_clock(dbfile, freeze_day):
    """1am on the 1st, day_key() still says last month (4am rollover): name the file from day_key(), not the clock."""
    _seed(dbfile)
    freeze_day("2026-08-31")
    info = backup.run()
    assert info["file"].startswith("app-20260831-")
    assert backup.run()["skipped"]          # the same month, still done


def test_a_failed_copy_does_not_claim_the_month(dbfile, frozen_day, monkeypatch):
    """A copy that dies halfway must not leave a file whose name says the month is covered."""
    _seed(dbfile)

    real_connect = sqlite3.connect
    disk = {"broken": True}

    def dies_on_the_copy(path, *a, **kw):
        if disk["broken"] and str(path).endswith(".part"):
            Path(path).write_bytes(b"half a database")   # wreckage on disk
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(path, *a, **kw)

    # Not monkeypatch.undo(): it also reverts dbfile's db.APP_DB and would back up the real database.
    monkeypatch.setattr(backup.sqlite3, "connect", dies_on_the_copy)
    with pytest.raises(sqlite3.OperationalError):
        backup.run()

    backups = dbfile.parent / "backups"
    assert list(backups.glob("app-*.db")) == []      # nothing claims the month
    assert list(backups.glob("*.part")) == []        # and no wreckage left
    assert backup.existing("2026-08") is None

    disk["broken"] = False
    assert "skipped" not in backup.run()             # so the next cycle retries


def test_prune_keeps_the_newest(dbfile, frozen_day, freeze_day, monkeypatch):
    _seed(dbfile)
    monkeypatch.setattr(backup, "KEEP_SNAPSHOTS", 3)
    for day in ("2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"):
        freeze_day(day)
        backup.run()
    kept = sorted(p.name for p in (dbfile.parent / "backups").glob("app-*.db"))
    assert len(kept) == 3
    assert kept[0].startswith("app-20260601")        # the May one went
    assert kept[-1].startswith("app-20260801")


def test_the_copy_is_a_readable_database(dbfile, frozen_day):
    """A backup you can't open is a file, not a backup."""
    _seed(dbfile)
    info = backup.run()
    copy = dbfile.parent / "backups" / info["file"]
    con = sqlite3.connect(copy)
    rows = con.execute("SELECT day, value FROM stub_days").fetchall()
    con.close()
    assert rows == [("2026-08-01", 1.0)]
