"""Deliveries & travel.

The travel card: `/api/packages` returns the backend's day_key (eta_day is one), and
`/api/trips` defaults to a card-sized horizon while the brief keeps 48h.
Design: DESIGN.md → §15 packages / §16 travel (extraction).
"""

from datetime import date, timedelta

import pytest

from app import db
from app.routes import extract as extract_routes


@pytest.fixture
def today(dbfile):
    return db.day_key()


def test_packages_carry_the_backend_day_key(today):
    """eta_day is a day_key; a browser midnight disagrees until 4am."""
    assert extract_routes.get_packages()["day"] == db.day_key()


def test_trips_horizon_is_a_year_by_default(dbfile):
    """The route defaults to a year; the brief still gets 48h via upcoming_trips()."""
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
    """?hours= is clamped: no unbounded or negative window."""
    assert extract_routes.get_trips(hours=0)["count"] == 0
    assert extract_routes.get_trips(hours=10 ** 9)["count"] == 0
