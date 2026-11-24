"""The line between Claude and every other model.

An outside agent gets a brief that passed `scrub` (and `gate`), an allowlisted env, and a seatbelt
sandbox with no home, vault or app data. `launch` is the only function that starts one. Imports
only stdlib and `config`: no path here can reach personal data (tests/test_delegate_boundary.py).
Seatbelt doesn't nest, so the CLIs run with their own sandbox off. Design: DESIGN.md → Other models.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
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
    """A brief that went through `scrub`. `redactions`: what was taken out, by kind.
    `purpose`: set when it also passed `gate`, which `launch` requires."""
    text: str
    redactions: tuple[str, ...]
    _key: object = None
    purpose: str | None = None

    def __post_init__(self):
        if self._key is not _KEY:
            raise BoundaryError("a Brief is made by boundary.scrub, not by hand")


# ---------- the gate: what kind of work may leave at all ----------
#
# Scrub removes identifiers; the gate decides whether work leaves at all: grunt work and
# images only, never coursework or personal data. A project's own ideas can't be read off
# the text, so `purpose` is an enum and anything outside it stays with Claude.

PURPOSES = {
    "boilerplate": "boilerplate and scaffolding",
    "refactor": "a mechanical refactor",
    "tests": "test scaffolding",
    "format": "reformatting or converting",
    "public-research": "a summary of public material",
    "variants": "throwaway variants",
    "image": "an image",
}
# Whose work it is, matched against an account's "allowed for"; commercial = a business's or client's.
WORK_FOR = ("personal", "learning", "commercial")


@dataclass(frozen=True)
class Origin:
    """The console tab's profile (general | study | dev) and, for dev, the repo folder."""
    profile: str | None = None
    project: str | None = None


# Course-code subject prefixes ("WRIT 150"); the vault's course notes add the user's own.
_SUBJECTS = ("WRIT", "CSCI", "MATH", "ENGR", "EE", "ITP", "BUAD", "ECON", "PHYS", "CHEM",
             "BISC", "GESM", "PSYC", "COMM", "DSCI", "AME", "CE", "ISE", "BME", "MASC",
             "ACCT", "FBE", "PHIL", "HIST", "POSC", "SOCI", "ENGL", "CTCS", "ARCH", "JOUR",
             "QBIO", "DATA", "INF", "CS", "STAT", "BIO", "ECE", "LING", "ARLT", "ARTS")
_COURSE_FILE = re.compile(r"([A-Z]{2,5})-(\d{3}[A-Z]?)\.md")
_SCHOOL_WORDS = re.compile(
    r"\b(homework|problem ?sets?|psets?|essays?|rubrics?|midterms?|final exams?|exams?|"
    r"quiz(?:zes)?|lab reports?|syllabus|syllabi|graded|my professor|my prof|"
    r"my (?:class|course|ta)|for (?:my |a |the )?class|coursework|course ?work|"
    r"brightspace|d2l|blackboard|gradescope|due (?:on|by|date)|"
    r"academic integrity|discussion post|reading response|thesis statement|"
    r"writing (?:project|portfolio)|(?:class|course|school|writing|canvas) assignments?)\b",
    re.I)
_SCHOOL_PATH = re.compile(r"02-Areas/usc|/usc/(?:fall|spring|summer)-\d{4}", re.I)
_WIKILINK = re.compile(r"\[\[[^\]\n]{1,120}\]\]")
_ADDRESS = re.compile(r"\b\d{2,5} [A-Z][a-z]+(?: [A-Z][a-z]+)* (?:St|Street|Ave|Avenue|Blvd|"
                      r"Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Pl|Place|Ct|Court)\b\.?")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_VAULT_FOLDER = re.compile(r"\b0\d-(?:Areas|Projects|Resources|Archive|Inbox|Generated|Daily)\b")


