"""What a console tab is *for*: general, study, or dev on a project.

A profile is the part of a session fixed at start — cwd, prompt, tools,
permission mode — so it is chosen when the tab opens (the new-tab picker),
not guessed from the first message: a misread would run the whole session in
the wrong directory with the wrong tools. A tab can still move work to the
right place mid-conversation through `open_tab` (console_tools), which opens
a new tab rather than reshaping this one.

- **general** — the vault console as it always was.
- **study** — the same vault and tools, prompted to tutor: work it through
  with the user rather than hand over the answer. Builds on §22's course tools.
- **dev** — Claude Code on a repo under `DEV_PROJECT_ROOTS`: cwd at the repo,
  so its CLAUDE.md, settings, skills and auto-memory load the way they do in a
  terminal; the stock prompt plus a short note (no vault rules); subagents on;
  none of the vault tools but `open_tab` and `project_info`. Other projects
  are readable from it (Read/Grep/Glob without a card, see `is_project_path`)
  but never added as working directories — writing there still asks.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

from . import db
from .config import DEV_PROJECT_ROOTS

# Claude Code's per-project state: ~/.claude/projects/<cwd, every
# non-alphanumeric as "-">/memory is a repo's auto-memory.
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# A new project's directory name: no separators, no leading dot (list_projects
# hides those), nothing a shell or a URL would trip on.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

PROFILES = ("general", "study", "dev")

# What a profile starts in before you pick one for it (the browser remembers
# your pick per profile). Dev runs never ask since 11-03, the user's call: a
# Claude Code session on a repo you opened on purpose doesn't stop for clicks,
# and the turn's diff is the review. That includes a tab another tab opened,
# which sat on a card nobody was watching while it was held to ask. General
# and study stay on auto: they hold the mail and calendar tools, and auto still
# stops for what can't be undone.
DEFAULT_MODE = {"general": "auto", "study": "auto", "dev": "bypassPermissions"}


class ProfileError(ValueError):
    pass


def normalize(profile: str | None) -> str:
    return profile if profile in PROFILES else "general"


def _is_project(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".")


def list_projects() -> list[dict]:
    """Every directory directly under a project root, most recently touched
    first — the picker's list. `git` marks the ones the diff pane can read."""
    out = []
    for root in DEV_PROJECT_ROOTS:
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if not _is_project(p):
                continue
            git = (p / ".git").exists()
            try:
                # A repo's .git/index moves on every stage and checkout, which
                # is a better "last worked on" than the directory's own mtime.
                mtime = (p / ".git" / "index").stat().st_mtime if git else p.stat().st_mtime
            except OSError:
                mtime = 0
            out.append({"name": p.name, "path": str(p), "git": git,
                        "root": str(root), "mtime": mtime})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def resolve_project(value: str | None) -> Path:
    """A project path or bare name → the directory, or ProfileError.

    The only gate between a string from the browser (or the model, through
    open_tab) and a session's cwd, so it accepts nothing but an immediate
    child of a configured root: no `..`, no symlink out, no other directory.
    """
    if not value:
        raise ProfileError("a dev tab needs a project")
    projects = list_projects()
    want = str(value).strip()
    for p in projects:
        if want in (p["path"], p["name"]):
            path = Path(p["path"]).resolve()
            if any(path.parent == r.resolve() for r in DEV_PROJECT_ROOTS):
                return path
    names = ", ".join(p["name"] for p in projects) or "none"
    raise ProfileError(f"no project named {want!r} — the projects are: {names}")


def with_folders(rows: list[dict]) -> list[dict]:
    """`list_projects` rows with the folder each is filed under. Apart from
    list_projects on purpose: a folder is only how the picker groups things,
    so it can't change what a name resolves to or what a tab may read."""
    folders = db.project_folders()
    return [{**p, "folder": folders.get(str(Path(p["path"]).resolve()))} for p in rows]


def file_project(project: str | None, folder: str | None) -> dict:
    """Move a project into a folder (blank: out of it). App metadata only:
    the directory stays put. One level, so no `/` in a folder's name."""
    path = resolve_project(project)
    name = " ".join(str(folder or "").split())
    if len(name) > 64 or "/" in name:
        raise ProfileError("a folder name is up to 64 characters, with no /")
    return {"path": str(path), "folder": db.set_project_folder(str(path), name or None)}


