"""What you actually do with email — the prior the model didn't have.

The model's guess at an action agreed with the owner 28.9% of 875 decisions
(always "archive": 78.3%), so the recommendation comes from the record instead:
a sender (then domain) decided the same way twice. `predict_label` does the same
for labels.

Source of truth: email_triage. `email_sender_stats` is a derived aggregate that
`rebuild()` recomputes; never hand-edit it. Traps: `received_at` has a tz offset
and `user_action_at` doesn't (`_parse`); retired `delete`/`bin` read as `archive`
(FOLD).
Design: DESIGN.md → §5 Email.
"""

from collections import Counter
from datetime import datetime

# Chosen by backtest: 30d-365d all score within 0.2 points; 90d beats no decay by 3 points of precision.
HALF_LIFE_DAYS = 90.0

# The floor compares raw `obs`, never the decayed weight. Below AGREEMENT the sender is mixed: say nothing.
MIN_OBSERVATIONS = 2
AGREEMENT = 0.75

# A domain speaks only when the exact address is unseen, and needs more evidence.
DOMAIN_MIN_OBSERVATIONS = 3

# Every action ever recorded, so the record replays whole; only the first three are still offered.
ACTIONS = ("archive", "unsubscribe", "keep", "delete", "bin")
FOLD = {"delete": "archive", "bin": "archive"}

# Past tense for the card's sentence ("you archived 4 of the last 5").
_DID = {"archive": "archived", "unsubscribe": "unsubscribed from",
        "keep": "kept"}

# How many of the sender's most recent filings the label prior looks at.
LABEL_WINDOW = 5


def domain_of(addr: str | None) -> str:
    return (addr or "").rsplit("@", 1)[-1].strip().lower()


