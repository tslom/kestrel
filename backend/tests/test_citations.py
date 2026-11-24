"""Citations: the ref a console sentence carries back to its source.

Pins three properties: a ref is stable across sessions, one nobody handed out
does not resolve, and one whose row is gone resolves to "gone".
"""
from app import citations, db


def _seed(con):
    with con:
        con.execute(
            "INSERT INTO emails (msg_id, thread_id, day, received_at,"
            " from_name, from_addr, subject, snippet, in_inbox, unread,"
            " refreshed_at) VALUES"
            " ('1a0c509c','t1','2026-08-01','2026-08-01T10:35','Registrar',"
            "  'reg@usc.edu','Room change','Moved to WPH 205',1,1,'now')")
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, summary,"
            " start_at, end_at, all_day, location, calendar_name) VALUES"
            " ('2026-08-01','ev1','cal@g','Advising','2026-08-01T14:00',"
            "  '2026-08-01T14:30',0,'GFS 116','Class')")
        con.execute(
            "INSERT INTO messages (guid, day, ts, chat_guid, handle,"
            " contact_name, is_from_me, text, refreshed_at) VALUES"
            " ('G-1','2026-08-01','2026-08-01T09:00','c1','+15551234',"
            "  'Rowan',0,'bring the charger','now')")


def test_ref_is_stable_and_resolves(dbfile):
    con = db.connect()
    _seed(con)
    ref = citations.remember("email", "1a0c509c", "Registrar: Room change",
                             "2026-08-01", con)
    # The ref is a function of its target: a second read reuses it.
    again = citations.remember("email", "1a0c509c", "Registrar: Room change",
                               "2026-08-01", con)
    con.close()
    assert ref == again
    assert ref.startswith("e")

    got = citations.resolve(ref)
    assert got["found"] is True
    assert got["kind"] == "email"
    assert got["title"] == "Room change"
    assert got["who"] == "Registrar"
    assert got["id"] == "1a0c509c"


def test_invented_ref_does_not_resolve(dbfile):
    con = db.connect()
    _seed(con)
    con.close()
    # Nothing was handed out, so nothing resolves.
    assert citations.resolve("e00000") is None
    assert citations.resolve("ezzzzz") is None      # not even ref-shaped
    assert citations.resolve("") is None


def test_row_gone_resolves_to_gone_not_to_nothing(dbfile):
    con = db.connect()
    _seed(con)
    ref = citations.remember("message", "G-1", "Rowan: bring the charger",
                             "2026-08-01", con)
    with con:
        con.execute("DELETE FROM messages WHERE guid = 'G-1'")
    con.close()
    got = citations.resolve(ref)
    assert got is not None                  # ref was real
    assert got["found"] is False            # row is not
    assert got["label"] == "Rowan: bring the charger"
    assert "detail" not in got              # nothing invented to fill the gap


def test_collision_lengthens_rather_than_steals(dbfile, monkeypatch):
    con = db.connect()
    _seed(con)
    # Force every 5-digit ref to collide, whatever the target.
    real = citations._ref
    monkeypatch.setattr(citations, "_ref",
                        lambda k, t, w: (f"{citations.KINDS[k]}aaaaa"
                                         if w == 5 else real(k, t, w)))
    first = citations.remember("email", "1a0c509c", "first", "2026-08-01", con)
    second = citations.remember("email", "OTHER", "second", "2026-08-01", con)
    con.close()
    assert first == "eaaaaa"
    # A transcript already holding `eaaaaa` keeps pointing at its email.
    assert second != first
    assert len(second) > len(first)
    assert citations.resolve(first)["id"] == "1a0c509c"


def test_event_ref_is_per_occurrence(dbfile):
    con = db.connect()
    _seed(con)
    with con:
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, summary,"
            " start_at, end_at, all_day, location, calendar_name) VALUES"
            " ('2026-08-08','ev1','cal@g','Advising','2026-08-08T14:00',"
            "  '2026-08-08T14:30',0,'GFS 116','Class')")
    a = citations.remember("event", "2026-08-01/ev1", "Advising", "2026-08-01", con)
    b = citations.remember("event", "2026-08-08/ev1", "Advising", "2026-08-08", con)
    con.close()
    # Two instances of a recurring event get two refs.
    assert a != b
    assert citations.resolve(a)["day"] == "2026-08-01"
    assert citations.resolve(b)["day"] == "2026-08-08"


