"""Kind-prefixed keys — the identity scheme both §21 delivery channels use.

Every alarm and every notification is named by a `kind:part:part` string, and
the scheme is load-bearing twice over. The prefix is what lets the alarm
reconciler partition written rows by source, so a source that failed to read can
only ever hold its *own* alarms (see alarms.py). And the join has to be
injective, because two different facts landing on one key means one of them
silently inherits the other's delivery record — it is recorded as already sent
and never fires.

Its own module because the second channel (notify.py) needs the same rule for
the same reason, and the reason is subtle enough that two copies of it would be
one copy too many.
"""

from __future__ import annotations


def escape_part(value: str) -> str:
    """Make arbitrary id-shaped text safe to pass to `join`.

    `packages.order_key` is literally `merchant:order_id` — the natural identity
    for a package cannot be a key part at all. Escaping rather than stripping,
    because the ban on ':' is what makes the join injective: with colons merely
    replaced, ('a:b', 'c') and ('a', 'b:c') would land on one key and two
    packages would share one row. Percent-encoding keeps distinct ids distinct
    while staying readable in a log line.
    """
    return value.replace("%", "%25").replace(":", "%3A")


def kind_of(key: str) -> str:
    """The kind a stored key belongs to. Total: an unparseable key returns a
    kind no source claims, so it is held rather than attributed to one."""
    return key.split(":", 1)[0]


def join(kind: str, *parts: str, allowed: tuple[str, ...], label: str) -> str:
    """`kind:part:part`, with the two checks that keep the scheme honest.

    `allowed` is per-channel — the kinds an alarm can carry are not the kinds a
    notification can — and `label` only shapes the error message, which earns a
    parameter because these raise inside a source, where the message is the
    whole diagnosis.
    """
    if kind not in allowed:
        raise ValueError(f"unknown {label} kind: {kind!r}")
    if any(":" in p for p in parts):
        # A colon inside a part would forge a kind boundary on read-back.
        raise ValueError(f"{label} key parts may not contain ':': {parts!r}")
    return ":".join((kind, *parts))
