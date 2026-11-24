"""Monthly app.db snapshot, taken lazily by the poller (not cron).

The month and the filename date are both `db.day_key()`'s; a wall-clock name would
make the due-check re-fire every cycle until 4am. Snapshots hold OAuth tokens:
this machine only. Design: DESIGN.md → Backups.
"""

import sqlite3
from datetime import datetime

from . import db

# One a month: about a year, ~1.4GB at ~115MB a copy. Oldest pruned past this.
KEEP_SNAPSHOTS = 12


def backups_dir():
    """Beside `db.APP_DB`, so a test that redirects the db redirects this too."""
    return db.APP_DB.parent / "backups"


def month_key(day: str | None = None) -> str:
    """"2026-09" — the month a snapshot is filed under."""
    return (day or db.day_key())[:7]


def existing(month: str | None = None):
    """The snapshot for `month` (newest if a forced run made two), or None."""
    month = month or month_key()
    hits = sorted(backups_dir().glob(f"app-{month.replace('-', '')}*.db"))
    return hits[-1] if hits else None


def copy_to(dest, src_path=None) -> None:
    """A consistent copy of app.db (or `src_path`) at `dest` via SQLite's backup
    API (also the merge guard's snapshot). Written to `.part` and renamed, so a
    copy that dies halfway never leaves a truncated file named like a backup."""
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
    """Take this month's snapshot unless it exists. Raises on a failed copy (the
    poller wrapper absorbs it; swallowing here would make the CLI report success)."""
    month = month_key()
    if not force and (have := existing(month)) is not None:
        return {"skipped": "already backed up", "month": month,
                "file": have.name}

    backups = backups_dir()
    backups.mkdir(parents=True, exist_ok=True)
    stem = f"app-{db.day_key().replace('-', '')}-{datetime.now():%H%M%S}"
    # Second-resolution name: suffix on collision so a forced copy never replaces one.
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