def create_project(name: str | None, root: str | None = None) -> dict:
    """A new, empty project — the picker's "new project" row. `git init`ed so
    the diff pane works from the first turn; nothing else is scaffolded (the
    first dev turn is where a project gets its shape). `root` must be one of
    DEV_PROJECT_ROOTS (default: the first); an existing name is refused, not
    reused."""
    name = str(name or "").strip()
    if not _NAME.fullmatch(name):
        raise ProfileError("a project name is letters, digits, . _ - "
                           "(up to 64, not starting with . _ -)")
    roots = [r for r in DEV_PROJECT_ROOTS if r.is_dir()]
    if root:
        roots = [r for r in roots if str(r) == root or r.resolve() == Path(root).resolve()]
    if not roots:
        raise ProfileError(f"no project root {root!r}" if root else "no project root exists")
    path = roots[0] / name
    if any((r / name).exists() for r in DEV_PROJECT_ROOTS):
        raise ProfileError(f"{name!r} already exists")
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return next(p for p in list_projects() if p["path"] == str(path))


def memory_dir(path: Path) -> Path:
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(path)) / "memory"


FILE_WALK_CAP = 20_000
FILE_MATCHES = 12
FILE_MAX_BYTES = 1_000_000
# Generated from app.db and index.db: an edit here would be overwritten, or
# worse, read as the truth (CLAUDE.md, one source of truth per datum).
FILE_READ_ONLY = {"tasks.md"}


class FileConflict(ProfileError):
    """The file changed on disk after it was opened."""


def _inside(cwd: Path, rel: str) -> Path:
    root = cwd.resolve()
    target = (root / rel).resolve()
    if not rel or not target.is_relative_to(root):
        raise ProfileError("that path isn't under this tab's folder")
    return target


def read_file(cwd: Path, rel: str) -> dict:
    target = _inside(cwd, rel)
    if not target.is_file():
        raise ProfileError("no such file")
    if target.stat().st_size > FILE_MAX_BYTES:
        raise ProfileError("too large to open here")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ProfileError("not a text file") from None
    return {"path": rel, "content": content, "mtime": target.stat().st_mtime_ns,
            "read_only": target.name in FILE_READ_ONLY}


def write_file(cwd: Path, rel: str, content: str, mtime: Any) -> dict:
    target = _inside(cwd, rel)
    if target.name in FILE_READ_ONLY:
        raise ProfileError(f"{target.name} is generated, so edit its source instead")
    if not target.is_file():
        raise ProfileError("no such file")
    if target.stat().st_mtime_ns != mtime:
        raise FileConflict("the file changed since you opened it")
    target.write_text(content, encoding="utf-8")
    return {"mtime": target.stat().st_mtime_ns}


def matching_files(cwd: Path, query: str) -> list[str]:
    """Relative paths under `cwd` containing `query`, names before folders.
    git's list where there is one, so a repo's ignored files don't drown it."""
    import itertools
    import os
    import subprocess

    query = query.lower()
    paths: list[str] = []
    if (cwd / ".git").exists():
        try:
            paths = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=cwd, capture_output=True, text=True, timeout=3).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            paths = []
    if not paths:
        def walk():
            for root, dirs, files in os.walk(cwd):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
                for name in files:
                    if not name.startswith("."):
                        yield os.path.relpath(os.path.join(root, name), cwd)
        paths = list(itertools.islice(walk(), FILE_WALK_CAP))
    hits = [p for p in paths if query in p.lower()]
    hits.sort(key=lambda p: (query not in p.rsplit("/", 1)[-1].lower(), len(p)))
    return hits[:FILE_MATCHES]