def course_codes() -> set[str]:
    """The user's own course subjects, from the vault's course notes
    (02-Areas/usc/<term>/<SUBJ>-<num>.md). Names only, never contents."""
    root = VAULT_PATH / "02-Areas" / "usc"
    try:
        return {m.group(1) for term in root.iterdir() if term.is_dir()
                for f in term.iterdir() if (m := _COURSE_FILE.fullmatch(f.name))}
    except OSError:
        return set()


def _course_re(extra: set[str]) -> re.Pattern:
    subjects = sorted({*_SUBJECTS, *extra}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(subjects) + r")[ -]?\d{3}[A-Z]?\b")


def gate(text: str, purpose: str | None, origin: Origin | None = None) -> str:
    """Returns the purpose if this work may go outside; else BoundaryError with a reason for Claude.
    Run on the raw brief, before `scrub` hides the paths it looks for."""
    keep = " Do it yourself; outside models get grunt work and images only."
    purpose = (purpose or "").strip().lower()
    if purpose not in PURPOSES:
        raise BoundaryError(
            f"purpose must be one of: {', '.join(PURPOSES)}. Work that's none of these "
            "(a project's own ideas or design, anything personal, school work) stays "
            "with you." + ("" if not purpose else f" '{purpose}' isn't one."))
    if origin and origin.profile == "study":
        raise BoundaryError("this is a study tab: nothing from it goes to an outside model "
                            "(coursework is an academic-integrity matter)." + keep)
    hit = (_SCHOOL_PATH.search(text) or _course_re(course_codes()).search(text)
           or _SCHOOL_WORDS.search(text))
    if hit:
        raise BoundaryError(
            f"this looks like coursework (it says '{hit.group(0)}'). School writing, "
            "homework and graded code stay with you: academic integrity, and a school "
            "account may only hold the school's public data." + keep
            + " If it really isn't school work, reword the brief without it.")
    return purpose


def _leftovers(text: str, redactions: tuple[str, ...]) -> list[str]:
    """Personal details a scrubbed brief still carries: contact people, vault links/folders, addresses, SSNs."""
    found = [r for r in redactions if r.split(" ", 1)[-1].startswith("person")]
    for pattern, what in ((_WIKILINK, "a vault link"), (_VAULT_FOLDER, "a vault folder"),
                          (_ADDRESS, "a street address"), (_SSN, "an SSN")):
        if pattern.search(text):
            found.append(what)
    return found


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?![\w])")
_DIGITS = re.compile(r"(?<![\w.#])\d{9,}(?!\w)")       # account and card numbers
_TILDE = re.compile(r"(?<![\w/])~/[^\s\"'`)>\]]+")


def _path_re(root: Path) -> re.Pattern:
    return re.compile(re.escape(str(root)) + r"[^\s\"'`)>\]]*")


def scrub(text: str, names: tuple[str, ...] | list[str] = (), *,
          purpose: str | None = None, origin: Origin | None = None) -> Brief:
    """Replace emails, phones, long digit runs, home/vault paths and contact names with placeholders.

    With a `purpose` the brief passes `gate` first and is refused if personal details remain;
    without one it's for Claude's own variants and `launch` won't take it.
    Catches identifiers, not meaning: the sandbox is what holds if a plain-words brief passes."""
    text = str(text or "").strip()
    if purpose is not None:
        purpose = gate(text, purpose, origin)
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
    if purpose is not None and (left := _leftovers(text, tuple(found))):
        raise BoundaryError(
            f"the brief still carries personal details after the scrub ({', '.join(left)}). "
            "Say it generically, with no one's name and nothing from the vault, or do it "
            "yourself.")
    return Brief(text, tuple(found), _KEY, purpose)


