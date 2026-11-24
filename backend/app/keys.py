"""Kind-prefixed keys (`kind:part:part`) shared by alarms and notify.

The prefix lets the alarm reconciler partition rows by source (a source that
failed to read holds only its own alarms). The join must be injective: two facts
on one key means one inherits the other's delivery record and never fires.
Design: DESIGN.md → `app/keys.py`.
"""

from __future__ import annotations


def escape_part(value: str) -> str:
    """Percent-encode `%` and `:` so text is a safe `join` part. Escape, don't
    strip or replace: ('a:b','c') and ('a','b:c') must stay distinct keys."""
    return value.replace("%", "%25").replace(":", "%3A")


def kind_of(key: str) -> str:
    """The kind a stored key belongs to. Total: an unparseable key yields a kind no source claims."""
    return key.split(":", 1)[0]


def join(kind: str, *parts: str, allowed: tuple[str, ...], label: str) -> str:
    """`kind:part:part`. `allowed` is per-channel; `label` only names the channel in the error."""
    if kind not in allowed:
        raise ValueError(f"unknown {label} kind: {kind!r}")
    if any(":" in p for p in parts):
        # A colon in a part would forge a kind boundary on read-back.
        raise ValueError(f"{label} key parts may not contain ':': {parts!r}")
    return ":".join((kind, *parts))
