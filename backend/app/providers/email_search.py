"""Finding a message again (IDEAS §5) — the reason review files anything.

Two tiers. Locally, an FTS5 index (`email_fts`) over what app.db already
holds for every cached message: subject, sender, the model's summary, its
filing label, and `terms` — the order numbers, amounts, people and dates the
summarizer pulled out of the body. That covers "the Lyft ride in September"
and "order 124057035" without a body ever being stored (§5's rule). Past the
cache, Gmail's own search, which does read bodies: its hits are fetched as
metadata and written into `emails`, so the backfill summarizes them and the
next search finds them locally.

`email_fts` is derived and droppable. Rather than being kept in step by every
writer — the summarizer, both sync paths, the action route, undo — it is
rebuilt whole whenever a signature over the tables it reads stops matching
(`ensure_index`). A rebuild of the ~1,200 cached messages is tens of
milliseconds and runs only when a search does, so one cheap rule replaces a
hook in five places that each could be forgotten.
"""

import re
from datetime import date, datetime, timedelta

from .. import db

LIMIT = 25

_SIGNATURE = (
    "SELECT (SELECT COUNT(*) || ':' || IFNULL(MAX(refreshed_at), '')"
    "        FROM emails)"
    " || '|' || (SELECT COUNT(*) || ':' || IFNULL(MAX(summarized_at), '')"
    "        || ':' || IFNULL(MAX(user_action_at), '')"
    "        || ':' || COUNT(user_label) FROM email_triage) AS sig")

# One row per message: the filed label wins over the model's pick, since that
# is what the message is actually under in Gmail.
_ROWS = (
    "SELECT e.msg_id, IFNULL(e.subject, '') AS subject,"
    " TRIM(IFNULL(e.from_name, '') || ' ' || IFNULL(e.from_addr, '')"
    "      || ' ' || IFNULL(e.to_addr, '')) AS sender,"
    " IFNULL(t.summary, e.snippet) AS summary, IFNULL(t.terms, '') AS terms,"
    " REPLACE(IFNULL(t.user_label, IFNULL(t.label, '')), '/', ' ') AS label"
    " FROM emails e LEFT JOIN email_triage t USING(msg_id)")


def ensure_index(con) -> bool:
    """Rebuild email_fts if anything it indexes has moved. True if rebuilt."""
    sig = con.execute(_SIGNATURE).fetchone()["sig"]
    have = con.execute("SELECT signature FROM email_fts_state WHERE id=1").fetchone()
    if have and have["signature"] == sig:
        return False
    with con:
        con.execute("DELETE FROM email_fts")
        con.execute("INSERT INTO email_fts (msg_id, subject, sender, summary,"
                    " terms, label) " + _ROWS)
        con.execute("INSERT INTO email_fts_state (id, signature) VALUES (1, ?)"
                    " ON CONFLICT(id) DO UPDATE SET signature=excluded.signature",
                    (sig,))
    return True


_WORD = re.compile(r"[\w$.#@'-]+", re.U)


def fts_query(text: str) -> str | None:
    """Free text -> an FTS5 MATCH expression: every word must appear, each
    quoted (so `c++`, `#1240` or `AND` can't be read as syntax) and each a
    prefix — a half-typed word already matches, and "receipt" finds
    "receipts" without a stemmer (see the migration for why there isn't one)."""
    words = [w.strip(".'-#@$") for w in _WORD.findall(text or "")]
    words = [w for w in words if w]
    if not words:
        return None
    return " ".join('"%s"*' % w.replace('"', '') for w in words)


def _card(r) -> dict:
    return {"id": r["msg_id"], "day": r["day"], "received": r["received_at"],
            "from": r["from_name"] or r["from_addr"],
            "from_addr": r["from_addr"], "subject": r["subject"],
            "summary": r["summary"], "label": r["user_label"] or r["label"],
            "filed": bool(r["user_label"]),
            "in_inbox": bool(r["in_inbox"]), "unread": bool(r["unread"])}


