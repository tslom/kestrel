"""Creating Contacts cards — the one direction the AddressBook read path can't go.

providers/messages.py reads the AddressBook sqlite files directly to resolve
`contact_name` and cluster identities. Those files are the sync engine's
private store: writing into them behind CardDAV's back is how you get clobbered
records or a wedged sync. So the write takes the long way round —
osascript -> Contacts.app -> CardDAV -> iCloud -> phone. Slower and
permission-gated, and the only route that ends anywhere but this machine.

Shaped after reminders.py (arguments never interpolation, one `_run`, a probe
for the permission) but importing nothing from it: that module lives on the
§21 alarms branch, and creating a contact shouldn't wait on an alarm
reconciler to land.

Three things checked against the real dictionary
(/System/Applications/Contacts.app/Contents/Resources/Contacts.sdef) rather
than remembered:

  * **There is no `account` class.** `make new person` lands in whichever
    account Contacts.app has set as default and AppleScript cannot name one.
    This machine has five sources — three CardDAV, two Exchange — so "did it
    land somewhere that reaches the phone?" is a real question. `locate()`
    answers it after the fact instead of guessing before.
  * **`id` is read-only**, inherited from `entry`, formatted `UUID:ABPerson` —
    byte-identical to ZABCDRECORD.ZUNIQUEID, which is what lets locate() be a
    plain lookup rather than a name match.
  * **`save` is required.** Without it the card lives only in Contacts.app's
    memory and disappears when it quits. Every mutating script here ends with
    one.

Dedupe reuses providers/messages.py's `normalize_handle` and record grouping
rather than reimplementing either. A card created here has to unify with the
CRM's existing people on the same rules the CRM already uses, or it shows up
as a second copy of someone you've been texting for years.
"""

from __future__ import annotations

import glob
import os
import sqlite3
import sys

from . import macos
from .config import ADDRESSBOOK_GLOB
from .providers.messages import addressbook_records, normalize_handle

_TIMEOUT = 30       # Contacts is slow on first launch, same as Reminders

# Contacts accepts these bare words and maps them onto its internal
# "_$!<Mobile>!$_" label constants; anything else becomes a custom label.
_DEFAULT_PHONE_LABEL = "mobile"
_DEFAULT_EMAIL_LABEL = "home"


class ContactError(RuntimeError):
    """osascript failed, or Contacts refused the write. Callers surface this —
    unlike an alarm delivery there is no retry loop to swallow it."""


class DuplicateContact(ContactError):
    """An existing card already holds one of these identifiers. Carries the
    matches so the caller can show what it collided with instead of just
    refusing."""

    def __init__(self, matches: list[dict]):
        self.matches = matches
        names = ", ".join(m["name"] or "(unnamed)" for m in matches)
        super().__init__(f"already on a card: {names}")


def _run(script: str, *args: str) -> str:
    return macos.osascript(script, *args, timeout=_TIMEOUT, error=ContactError)


# `repeat while i < n` walks (kind, label, value) triples: the last one starts
# at n-2, and i advances by exactly 3, so it lands on the end rather than past
# it. Python always emits whole triples, so a partial tail can't happen.
_CREATE = """
on run argv
  set theFirst to item 1 of argv
  set theLast to item 2 of argv
  set theOrg to item 3 of argv
  set theNote to item 4 of argv
  tell application "Contacts"
    set p to make new person
    if theFirst is not "" then set first name of p to theFirst
    if theLast is not "" then set last name of p to theLast
    if theOrg is not "" then set organization of p to theOrg
    if theNote is not "" then set note of p to theNote
    set n to count of argv
    set i to 5
    repeat while i < n
      set k to item i of argv
      set lbl to item (i + 1) of argv
      set val to item (i + 2) of argv
      if k is "phone" then
        make new phone at end of phones of p with properties {label:lbl, value:val}
      else
        make new email at end of emails of p with properties {label:lbl, value:val}
      end if
      set i to i + 3
    end repeat
    save
    return id of p
  end tell
end run
"""

