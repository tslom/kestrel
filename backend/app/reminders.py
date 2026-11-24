"""Alarm delivery via Reminders, the surface iCloud syncs to the phone (write()/withdraw() keep it swappable).

Traps: values go through `on run argv`, never script interpolation; set `day` to 1 before
`month` (AppleScript dates mutate in place); `fire_at` is an instant, converted to local wall clock.
Design: DESIGN.md → Reaching the user (§21).
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

from . import macos
from .config import ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST

_TIMEOUT = 30       # Reminders is slow on first launch; well under a poll cycle

# Windowless Reminders auto-quits after a script; connecting mid-quit gives -609/-600 (273 failed
# deliveries in the log, reproduced 1 in 4). The retry relaunches it.
_RELAUNCH_CODES = ("(-609)", "(-600)")
_RETRIES = 2
_RETRY_WAIT = 1.5   # seconds: lets the dying instance finish exiting
_RESTART_WAIT = 2.0  # seconds, after the kill and after the relaunch


class ReminderError(RuntimeError):
    """AppleScript failed; callers catch it (the row stays pending, retried next cycle)."""


def _run(script: str, *args: str) -> str:
    # Retrying is safe: _DELETE answers NOTFOUND for what an earlier attempt removed, and
    # _CREATE adopts an identical open reminder (else a -609 on `id of r` orphans one).
    for attempt in range(_RETRIES + 1):
        try:
            return macos.osascript(script, *args, timeout=_TIMEOUT,
                                   error=ReminderError)
        except ReminderError as e:
            if not any(c in str(e) for c in _RELAUNCH_CODES):
                raise
            if attempt == _RETRIES:
                last = e
                break
            time.sleep(_RETRY_WAIT)
    # Every retry hit -609: a wedged instance; waiting won't clear it, killing does (data is in iCloud).
    _restart_reminders()
    try:
        return macos.osascript(script, *args, timeout=_TIMEOUT, error=ReminderError)
    except ReminderError:
        raise last from None


def _restart_reminders() -> None:
    subprocess.run(["pkill", "-x", "Reminders"], capture_output=True, timeout=10)
    time.sleep(_RESTART_WAIT)
    subprocess.run(["open", "-gj", "-a", "Reminders"], capture_output=True, timeout=10)
    time.sleep(_RESTART_WAIT)


_CREATE = """
on run argv
  set acct to item 1 of argv
  set listName to item 2 of argv
  set theTitle to item 3 of argv
  set theBody to item 4 of argv
  set y to (item 5 of argv) as integer
  set mo to (item 6 of argv) as integer
  set dy to (item 7 of argv) as integer
  set hh to (item 8 of argv) as integer
  set mi to (item 9 of argv) as integer
  tell application "Reminders"
    if not (exists list listName of account acct) then
      make new list at account acct with properties {name:listName}
    end if
    set d to current date
    set day of d to 1
    set year of d to y
    set month of d to mo
    set day of d to dy
    set hours of d to hh
    set minutes of d to mi
    set seconds of d to 0
    set same to (every reminder of list listName of account acct ¬
      whose name is theTitle and remind me date is d and completed is false)
    if (count of same) > 0 then return id of item 1 of same
    set r to make new reminder at end of list listName of account acct ¬
      with properties {name:theTitle, body:theBody, remind me date:d}
    return id of r
  end tell
end run
"""

_DELETE = """
on run argv
  set acct to item 1 of argv
  set listName to item 2 of argv
  set targetId to item 3 of argv
  tell application "Reminders"
    if not (exists list listName of account acct) then return "NOTFOUND"
    set matches to (every reminder of list listName of account acct ¬
      whose id is targetId)
    if (count of matches) is 0 then return "NOTFOUND"
    delete item 1 of matches
    return "OK"
  end tell
end run
"""

_PROBE = """
on run argv
  tell application "Reminders" to return name of every account
end run
"""


def available() -> bool:
    """Can we talk to Reminders at all (not a Mac, Automation not granted, or headless)?"""
    try:
        return bool(_run(_PROBE))
    except ReminderError:
        return False


def write(*, title: str, body: str, fire_at: str) -> str:
    """Create the alarm; returns the Reminders id (the `external_id`). `fire_at` is offset-aware."""
    local = datetime.fromisoformat(fire_at).astimezone()
    rid = _run(_CREATE, ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST,
               title, body or "", str(local.year), str(local.month),
               str(local.day), str(local.hour), str(local.minute))
    if not rid:
        raise ReminderError("Reminders returned no id for the new alarm")
    return rid


def withdraw(external_id: str) -> bool:
    """Delete the alarm. True once it is gone, including already gone (hand-deleted must not block cleanup)."""
    return _run(_DELETE, ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST,
                external_id) in ("OK", "NOTFOUND")