_CARD = (
    "SELECT e.msg_id, e.day, e.received_at, e.from_name, e.from_addr,"
    " e.subject, e.in_inbox, e.unread, t.summary, t.label, t.user_label"
    " FROM emails e LEFT JOIN email_triage t USING(msg_id)")


def local(con, text: str, *, label: str | None = None, since: str | None = None,
          limit: int = LIMIT) -> list[dict]:
    """Best matches in the cache, best first (bm25, subject and terms weighted
    over the summary). `label` narrows to one label and its sublabels;
    `since` is a YYYY-MM-DD floor on the day received."""
    q = fts_query(text)
    if q is None and not label:
        return []
    where, args = [], []
    if label:
        where.append("(IFNULL(t.user_label, t.label) = ?"
                     " OR IFNULL(t.user_label, t.label) LIKE ?)")
        args += [label, label + "/%"]
    if since:
        where.append("e.day >= ?")
        args.append(since)
    if q is None:                    # a label on its own: newest first
        sql = (_CARD + " WHERE " + " AND ".join(where)
               + " ORDER BY e.received_at DESC LIMIT ?")
        return [_card(r) for r in con.execute(sql, [*args, limit])]
    ensure_index(con)
    # bm25 column weights follow the fts5() column order: msg_id, subject,
    # sender, summary, terms, label. A subject or an extracted term hit is a
    # far stronger signal than a word the summary happened to use.
    sql = (_CARD.replace(" FROM emails e",
                         " FROM email_fts f JOIN emails e ON e.msg_id = f.msg_id")
           + " WHERE email_fts MATCH ?"
           + "".join(" AND " + w for w in where)
           + " ORDER BY bm25(email_fts, 0, 8, 4, 2, 6, 3), e.received_at DESC"
           + " LIMIT ?")
    return [_card(r) for r in con.execute(sql, [q, *args, limit])]


def gmail(con, text: str, limit: int = LIMIT) -> list[dict]:
    """Gmail's own search (bodies included) for what the cache doesn't hold.
    Hits are cached as metadata so they're local from then on. Network — the
    caller must not be holding a write transaction."""
    from . import email as provider
    token = provider.access_token()
    ids = provider.list_ids(token, text, max_ids=limit)
    now = datetime.now().isoformat(timespec="seconds")
    new = provider.fetch_new(con, token, ids)
    if new:
        with con:
            for row in new:
                provider.upsert(con, row, now)
    if not ids:
        return []
    order = {mid: i for i, mid in enumerate(ids)}
    rows = con.execute(_CARD + " WHERE e.msg_id IN (%s)"
                       % ",".join("?" * len(ids)), ids).fetchall()
    return sorted((_card(r) for r in rows), key=lambda c: order[c["id"]])


def search(text: str, *, label: str | None = None, days: int | None = None,
           limit: int = LIMIT, use_gmail: bool | None = None) -> dict:
    """Local first; Gmail when asked, or when the cache finds nothing.

    Returns {query, count, data, gmail} — `gmail` says whether Gmail was
    searched, so a caller can offer it when it wasn't. A label-scoped search
    stays local: Gmail only knows the labels review actually applied, so it
    would answer a narrower question than the one asked."""
    since = ((date.fromisoformat(db.day_key()) - timedelta(days=days)).isoformat()
             if days else None)
    with db.session() as con:
        hits = local(con, text, label=label, since=since, limit=limit)
        searched = False
        if (fts_query(text) and not label
                and (use_gmail or (use_gmail is None and not hits))):
            searched = True
            seen = {h["id"] for h in hits}
            gq = text + (f" after:{since.replace('-', '/')}" if since else "")
            hits += [h for h in gmail(con, gq, limit) if h["id"] not in seen]
    hits = hits[:limit]
    return {"query": text, "count": len(hits), "data": hits, "gmail": searched}
