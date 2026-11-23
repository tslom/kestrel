"""Deliveries & travel.

This file once held the Screen card's tests too; they went with the provider,
including the cases that pinned its honesty (centering on the
newest day that *has* data, keeping a day AW saw nothing on as a gap rather
than a zero, never pulling from aw-server during a render). Those rules were
right and are worth knowing if a screen source ever comes back from a source
that can see the phone; they are in git, at the commit that removed screen time.

What's left is the travel card, and what's worth pinning about it:

  - `/api/packages` hands the browser the backend's day_key, because eta_day
    is a day_key and a local `new Date()` disagrees with it until 4am;
  - `/api/trips` defaults to a horizon a *card* can use, while the brief
    keeps the 48h one it asks for directly.
"""

from datetime import date, timedelta

import pytest

from app import db
from app.routes import extract as extract_routes


@pytest.fixture
def today(dbfile):
    return db.day_key()


def test_packages_carry_the_backend_day_key(today):
    """eta_day is a day_key; the browser comparing it against its own
    midnight would disagree for four hours every night."""
    assert extract_routes.get_packages()["day"] == db.day_key()


def test_trips_horizon_is_a_year_by_default(dbfile):
    """A flight booked months out is exactly what a card is for. The brief
    still gets 48h — it calls upcoming_trips() directly."""
    far = (date.fromisoformat(db.day_key()) + timedelta(days=120)).isoformat()
    with db.session() as con, con:
        con.execute(
            "INSERT INTO trips (pnr, flight_no, dep_day, carrier, dep_airport,"
            " arr_airport, dep_local, status, updated_at)"
            " VALUES ('ABC','2308',?,'AA','LAX','RDU',?,'confirmed',?)",
            (far, f"{far}T09:15:00", "2026-07-21T11:05:04"))
    assert extract_routes.get_trips()["count"] == 1
    assert extract_routes.get_trips(hours=48)["count"] == 0


def test_trips_horizon_is_clamped(dbfile):
    """A hand-typed ?hours= shouldn't be able to ask for a scan to the heat
    death of the universe, or for a negative window."""
    assert extract_routes.get_trips(hours=0)["count"] == 0
    assert extract_routes.get_trips(hours=10 ** 9)["count"] == 0
