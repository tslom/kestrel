"""Alarm delivery via Reminders — the surface that reaches a shut laptop.

Reminders is the target for one reason: iCloud syncs it to the phone, so an
alarm written here fires on a device the user actually has, hours after this
machine went to sleep. Everything else about it is worse than a notification —
no urgency control, no actions — and none of that matters next to being able to
fire at all.

Kept behind write()/withdraw() so the backend is swappable. A dedicated calendar
was the other candidate and is easier to inspect and wipe; Reminders won on
alert semantics (a real alarm with a snooze, not an event you have to notice).

Two AppleScript traps are handled here rather than discovered later:

  * **Arguments, never interpolation.** Titles carry merchant names and flight
    numbers — user-shaped text with quotes in it. Values go through `on run
    argv` and subprocess's argument list, so nothing is ever parsed as script.
  * **Set `day` to 1 before setting `month`.** An AppleScript date mutates in
    place, so writing month=2 while day=31 rolls into March. The classic
    version of this bug only appears on 3 days of the month.

A date built from components lands in the *machine's* zone, which is correct
and not an accident: `fire_at` is an instant, Python converts it to local wall
clock here, and Reminders stores an absolute date. Move to Pacific and the
already-written alarm still fires at the same moment.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

from . import macos
from .config import ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST

_TIMEOUT = 30       # Reminders is slow on first launch; well under a poll cycle

# Reminders launched by osascript has no window, and macOS terminates it a
# few seconds after the script ends. A script that connects while it is
# shutting down gets -609 ("Connection is invalid") or -600 ("isn't
# running") — 273 failed deliveries in the log, 75 of 88 update cycles,
# reproduced 1 in 4 from a shell. The retry relaunches it.
_RELAUNCH_CODES = ("(-609)", "(-600)")
_RETRIES = 2
_RETRY_WAIT = 1.5   # seconds: lets the dying instance finish exiting
_RESTART_WAIT = 2.0  # seconds, after the kill and after the relaunch


class ReminderError(RuntimeError):
    """AppleScript failed. Always caught by the caller — a delivery failure
    leaves the row pending and retries next cycle, it never sinks a reconcile."""


def _run(script: str, *args: str) -> str:
    # A timeout or a missing osascript is a ReminderError too — the reconcile
    # loop's contract is one exception type per failed delivery.
    #
    # Retrying is safe for every script here: _DELETE answers NOTFOUND for
    # what an earlier attempt already removed, and _CREATE adopts an
    # identical open reminder rather than making a second — which also covers
    # a -609 on `id of r`, after the reminder was made but before its id came
    # back (that one used to leave an orphan on the phone every cycle).
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
    # Every retry hit -609: not the auto-quit race but a wedged instance.
    # One can answer -609 to every attempt for several cycles running, and
    # then an alarm never reaches the phone. Waiting doesn't clear that;
    # killing it does (its data is in iCloud, nothing is lost).
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
    """Can we talk to Reminders at all? False for a machine that isn't a Mac,
    an Automation permission that was never granted, or a headless session."""
    try:
        return bool(_run(_PROBE))
    except ReminderError:
        return False


def write(*, title: str, body: str, fire_at: str) -> str:
    """Create the alarm; returns the Reminders id to store as external_id.

    `fire_at` is offset-aware. It is converted to this machine's local wall
    clock because that is the only frame AppleScript date components speak —
    the instant is preserved, which is what the alarm actually promises.
    """
    local = datetime.fromisoformat(fire_at).astimezone()
    rid = _run(_CREATE, ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST,
               title, body or "", str(local.year), str(local.month),
               str(local.day), str(local.hour), str(local.minute))
    if not rid:
        raise ReminderError("Reminders returned no id for the new alarm")
    return rid


def withdraw(external_id: str) -> bool:
    """Delete the alarm. True once it is gone — including when it was already
    gone, since a user who deleted it by hand has made the same decision and a
    permanent failure here would block the row from ever being cleaned up."""
    return _run(_DELETE, ALARM_REMINDERS_ACCOUNT, ALARM_REMINDERS_LIST,
                external_id) in ("OK", "NOTFOUND")