def clean_env(keys: tuple[str, ...] = (), scratch: Path | None = None,
              fixed: tuple[tuple[str, str], ...] = ()) -> dict[str, str]:
    """ENV_KEEP plus the provider's `keys` and registry `fixed` values, and a TMPDIR in scratch
    (the usual one is out of reach)."""
    env = {k: os.environ[k] for k in (*ENV_KEEP, *keys) if os.environ.get(k)}
    env.update(fixed)
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
    """Where a CLI installed under the home dir (nvm node, npm prefix) keeps its code, to be made readable."""
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
    """The seatbelt profile; later rules win: allow all, deny home/volumes/temp dirs, allow the CLI's
    config and code, deny vault and app data regardless, then allow scratch (the only write)."""
    home = Path.home()
    walled = [home, "/Volumes", "/private/tmp", "/private/var/folders", "/tmp", "/var/folders"]
    # Path resolution under a walled dir stats its parents (SQLite lstats every component);
    # allow their metadata only.
    parents = {*Path(_real(scratch)).parents}
    for d in config_dirs:
        parents.update(Path(_real(d)).parents)
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* file-write* {_subpaths(walled)})",
        "(allow file-read-metadata " + " ".join(
            f"(literal {_quote(_real(p))})" for p in (*walled, *sorted(parents))) + ")",
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
    code: int | None          # None: killed, and `cut` says why
    stdout: str
    stderr: str
    cut: str | None = None    # "time" or "turns": stopped at a cap, stdout is what it had


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):   # gone; macOS says EPERM for a zombie group
        pass


async def launch(argv_for: Callable[[str], list[str]], brief: Brief, scratch: Path, *,
                 env_keys: tuple[str, ...] = (), env_set: tuple[tuple[str, str], ...] = (),
                 config_dirs: tuple[Path, ...] = (),
                 stdin_brief: bool = False, timeout: float = 600,
                 ends_turn: Callable[[str], bool] | None = None, max_turns: int = 0) -> Finished:
    """Run an outside CLI on `brief` in `scratch`, sandboxed; `argv_for` maps the scrubbed text to argv.
    Caps: `timeout` wall-clock seconds and `max_turns` turns (stdout lines `ends_turn` marks). A
    turn's tool calls run after its line, so the cut waits for the next one. At a cap it is killed
    and returns what it printed, with `cut` set."""
    if not isinstance(brief, Brief):
        raise BoundaryError("launch takes a scrubbed Brief (boundary.scrub), never raw text")
    if brief.purpose not in PURPOSES:
        raise BoundaryError("launch takes a brief that passed the gate (scrub with a purpose)")
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
        cwd=str(scratch), env=clean_env(env_keys, scratch, env_set),
        stdin=asyncio.subprocess.PIPE if stdin_brief else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=OUTPUT_MAX_BYTES)
    out = bytearray()
    cut: str | None = None

    async def read_out() -> None:
        nonlocal cut
        turns = 0
        while line := await proc.stdout.readline():
            if len(out) < OUTPUT_MAX_BYTES:
                out.extend(line)
            if ends_turn and max_turns and ends_turn(line.decode("utf-8", "replace")):
                turns += 1
                if turns > max_turns:
                    cut = "turns"
                    _kill(proc)
                    return

    async def feed() -> None:
        if stdin_brief:
            proc.stdin.write(brief.text.encode())
            await proc.stdin.drain()
            proc.stdin.close()

    err_task = asyncio.ensure_future(proc.stderr.read())
    try:
        await asyncio.wait_for(asyncio.gather(feed(), read_out()), timeout)
    except asyncio.TimeoutError:
        cut = "time"
        _kill(proc)
    except asyncio.CancelledError:
        _kill(proc)
        err_task.cancel()
        raise
    except ValueError:      # one line past `limit`: more output than is ever kept
        _kill(proc)
        err_task.cancel()
        raise BoundaryError(f"it printed a line over {OUTPUT_MAX_BYTES:,} bytes; stopped") from None
    err = await err_task
    await proc.wait()
    return Finished(None if cut else proc.returncode,
                    bytes(out[:OUTPUT_MAX_BYTES]).decode("utf-8", "replace"),
                    err[-20_000:].decode("utf-8", "replace"), cut)


# ---------- an account's own home, and signing in there ----------

KEYCHAIN = "login.keychain-db"


