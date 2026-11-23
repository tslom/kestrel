"""The line between Claude and every other model.

An outside agent (anything that isn't Claude) gets three things and nothing
else: a brief that has been through `scrub`, an environment built from an
allowlist, and a seatbelt sandbox that can't read the home directory, the
vault or the app's data. `launch` is the one function that starts one, and
it takes a `Brief`, which only `scrub` makes.

This module imports the standard library and `config` and nothing else of
the app, so no code path here can reach personal data to leak it
(tests/test_delegate_boundary.py checks the imports, that nothing else in
the package starts a process, and the sandbox itself against real files).

The sandbox is ours rather than each CLI's own because theirs answer a
different question: Codex's read-only mode still reads the whole disk. And
seatbelt doesn't nest, so the CLIs run with their own sandbox off inside it.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import DATA_DIR, VAULT_PATH

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BRIEF_MAX_CHARS = 60_000      # it rides argv or stdin whole
OUTPUT_MAX_BYTES = 4_000_000

# What a child process inherits. The backend's own environment holds Plaid,
# Google and Spotify secrets (.env); none of it crosses.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM")

_KEY = object()


class BoundaryError(RuntimeError):
    """The run was refused, or couldn't be contained. Written to be shown."""


@dataclass(frozen=True)
class Brief:
    """A brief that went through `scrub`. `redactions`: what was taken out,
    by kind, so the caller can tell Claude what the agent didn't see."""
    text: str
    redactions: tuple[str, ...]
    _key: object = None

    def __post_init__(self):
        if self._key is not _KEY:
            raise BoundaryError("a Brief is made by boundary.scrub, not by hand")


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?![\w])")
_DIGITS = re.compile(r"(?<![\w.#])\d{9,}(?!\w)")       # account and card numbers
_TILDE = re.compile(r"(?<![\w/])~/[^\s\"'`)>\]]+")


def _path_re(root: Path) -> re.Pattern:
    return re.compile(re.escape(str(root)) + r"[^\s\"'`)>\]]*")


def scrub(text: str, names: tuple[str, ...] | list[str] = ()) -> Brief:
    """The brief an outside agent may read: email addresses, phone numbers,
    long digit runs, paths into the home directory or the vault, and the
    given names (the people in the user's contacts) replaced by a
    placeholder. Whoever integrates the result puts the real value back.

    This catches identifiers, not meaning: a brief that describes someone's
    week in plain words passes. The sandbox is what holds if it does."""
    text = str(text or "").strip()
    if not text:
        raise BoundaryError("the brief is empty")
    if len(text) > BRIEF_MAX_CHARS:
        raise BoundaryError(f"the brief is {len(text):,} characters; the most is {BRIEF_MAX_CHARS:,}. "
                            "A brief is a task, not a document")
    found: list[str] = []

    def sub(pattern: re.Pattern, label: str, s: str) -> str:
        s, n = pattern.subn(f"[{label}]", s)
        if n:
            found.append(f"{n} {label}{'s' if n > 1 and not label.endswith('s') else ''}")
        return s

    text = sub(_EMAIL, "email", text)
    for root in sorted({VAULT_PATH, Path.home()}, key=lambda p: -len(str(p))):
        text = sub(_path_re(root), "path", text)
    text = sub(_TILDE, "path", text)
    text = sub(_PHONE, "phone", text)
    text = sub(_DIGITS, "number", text)
    full = sorted({n.strip() for n in names if len(n.split()) >= 2}, key=len, reverse=True)
    if full:
        people = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in full) + r")(?!\w)", re.I)
        text = sub(people, "person", text)
    return Brief(text, tuple(found), _KEY)


def clean_env(keys: tuple[str, ...] = (), scratch: Path | None = None) -> dict[str, str]:
    """ENV_KEEP plus the provider's own variables (its API key), and a TMPDIR
    inside the scratch directory, since the usual one is out of reach."""
    env = {k: os.environ[k] for k in (*ENV_KEEP, *keys) if os.environ.get(k)}
    if scratch is not None:
        tmp = scratch / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)
    env["NO_COLOR"] = "1"
    return env


def _real(p: Path | str) -> str:
    return os.path.realpath(str(p))


