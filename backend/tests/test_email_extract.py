"""Package identity in providers/email_extract.py.

The spec here is one real order. Campus Ready's confirmation (2026-08-02)
carried merchant + order id and created `Campus Ready:41415`. Its reply two
days later gave the pickup date — "Orders will be available for pick-up at the
USC bookstone patio on August 18th" — and named neither an order number nor a
tracking number, only the sending domain. The extractor parsed it correctly
and then dropped it on the floor, so the deliveries card showed no ETA while
the right date sat in the `email_extract` cache.
"""

import json
import urllib.error
from datetime import datetime, timedelta

import pytest

from app import db, notify
from app.providers import email_extract as ex


def _upsert(item, msg_id="m1", now="2026-08-02T19:28:32"):
    con = db.connect()
    try:
        with con:
            ex._upsert_package(con, item, msg_id, now)
    finally:
        con.close()


def _packages():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM packages ORDER BY order_key")]
    finally:
        con.close()


CONFIRMATION = {"merchant": "Campus Ready", "order_id": "41415",
                "item_desc": "College Twin XL Extra Comfort Pack",
                "status": "ordered"}
PICKUP_REPLY = {"merchant": "campusready.com", "item_desc": "Linen order",
                "order_id": None, "tracking_no": None,
                "eta_day": "2026-08-18", "status": "ordered"}


# ---------- merchant normalization ----------

@pytest.mark.parametrize("spelling", [
    "Campus Ready", "campusready.com", "CAMPUS READY", "campus-ready",
    "www.campusready.com", "https://campusready.com/orders",
])
def test_merchant_key_survives_how_the_email_spells_it(spelling):
    assert ex._merchant_key(spelling) == "campusready"


def test_merchant_key_keeps_a_display_name_with_a_dot_intact():
    """A dot inside a name with spaces is punctuation, not a domain — cutting
    at it would collapse 'J. Crew' and 'J. Peterman' onto the same key."""
    assert ex._merchant_key("J. Crew") == "jcrew"
    assert ex._merchant_key("J. Peterman") == "jpeterman"


def test_merchant_key_is_none_for_nothing_usable():
    assert ex._merchant_key(None) is None
    assert ex._merchant_key("  ") is None


# ---------- the follow-up that named no order ----------

def test_pickup_date_lands_on_the_open_order(dbfile):
    _upsert(CONFIRMATION)
    _upsert(PICKUP_REPLY, msg_id="m2", now="2026-08-04T17:04:48")
    rows = _packages()
    assert len(rows) == 1, "the reply must not fork the order into a second row"
    assert rows[0]["order_key"] == "Campus Ready:41415"
    assert rows[0]["eta_day"] == "2026-08-18"
    assert rows[0]["last_msg_id"] == "m2"


def test_the_reply_does_not_overwrite_what_the_order_is(dbfile):
    """The card reads both of these. A follow-up describes the order in
    passing — 'campusready.com', 'Linen order' — while the confirmation that
    opened the row named it properly. Trust the follow-up for what changed,
    not for what the thing is."""
    _upsert(CONFIRMATION)
    _upsert(PICKUP_REPLY, msg_id="m2")
    row = _packages()[0]
    assert row["merchant"] == "Campus Ready"
    assert row["item_desc"] == "College Twin XL Extra Comfort Pack"


def test_a_follow_up_abstains_when_the_merchant_has_two_open_orders(dbfile):
    """Guessing which of two orders a bare date belongs to is worse than
    leaving the card blank."""
    _upsert(CONFIRMATION)
    _upsert({**CONFIRMATION, "order_id": "41416"}, msg_id="m2")
    _upsert(PICKUP_REPLY, msg_id="m3")
    rows = _packages()
    assert len(rows) == 2
    assert [r["eta_day"] for r in rows] == [None, None]


def test_a_follow_up_with_no_open_order_is_still_dropped(dbfile):
    """Nothing to attach to — and it carries no id, so it can't open a row of
    its own either."""
    _upsert(PICKUP_REPLY)
    assert _packages() == []


def test_a_delivered_order_does_not_absorb_a_later_follow_up(dbfile):
    _upsert({**CONFIRMATION, "status": "delivered"})
    _upsert(PICKUP_REPLY, msg_id="m2")
    rows = _packages()
    assert len(rows) == 1 and rows[0]["eta_day"] is None


