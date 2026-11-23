"""Take an app.db snapshot by hand.

Usage (from backend/):
    .venv/bin/python scripts/backup_app_db.py          # only if this month has none
    .venv/bin/python scripts/backup_app_db.py --force  # always

Nothing needs to be scheduled: the poller takes one a month on its own (see
app/backup.py for why lazy beat cron here). This is the "I'm about to do
something alarming to the database" path — run it with --force first.

The logic lives in app/backup.py so that the thing the poller runs every five
minutes and the thing you run before a migration are the same code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import backup, db


def main() -> None:
    force = "--force" in sys.argv[1:]
    info = backup.run(force=force)
    if "skipped" in info:
        print(f"{info['month']} already backed up ({info['file']}) —"
              f" pass --force to take another")
        return
    print(f"backed up {db.APP_DB} -> {info['file']} ({info['bytes']} bytes);"
          f" pruned {info['pruned']}, keeping last {info['keeping']}")


if __name__ == "__main__":
    main()