# Same triple walk as _CREATE, offset by the id in item 1. Every op takes two
# slots even when it only needs one, so a mis-sized op can't shift everything
# after it — the failure mode being avoided is a phone number landing in a
# label field on a card that then syncs to the phone.
_UPDATE = """
on run argv
  set targetId to item 1 of argv
  tell application "Contacts"
    set matches to (every person whose id is targetId)
    if (count of matches) is 0 then return "NOTFOUND"
    set p to item 1 of matches
    set n to count of argv
    set i to 2
    repeat while i < n
      set op to item i of argv
      set a to item (i + 1) of argv
      set b to item (i + 2) of argv
      if op is "first" then
        set first name of p to a
      else if op is "last" then
        set last name of p to a
      else if op is "org" then
        set organization of p to a
      else if op is "note" then
        set note of p to a
      else if op is "phone" then
        make new phone at end of phones of p with properties {label:a, value:b}
      else if op is "email" then
        make new email at end of emails of p with properties {label:a, value:b}
      else if op is "delphone" then
        delete (every phone of p whose value is a)
      else if op is "delemail" then
        delete (every email of p whose value is a)
      end if
      set i to i + 3
    end repeat
    save
    return "OK"
  end tell
end run
"""

_DELETE = """
on run argv
  set targetId to item 1 of argv
  tell application "Contacts"
    set matches to (every person whose id is targetId)
    if (count of matches) is 0 then return "NOTFOUND"
    delete item 1 of matches
    save
    return "OK"
  end tell
end run
"""

_PROBE = """
on run argv
  tell application "Contacts" to return count of every person
end run
"""


def available() -> bool:
    """Can we talk to Contacts at all? False on a non-Mac, on a headless
    session, or — the usual one — when the Automation permission was never
    granted to whichever binary launched uvicorn."""
    if sys.platform != "darwin":
        return False
    try:
        return _run(_PROBE) != ""
    except ContactError:
        return False


def _pairs(values, default_label: str) -> list[tuple[str, str]]:
    """Accept "555-1234" or ("work", "555-1234") interchangeably — the CLI
    passes bare strings, callers with real label data pass tuples."""
    out = []
    for v in values or ():
        if isinstance(v, (tuple, list)):
            label, value = v
        else:
            label, value = default_label, v
        value = (value or "").strip()
        if value:
            out.append((str(label), value))
    return out


def matches_for(phones=(), emails=(), exclude_id: str | None = None) -> list[dict]:
    """Existing cards sharing any of these identifiers.

    Same normalization and the same per-card record grouping the CRM unifies
    people by, so a "no match" here means the same thing it means there.
    Best-effort by inheritance: an unreadable AddressBook yields nothing,
    which degrades to letting the write through rather than blocking it.

    `exclude_id` drops one card from consideration — update() passes the card
    being edited, since a number already on it would otherwise register as a
    duplicate of itself and block its own re-labelling.
    """
    wanted = {normalize_handle(v) for _, v in
              _pairs(phones, "") + _pairs(emails, "")} - {""}
    if not wanted:
        return []
    out = []
    for uid, name, ids in addressbook_records():
        if exclude_id and uid == exclude_id:
            continue
        shared = ids & wanted
        if shared:
            out.append({"id": uid, "name": name, "shared": sorted(shared)})
    return out


def find(query: str) -> list[dict]:
    """Cards whose name or any identifier contains `query` (case-insensitive).

    Reads the AddressBook files, not Contacts.app — a substring search over
    2000-odd cards is a sqlite scan, and paying an app launch plus an
    Automation prompt for a read the process can already do would be silly.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for uid, name, ids in addressbook_records():
        if q in (name or "").lower() or any(q in i for i in ids):
            out.append({"id": uid, "name": name, "identifiers": sorted(ids)})
    return out


def _card(contact_id: str) -> dict | None:
    """One card's summary by id, or None if no source file holds it."""
    for uid, name, ids in addressbook_records():
        if uid == contact_id:
            return {"id": uid, "name": name, "identifiers": sorted(ids)}
    return None


def _raw_identifiers(contact_id: str) -> list[dict]:
    """The card's phone/email values **as stored** — "(202) 555-1111", not the
    normalized "2025551111".

    Removal needs these: AppleScript matches `every phone whose value is …` on
    the literal string, and it can't normalize. addressbook_records
    deliberately yields only normalized identifiers (its job is identity, not
    presentation), so this asks the raw question directly rather than
    weakening that one.
    """
    out: list[dict] = []
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for kind, table, col in (("phone", "ZABCDPHONENUMBER", "ZFULLNUMBER"),
                                     ("email", "ZABCDEMAILADDRESS", "ZADDRESS")):
                for r in con.execute(
                        f"SELECT t.{col} AS v FROM {table} t"
                        " JOIN ZABCDRECORD r ON r.Z_PK = t.ZOWNER"
                        " WHERE r.ZUNIQUEID = ?", (contact_id,)):
                    if r[0]:
                        out.append({"kind": kind, "value": r[0],
                                    "normalized": normalize_handle(r[0])})
        except sqlite3.Error:
            continue    # schema drift in one source; others may still answer
        finally:
            con.close()
    return out