def _quote(p: str) -> str:
    return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _subpaths(paths) -> str:
    return " ".join(f"(subpath {_quote(_real(p))})" for p in paths)


def tool_dirs(binary: str | None) -> list[Path]:
    """Where a CLI installed under the home directory keeps its code (an nvm
    node, an npm prefix in ~): readable, since the home directory otherwise
    isn't. A Homebrew install lives outside it and needs nothing."""
    home, out = Path(_real(Path.home())), []
    for name in filter(None, (binary, "node")):
        found = shutil.which(name)
        if not found:
            continue
        real = Path(_real(found))
        if home not in real.parents:
            continue
        parts = real.parts
        if "node_modules" in parts:
            at = parts.index("node_modules")
            out.append(Path(*parts[:at + 2]))       # the package itself
        else:
            out.append(real.parent.parent if real.parent.name == "bin" else real.parent)
        out.append(Path(found).parent)
    return [p for p in out if p != home and home in p.parents]


def profile(scratch: Path, config_dirs: tuple[Path, ...] = (), read_dirs: tuple[Path, ...] = ()) -> str:
    """The seatbelt profile. Later rules win, so it reads top to bottom as:
    everything; then not the home directory, other volumes or the temp
    directories (other apps' caches, this session's transcripts); then the
    CLI's own config and code; then, whatever those said, never the vault or
    the app's data; then the scratch directory, which lives inside the data
    directory and is the one place it writes."""
    home = Path.home()
    walled = [home, "/Volumes", "/private/tmp", "/private/var/folders", "/tmp", "/var/folders"]
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* file-write* {_subpaths(walled)})",
        # Resolving a path under a walled directory needs to stat its parents.
        "(allow file-read-metadata " + " ".join(
            f"(literal {_quote(_real(p))})" for p in (*walled, *Path(_real(scratch)).parents)) + ")",
    ]
    if read_dirs:
        lines.append(f"(allow file-read* {_subpaths(read_dirs)})")
    if config_dirs:
        lines.append(f"(allow file-read* file-write* {_subpaths(config_dirs)})")
    lines.append(f"(deny file-read* file-write* {_subpaths([VAULT_PATH, DATA_DIR])})")
    lines.append(f"(allow file-read* file-write* {_subpaths([scratch])})")
    return "\n".join(lines)


@dataclass
class Finished:
    code: int | None          # None: killed at the time limit
    stdout: str
    stderr: str


async def launch(argv_for: Callable[[str], list[str]], brief: Brief, scratch: Path, *,
                 env_keys: tuple[str, ...] = (), config_dirs: tuple[Path, ...] = (),
                 stdin_brief: bool = False, timeout: float = 600) -> Finished:
    """Run an outside CLI on `brief` in `scratch`, sandboxed. `argv_for` gets
    the scrubbed text and returns the command line; with `stdin_brief` the
    text goes in on stdin too."""
    if not isinstance(brief, Brief):
        raise BoundaryError("launch takes a scrubbed Brief (boundary.scrub), never raw text")
    if not os.access(SANDBOX_EXEC, os.X_OK):
        raise BoundaryError("no sandbox-exec on this machine, so an outside agent can't be "
                            "walled off from the vault; it won't run without that")
    scratch.mkdir(parents=True, exist_ok=True)
    argv = argv_for(brief.text)
    binary = shutil.which(argv[0])
    if not binary:
        raise BoundaryError(f"`{argv[0]}` isn't installed")
    rules = profile(scratch, tuple(config_dirs), tuple(tool_dirs(argv[0])))
    proc = await asyncio.create_subprocess_exec(
        SANDBOX_EXEC, "-p", rules, binary, *argv[1:],
        cwd=str(scratch), env=clean_env(env_keys, scratch),
        stdin=asyncio.subprocess.PIPE if stdin_brief else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(brief.text.encode() if stdin_brief else None), timeout)
        code: int | None = proc.returncode
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = await proc.communicate()
        code = None
    except asyncio.CancelledError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    return Finished(code, out[:OUTPUT_MAX_BYTES].decode("utf-8", "replace"),
                    err[-20_000:].decode("utf-8", "replace"))
