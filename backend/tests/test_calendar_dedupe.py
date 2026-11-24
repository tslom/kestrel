"""Calendar duplicates: one invite by two routes gets two Google ids, which the (day, event_id) PK can't collapse."""

from app.providers.calendar import _dedupe


def _ev(summary, start, end, calendar, id_):
    return {"day": "2026-06-10", "id": id_, "summary": summary, "start": start,
            "end": end, "all_day": False, "location": None, "continued": False,
            "calendar": calendar}


def test_same_event_twice_collapses_and_records_the_other_copy():
    data = [
        _ev("Course Registration", "2026-06-10T17:00:00-04:00",
            "2026-06-10T19:00:00-04:00", "student@usc.edu", "74cp55pvl3s5"),
        _ev("Course Registration", "2026-06-10T17:00:00-04:00",
            "2026-06-10T19:00:00-04:00", "usc-imported", "_6ss6cc366sr38b9k"),
    ]
    out = _dedupe(data)
    assert len(out) == 1
    assert out[0]["id"] == "74cp55pvl3s5"      # display order wins
    assert out[0]["duplicates"] == ["usc-imported"]


def test_same_title_at_a_different_time_is_a_different_event():
    data = [
        _ev("Standup", "2026-06-10T09:00:00-04:00",
            "2026-06-10T09:15:00-04:00", "work", "a"),
        _ev("Standup", "2026-06-10T17:00:00-04:00",
            "2026-06-10T17:15:00-04:00", "work", "b"),
    ]
    assert len(_dedupe(data)) == 2
    assert all("duplicates" not in e for e in _dedupe(data))