def test_refs_in_reads_a_sentence(dbfile):
    text = ("Your 2pm is in WPH 205 [cite:e7a3f1], moved from GFS 116 "
            "[cite:v2b0c4]. Rowan has the charger [cite:e7a3f1].")
    assert citations.refs_in(text) == ["e7a3f1", "v2b0c4"]
    assert citations.refs_in("no refs here [cite:xyz]") == []
    assert citations.refs_in("") == []


def test_provider_cite_hooks_mint_refs(dbfile):
    from app.providers import calendar as cal_provider
    from app.providers import email as email_provider
    from app.providers import messages as msg_provider

    con = db.connect()
    _seed(con)
    con.close()

    # Rows are in each provider's get() shape, not the DB shape (cite() absorbs the gap).
    e = email_provider.cite({"id": "1a0c509c", "day": "2026-08-01",
                             "received": "2026-08-01T10:35",
                             "from": "Registrar", "subject": "Room change"})
    v = cal_provider.cite({"id": "ev1", "day": "2026-08-01",
                           "start": "2026-08-01T14:00", "summary": "Advising"})
    m = msg_provider.cite({"guid": "G-1", "day": "2026-08-01",
                           "ts": "2026-08-01T09:00", "contact_name": "Rowan",
                           "text": "bring the charger"})
    assert citations.resolve(e)["title"] == "Room change"
    assert citations.resolve(v)["title"] == "Advising"
    assert citations.resolve(m)["detail"] == "bring the charger"

    # An aggregate row has no one message behind it: decline.
    assert msg_provider.cite({"day": "2026-08-01", "value": 12}) is None
    assert email_provider.cite({"day": "2026-08-01", "value": 3}) is None


def test_brief_lines_carry_their_ref(dbfile):
    """The ref rides the row the model actually reads."""
    from app.providers import email as email_provider
    from app import console_tools

    con = db.connect()
    _seed(con)
    con.close()

    items = [{"id": "1a0c509c", "day": "2026-08-01",
              "received": "2026-08-01T10:35", "from": "Registrar",
              "subject": "Room change", "unread": True}]
    body = [email_provider.brief_line(items[0])]
    out = console_tools._with_refs(email_provider, items, body)
    assert len(out) == 1
    refs = citations.refs_in(f"[cite:{out[0].rsplit('[', 1)[1].rstrip(']')}]")
    assert refs and citations.resolve(refs[0])["id"] == "1a0c509c"


def test_with_refs_passes_through_uncitable_sources(dbfile):
    from app import console_tools
    from app.providers import weather as weather_provider

    body = ["72F clear", "68F cloudy"]
    # No cite() on the module: rows render exactly as they did before.
    assert not hasattr(weather_provider, "cite")
    assert console_tools._with_refs(weather_provider, [{}, {}], body) == body


def test_full_read_carries_refs_too(dbfile):
    """The `brief=false` path carries refs too (else the model cited the raw Gmail id)."""
    from app import console_tools
    from app.providers import email as email_provider

    con = db.connect()
    _seed(con)
    con.close()

    out = {"data": [{"id": "1a0c509c", "day": "2026-08-01",
                     "received": "2026-08-01T10:35", "from": "Registrar",
                     "subject": "Room change"}],
           "count": 1}
    shaped = console_tools._cited(email_provider, out)
    ref = shaped["data"][0]["cite"]
    assert citations.resolve(ref)["id"] == "1a0c509c"
    # The provider's row is not mutated (caches still hold some).
    assert "cite" not in out["data"][0]


def test_full_read_leaves_uncitable_sources_alone(dbfile):
    from app import console_tools
    from app.providers import weather as weather_provider

    out = {"data": [{"temp": 72}], "count": 1}
    assert console_tools._cited(weather_provider, out) == out