def locate(contact_id: str) -> str | None:
    """Which AddressBook source the card landed in, as the source directory's
    UUID — the observable answer to the account-routing question AppleScript
    won't answer up front. None means no source file admits to holding it,
    which for a just-created card means Contacts hasn't flushed yet.
    """
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(
                "SELECT 1 FROM ZABCDRECORD WHERE ZUNIQUEID=? LIMIT 1",
                (contact_id,)).fetchone()
            if row:
                return os.path.basename(os.path.dirname(path))
        except sqlite3.Error:
            continue    # schema drift in one source; others may still answer
        finally:
            con.close()
    return None


def create(*, first: str = "", last: str = "", org: str = "", note: str = "",
           phones=(), emails=(), allow_duplicate: bool = False) -> dict:
    """Create a card. Returns {"id", "source", "name", "phones", "emails"}.

    Refuses a card with nothing to call it by, and — unless overridden —
    refuses one whose phone or email already sits on an existing card, since
    the CRM would then show the same human twice with half a history each.
    """
    first, last, org = first.strip(), last.strip(), org.strip()
    if not (first or last or org):
        raise ValueError("a contact needs at least a first name, "
                         "last name, or organization")
    ph = _pairs(phones, _DEFAULT_PHONE_LABEL)
    em = _pairs(emails, _DEFAULT_EMAIL_LABEL)
    for _, value in em:
        if "@" not in value:
            raise ValueError(f"not an email address: {value!r}")
    for _, value in ph:
        if not normalize_handle(value):
            raise ValueError(f"no digits in phone number: {value!r}")

    if not allow_duplicate:
        found = matches_for(ph, em)
        if found:
            raise DuplicateContact(found)

    triples: list[str] = []
    for label, value in ph:
        triples += ["phone", label, value]
    for label, value in em:
        triples += ["email", label, value]

    cid = _run(_CREATE, first, last, org, note, *triples)
    if not cid:
        raise ContactError("Contacts returned no id for the new card")
    return {
        "id": cid,
        "source": locate(cid),
        "name": " ".join(p for p in (first, last) if p) or org,
        "phones": [v for _, v in ph],
        "emails": [v for _, v in em],
    }


def update(contact_id: str, *, first: str | None = None,
           last: str | None = None, org: str | None = None,
           note: str | None = None, add_phones=(), add_emails=(),
           remove=(), allow_duplicate: bool = False,
           dry_run: bool = False) -> dict:
    """Edit an existing card. Returns the before/after summary and what changed.

    `None` means leave the field alone; `""` means clear it. The distinction is
    the whole point — without it, every unspecified field gets blanked on any
    edit, and the damage syncs to the phone before you notice.

    `remove` takes identifier values in any format; they're matched against the
    card's stored values by the same normalization the CRM uses, so removing
    "2025551111" finds the phone stored as "+1 (202) 555-1111". An identifier
    that isn't on the card raises rather than silently doing nothing — a typo'd
    removal that reports success is worse than one that fails.
    """
    before = _card(contact_id)
    if before is None:
        raise ContactError(f"no card with id {contact_id!r}")

    ph = _pairs(add_phones, _DEFAULT_PHONE_LABEL)
    em = _pairs(add_emails, _DEFAULT_EMAIL_LABEL)
    for _, value in em:
        if "@" not in value:
            raise ValueError(f"not an email address: {value!r}")
    for _, value in ph:
        if not normalize_handle(value):
            raise ValueError(f"no digits in phone number: {value!r}")

    if (ph or em) and not allow_duplicate:
        found = matches_for(ph, em, exclude_id=contact_id)
        if found:
            raise DuplicateContact(found)

    ops: list[str] = []
    changed: list[str] = []
    for field, value in (("first", first), ("last", last),
                         ("org", org), ("note", note)):
        if value is not None:
            ops += [field, value, ""]
            changed.append(f"{field}={value!r}")

    on_card = _raw_identifiers(contact_id)
    for raw in remove or ():
        target = normalize_handle(raw)
        hits = [i for i in on_card if target and i["normalized"] == target]
        if not hits:
            raise ValueError(f"{raw!r} is not on this card")
        for hit in hits:
            ops += [f"del{hit['kind']}", hit["value"], ""]
            changed.append(f"-{hit['kind']} {hit['value']}")

    for label, value in ph:
        ops += ["phone", label, value]
        changed.append(f"+phone {value}")
    for label, value in em:
        ops += ["email", label, value]
        changed.append(f"+email {value}")

    if not ops:
        return {"id": contact_id, "before": before, "after": before,
                "changed": [], "applied": False}
    if dry_run:
        return {"id": contact_id, "before": before, "after": None,
                "changed": changed, "applied": False}

    if _run(_UPDATE, contact_id, *ops) == "NOTFOUND":
        # The AddressBook read found it, Contacts didn't — the card was deleted
        # between the two, or this source isn't in the running Contacts session.
        raise ContactError(f"Contacts has no card with id {contact_id!r}")
    return {"id": contact_id, "before": before, "after": _card(contact_id),
            "changed": changed, "applied": True}


