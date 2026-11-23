"""Monthly app.db snapshot.

app.db is the source of truth for tasks, habits, checkins, debts, lists and
the OAuth tokens — everything the vault's markdown deliberately does *not*
hold. It had a backup script from day one and no schedule, which is a
distinction only visible from outside: the newest full snapshot was once 59
days and 97MB of growth old, and nothing anywhere said so. A script nobody runs is not a backup.

**Lazy, not cron.** The scheduler this replaces would have fired at 4:30am
into a closed lid (the same reason the daily brief generates on first open and
not on a timer). Instead the poller asks once a cycle whether this month has a
snapshot yet, and takes one if it doesn't — so the backup happens shortly
after you next open the dashboard in a new month, which is the earliest moment
the machine is demonstrably awake. The check is a glob, so asking costs
nothing and can happen every cycle.

**The month is `db.day_key()`'s, and so is the filename.** Both halves matter:
the date in the name is the day key rather than the wall clock, so that a
backup taken at 1am on the 1st — when day_key() still says last month — files
itself under the month it belongs to. Naming it from `datetime.now()` instead
would file it under the new month while the due-check was still asking about
the old one, and the loop would take a fresh copy every cycle until 4am.

Snapshots contain OAuth tokens — same rule as app.db itself: this machine
only, never synced anywhere.
"""

import sqlite3
from datetime import datetime

from . import db

# Snapshots kept, oldest pruned past this. One a month, so roughly a year of
# history — at ~115MB a copy that is ~1.4GB, which is the trade this cadence
# was chosen for. It was 14 when the job was meant to run nightly and the
# database was 18MB; a retention count means nothing without its cadence.
KEEP_SNAPSHOTS = 12


def backups_dir():
    """Beside the database, so a test that redirects `db.APP_DB` gets its
    backups redirected with it rather than writing into the real data dir."""
    return db.APP_DB.parent / "backups"


def month_key(day: str | None = None) -> str:
    """"2026-09" — the month a snapshot is filed under."""
    return (day or db.day_key())[:7]


def existing(month: str | None = None):
    """The snapshot for `month`, if there is one. Newest wins if a forced run
    made a second."""
    month = month or month_key()
    hits = sorted(backups_dir().glob(f"app-{month.replace('-', '')}*.db"))
    return hits[-1] if hits else None


def copy_to(dest, src_path=None) -> None:
    """A consistent copy of app.db (or `src_path`) at `dest`, through SQLite's
    backup API so a live WAL database copies whole. Also the merge guard's
    snapshot (merge_guard.py).

    Written aside and renamed on success. A copy that dies halfway (disk
    full, laptop shut) would otherwise leave a truncated file whose *name*
    says it's a backup — the one failure that turns a backup into the
    absence of one."""
    part = dest.parent / (dest.name + ".part")
    src = sqlite3.connect(src_path or db.APP_DB)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        dst = sqlite3.connect(part)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    part.rename(dest)


def run(force: bool = False) -> dict:
    """Take this month's snapshot unless it already exists.

    Raises on a failed copy — the poller wrapper is what decides that a
    failure costs the cycle nothing, and swallowing it here would leave the
    CLI reporting success over an unwritten file.
    """
    month = month_key()
    if not force and (have := existing(month)) is not None:
        return {"skipped": "already backed up", "month": month,
                "file": have.name}

    backups = backups_dir()
    backups.mkdir(parents=True, exist_ok=True)
    stem = f"app-{db.day_key().replace('-', '')}-{datetime.now():%H%M%S}"
    # Seconds are the whole timestamp, so a forced snapshot taken in the same
    # second as an automatic one would land on its name and replace it — a
    # request for a second copy answered by destroying the first.
    dest = backups / f"{stem}.db"
    n = 1
    while dest.exists():
        dest = backups / f"{stem}-{n}.db"
        n += 1

    copy_to(dest)

    old = sorted(backups.glob("app-*.db"))[:-KEEP_SNAPSHOTS]
    for f in old:
        f.unlink()
    return {"month": month, "file": dest.name, "bytes": dest.stat().st_size,
            "pruned": len(old), "keeping": KEEP_SNAPSHOTS}
