"""The relay pull: phone → Cloudflare Worker → this. A fake relay stands in
for the Worker (relay/src/index.js) with the same three-call contract."""

import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.config import HOME_LAT, HOME_LON
from app.providers import location, location_relay


def _msg(minutes_ago: float, lat=34.0, lon=-118.0, **extra):
    tst = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp())
    return {"_type": "location", "lat": lat, "lon": lon, "tst": tst, **extra}


class FakeRelay:
    """In-memory Worker: /pull returns id > after up to limit, /ack deletes."""

    def __init__(self, msgs, contact_ms=None):
        self.points = [{"id": i + 1, "msg": m} for i, m in enumerate(msgs)]
        self.contact = contact_ms
        self.acks = []
        self.fail_ack = False

    def __call__(self, path, *, method="GET", body=None):
        if path.startswith("/pull"):
            limit = int(path.split("limit=")[1])
            return {"points": self.points[:limit], "last_contact": self.contact}
        if path == "/ack":
            if self.fail_ack:
                raise RuntimeError("location relay 500: boom")
            self.acks.append(body["upto"])
            self.points = [p for p in self.points if p["id"] > body["upto"]]
            return {"deleted": 1}
        raise AssertionError(path)


@pytest.fixture
def relay(dbfile, monkeypatch):
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_TOKEN", "t")

    def install(fake):
        monkeypatch.setattr(location_relay, "_request", fake)
        return fake
    return install


def _points():
    with db.session() as con:
        return con.execute("SELECT ts, lat, speed, battery FROM location_points"
                           " ORDER BY ts").fetchall()


def test_row_normalises_owntracks_units_and_skips_non_fixes():
    r = location_relay.row(_msg(0, vel=36, batt=50, acc=12))
    assert r[4] == pytest.approx(10.0) and r[6] == 0.5 and r[7] == 12
    assert location_relay.row({"_type": "transition", "lat": 1, "lon": 2, "tst": 1}) is None
    assert location_relay.row({"_type": "location", "lat": "x", "lon": 2, "tst": 1}) is None
    assert location_relay.row(_msg(0, vel=-1))[4] is None      # -1 = unknown, not backwards


def test_pull_stores_fixes_acks_and_remembers_contact(relay):
    fake = relay(FakeRelay([_msg(30), {"_type": "status"}, _msg(10)], contact_ms=123))
    out = location_relay.pull()
    assert out["stored"] == 2 and len(_points()) == 2
    assert fake.acks == [3] and fake.points == []
    with db.session() as con:
        assert con.execute("SELECT value FROM sync_state WHERE key=?",
                           (location_relay.CONTACT_KEY,)).fetchone()["value"] == "123"


def test_pull_drains_a_backlog_page_by_page(relay, monkeypatch):
    monkeypatch.setattr(location_relay, "PAGE", 2)
    fake = relay(FakeRelay([_msg(60 * 24 * 3 + i) for i in range(5)]))
    out = location_relay.pull()
    assert out["stored"] == 5 and fake.acks == [2, 4, 5]
    assert out["days"], "the backlog's days come back for recompute"


def test_a_lost_ack_repulls_without_duplicating(relay):
    fake = relay(FakeRelay([_msg(5)]))
    fake.fail_ack = True
    with pytest.raises(RuntimeError):
        location_relay.pull()
    assert len(_points()) == 1            # committed before the ack failed
    fake.fail_ack = False
    location_relay.pull()
    assert len(_points()) == 1 and fake.points == []


def test_refresh_recomputes_the_days_a_backlog_landed_on(relay):
    old = _msg(60 * 24 * 5)
    relay(FakeRelay([old]))
    out = location.refresh()
    day = location_relay.row(old)[1]
    assert day in out["days"] and out["pulled"] == 1
    with db.session() as con:
        assert con.execute("SELECT points FROM location_sync WHERE day=?",
                           (day,)).fetchone()["points"] == 1


def _queued(kind="location"):
    with db.session() as con:
        return con.execute("SELECT key, title FROM notifications WHERE kind=?",
                           (kind,)).fetchall()


def test_quiet_location_notifies_once_per_silence(relay):
    relay(FakeRelay([_msg(60 * 10, lat=HOME_LAT + 1, lon=HOME_LON)]))   # away, 10h old
    location.refresh()
    location.refresh()
    assert [r["title"] for r in _queued()] == ["Location has gone quiet"]


def test_a_phone_that_checked_in_is_not_quiet(relay):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    relay(FakeRelay([_msg(60 * 10, lat=HOME_LAT + 1, lon=HOME_LON)], contact_ms=now_ms))
    location.refresh()
    h = location.health()
    assert h["state"] == "ok" and "checked in" in h["detail"]
    assert _queued() == []


def test_a_refusing_relay_notifies_and_raises(relay):
    def refuse(path, **kw):
        raise RuntimeError("location relay 403: forbidden")
    relay(refuse)
    with pytest.raises(RuntimeError):
        location.refresh()
    assert [r["title"] for r in _queued()] == ["Location relay is refusing the pull"]


def test_an_offline_mac_raises_but_does_not_notify(relay):
    def offline(path, **kw):
        raise urllib.error.URLError("nodename nor servname provided")
    relay(offline)
    with pytest.raises(urllib.error.URLError):
        location.refresh()
    assert _queued() == []


def test_unconfigured_relay_is_left_alone(dbfile):
    assert not location_relay.configured()
    assert location.refresh()["pulled"] == 0