def test_the_follow_up_cannot_walk_status_backward(dbfile):
    """The reply says 'ordered' because that's all it knows; the shipping
    notice that came before it knows better."""
    _upsert({**CONFIRMATION, "status": "shipped"})
    _upsert(PICKUP_REPLY, msg_id="m2")
    assert _packages()[0]["status"] == "shipped"


# ---------- update_package (the console's write path) ----------

def test_update_package_may_move_status_backward(dbfile):
    """Forward-only guards against out-of-order email, not against the person
    looking at the package and telling you it isn't here yet."""
    _upsert({**CONFIRMATION, "status": "delivered"})
    row = ex.update_package("Campus Ready:41415", status="shipped")
    assert row["status"] == "shipped"


def test_update_package_is_none_for_an_unknown_key(dbfile):
    assert ex.update_package("Nike:nope", eta_day="2026-08-18") is None


def test_update_package_rejects_an_unknown_column(dbfile):
    _upsert(CONFIRMATION)
    with pytest.raises(ValueError, match="nothing to change"):
        ex.update_package("Campus Ready:41415", notes="sneaky")


# ---------- delete_package (rows that were never a real parcel) ----------

def test_delete_package_drops_the_row(dbfile):
    _upsert(CONFIRMATION)
    assert ex.delete_package("Campus Ready:41415") is True
    assert _packages() == []


def test_delete_package_is_false_for_an_unknown_key(dbfile):
    _upsert(CONFIRMATION)
    assert ex.delete_package("Nike:nope") is False
    assert len(_packages()) == 1


def test_delete_package_requires_a_key(dbfile):
    with pytest.raises(ValueError, match="order_key is required"):
        ex.delete_package("  ")


def test_delete_package_leaves_the_other_orders_alone(dbfile):
    _upsert(CONFIRMATION)
    _upsert({"merchant": "Nike", "order_id": "C016", "status": "shipped"})
    ex.delete_package("Campus Ready:41415")
    assert [p["order_key"] for p in _packages()] == ["Nike:C016"]


def test_a_later_email_re_opens_a_deleted_order(dbfile):
    """Delete clears the card; it is not a blacklist. The `email_extract`
    cache is keyed by msg_id and a package by order, so there is no honest
    set of cache rows to drop alongside the package — which means a genuinely
    new email about that order builds the row again. Pinned because it's the
    surprising half of the contract, not because it's desirable."""
    _upsert(CONFIRMATION)
    ex.delete_package("Campus Ready:41415")
    _upsert({**CONFIRMATION, "status": "shipped"}, msg_id="m2")
    assert [p["order_key"] for p in _packages()] == ["Campus Ready:41415"]


# ---------- the card's two buttons, over HTTP ----------

@pytest.fixture
def pkg_client(router_client):
    from app.routes import extract as extract_routes

    return router_client(extract_routes.router)


def test_route_marks_a_package_delivered(pkg_client):
    _upsert(CONFIRMATION)
    r = pkg_client.post("/api/packages/Campus%20Ready%3A41415/delivered")
    assert r.status_code == 200
    assert r.json()["status"] == "delivered"


def test_delivered_route_404s_for_an_unknown_key(pkg_client):
    assert pkg_client.post("/api/packages/Nike%3Anope/delivered").status_code == 404


def test_route_deletes_a_package(pkg_client):
    _upsert(CONFIRMATION)
    assert pkg_client.delete("/api/packages/Campus%20Ready%3A41415").status_code == 200
    assert _packages() == []


def test_delete_route_404s_for_an_unknown_key(pkg_client):
    assert pkg_client.delete("/api/packages/Nike%3Anope").status_code == 404


def test_the_card_still_shows_a_hand_delivered_package(pkg_client):
    """Marking delivered is not removing: the row keeps its three-day return
    window on the card, which is the whole reason delete exists separately."""
    _upsert(CONFIRMATION)
    pkg_client.post("/api/packages/Campus%20Ready%3A41415/delivered")
    body = pkg_client.get("/api/packages").json()
    assert [p["order_key"] for p in body["data"]] == ["Campus Ready:41415"]


# ---------- create_package (deliveries no email will ever mention) ----------
#
# Live case: a backpack and a shoe spray ordered on a parent's Amazon account.
# Nothing lands in this mailbox, so no candidate, no extraction, no row — the
# console put them on the shopping list marked bought and the arrival dates
# survived only as prose in an item note.