def dev_servers(cwd: Path, skip: tuple[int, ...] = ()) -> list[dict]:
    """What's listening on this machine from inside a project, for the view
    pane's preview: `lsof` for the listeners, then each one's cwd. A port with
    no process behind this project is someone else's, so it isn't offered."""
    import re
    import subprocess

    def lsof(*args: str) -> str:
        try:
            return subprocess.run(["/usr/sbin/lsof", "-nP", *args], capture_output=True,
                                  text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    ports: dict[int, set[int]] = {}
    pid = None
    for line in lsof("-iTCP", "-sTCP:LISTEN", "-Fpn").splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            if m := re.search(r":(\d+)$", line):
                ports.setdefault(pid, set()).add(int(m.group(1)))
    root = cwd.resolve()
    found = set()
    for pid, theirs in ports.items():
        where = [ln[1:] for ln in lsof("-a", "-p", str(pid), "-d", "cwd", "-Fn").splitlines()
                 if ln.startswith("n")]
        if where and Path(where[0]).is_relative_to(root):
            found |= theirs
    return [{"port": p, "url": f"http://localhost:{p}"}
            for p in sorted(found) if p not in skip]


def is_project_path(value: str | None) -> bool:
    """Whether a Read/Grep/Glob target is inside some project or its
    auto-memory — what a dev tab may read without a card."""
    if not value:
        return False
    try:
        p = Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for proj in list_projects():
        base = Path(proj["path"]).resolve()
        for d in (base, memory_dir(base)):
            if p == d or d in p.parents:
                return True
    return False


def _head(path: Path, limit: int) -> str | None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n… (cut; Read {path} for the rest)"


def _blurb(path: Path) -> str:
    """A project's first prose line — README's (it says what the thing is), else CLAUDE.md's."""
    for f in ("README.md", "readme.md", "CLAUDE.md", "AGENTS.md"):
        text = _head(path / f, 4000) or ""
        for line in text.splitlines():
            line = line.strip()
            # "@AGENTS.md" is an import, not prose — AGENTS.md is next anyway.
            if line and not line.startswith(("#", "<", "!", "[", "```", "---", "@")):
                return line[:160]
    return ""


def _git(path: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def describe(project: str | None = None, exclude: Path | None = None) -> str:
    """What one dev tab can learn about another project without leaving its
    own. No project: every project, one line each. A project: its CLAUDE.md
    (else README), its auto-memory index, recent commits, what's uncommitted
    and the top level — enough to know what to Read next, with the paths."""
    if not project:
        rows = [p for p in list_projects()
                if exclude is None or Path(p["path"]).resolve() != exclude.resolve()]
        if not rows:
            return "no other projects under " + ", ".join(map(str, DEV_PROJECT_ROOTS))
        return "\n".join(f"- {p['name']} ({p['path']})"
                         + (f" [{p['folder']}]" if p["folder"] else "")
                         + (f" — {b}" if (b := _blurb(Path(p["path"]))) else "")
                         for p in with_folders(rows))
    path = resolve_project(project)
    out = [f"# {path.name}\npath: {path}"]
    if folder := db.project_folders().get(str(path)):
        out[0] += f"\nfolder: {folder}"
    if (path / ".git").exists():
        branch = _git(path, "branch", "--show-current") or "(detached)"
        status = _git(path, "status", "--short")
        n = len(status.splitlines()) if status else 0
        out.append(f"git: on {branch}, {n} uncommitted file{'s' * (n != 1)}")
        if log := _git(path, "log", "--oneline", "-12", "--date=short", "--format=%ad %h %s"):
            out.append("## Recent commits\n" + log)
    else:
        out.append("git: not a repo")
    for f in ("CLAUDE.md", "README.md", "readme.md"):
        if text := _head(path / f, 6000):
            out.append(f"## {f}\n{text}")
            # A CLAUDE.md that is mostly `@AGENTS.md` imports: the imported
            # files are the instructions, so they come along (inside the repo only).
            for imp in re.findall(r"^@(\S+)\s*$", text, re.M)[:4]:
                target = (path / imp).resolve()
                if path.resolve() in target.parents and (body := _head(target, 6000)):
                    out.append(f"## {imp} (imported by {f})\n{body}")
            break
    mem = memory_dir(path)
    if text := _head(mem / "MEMORY.md", 4000):
        out.append(f"## Auto-memory index ({mem})\n{text}")
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir()
                     if p.name not in (".git", "node_modules", ".venv", "__pycache__", ".DS_Store"))
    except OSError:
        top = []
    out.append("## Top level\n" + (" ".join(top[:80]) or "(empty)"))
    return "\n\n".join(out)


def start_mode(profile: str, asked: str | None) -> str | None:
    """The permission mode a session starts in: your pick for the tab (stored by the
    picker; a switch after start is a live `mode` message), else the profile's default."""
    return asked or DEFAULT_MODE[profile]
