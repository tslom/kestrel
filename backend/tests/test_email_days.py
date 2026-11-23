"""email_days marks a day synced only once it has happened. A this-year read
on 08-13 marked Sep–Dec with empty results; a marked day is never backfilled,
so any of those days the recent-days refresh later missed stayed empty."""

from datetime import date, timedelta

from app import db
from app.providers import email
from app.providers.ranges import Range


def _stub_gmail(monkeypatch):
    queries = []
    monkeypatch.setattr(email, "access_token", lambda: "tok")
    monkeypatch.setattr(email, "list_ids",
                        lambda token, q: queries.append(q) or [])
    return queries


def _marked():
    with db.session() as con:
        return {r["day"] for r in con.execute("SELECT day FROM email_days")}


def test_future_days_are_never_marked_synced(dbfile, monkeypatch):
    _stub_gmail(monkeypatch)
    today = email._today()
    rng = Range("x", today - timedelta(days=1), today + timedelta(days=3), True)
    email.get(rng)
    assert _marked() == {(today - timedelta(days=1)).isoformat(),
                         today.isoformat()}


def test_an_all_future_range_asks_gmail_nothing(dbfile, monkeypatch):
    queries = _stub_gmail(monkeypatch)
    today = email._today()
    rng = Range("x", today + timedelta(days=2), today + timedelta(days=9), True)
    email.get(rng)
    assert queries == [] and _marked() == set()


def test_today_follows_the_later_of_day_key_and_civil_date(dbfile, freeze_day):
    freeze_day("2099-01-01")
    assert email._today() == date(2099, 1, 1)
