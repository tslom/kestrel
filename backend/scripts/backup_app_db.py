"""Take an app.db snapshot by hand.

Usage (from backend/):
    .venv/bin/python scripts/backup_app_db.py          # only if this month has none
    .venv/bin/python scripts/backup_app_db.py --force  # always

The poller takes one a month on its own; run this with --force before anything
alarming. Same code as the poller: app/backup.py.
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
