"""What a dev tab's turn changed in its repo, as a diff.

The change ledger (console_changes) lists the tool calls that wrote; this is
the other half a code review needs — the lines. It is read off git rather
than rebuilt from Edit/Write inputs, because a turn also writes through Bash
(a formatter, `npm install`, a codegen script) and those leave no input to
rebuild from. Git sees the tree, whoever touched it.

Per turn, not per repo: `snapshot()` at the start of a user turn records the
working tree as a commit (`git stash create` — it writes a dangling commit
object and touches neither the files, the index nor the stash list), plus the
untracked files and their contents. `turn_diff()` at the end diffs the tree
against that. So work you had uncommitted before the turn isn't charged to
it. `worktree_diff()` is the plain "everything uncommitted" view, and
`commit_diff()` the committed one a tab's branch review reads
(console_worktree.review).

Never raises: a diff that can't be read costs the pane, not the turn.
"""

import difflib
import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 10
# Per file and per turn, so one regenerated lockfile can't bury the change you
# came to read — the file still lists, with its counts, and says it's cut.
MAX_FILE_LINES = 600
MAX_TOTAL_LINES = 4000
# An untracked file bigger than this isn't kept at snapshot time: a change to
# it shows as "changed", without lines.
MAX_KEEP_BYTES = 256 * 1024
MAX_UNTRACKED = 400


def _git(cwd: Path, *args: str, ok=(0,)) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT_S, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in ok else None


def is_repo(cwd: Path) -> bool:
    return (_git(cwd, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def _untracked(cwd: Path) -> list[str]:
    out = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z") or ""
    return [p for p in out.split("\0") if p][:MAX_UNTRACKED]


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_KEEP_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def snapshot(cwd: Path) -> dict | None:
    """The tree as the turn found it, or None when there's nothing to diff
    against (not a repo, or a repo with no commit yet)."""
    if not is_repo(cwd):
        return None
    base = (_git(cwd, "stash", "create") or "").strip()
    if not base:   # a clean tree: stash create makes nothing
        base = (_git(cwd, "rev-parse", "HEAD") or "").strip()
    if not base:
        return None
    untracked = {p: _read(cwd / p) for p in _untracked(cwd)}
    return {"base": base, "untracked": untracked}


def _split(patch: str) -> list[dict]:
    """A multi-file patch → one entry per file, with its counts."""
    files: list[dict] = []
    for chunk in patch.split("\ndiff --git "):
        if not chunk.strip():
            continue
        text = chunk if chunk.startswith("diff --git ") else "diff --git " + chunk
        lines = text.split("\n")
        path, old = None, None
        status = "modified"
        # Everything from the first hunk header on is hunk content — matched by
        # position, not prefix, or a deleted `-- comment` line reads as a
        # `--- ` file header and vanishes.
        first = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), len(lines))
        for ln in lines[:first]:
            if ln.startswith("+++ ") and ln[4:] != "/dev/null":
                path = ln[4:].removeprefix("b/")
            elif ln.startswith("--- ") and ln[4:] != "/dev/null":
                old = ln[4:].removeprefix("a/")
            elif ln.startswith("new file"):
                status = "added"
            elif ln.startswith("deleted file"):
                status = "deleted"
            elif ln.startswith("rename from "):
                old, status = ln[len("rename from "):], "renamed"
            elif ln.startswith("rename to "):
                path = ln[len("rename to "):]
        if path is None:
            # deleted, binary, or mode-only: the header names it
            head = lines[0][len("diff --git "):]
            path = head.split(" b/", 1)[-1] if " b/" in head else head
        hunks =[ln for ln in lines[first:] if ln[:1] in ("@", "+", "-", " ", "\\")]
        files.append({
            "path": path, "old_path": old if status == "renamed" else None,
            "status": status,
            "binary": any(ln.startswith("Binary files") for ln in lines[:first]),
            "additions": sum(1 for ln in hunks if ln[:1] == "+"),
            "deletions": sum(1 for ln in hunks if ln[:1] == "-"),
            "lines": hunks,
        })
    return files


def _added(cwd: Path, rel: str) -> dict:
    text = _read(cwd / rel)
    if text is None:
        return {"path": rel, "old_path": None, "status": "added", "binary": True,
                "additions": 0, "deletions": 0, "lines": []}
    body = text.split("\n")
    if body and body[-1] == "":
        body.pop()
    return {"path": rel, "old_path": None, "status": "added", "binary": False,
            "additions": len(body), "deletions": 0,
            "lines": [f"@@ -0,0 +1,{len(body)} @@"] + ["+" + ln for ln in body]}