def test_create_package_opens_a_row_from_nothing_but_what_the_user_says(dbfile):
    row = ex.create_package("Amazon", item_desc="Fjällräven Raven 20",
                            eta_day="2026-08-06")
    assert row["order_key"] == "Amazon:manual-1"
    assert (row["item_desc"], row["eta_day"]) == ("Fjällräven Raven 20",
                                                  "2026-08-06")
    assert row["status"] == "ordered"       # the default
    assert row["last_msg_id"] is None       # no email behind it


def test_create_package_numbers_each_manual_row_for_a_merchant(dbfile):
    """Two parcels from one merchant are two cards, not one overwritten one —
    the same account orders a backpack and a spray in the same week."""
    first = ex.create_package("Amazon", item_desc="Fjällräven Raven 20")
    second = ex.create_package("Amazon", item_desc="Crep Protect spray")
    assert [first["order_key"], second["order_key"]] == ["Amazon:manual-1",
                                                         "Amazon:manual-2"]


def test_create_package_keys_on_a_tracking_number_when_given(dbfile):
    """Same rule the email path uses, so the carrier's own notification later
    lands on this row instead of forking a second card."""
    row = ex.create_package("Amazon", tracking_no="1Z999", carrier="ups")
    assert row["order_key"] == "1Z999"
    _upsert({"merchant": "UPS", "tracking_no": "1Z999", "order_id": None,
             "status": "out_for_delivery", "eta_day": "2026-08-06"},
            msg_id="m9")
    rows = _packages()
    assert len(rows) == 1
    assert rows[0]["status"] == "out_for_delivery"
    assert rows[0]["eta_day"] == "2026-08-06"


def test_a_later_email_does_not_blank_the_link_to_an_earlier_one(dbfile):
    """last_msg_id COALESCEs. A hand-amended row keeps the email that opened
    it — without that, create_package's msg_id=None would erase the pointer."""
    _upsert(CONFIRMATION)
    ex.create_package("Campus Ready", order_id="41415", eta_day="2026-08-18")
    row = _packages()[0]
    assert row["last_msg_id"] == "m1"
    assert row["eta_day"] == "2026-08-18"


def test_create_package_requires_a_merchant(dbfile):
    with pytest.raises(ValueError, match="merchant is required"):
        ex.create_package("  ", item_desc="a mystery box")


@pytest.mark.parametrize("bad, msg", [
    ({"eta_day": "Aug 18"}, "eta_day must be YYYY-MM-DD"),
    ({"status": "on a truck"}, "status must be one of"),
])
def test_create_package_validates_exactly_as_update_does(dbfile, bad, msg):
    with pytest.raises(ValueError, match=msg):
        ex.create_package("Amazon", **bad)


# ---------- backfill ----------

def _seed_cache(dbfile, msg_id, item, received_at):
    con = db.connect()
    try:
        with con:
            con.execute(
                "INSERT INTO emails (msg_id, thread_id, day, received_at,"
                " from_name, from_addr, subject, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (msg_id, msg_id, received_at[:10], received_at,
                 "Campus Ready", "mike@campusready.com",
                 "Re: USC Linen Order", received_at))
            con.execute(
                "INSERT INTO email_extract (msg_id, kind, payload, extracted_at)"
                " VALUES (?,?,?,?)",
                (msg_id, "package", json.dumps({"id": msg_id, "relevant": True,
                                                **item}), received_at))
    finally:
        con.close()


def test_backfill_replays_the_cache_through_the_fixed_rules(dbfile):
    """The cache is keyed by msg_id and the upsert only runs at extraction
    time, so fixing the matching rules reaches nothing already parsed. The
    pickup date sat correctly parsed in `email_extract` for two days."""
    _seed_cache(dbfile, "m1", CONFIRMATION, "2026-08-02T19:28:32")
    _seed_cache(dbfile, "m2", PICKUP_REPLY, "2026-08-04T17:04:48")
    assert _packages() == []                       # nothing upserted yet
    assert ex.backfill_packages() == 2
    rows = _packages()
    assert len(rows) == 1
    assert rows[0]["eta_day"] == "2026-08-18"


def test_backfill_skips_irrelevant_cached_rows(dbfile):
    _seed_cache(dbfile, "m1", {"relevant": False}, "2026-08-02T19:28:32")
    assert ex.backfill_packages() == 0
    assert _packages() == []


