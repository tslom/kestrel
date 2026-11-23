"""Rerunnable importer: macOS AddressBook birthdays -> people (archive/ROUND34_PLAN.md
§17). ZBIRTHDAY is a Core Data timestamp (seconds since 2001-01-01 — the same
APPLE_EPOCH offset chat.db uses for message.date, minus its nanosecond scale)
representing noon UTC on the birthday. Apple stores a sentinel year (commonly
1604) when the contact card has no real year, so a year is only kept when
it's plausible (>= 1900); otherwise birthday is stored as bare MM-DD.

Usage:
    python scripts/import_birthdays.py

Idempotent: upserts by person_key (name/birthday refresh on rerun); never
touches watch_cadence_days/notes once a person row exists. A contact with no
phone or email on file is skipped — there'd be nothing to key it to in
emails/messages history.
"""

import glob
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.config import ADDRESSBOOK_GLOB
from app.providers.messages import normalize_handle

APPLE_EPOCH = 978307200


def _ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _birthday_str(zbirthday: float | None) -> str | None:
    if zbirthday is None:
        return None
    dt = datetime.fromtimestamp(zbirthday + APPLE_EPOCH, tz=timezone.utc)
    if dt.year >= 1900:
        return dt.date().isoformat()
    return f"{dt.month:02d}-{dt.day:02d}"


def _people_from_source(path: str) -> list[dict]:
    con = _ro(path)
    try:
        records = {r["Z_PK"]: r for r in con.execute(
            "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZBIRTHDAY"
            " FROM ZABCDRECORD WHERE ZBIRTHDAY IS NOT NULL")}
        if not records:
            return []
        phones: dict[int, str] = {}
        for r in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
            phones.setdefault(r["ZOWNER"], r["ZFULLNUMBER"])
        emails: dict[int, str] = {}
        for r in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
            emails.setdefault(r["ZOWNER"], r["ZADDRESS"])
        out = []
        for pk, rec in records.items():
            raw = emails.get(pk) or phones.get(pk)
            if not raw:
                continue  # no way to key this person to email/messages history
            key = normalize_handle(raw)
            if not key:
                continue
            birthday = _birthday_str(rec["ZBIRTHDAY"])
            if not birthday:
                continue
            name = " ".join(p for p in (rec["ZFIRSTNAME"], rec["ZLASTNAME"]) if p) \
                or rec["ZORGANIZATION"]
            out.append({"person_key": key, "name": name, "birthday": birthday})
        return out
    finally:
        con.close()


def main() -> None:
    now = datetime.now().isoformat(timespec="seconds")
    imported = 0
    con = db.connect()
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            people = _people_from_source(path)
        except Exception as e:
            print(f"skip {path}: {e}")
            continue
        with con:
            for p in people:
                con.execute(
                    "INSERT INTO people (person_key, name, birthday, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(person_key) DO UPDATE SET"
                    "  name=excluded.name, birthday=excluded.birthday,"
                    "  updated_at=excluded.updated_at",
                    (p["person_key"], p["name"], p["birthday"], now, now))
                imported += 1
    con.close()
    print(f"imported/updated {imported} birthdays")


if __name__ == "__main__":
    main()
