"""The `reply_latency` feature — how fast you answered, per day.

The volume features (msgs_sent/received/contacts) can't tell a chatty day
from a responsive one; this is the responsiveness half, and every gate here
came out of measuring it against real history.
"""

from datetime import datetime

from app import db
from app.providers import features, messages
from app.providers.ranges import parse_range

DAY = "2026-03-10"


def _seed(con, rows, chats=()):
    """rows: (chat_guid, is_from_me, ts). Mirrors test_messages_unanswered's
    seeder; handle is only set on inbound, as chat.db does."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with con:
        for guid, is_group in chats:
            con.execute("INSERT INTO chats (chat_guid, display_name, is_group,"
                        " refreshed_at) VALUES (?,NULL,?,?)", (guid, is_group, now))
        for i, (guid, mine, ts) in enumerate(rows):
            con.execute(
                "INSERT INTO messages (guid, day, ts, chat_guid, handle,"
                " contact_name, is_from_me, text, refreshed_at, is_reaction)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f"g{i}", db.day_key(datetime.fromisoformat(ts)), ts, guid,
                 None if mine else "5551212", None, mine, "hi", now))


def _at(day: str, hh: int, mm: int = 0) -> str:
    return f"{day}T{hh:02d}:{mm:02d}:00"


def _pair(guid, day, in_h, in_m, out_h, out_m):
    """One inbound and the reply that closed it."""
    return [(guid, 0, _at(day, in_h, in_m)), (guid, 1, _at(day, out_h, out_m))]


def test_median_of_the_days_replies(con):
    rows = (_pair("c1", DAY, 9, 0, 9, 5)      # 5 min
            + _pair("c2", DAY, 10, 0, 10, 20)  # 20 min
            + _pair("c3", DAY, 11, 0, 11, 45))  # 45 min
    _seed(con, rows)
    assert messages.reply_latency_by_day(con, DAY, DAY) == {DAY: 20.0}


def test_a_reply_lands_on_the_day_you_sent_it(con):
    """Keyed on your reply, not their message: a Monday answer to a Sunday
    text is Monday's latency, so a finished day never gets revised later."""
    _seed(con, [("c1", 0, _at("2026-03-09", 22, 0)),
                ("c1", 1, _at(DAY, 8, 0))]      # 10h later, next day
               + _pair("c2", DAY, 9, 0, 9, 30)
               + _pair("c3", DAY, 10, 0, 10, 10))
    out = messages.reply_latency_by_day(con, DAY, DAY)
    assert out == {DAY: 30.0}          # median of 600, 30, 10
    assert "2026-03-09" not in out


def test_first_unanswered_message_starts_the_clock(con):
    """Five texts and one answer means you took as long as the first one
    waited — not as long as the last one did."""
    _seed(con, [("c1", 0, _at(DAY, 9, 0)), ("c1", 0, _at(DAY, 9, 20)),
                ("c1", 0, _at(DAY, 9, 40)), ("c1", 1, _at(DAY, 10, 0))]
               + _pair("c2", DAY, 11, 0, 12, 0)
               + _pair("c3", DAY, 13, 0, 14, 0))
    assert messages.reply_latency_by_day(con, DAY, DAY)[DAY] == 60.0


def test_group_chats_are_excluded(con):
    """Nobody owes a group a reply, so the gap isn't a decision you made —
    same reasoning as `unanswered`."""
    _seed(con, _pair("grp", DAY, 9, 0, 9, 5) + _pair("grp", DAY, 10, 0, 10, 5)
               + _pair("grp", DAY, 11, 0, 11, 5),
          chats=[("grp", 1)])
    assert messages.reply_latency_by_day(con, DAY, DAY) == {}


def test_new_conversation_is_not_a_slow_reply(con):
    """Texting someone a week after their last message is you starting
    something, not a 10,000-minute reply."""
    _seed(con, [("c1", 0, _at("2026-03-01", 9, 0)), ("c1", 1, _at(DAY, 9, 0))]
               + _pair("c2", DAY, 10, 0, 10, 10)
               + _pair("c3", DAY, 11, 0, 11, 20)
               + _pair("c4", DAY, 12, 0, 12, 30))
    # 10/20/30, not a median dragged toward nine days
    assert messages.reply_latency_by_day(con, DAY, DAY)[DAY] == 20.0


def test_stale_thread_does_not_discard_a_fast_reply(con):
    """Measured on real history: 61 of 111 dropped pairs were genuinely fast
    answers sitting behind one ancient unreplied text. The pair retries
    against their most recent message before being dropped."""
    _seed(con, [("c1", 0, _at("2026-02-01", 9, 0)),   # never answered, months old
                ("c1", 0, _at(DAY, 9, 0)),            # they write again
                ("c1", 1, _at(DAY, 9, 2))]            # answered in 2 min
               + _pair("c2", DAY, 10, 0, 10, 2)
               + _pair("c3", DAY, 11, 0, 11, 2))
    assert messages.reply_latency_by_day(con, DAY, DAY)[DAY] == 2.0


def test_thin_days_stay_a_gap(con):
    """One slow answer is not a habit. Below REPLY_MIN_SAMPLES the day is
    absent, not zero and not a wild median — the HR_MIN_HOURS precedent."""
    _seed(con, _pair("c1", DAY, 9, 0, 20, 0) + _pair("c2", DAY, 10, 0, 10, 5))
    assert messages.reply_latency_by_day(con, DAY, DAY) == {}


def test_unanswered_messages_contribute_nothing(con):
    """Silence is what `unanswered` measures. Folding it in here as a huge
    number would let one ignored text swamp a day of fast replies."""
    _seed(con, _pair("c1", DAY, 9, 0, 9, 10) + _pair("c2", DAY, 10, 0, 10, 10)
               + _pair("c3", DAY, 11, 0, 11, 10)
               + [("c4", 0, _at(DAY, 12, 0))])  # never answered
    assert messages.reply_latency_by_day(con, DAY, DAY)[DAY] == 10.0


def test_provider_and_features_never_drift(con):
    """features.py calls the provider's function rather than re-expressing it
    in SQL — the mistake that let `novelty` drift for twelve days (v35)."""
    _seed(con, _pair("c1", DAY, 9, 0, 9, 10) + _pair("c2", DAY, 10, 0, 10, 30)
               + _pair("c3", DAY, 11, 0, 11, 50))
    from_metric = messages.get(parse_range(f"{DAY}..{DAY}"), "reply_latency")["data"]
    from_features = [r for r in features._messages_rows(con, [DAY])
                     if r[1] == "reply_latency"]
    assert {d["day"]: d["value"] for d in from_metric} == {DAY: 30.0}
    assert [(d, v) for d, _, v in from_features] == [(DAY, 30.0)]


def test_features_only_yields_days_it_was_asked_for(con):
    """The extractor reaches back a gap-window for context, but the caller's
    DELETE only covers the requested days — a row outside them would never
    be recomputed."""
    _seed(con, _pair("c1", "2026-03-09", 9, 0, 9, 10)
               + _pair("c2", "2026-03-09", 10, 0, 10, 10)
               + _pair("c3", "2026-03-09", 11, 0, 11, 10)
               + _pair("c1", DAY, 9, 0, 9, 20) + _pair("c2", DAY, 10, 0, 10, 20)
               + _pair("c3", DAY, 11, 0, 11, 20))
    days = {d for d, f, _ in features._messages_rows(con, [DAY]) if f == "reply_latency"}
    assert days == {DAY}