def test_backfill_does_not_shove_an_old_delivery_into_the_return_window(dbfile):
    """updated_at drives the card's three-day 'recently delivered' window, so
    replaying with now() would resurrect a package delivered weeks ago."""
    _seed_cache(dbfile, "m1", {"merchant": "Vuori", "order_id": "27901251",
                               "status": "delivered"}, "2026-07-14T18:52:14")
    ex.backfill_packages()
    assert _packages()[0]["updated_at"] == "2026-07-14T18:52:14"
    assert ex.active_packages() == []


# ---------- the at-desk channel (§21 step 4) ----------

def _queued():
    con = db.connect()
    try:
        return [(r["key"], r["title"], r["body"]) for r in notify.recent(con)]
    finally:
        con.close()


def test_a_package_moving_forward_queues_a_banner(dbfile):
    _upsert(CONFIRMATION)
    _upsert({**CONFIRMATION, "status": "out_for_delivery"}, msg_id="m2")

    assert _queued() == [("package:Campus Ready%3A41415:out_for_delivery",
                          "Campus Ready out for delivery",
                          "College Twin XL Extra Comfort Pack")]


def test_a_newly_discovered_package_says_nothing(dbfile):
    """On a transition, never on discovery. A first sighting that already reads
    'delivered' can't be told apart from one that just happened, and the honest
    reading of it is the second one."""
    _upsert({**CONFIRMATION, "status": "delivered"})
    assert _queued() == []


def test_the_same_status_re_observed_queues_nothing(dbfile):
    """The poll cycle re-reads every open order — this is the case that would
    have made the channel unusable."""
    _upsert(CONFIRMATION)
    _upsert({**CONFIRMATION, "status": "shipped"}, msg_id="m2")
    _upsert({**CONFIRMATION, "status": "shipped"}, msg_id="m3")
    assert len(_queued()) == 1


def test_a_backwards_status_queues_nothing(dbfile):
    """Status only moves forward (_PACKAGE_RANK), so the row doesn't change and
    there is nothing to announce."""
    _upsert({**CONFIRMATION, "status": "delivered"})
    _upsert({**CONFIRMATION, "status": "shipped"}, msg_id="m2")
    assert _queued() == []


def test_the_order_confirmation_itself_says_nothing(dbfile):
    """You pressed the button thirty seconds ago."""
    _upsert({"merchant": "Amazon", "order_id": "1", "status": "shipped"})
    con = db.connect()
    with con:
        con.execute("UPDATE packages SET status='x' WHERE order_key='Amazon:1'")
    con.close()
    _upsert({"merchant": "Amazon", "order_id": "1", "status": "ordered"},
            msg_id="m2")
    assert _queued() == []


def test_a_rolled_back_extraction_leaves_no_promise_to_announce(dbfile):
    """The queue write shares the caller's transaction on purpose: a fact that
    didn't land must not be announced."""
    _upsert(CONFIRMATION)
    con = db.connect()
    try:
        with pytest.raises(RuntimeError):
            with con:
                ex._upsert_package(
                    con, {**CONFIRMATION, "status": "delivered"}, "m2", "now")
                raise RuntimeError("the batch blew up after the upsert")
    finally:
        con.close()
    assert _queued() == []
    assert _packages()[0]["status"] == "ordered"


def test_a_gate_change_queues_a_banner_through_the_shared_channel(dbfile):
    """The channel used to live in this module with one caller, below
    AEROAPI_KEY's early return, and had never fired.

    The row opens with a gate already known (C10), not NULL: a gate learned for
    the first time is not a gate change and is deliberately silent, so a NULL
    here would leave the ' — gate C12' half of the body untested."""
    con = db.connect()
    with con:
        con.execute(
            "INSERT INTO trips (pnr, flight_no, dep_day, carrier, dep_local,"
            " status, gate, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("FLZUNL", "2299", "2026-08-17", "AA", "2026-08-17T18:00:00",
             "On Time", "C10", "now"))
    row = dict(con.execute("SELECT * FROM trips").fetchone())
    ex._apply_flight_status(con, row, {"status": "Delayed", "gate": "C12"},
                            "2026-08-17T09:00:00")
    con.close()

    key, title, body = _queued()[0]
    assert key == "flight:FLZUNL:2299:2026-08-17:Delayed:C12"
    assert (title, body) == ("Flight AA2299", "Delayed — gate C12")


