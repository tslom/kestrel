"""The two ways the backend reaches into macOS: osascript and `open`.

Arguments, never interpolation (osascript values after `--` via `on run argv`;
`open` gets the URL as argv). Always a timeout; every failure comes back as one
exception type the caller chose. Probes (notify._probe, wake._run) stay separate:
they swallow failure by design. Design: DESIGN.md → `app/macos.py`.
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
