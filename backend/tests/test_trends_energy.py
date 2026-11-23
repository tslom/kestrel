"""Energy is computed from `health_days`; the provider read only tops it up.

A lapsed Google login used to 502 the whole series for as long as the account
stayed disconnected (18 times, 10-17 to 11-02), with the days it needed sitting
in the cache.
"""

from app import db, trends
from app.providers import health, oauth, ranges


def _no_tokens(*a, **k):
    with oauth.as_value_error():
        raise oauth.NotAuthorized("no tokens for google/personal")


def test_a_lapsed_login_still_scores_the_cached_days(con, monkeypatch):
    today = db.day_key()
    with con:
        con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'sleep_hours', 8.0, ?)", (today, today))
    monkeypatch.setattr(health, "get", _no_tokens)

    rng = ranges.parse_range(f"{today}..{today}")
    assert trends._energy_series(rng, [today]) == [{"day": today, "value": 100}]


def test_a_lapsed_login_with_nothing_cached_is_a_gap_not_a_failure(dbfile, monkeypatch):
    today = db.day_key()
    monkeypatch.setattr(health, "get", _no_tokens)

    rng = ranges.parse_range(f"{today}..{today}")
    assert trends._energy_series(rng, [today]) == [{"day": today, "value": None}]