def test_an_unchanged_flight_status_queues_nothing(dbfile):
    con = db.connect()
    with con:
        con.execute(
            "INSERT INTO trips (pnr, flight_no, dep_day, carrier, dep_local,"
            " status, gate, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("FLZUNL", "2299", "2026-08-17", "AA", "2026-08-17T18:00:00",
             "Delayed", "C12", "now"))
    row = dict(con.execute("SELECT * FROM trips").fetchone())
    ex._apply_flight_status(con, row, {"status": "Delayed", "gate": "C12"},
                            "2026-08-17T09:00:00")
    con.close()
    assert _queued() == []


# ---------- create_trip (flights no email will ever mention) ----------
#
# Live case: the 2026-08-17 RDU->LAX move flight, AA 2299 on PNR FLZUNL. It sat
# on the calendar and in no other store, so /api/trips returned only the two
# November legs and the travel card read "2 trips booked" meaning Thanksgiving.
# The booking predates the mail sync's own history (earliest message 06-01), so
# there was no message to extract and never would be.

def _trips():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM trips ORDER BY dep_local")]
    finally:
        con.close()


def test_create_trip_opens_a_row_from_nothing_but_what_the_user_says(dbfile):
    row = ex.create_trip("2299", "2026-08-17T15:00:00", pnr="FLZUNL",
                         carrier="AA", dep_airport="RDU", arr_airport="LAX",
                         arr_local="2026-08-17T20:19:00")
    assert (row["pnr"], row["flight_no"], row["dep_day"]) == ("FLZUNL", "2299",
                                                              "2026-08-17")
    assert (row["dep_airport"], row["arr_airport"]) == ("RDU", "LAX")
    assert row["status"] == "confirmed"
    assert row["last_msg_id"] is None       # no email behind it


def test_create_trip_splits_a_carrier_prefixed_flight_number(dbfile):
    """The card renders `{carrier}{flight_no}`, so an unsplit "AA2299" prints
    correctly with an empty carrier while keying the row wrong."""
    row = ex.create_trip("AA2299", "2026-08-17T15:00:00")
    assert (row["carrier"], row["flight_no"]) == ("AA", "2299")


def test_create_trip_keys_on_the_pnr_so_a_later_email_lands_on_this_row(dbfile):
    """The same job tracking_no does for packages: an airline schedule-change
    mail for this leg must amend this row, not fork a second card."""
    ex.create_trip("2299", "2026-08-17T15:00:00", pnr="FLZUNL", carrier="AA")
    con = db.connect()
    try:
        with con:
            ex._upsert_trip(con, {"pnr": "flzunl", "flight_no": "2299",
                                  "dep_local": "2026-08-17T16:30:00",
                                  "dep_airport": "RDU", "arr_airport": "LAX"},
                            "m7", "2026-08-06T09:00:00")
    finally:
        con.close()
    rows = _trips()
    assert len(rows) == 1
    assert rows[0]["dep_local"] == "2026-08-17T16:30:00"   # the delay applied
    assert rows[0]["last_msg_id"] == "m7"


def test_a_later_email_does_not_blank_the_link_on_a_hand_written_trip(dbfile):
    """last_msg_id COALESCEs, as the packages one does — re-filing a flight by
    hand must not erase the email that opened the row."""
    con = db.connect()
    try:
        with con:
            ex._upsert_trip(con, {"pnr": "FLZUNL", "flight_no": "2299",
                                  "dep_local": "2026-08-17T15:00:00"},
                            "m1", "2026-08-02T19:28:32")
    finally:
        con.close()
    ex.create_trip("2299", "2026-08-17T15:00:00", pnr="FLZUNL",
                   dep_airport="RDU")
    row = _trips()[0]
    assert row["last_msg_id"] == "m1"
    assert row["dep_airport"] == "RDU"


def test_create_trip_numbers_a_placeholder_pnr_when_the_user_has_none(dbfile):
    first = ex.create_trip("2299", "2026-08-17T15:00:00")
    second = ex.create_trip("1188", "2026-09-02T08:00:00")
    assert [first["pnr"], second["pnr"]] == ["MANUAL-1", "MANUAL-2"]


@pytest.mark.parametrize("args, msg", [
    (("", "2026-08-17T15:00:00"), "flight_no is required"),
    (("2299", "Aug 17 3pm"), "dep_local must be an ISO local datetime"),
    (("2299", "2026-08-17"), "needs a departure time"),
])
def test_create_trip_rejects_what_it_cannot_place(dbfile, args, msg):
    with pytest.raises(ValueError, match=msg):
        ex.create_trip(*args)