def make_keychain(home: Path) -> None:
    """An empty-password default keychain in the home, so agy's go-keyring never writes the login keychain.
    No lock timeout: a locked one would stop a headless run on a dialog."""
    (home / "Library" / "Keychains").mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    if (home / "Library" / "Keychains" / KEYCHAIN).exists():
        return
    env = {**clean_env(), "HOME": str(home)}
    for args in (("create-keychain", "-p", "", KEYCHAIN), ("set-keychain-settings", KEYCHAIN),
                 ("list-keychains", "-d", "user", "-s", KEYCHAIN),
                 ("default-keychain", "-d", "user", "-s", KEYCHAIN)):
        done = subprocess.run(["/usr/bin/security", *args], env=env, capture_output=True,
                              text=True, timeout=10, stdin=subprocess.DEVNULL)
        if done.returncode:
            raise BoundaryError(f"couldn't make the account's keychain: {done.stderr.strip()[:200]}")


# What a process needs to open a browser URL; shut for sign-ins Kestrel opens itself (codex).
_LAUNCH_SERVICES = ('(global-name "com.apple.coreservices.launchservicesd") '
                    '(global-name-prefix "com.apple.lsd.")')


def login_profile(home: Path, binary: str, ports: tuple[int, ...] | None,
                  browser: bool) -> str:
    """A sign-in's seatbelt: a run's walls plus loopback listening. `ports`: bind only these;
    None: any loopback port; (): none. `browser`: may open the browser (LaunchServices).

    Seatbelt refuses ~1 in 8 literal-port binds ("localhost:*" has no holes); 1455 and 1457 pass,
    so changing CODEX_LOGIN_PORTS needs test_a_sign_in_listens_only_where_it_may green."""
    rules = [profile(home / ".login", (home,), tuple(tool_dirs(binary)))]
    rules.append("(deny network-bind (local ip))")
    if ports is None:
        rules.append('(allow network-bind (local ip "localhost:*"))')
    elif ports:
        rules.append("(allow network-bind " + " ".join(
            f'(local ip "localhost:{int(p)}")' for p in ports) + ")")
    if not browser:
        rules.append(f"(deny mach-lookup {_LAUNCH_SERVICES})")
    return "\n".join(rules)


async def start_login(argv: list[str], home: Path, env_set: tuple[tuple[str, str], ...] = (), *,
                      ports: tuple[int, ...] | None = (), browser: bool = False):
    """Start a CLI's sign-in sandboxed to the account's home (`login_profile`); stdout+stderr
    merged, stdin open (agy's ACP). The caller reads it and calls `stop_login`."""
    if not os.access(SANDBOX_EXEC, os.X_OK):
        raise BoundaryError("no sandbox-exec on this machine; sign in from a terminal instead")
    binary = shutil.which(argv[0])
    if not binary:
        raise BoundaryError(f"`{argv[0]}` isn't installed")
    work = home / ".login"
    rules = login_profile(home, argv[0], ports, browser)
    return await asyncio.create_subprocess_exec(
        SANDBOX_EXEC, "-p", rules, binary, *argv[1:], cwd=str(work),
        env=clean_env((), work, env_set), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, limit=OUTPUT_MAX_BYTES)


def stop_login(proc) -> None:
    if proc.returncode is None:
        _kill(proc)


# The only pages Kestrel opens in the browser for a sign-in.
SIGN_IN_HOSTS = ("auth.openai.com",)


def open_sign_in(url: str) -> None:
    """Open a sign-in page in the user's default browser (where they're
    already signed in), only an https page on SIGN_IN_HOSTS."""
    m = re.fullmatch(r"https://([A-Za-z0-9.-]+)/[^\s]*", url or "")
    if not m or m.group(1).lower() not in SIGN_IN_HOSTS:
        raise BoundaryError("not a sign-in page Kestrel opens")
    done = subprocess.run(["/usr/bin/open", url], capture_output=True, text=True, timeout=10,
                          stdin=subprocess.DEVNULL)
    if done.returncode:
        raise BoundaryError(f"couldn't open the browser: {done.stderr.strip()[:200]}")
