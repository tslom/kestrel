"""The two ways the backend reaches past itself into macOS: osascript and `open`.

Every caller used to carry its own copy of the subprocess call, and the copies
had drifted — reminders.py let a TimeoutExpired escape as itself instead of its
own error type, and the Obsidian hand-off ran `open` with no timeout at all.
One wrapper each, so the rules are written once:

  * **Arguments, never interpolation.** osascript gets the script as `-e` and
    user-shaped values after `--`, read back through `on run argv`; `open`
    gets the URL as an argv element. Nothing a user typed is ever parsed as
    script or shell.
  * **Always a timeout**, and every way the process can fail — missing binary,
    timeout, non-zero exit — comes back as one exception type the caller
    chose, so callers catch what they already caught.

Probes that answer "is this machine able to…" (notify._probe, wake._run) stay
where they are: they swallow failure by design and share nothing but the call.
"""

from __future__ import annotations

import subprocess


def osascript(script: str, *args: str, timeout: float,
              error: type[Exception] = RuntimeError) -> str:
    """Run `script` with `args` as its argv; return stripped stdout.

    Raises `error` if osascript couldn't run, timed out, or exited non-zero
    (with AppleScript's own stderr as the message in the last case)."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, "--", *args],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise error(f"could not run osascript: {e}") from e
    if proc.returncode != 0:
        raise error((proc.stderr or "osascript failed").strip())
    return (proc.stdout or "").strip()


def reveal(path: str, timeout: float = 10) -> None:
    """Select `path` in a Finder window (`open -R`). Raises like open_url."""
    _open(["open", "-R", path], timeout)


def open_url(url: str, background: bool = False, timeout: float = 10) -> None:
    """Hand `url` to LaunchServices via `open` (`-g` keeps focus where it is).

    Raises RuntimeError with the reason when `open` refuses or can't run."""
    _open(["open", *(["-g"] if background else []), url], timeout)


def _open(argv: list[str], timeout: float) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True,
                       timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError((e.stderr or "").strip()
                           or f"`open` refused the URL (exit {e.returncode})") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"could not run `open`: {e}") from e