def test_create_trip_rejects_a_non_iata_airport(dbfile):
    with pytest.raises(ValueError, match="dep_airport must be a 3-letter"):
        ex.create_trip("2299", "2026-08-17T15:00:00", dep_airport="Raleigh")


# ---------- live status: which of an ident's flights, and from which zone ----------
#
# Payloads below are trimmed from real AeroAPI responses for AA2299 (the move
# flight's own ident), captured 2026-08-12 while verifying the poller had ever
# worked. Two facts they encode and no unit test can rediscover: the API returns
# an ident's operations *newest first*, and some rows carry no schedule at all.

def _op(sched_out, *, zone="America/New_York", gate=None, status="Scheduled",
        cancelled=False):
    return {"ident": "AAL2299", "scheduled_out": sched_out, "status": status,
            "cancelled": cancelled, "gate_origin": gate,
            "origin": {"code_iata": "RDU", "timezone": zone}}


def test_match_flight_ignores_the_ident_s_other_days(dbfile):
    """A window wide enough to hold the local day holds the neighbouring
    operations too, so flights[0] is the most recent one, not ours."""
    flights = [_op("2026-08-13T22:00:00Z", gate="C9"),
               _op("2026-08-12T22:00:00Z", gate="C15"),
               _op("2026-08-11T22:00:00Z", gate="C21")]
    assert ex._match_flight(flights, "2026-08-12")["gate_origin"] == "C15"


def test_match_flight_skips_a_row_with_no_schedule(dbfile):
    """AeroAPI mixes in 'result unknown' rows carrying scheduled_out=None.
    One sorted to the front would otherwise be published as the status."""
    flights = [{"ident": "AAL2299", "scheduled_out": None,
                "status": "result unknown", "origin": {}},
               _op("2026-08-12T22:00:00Z", gate="C15")]
    assert ex._match_flight(flights, "2026-08-12")["gate_origin"] == "C15"


def test_match_flight_reads_the_departure_in_the_origin_s_zone(dbfile):
    """The regression the whole change exists for: a 21:00 departure from LAX
    is 04:00Z the *next* day. Matched in UTC it belongs to 08-18 and the row
    for 08-17 comes back empty — a flight recorded as checked and never read."""
    flights = [_op("2026-08-18T04:00:00Z", zone="America/Los_Angeles",
                   gate="46B")]
    assert ex._match_flight(flights, "2026-08-17")["gate_origin"] == "46B"
    assert ex._match_flight(flights, "2026-08-18") is None


def test_fetch_flight_status_reads_the_day_it_was_asked_for(dbfile, monkeypatch):
    """End to end over a real-shaped payload, because the tests above call
    _match_flight directly and so pass just as well with the caller still
    taking flights[0] — the wiring is where the original bug lived."""
    payload = {"flights": [_op("2026-08-18T22:00:00Z", gate="C9",
                               status="Scheduled / On Time"),
                           _op("2026-08-17T22:00:00Z", gate="C15",
                               status="Scheduled / Delayed")]}

    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ex.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(ex, "AEROAPI_KEY", "k")
    got = ex._fetch_flight_status("AA", "2299", "2026-08-17T18:00:00")
    assert got == {"status": "Scheduled / Delayed", "gate": "C15"}


def test_fetch_window_brackets_the_local_day(dbfile, monkeypatch):
    """Start a day back and end two on: only the start bound is capped at 2
    days out, and it must stay inside that cap for a flight at the horizon."""
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        raise urllib.error.URLError("stop here")

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ex, "AEROAPI_KEY", "k")
    ex._fetch_flight_status("AA", "2299", "2026-08-17T18:00:00")
    assert "start=2026-08-16T00:00:00Z" in seen["url"]
    assert "end=2026-08-19T00:00:00Z" in seen["url"]


def test_a_bad_request_does_not_mark_the_source_degraded(dbfile, monkeypatch):
    """400 is the window being wrong, not FlightAware being down. A status
    panel that reports an outage for a structural limit gets ignored — but a
    dead credential has to show, or it stays dead for a week."""
    logged = []
    monkeypatch.setattr(ex.heartbeat, "log_event",
                        lambda *a: logged.append(a))
    monkeypatch.setattr(ex, "AEROAPI_KEY", "k")

    def failing(code):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "err", None, None)
        return fake

    monkeypatch.setattr(ex.urllib.request, "urlopen", failing(400))
    assert ex._fetch_flight_status("AA", "2299", "2026-08-17T18:00:00") is None
    assert logged == []

    monkeypatch.setattr(ex.urllib.request, "urlopen", failing(401))
    assert ex._fetch_flight_status("AA", "2299", "2026-08-17T18:00:00") is None
    assert [a[1] for a in logged] == ["degraded"]