def delete(contact_id: str) -> bool:
    """Delete a card. True once it's gone, including when it already was —
    a card the user deleted by hand has reached the same end state."""
    return _run(_DELETE, contact_id) in ("OK", "NOTFOUND")


def _main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(
        prog="python -m app.contacts",
        description="Create/search Contacts cards (syncs to the phone via "
                    "whichever account Contacts.app defaults to).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a new card")
    c.add_argument("--first", default="")
    c.add_argument("--last", default="")
    c.add_argument("--org", default="")
    c.add_argument("--note", default="")
    c.add_argument("--phone", action="append", default=[],
                   help="repeatable; 'mobile' label unless given as label=value")
    c.add_argument("--email", action="append", default=[],
                   help="repeatable; 'home' label unless given as label=value")
    c.add_argument("--allow-duplicate", action="store_true",
                   help="create even if an existing card shares an identifier")

    u = sub.add_parser("update", help="edit an existing card (prints the "
                                      "planned change unless --apply)")
    u.add_argument("contact_id", help="the id from `find`")
    u.add_argument("--first", default=None,
                   help="pass an empty string to clear the field")
    u.add_argument("--last", default=None)
    u.add_argument("--org", default=None)
    u.add_argument("--note", default=None)
    u.add_argument("--add-phone", action="append", default=[])
    u.add_argument("--add-email", action="append", default=[])
    u.add_argument("--remove", action="append", default=[],
                   help="repeatable; an identifier currently on the card, in "
                        "any format")
    u.add_argument("--allow-duplicate", action="store_true")
    u.add_argument("--apply", action="store_true",
                   help="actually write it. Without this the change is only "
                        "described — an edit overwrites real data with no undo")

    f = sub.add_parser("find", help="search existing cards")
    f.add_argument("query")

    d = sub.add_parser("delete", help="delete a card by id")
    d.add_argument("contact_id")

    sub.add_parser("check", help="report whether Contacts is reachable")

    args = p.parse_args()

    def _labelled(raw: str, default: str) -> tuple[str, str]:
        label, sep, value = raw.partition("=")
        return (label, value) if sep else (default, raw)

    try:
        if args.cmd == "check":
            ok = available()
            print(json.dumps({"available": ok,
                              "sources": len(glob.glob(ADDRESSBOOK_GLOB))}))
            sys.exit(0 if ok else 1)
        if args.cmd == "find":
            print(json.dumps(find(args.query), indent=2))
            return
        if args.cmd == "delete":
            print(json.dumps({"deleted": delete(args.contact_id)}))
            return
        if args.cmd == "update":
            out = update(
                args.contact_id, first=args.first, last=args.last,
                org=args.org, note=args.note,
                add_phones=[_labelled(x, _DEFAULT_PHONE_LABEL)
                            for x in args.add_phone],
                add_emails=[_labelled(x, _DEFAULT_EMAIL_LABEL)
                            for x in args.add_email],
                remove=args.remove, allow_duplicate=args.allow_duplicate,
                dry_run=not args.apply)
            print(json.dumps(out, indent=2))
            if not args.apply and out["changed"]:
                print("\n(nothing written — re-run with --apply)",
                      file=sys.stderr)
            return
        out = create(
            first=args.first, last=args.last, org=args.org, note=args.note,
            phones=[_labelled(x, _DEFAULT_PHONE_LABEL) for x in args.phone],
            emails=[_labelled(x, _DEFAULT_EMAIL_LABEL) for x in args.email],
            allow_duplicate=args.allow_duplicate)
        print(json.dumps(out, indent=2))
    except DuplicateContact as e:
        sys.exit(f"error: {e}\n(pass --allow-duplicate to create anyway)")
    except (ContactError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    _main()
