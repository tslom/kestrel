"""What you actually do with email — the prior the model didn't have.

The summarizer in email_triage.py guesses an action from the text of a message.
Measured against 875 recorded decisions it agreed with the mailbox owner 28.9%
of the time, where always saying "archive" agrees 78.3%. It recommended `keep`
331 times; the owner kept 8. The model's idea of a person ("mail from a real
human deserves attention") is not this person, and no amount of prompt wording
fixes a prior that is simply wrong.

So the recommendation stops being a guess about the text and becomes a
statement about the record:

  sender memory   a sender you have decided about twice, the same way, is
                  decided. 91% precise over 349 calls (97% over the 289 that
                  say "archive"; `delete` is the weak one at 60%).
  domain          77%, and only where the exact address is unknown.
  dwell           how long a card has sat runs opposite to intuition. Mail
                  handled within six hours is 92% archived; mail still
                  sitting after three days is 57% *deleted*. Sitting in the
                  queue does not mean "I am keeping this", it means "I did
                  not care". Weakest of the three at 52%, so it speaks only
                  when nothing is known about the sender.

Backtested time-ordered over the 831 decisions on record, replaying them
through this module and predicting each from strictly earlier ones
(scratchpad/verify_real.py; rerun it after changing a threshold):

    strategy          overall   non-archive caught   precision
    model (today)      30.4%        52/178 = 29%      52/610 =  9%
    always archive     78.6%         0/178 =  0%        never fires
    this module        82.7%        78/178 = 44%      78/137 = 57%

The precision column is the one that matters. "Always archive" scores 78.6%
and is useless, because the 178 decisions that were not "archive" are the only
ones where a recommendation is worth reading. The model finds 52 of them but
cries wolf 610 times to do it; this finds 78 and cries wolf 137 times.

Review has since collapsed to three actions — archive, unsubscribe, keep —
with every one of them filing the message under a label, because a reviewed
message's only remaining use is being found. That retired `delete` and `bin`,
and with them dwell: its only ever verdict was "delete", which now means
"archive", which is already the default. The record keeps the old actions
exactly as they happened; _counts() reads them as `archive` (FOLD), so
nothing had to be rewritten to change what they mean.

Labels get their own prior from the same record (predict_label): what this
sender's mail was actually filed under, read straight from
email_triage.user_label — a sender whose last two messages went to the same
label is decided, the rule the action side already earned.

Nothing here is a source of truth. email_sender_stats is an aggregate of
email_triage.user_action and rebuild() recomputes it from scratch, so the
table is droppable like any other derived cache (CLAUDE.md).
"""

from collections import Counter
from datetime import datetime

# A sender's habits can drift, so old decisions count for less. Chosen by
# backtest rather than taste: over the current record every half-life from 30d
# to 365d scores within 0.2 points of the others (the data spans ten weeks, so
# decay has little to bite on). 90d wins on precision by 3 points over no
# decay at all and is the shortest window that does not thrash on a sender
# seen twice a quarter.
HALF_LIFE_DAYS = 90.0

# Two decisions is enough to call a sender, because the third is 97% likely to
# match when the first two agree. Below 0.75 agreement the sender is genuinely
# mixed and we say nothing rather than flip a coin with a confident sentence
# attached to it.
#
# The count compared against this floor is the raw one (`obs`), never the
# decayed weight — see the migration. Evidence does not evaporate; only the
# question of which action is *current* decays.
MIN_OBSERVATIONS = 2
AGREEMENT = 0.75

# A domain speaks only when the exact address has not been seen. Weaker and
# noisier (one newsletter says nothing about a colleague at the same company),
# so it needs more evidence before it counts.
DOMAIN_MIN_OBSERVATIONS = 3

# Every action ever recorded, so the record replays whole; only the first
# three are still offered. Retired ones are read as what review would do with
# that message today — both left the inbox with nothing further to do.
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
    """ISO timestamp -> naive local datetime.

    emails.received_at carries an offset (it comes from the Gmail header) and
    email_triage.user_action_at does not (we write it with
    datetime.now().isoformat()). Subtracting one from the other raises, which
    is how the first pass at this silently measured no dwell at all — every
    difference landed in an `except` and came back zero. Both sides get
    flattened here so that can't recur.
    """
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

    Called from the action route after the mailbox write succeeds — a
    recommendation learned from an action that errored out would be learned
    from something that never happened.

    Decay is applied on write rather than on read: each key's counts are aged
    to `now` before the new decision lands, so a row's `n` is always current
    as of its own `last_at` and a read is a plain SELECT.
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
        # Every action for this key ages by the same factor, taken from the
        # most recent decision of any shape — otherwise an action not seen in
        # months would decay relative to one seen yesterday twice over, once
        # here and again the next time anything arrives.
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
    """Back a retracted decision out of the aggregates.

    Subtracting is not the exact inverse of record() — the decay applied on
    the way in is not undone, so a key that has aged since keeps a sliver of
    the retracted decision. That residue is bounded by the decay of one
    action and disappears at the next rebuild(); paying for exactness here
    would mean storing every decision individually, which is what
    email_triage already is.
    """
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
    """Recompute every aggregate from email_triage. Returns decisions folded.

    This is what makes the table droppable. It is also how the learner starts
    life with 875 decisions behind it instead of cold — without it the first
    months of recommendations would be the same constant "archive" the model
    should have been saying all along.
    """
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

    This is what makes "droppable" true rather than merely claimed. Dropped or
    never backfilled, the table is recreated empty by the migration and every
    recommendation silently falls back to a bare "archive" forever — the
    failure looks exactly like a learner that has not learned anything yet, so
    nothing would ever point at it. Lazily on first read instead of on a
    schedule, the way the rest of the dashboard regenerates derived state.

    The guard is a LIMIT 1 on a table of a few hundred rows, so the steady
    state costs one index probe per queue render.
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

    The action is chosen by decayed weight so a sender whose habits changed
    reports the new habit, but the share quoted back to the card is the raw
    one: "you archived 4 of the last 5" has to mean five actual messages, or
    the sentence on the card is not a fact about the record.
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

    Returns {action, reason, basis, confidence}. `reason` is written to be
    shown on the card: a recommendation learned from two clicks has to say so
    out loud before it is trusted, the same rule place_suggest follows for places.
    """
    addr = (from_addr or "").strip().lower()
    if not addr:
        return None
    weight, seen = _counts(con, "addr", addr)
    if (v := _verdict(weight, seen, MIN_OBSERVATIONS)):
        action, share, total = v
        # Quote the count, not a hedge: "most of the last 43" is vaguer
        # than the domain line sitting right under it, and the whole point
        # of showing the reason is that you can check it.
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

    Decided when the sender's last two filings agree. Recency rather than a
    majority on purpose: this is the correction path. Relabel a sender twice
    and the next card already says so, instead of waiting for the new label to
    outvote a history of the old one.

    Read straight from email_triage rather than aggregated like actions: a
    sender's last few filings are one indexed scan, and there is nothing to
    decay or rebuild.
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