# ---------- the poll loop itself, which nothing covered ----------
#
# Everything above tests one lookup. These test which rows get looked up at all
# — the horizon SQL, the re-check interval, and what gets written back. Until
# 2026-08-12 the only poll_flight_status test asserted it returns 0 with the key
# unset, so the loop had neither run in production nor been exercised here.

def _banners():
    """What the at-desk channel was told, oldest first. Ordered by rowid rather
    than queued_at: the stamp is second-resolution, so two banners raised inside
    one test tie and come back in whatever order SQLite likes."""
    con = db.connect()
    try:
        return [(r["title"], r["body"]) for r in con.execute(
            "SELECT title, body FROM notifications ORDER BY rowid")]
    finally:
        con.close()


@pytest.fixture
def polling(monkeypatch):
    """The poller with its one outside edge stubbed: the network. The
    notification edge needs no stub since §21 step 4 — notify.send() only
    queues a row, and it is _post(), reached from flush(), that shells out to
    osascript. So the assertions below read the queue via _banners()."""
    monkeypatch.setattr(ex, "AEROAPI_KEY", "k")
    calls = []

    def fake_fetch(carrier, flight_no, dep_local):
        calls.append((carrier, flight_no, dep_local))
        return {"status": "Scheduled / On Time", "gate": "C15"}

    monkeypatch.setattr(ex, "_fetch_flight_status", fake_fetch)
    return {"calls": calls}


def _trip_in(hours, flight_no="2299", pnr="FLZUNL"):
    dep = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    ex.create_trip(flight_no, dep, pnr=pnr, carrier="AA")
    return dep


def _trip_row(con, flight_no="2299"):
    return dict(con.execute("SELECT * FROM trips WHERE flight_no=?",
                            (flight_no,)).fetchone())


def test_poll_ignores_flights_beyond_the_horizon(dbfile, polling):
    """48h is the API's ceiling as much as a cost choice — a start bound past
    2 days is a 400, so a further-out flight must not even be attempted."""
    _trip_in(24, flight_no="2299")
    _trip_in(24 * 5, flight_no="2308")
    con = db.connect()
    try:
        assert ex.poll_flight_status(con) == 1
    finally:
        con.close()
    assert [c[1] for c in polling["calls"]] == ["2299"]


def test_poll_writes_status_gate_and_the_check_stamp(dbfile, polling):
    _trip_in(24)
    con = db.connect()
    try:
        ex.poll_flight_status(con)
        row = _trip_row(con)
    finally:
        con.close()
    assert (row["status"], row["gate"]) == ("Scheduled / On Time", "C15")
    assert row["last_checked_at"]          # 'confirmed' until the first poll


def test_poll_respects_the_recheck_interval_until_close_in(dbfile, polling):
    """Cost control: a row checked minutes ago is skipped while it's far out,
    and re-checked every cycle once inside FLIGHT_POLL_CLOSE_HOURS."""
    _trip_in(30, flight_no="2299")
    _trip_in(3, flight_no="2308")
    just_now = datetime.now().isoformat(timespec="seconds")
    con = db.connect()
    try:
        con.execute("UPDATE trips SET last_checked_at=?", (just_now,))
        con.commit()
        assert ex.poll_flight_status(con) == 1
    finally:
        con.close()
    assert [c[1] for c in polling["calls"]] == ["2308"]   # the close-in one


def test_the_first_poll_is_silent_even_though_it_changes_both_fields(
        dbfile, polling):
    """Rows open as status='confirmed' with gate NULL, so the first poll always
    changes both — which is why neither arm can trigger on change alone. The
    status arm always guarded this (is_delay); the gate arm did not, so every
    flight buzzed once at T-48h to announce a gate nobody had been told."""
    _trip_in(24)
    con = db.connect()
    try:
        ex.poll_flight_status(con)
    finally:
        con.close()
    assert _banners() == []