def _changed_untracked(cwd: Path, rel: str, before: str | None) -> dict | None:
    after = _read(cwd / rel)
    if after is None and not (cwd / rel).exists():
        lines = (before or "").split("\n")
        return {"path": rel, "old_path": None, "status": "deleted", "binary": before is None,
                "additions": 0, "deletions": len(lines) if before else 0,
                "lines": ["-" + ln for ln in lines] if before else []}
    if before == after:
        return None
    if before is None or after is None:
        return {"path": rel, "old_path": None, "status": "modified", "binary": True,
                "additions": 0, "deletions": 0, "lines": []}
    diff = list(difflib.unified_diff(before.split("\n"), after.split("\n"), lineterm="", n=3))[2:]
    return {"path": rel, "old_path": None, "status": "modified", "binary": False,
            "additions": sum(1 for ln in diff if ln.startswith("+")),
            "deletions": sum(1 for ln in diff if ln.startswith("-")),
            "lines": diff}


def _cap(files: list[dict]) -> list[dict]:
    total = 0
    for f in files:
        room = max(0, min(MAX_FILE_LINES, MAX_TOTAL_LINES - total))
        f["truncated"] = len(f["lines"]) > room
        f["lines"] = f["lines"][:room]
        total += len(f["lines"])
    return files


def turn_diff(cwd: Path, snap: dict | None) -> list[dict]:
    """What changed since `snap`. Empty when nothing did, or nothing can say."""
    if not snap:
        return []
    try:
        patch = _git(cwd, "diff", "--no-color", "--no-ext-diff", "-M", snap["base"], "--") or ""
        files = _split(patch)
        before = snap["untracked"]
        seen = {f["path"] for f in files}
        for rel in _untracked(cwd):
            if rel in seen:
                continue
            if rel not in before:
                files.append(_added(cwd, rel))
            else:
                f = _changed_untracked(cwd, rel, before[rel])
                if f:
                    files.append(f)
        now = set(_untracked(cwd))
        for rel, text in before.items():
            if rel not in now and not (cwd / rel).exists() and rel not in seen:
                f = _changed_untracked(cwd, rel, text)
                if f:
                    files.append(f)
        return _cap(files)
    except Exception:   # see module docstring
        return []


def worktree_diff(cwd: Path) -> list[dict]:
    """Everything uncommitted: tracked changes against HEAD, plus untracked
    files as added."""
    try:
        if not is_repo(cwd):
            return []
        head = (_git(cwd, "rev-parse", "HEAD") or "").strip()
        files = _split(_git(cwd, "diff", "--no-color", "--no-ext-diff", "-M", head, "--") or "") if head else []
        seen = {f["path"] for f in files}
        files += [_added(cwd, rel) for rel in _untracked(cwd) if rel not in seen]
        return _cap(files)
    except Exception:
        return []


# A revision the browser may name: an object id, or HEAD. Nothing that could
# read as an option, a range or a reflog walk.
REV = re.compile(r"[0-9a-f]{7,64}|HEAD")
BLOB_MAX_BYTES = 512 * 1024


def commit_diff(cwd: Path, old: str, new: str = "HEAD") -> list[dict]:
    """What the commits from `old` to `new` changed. No working tree and no
    untracked files: a branch review shows what a merge would carry."""
    if not (REV.fullmatch(old or "") and REV.fullmatch(new or "")):
        return []
    try:
        return _cap(_split(_git(cwd, "diff", "--no-color", "--no-ext-diff", "-M",
                                old, new, "--") or ""))
    except Exception:
        return []


def blob(cwd: Path, rev: str, path: str) -> str | None:
    """A file's text at `rev` (a commit or a tree), so the pane can colour a
    hunk with the whole file in view: a docstring's middle line is only a
    string to a reader who saw where it opened. None for binary, oversized,
    missing, or a path that climbs out."""
    if not REV.fullmatch(rev or "") or not path or "\0" in path or path.startswith(("/", "-")):
        return None
    if ".." in path.split("/"):
        return None
    size = (_git(cwd, "cat-file", "-s", f"{rev}:{path}") or "").strip()
    if not size.isdigit() or int(size) > BLOB_MAX_BYTES:
        return None
    try:
        r = subprocess.run(["git", "-C", str(cwd), "cat-file", "blob", f"{rev}:{path}"],
                           capture_output=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode or b"\0" in r.stdout:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