def _parse(value: str | None) -> datetime | None:
    """ISO timestamp -> naive local datetime (received_at has an offset, user_action_at doesn't)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def _decay_factor(last_at: str | None, now: datetime) -> float:
    last = _parse(last_at)
    if last is None:
        return 1.0
    days = (now - last).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    return 0.5 ** (days / HALF_LIFE_DAYS)


# ---------- writing ----------

def record(con, from_addr: str | None, action: str,
           at: str | None = None) -> None:
    """Fold one decision into the sender and domain aggregates.

    Call only after the mailbox write succeeded. Decay is applied on write, so a
    row's `n` is current as of its own `last_at`.
    """
    if action not in ACTIONS:
        return
    addr = (from_addr or "").strip().lower()
    if not addr:
        return
    now_dt = _parse(at) or datetime.now()
    now = now_dt.isoformat(timespec="seconds")

    for scope, key in (("addr", addr), ("domain", domain_of(addr))):
        if not key:
            continue
        rows = con.execute(
            "SELECT action, n, last_at FROM email_sender_stats"
            " WHERE scope=? AND key=?", (scope, key)).fetchall()
        # One factor for every action of the key, from its newest decision: else stale actions decay twice.
        newest = max((r["last_at"] for r in rows if r["last_at"]), default=None)
        factor = _decay_factor(newest, now_dt)
        for r in rows:
            con.execute(
                "UPDATE email_sender_stats SET n=? WHERE scope=? AND key=? AND action=?",
                (r["n"] * factor, scope, key, r["action"]))
        con.execute(
            "INSERT INTO email_sender_stats (scope, key, action, n, obs, last_at)"
            " VALUES (?,?,?,1,1,?)"
            " ON CONFLICT(scope, key, action) DO UPDATE SET"
            " n = n + 1, obs = obs + 1, last_at = excluded.last_at",
            (scope, key, action, now))


def unrecord(con, msg_id: str, action: str) -> None:
    """Back a retracted decision out. Not an exact inverse of record(): a sliver of decay residue stays until rebuild()."""
    if action not in ACTIONS:
        return
    row = con.execute("SELECT from_addr FROM emails WHERE msg_id=?",
                      (msg_id,)).fetchone()
    addr = ((row["from_addr"] if row else "") or "").strip().lower()
    if not addr:
        return
    for scope, key in (("addr", addr), ("domain", domain_of(addr))):
        if not key:
            continue
        con.execute(
            "UPDATE email_sender_stats SET n = MAX(n - 1, 0), obs = MAX(obs - 1, 0)"
            " WHERE scope=? AND key=? AND action=?", (scope, key, action))
        con.execute(
            "DELETE FROM email_sender_stats"
            " WHERE scope=? AND key=? AND action=? AND obs <= 0",
            (scope, key, action))


def rebuild(con) -> int:
    """Recompute every aggregate from email_triage. Returns decisions folded."""
    rows = con.execute(
        "SELECT t.user_action AS action, t.user_action_at AS at, e.from_addr"
        " FROM email_triage t JOIN emails e USING(msg_id)"
        " WHERE t.user_action IS NOT NULL AND e.from_addr IS NOT NULL"
        " ORDER BY t.user_action_at").fetchall()
    con.execute("DELETE FROM email_sender_stats")
    n = 0
    for r in rows:
        record(con, r["from_addr"], r["action"], r["at"])
        n += 1
    return n


# ---------- reading ----------

def ensure_warm(con) -> int:
    """Rebuild the aggregates if they are empty and there is history to build
    them from. Returns how many decisions were folded in (0 = nothing to do).

    Without it an empty table silently reads as a learner that hasn't learned yet.
    """
    if con.execute("SELECT 1 FROM email_sender_stats LIMIT 1").fetchone():
        return 0
    if not con.execute("SELECT 1 FROM email_triage WHERE user_action IS NOT NULL"
                       " LIMIT 1").fetchone():
        return 0          # genuinely cold: no decisions on record yet
    with con:
        return rebuild(con)


def _counts(con, scope: str, key: str) -> tuple[Counter, Counter]:
    """(decayed weights, raw tallies) for one key. The first picks the winner,
    the second decides whether there is enough evidence to pick at all.
    Retired actions are counted as what they fold to."""
    rows = con.execute(
        "SELECT action, n, obs FROM email_sender_stats"
        " WHERE scope=? AND key=?", (scope, key)).fetchall()
    weight, seen = Counter(), Counter()
    for r in rows:
        action = FOLD.get(r["action"], r["action"])
        weight[action] += r["n"]
        seen[action] += r["obs"]
    return weight, seen


def _verdict(weight: Counter, seen: Counter,
             floor: int) -> tuple[str, float, int] | None:
    """Winning action, its share of the raw record, and how many decisions
    that is — or None when the sender is too new or genuinely mixed.

    Winner by decayed weight; the quoted share is raw, so the card's count is real.
    """
    total = sum(seen.values())
    if total < floor:
        return None
    action = weight.most_common(1)[0][0] if weight else None
    if action is None:
        return None
    share = seen[action] / total
    return (action, share, total) if share >= AGREEMENT else None


def _plural(n: float, word: str) -> str:
    whole = int(round(n))
    return f"{whole} {word}" if whole == 1 else f"{whole} {word}s"


def predict(con, from_addr: str | None) -> dict | None:
    """Recommend an action from the record, or None for the default.

    Returns {action, reason, basis, confidence}; `reason` is shown on the card verbatim.
    """
    addr = (from_addr or "").strip().lower()
    if not addr:
        return None
    weight, seen = _counts(con, "addr", addr)
    if (v := _verdict(weight, seen, MIN_OBSERVATIONS)):
        action, share, total = v
        # Quote the count, not a hedge: the reason is there to be checked.
        how = ("every one of the last" if share == 1.0
               else f"{seen[action]} of the last")
        return {"action": action,
                "reason": f"you {_DID[action]} {how} "
                          f"{_plural(total, 'message')} from this sender",
                "basis": "sender",
                "confidence": round(share, 2)}

    dom = domain_of(addr)
    if dom:
        weight, seen = _counts(con, "domain", dom)
        if (v := _verdict(weight, seen, DOMAIN_MIN_OBSERVATIONS)):
            action, share, total = v
            return {"action": action,
                    "reason": f"you {_DID[action]} {seen[action]} of the last "
                              f"{_plural(total, 'message')} from {dom}",
                    "basis": "domain",
                    "confidence": round(share, 2)}
    return None


def predict_label(con, from_addr: str | None) -> dict | None:
    """The label this sender's mail is filed under, from the record — or None
    to leave the model's pick.

    Decided when the sender's last two filings agree: recency, not majority, so a
    relabel twice takes effect at once. Read straight from email_triage (no aggregate).
    """
    addr = (from_addr or "").strip().lower()
    if not addr:
        return None
    recent = [r["user_label"] for r in con.execute(
        "SELECT t.user_label FROM email_triage t JOIN emails e USING(msg_id)"
        " WHERE lower(e.from_addr)=? AND t.user_label IS NOT NULL"
        " ORDER BY t.user_action_at DESC LIMIT ?", (addr, LABEL_WINDOW))]
    if len(recent) < MIN_OBSERVATIONS or recent[0] != recent[1]:
        return None
    run = next((i for i, l in enumerate(recent) if l != recent[0]), len(recent))
    return {"label": recent[0],
            "reason": f"your last {_plural(run, 'message')} from this sender "
                      f"went here",
            "basis": "sender"}