def test_poll_notifies_on_a_real_gate_change_and_on_a_delay(
        dbfile, polling, monkeypatch):
    _trip_in(24)
    con = db.connect()
    try:
        ex.poll_flight_status(con)                       # learns C15, silent
        for status in ({"status": "Scheduled / On Time", "gate": "C21"},
                       {"status": "Delayed", "gate": "C21"}):
            monkeypatch.setattr(ex, "_fetch_flight_status",
                                lambda c, f, d, s=status: s)
            con.execute("UPDATE trips SET last_checked_at=NULL")
            con.commit()
            ex.poll_flight_status(con)
    finally:
        con.close()
    assert [b[1] for b in _banners()] == [
        "Scheduled / On Time — gate C21", "Delayed"]


def test_a_lookup_that_finds_nothing_still_stamps_the_row(dbfile, polling,
                                                          monkeypatch):
    """Deliberate and worth pinning: a miss records the attempt, so the row
    isn't retried every cycle. It is also why the UTC-window bug was invisible
    — a flight never actually read still reads as checked."""
    _trip_in(24)
    monkeypatch.setattr(ex, "_fetch_flight_status", lambda c, f, d: None)
    con = db.connect()
    try:
        assert ex.poll_flight_status(con) == 1
        row = _trip_row(con)
    finally:
        con.close()
    assert row["last_checked_at"] and row["status"] == "confirmed"


# ---------- check-in tasks, which never needed an API key ----------

def test_checkin_task_is_created_without_an_aeroapi_key(dbfile, monkeypatch):
    """The whole point of the hoist: poll_flight_status() returns 0 before its
    loop when the key is unset, so this task had never once been created."""
    monkeypatch.setattr(ex, "AEROAPI_KEY", None)
    soon = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    ex.create_trip("2299", soon, pnr="FLZUNL", carrier="AA")
    con = db.connect()
    try:
        assert ex.poll_flight_status(con) == 0      # no key, no poll
        assert ex.ensure_checkin_tasks(con) == 1
        titles = [r["title"] for r in con.execute("SELECT title FROM tasks")]
    finally:
        con.close()
    assert titles == ["Check in AA2299"]


def test_checkin_task_is_not_created_twice(dbfile):
    soon = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    ex.create_trip("2299", soon, pnr="FLZUNL", carrier="AA")
    con = db.connect()
    try:
        assert ex.ensure_checkin_tasks(con) == 1
        assert ex.ensure_checkin_tasks(con) == 0
    finally:
        con.close()


def test_no_checkin_task_for_a_flight_beyond_the_lead_window(dbfile):
    """Thanksgiving is months out; the card should not carry a check-in task
    for it in August."""
    ex.create_trip("2308", "2026-11-25T09:15:00", pnr="FLZUNL", carrier="AA")
    con = db.connect()
    try:
        assert ex.ensure_checkin_tasks(con) == 0
    finally:
        con.close()


# ---------- a body Gmail won't give up degrades, it doesn't sink the batch ----------

def test_extraction_runs_on_with_an_empty_excerpt_when_a_fetch_fails(
        dbfile, monkeypatch):
    """fetch_body raises now (so the reader can say why); the extractor goes
    through body_excerpt, which still turns a failure into "" — the other
    message in the batch is extracted and the failed one is still recorded."""
    from app.providers import email as email_provider

    def gmail(url, token, *a, **k):
        if "/messages/bad" in url:
            raise RuntimeError("gmail API 401: unauthorized")
        return {"payload": {"mimeType": "text/plain",
                            "body": {"data": "aGVsbG8="}}}     # "hello"
    monkeypatch.setattr(email_provider, "api_request", gmail)
    prompts = []

    async def ask(prompt, **k):
        prompts.append(prompt)
        return json.dumps([{"id": "good", "relevant": True}])
    monkeypatch.setattr(ex.agent, "ask", ask)
    upserted = []
    batch = [{"msg_id": m, "from_addr": "a@b.c", "subject": "s"}
             for m in ("good", "bad")]
    con = db.connect()
    try:
        n = ex._run_extraction(con, "tok", "package", batch, "sys",
                               lambda con, it, mid, now: upserted.append(mid))
        kinds = {r["msg_id"]: r["payload"] for r in con.execute(
            "SELECT msg_id, payload FROM email_extract")}
    finally:
        con.close()
    assert n == 1 and upserted == ["good"]
    excerpts = {m["id"]: m["body_excerpt"]
                for m in json.loads(prompts[0].split("Messages:\n", 1)[1])}
    assert excerpts == {"good": "hello", "bad": ""}
    assert set(kinds) == {"good", "bad"}
