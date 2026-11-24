"""db habit helpers: checkin upsert/un-check and the _streak walk (via list_habits); an un-done today doesn't break a streak until it is actually missed."""

from app import db

DAY = "2026-07-22"
D = lambda n: f"2026-07-{n:02d}"  # terse day literal for this file


def _streak(habit_id, day=DAY):
    row = next(h for h in db.list_habits(day=day) if h["id"] == habit_id)
    return row["streak"]


def test_create_and_default_today_zero(dbfile):
    h = db.create_habit("meditate")
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 0
    assert row["streak"] == 0


def test_checkin_sets_today(dbfile):
    h = db.create_habit("floss")
    db.checkin(h["id"], day=DAY, value=1)
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 1


def test_checkin_zero_unchecks(dbfile):
    h = db.create_habit("floss")
    db.checkin(h["id"], day=DAY, value=1)
    db.checkin(h["id"], day=DAY, value=0)
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 0


def test_checkin_is_idempotent_upsert(dbfile):
    h = db.create_habit("water")
    db.checkin(h["id"], day=DAY, value=1)
    db.checkin(h["id"], day=DAY, value=5)  # overwrite, not duplicate
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 5


def test_streak_consecutive_including_today(dbfile):
    h = db.create_habit("run")
    for n in (20, 21, 22):
        db.checkin(h["id"], day=D(n), value=1)
    assert _streak(h["id"]) == 3


def test_streak_survives_undone_today(dbfile):
    # Today (22) not checked, but 20 and 21 are — streak anchors on yesterday.
    h = db.create_habit("run")
    for n in (20, 21):
        db.checkin(h["id"], day=D(n), value=1)
    assert _streak(h["id"]) == 2


def test_streak_zero_when_last_check_too_old(dbfile):
    # Most recent check is 20 — neither today (22) nor yesterday (21).
    h = db.create_habit("run")
    for n in (19, 20):
        db.checkin(h["id"], day=D(n), value=1)
    assert _streak(h["id"]) == 0


def test_streak_stops_at_gap(dbfile):
    # 22 done, 21 missing, 20 done → only today counts.
    h = db.create_habit("run")
    db.checkin(h["id"], day=D(22), value=1)
    db.checkin(h["id"], day=D(20), value=1)
    assert _streak(h["id"]) == 1


def test_auto_checkin_fills_untouched_day(dbfile):
    h = db.create_habit("Exercise")
    assert db.auto_checkin("Exercise", DAY) is True
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 1


def test_auto_checkin_never_overwrites_manual_check(dbfile):
    h = db.create_habit("Exercise")
    db.checkin(h["id"], day=DAY, value=1, note="did yoga")
    assert db.auto_checkin("Exercise", DAY, note="auto") is False
    # The person's row (value + note) is untouched.
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 1


def test_auto_checkin_respects_manual_uncheck_tombstone(dbfile):
    # A deliberate un-check leaves a value=0 tombstone that auto can't resurrect.
    h = db.create_habit("Exercise")
    db.auto_checkin("Exercise", DAY)          # auto sets it
    db.checkin(h["id"], day=DAY, value=0)     # user clears it
    assert db.auto_checkin("Exercise", DAY) is False  # stays cleared
    row = next(x for x in db.list_habits(day=DAY) if x["id"] == h["id"])
    assert row["today"] == 0


def test_auto_checkin_noop_when_habit_absent(dbfile):
    # No such habit (renamed/archived) → decoupled no-op, no crash.
    assert db.auto_checkin("Nonexistent", DAY) is False
    db.create_habit("Exercise")
    db.update_habit(db.list_habits(day=DAY)[0]["id"], archived=True)
    assert db.auto_checkin("Exercise", DAY) is False


def test_uncheck_tombstone_breaks_streak(dbfile):
    # Value=0 rows must read as "not done" for the streak walk.
    h = db.create_habit("run")
    for n in (20, 21, 22):
        db.checkin(h["id"], day=D(n), value=1)
    db.checkin(h["id"], day=D(21), value=0)  # tombstone in the middle
    assert _streak(h["id"]) == 1  # only 22 counts; gap at 21


def test_update_habit_rename_and_archive(dbfile):
    h = db.create_habit("old name")
    renamed = db.update_habit(h["id"], name="new name")
    assert renamed["name"] == "new name"
    db.update_habit(h["id"], archived=True)
    # Archived habits drop out of list_habits.
    assert all(x["id"] != h["id"] for x in db.list_habits(day=DAY))


def test_habit_history_span_and_values(dbfile):
    h = db.create_habit("read")
    today = db.day_key()
    db.checkin(h["id"], day=today, value=1)
    hist = db.habit_history(days=7)
    assert len(hist["span"]) == 7
    assert hist["span"][-1] == today
    habit = next(x for x in hist["habits"] if x["id"] == h["id"])
    assert len(habit["days"]) == 7
    assert habit["days"][-1] == {"day": today, "value": 1}


def test_delete_habit_takes_its_checkins(dbfile):
    """Hard delete must leave no rows: an orphaned habit_checkins row reappears in habit_history as a nameless habit."""
    h = db.create_habit("typo")
    db.checkin(h["id"], day=db.day_key(), value=1)
    gone = db.delete_habit(h["id"])

    assert gone["name"] == "typo"
    assert all(x["id"] != h["id"] for x in db.list_habits(day=DAY))
    with db.session() as con:
        left = con.execute("SELECT COUNT(*) n FROM habit_checkins"
                           " WHERE habit_id=?", (h["id"],)).fetchone()["n"]
    assert left == 0


def test_delete_habit_is_not_archive(dbfile):
    """Archive keeps the history the streak is built from; delete is for a row that should never have existed."""
    keep = db.create_habit("keep")
    db.checkin(keep["id"], day=db.day_key(), value=1)
    db.update_habit(keep["id"], archived=True)

    with db.session() as con:
        rows = con.execute("SELECT COUNT(*) n FROM habit_checkins"
                           " WHERE habit_id=?", (keep["id"],)).fetchone()["n"]
    assert rows == 1, "archiving must not touch check-ins"


def test_delete_missing_habit_is_none_not_an_error(dbfile):
    """The route turns None into a 404; a raise here would be a 500."""
    assert db.delete_habit(4321) is None


def test_delete_frees_the_name_for_reuse(dbfile):
    """`name` is UNIQUE: a deleted habit leaving its row would block re-adding the same name."""
    h = db.create_habit("Exercise")
    db.delete_habit(h["id"])
    again = db.create_habit("Exercise")

    assert again["name"] == "Exercise"
