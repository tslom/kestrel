"""Each dev tab in its own git worktree (`<repo>/.claude/worktrees/tab-<hex>`, branch `kestrel/tab-<hex>`).

A tab's work goes live only when `merge` rebases it onto its base and fast-forwards the
main checkout; a conflict never reaches the main checkout. Orphan branches are swept
unless a socket here or a live pid (git lock) holds them. Stdlib only and py3.9-safe:
the SessionStart hook (.claude/hooks/session_start.py) loads it outside the venv.
Design: DESIGN.md → The console sheet, "A worktree per dev tab".
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from collections import Counter
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
DIR = Path(".claude") / "worktrees"
BRANCH_PREFIX = "kestrel/"
CONFIG = Path(".kestrel") / "worktree.json"
DEFAULT_LINKS = (".claude/settings.local.json",)
SETUP_TIMEOUT = 300
NAME = re.compile(r"tab-[0-9a-f]{8}")
NIGHT_PREFIX = "worktree-"
# Branches the orphan sweep may touch: a tab's, or a `claude --worktree` session's (worktree-<name>).
MANAGED = re.compile(r"kestrel/tab-[0-9a-f]{8}|worktree-[\w.-]+")
LOCK_PID = re.compile(r"\bpid (\d+)\b")
# KESTREL_DEV_WORKTREES=0 turns it off: dev tabs open on the main checkout.
ENABLED = os.environ.get("KESTREL_DEV_WORKTREES", "1") != "0"

_lock = threading.Lock()
# Worktrees a live socket holds; never removed.
_live: Counter[str] = Counter()
# Notified under _lock after each release() finishes (for wait_released).
_released = threading.Condition(_lock)


class WorktreeError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, timeout=timeout)


def _out(cwd: Path, *args: str) -> str | None:
    r = _git(cwd, *args)
    return r.stdout.strip() if r.returncode == 0 else None


def _munge(path: Path) -> str:
    # Claude Code's project-dir name for a cwd (see console_profiles.memory_dir).
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def eligible(repo: Path) -> bool:
    """The main worktree, on a branch, with a commit."""
    if not ENABLED or not (repo / ".git").is_dir():
        return False
    return bool(_out(repo, "rev-parse", "--verify", "-q", "HEAD")
                and _out(repo, "symbolic-ref", "-q", "--short", "HEAD"))


def _config(repo: Path) -> dict:
    try:
        cfg = json.loads((repo / CONFIG).read_text())
    except FileNotFoundError:
        return {"link": list(DEFAULT_LINKS), "setup": []}
    except (OSError, ValueError) as e:
        raise WorktreeError(f"{CONFIG} doesn't parse: {e}")
    return {"link": [str(p) for p in cfg.get("link") or []],
            "setup": [str(c) for c in cfg.get("setup") or []]}


def _exclude(repo: Path, links: list[str]) -> None:
    """The worktrees dir and every link in .git/info/exclude. Links need their own line:
    `.venv/` in .gitignore doesn't match a symlink (a file to git), so `git add -A` would commit it."""
    path = repo / ".git" / "info" / "exclude"
    want = [f"/{DIR.as_posix()}/"] + [f"/{p.strip('/')}" for p in links]
    try:
        have = path.read_text().splitlines()
    except FileNotFoundError:
        have = []
    missing = [w for w in want if w not in have]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            if have and have[-1].strip():
                fh.write("\n")
            fh.write("# Kestrel dev-tab worktrees (app/console_worktree.py)\n")
            fh.write("".join(m + "\n" for m in missing))


def link(repo: Path, tree: Path, cfg: dict | None = None) -> list[str]:
    """Symlink the configured paths from the main checkout into `tree`; returns those linked.
    Existing or missing paths are left alone. session_start.py runs this too."""
    cfg = cfg or _config(repo)
    _exclude(repo, cfg["link"])
    made = []
    for rel in cfg["link"]:
        src, dst = repo / rel, tree / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        made.append(rel)
    return made


def _setup(repo: Path, tree: Path) -> list[str]:
    """Links and setup commands. Returns what went wrong (for the start line); never refuses."""
    cfg = _config(repo)
    link(repo, tree, cfg)
    notes = []
    for cmd in cfg["setup"]:
        try:
            r = subprocess.run(["/bin/zsh", "-c", cmd], cwd=tree, capture_output=True,
                               text=True, timeout=SETUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            notes.append(f"setup `{cmd}` timed out")
            continue
        if r.returncode:
            notes.append(f"setup `{cmd}` exited {r.returncode}: "
                         + (r.stderr or r.stdout).strip()[-300:])
    return notes


def _unlink(repo: Path, tree: Path) -> None:
    """Unlink before git removes the tree, so nothing follows a link into the main checkout's data."""
    try:
        links = _config(repo)["link"]
    except WorktreeError:
        links = list(DEFAULT_LINKS)
    for rel in links:
        p = tree / rel
        if p.is_symlink():
            p.unlink()


def _base(repo: Path, branch: str) -> str:
    """The base a branch was cut from, else the main checkout's branch."""
    return _out(repo, "config", f"branch.{branch}.kestrelbase") or \
        _out(repo, "symbolic-ref", "--short", "HEAD") or "main"


def _info(repo: Path, tree: Path, notes: list[str] | None = None) -> dict:
    # The tree's current branch, not the made-on one (a site repo's trees sit on try/*).
    branch = (_out(tree, "symbolic-ref", "-q", "--short", "HEAD") if tree.is_dir() else None) \
        or BRANCH_PREFIX + tree.name
    return {"path": str(tree), "name": tree.name, "branch": branch,
            "base": _base(repo, branch), "repo": str(repo), "notes": notes or []}


def _add(repo: Path, tree: Path, base: str) -> list[str]:
    branch = BRANCH_PREFIX + tree.name
    _git(repo, "worktree", "prune")
    if _out(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        r = _git(repo, "worktree", "add", "-q", str(tree), branch)
    else:
        r = _git(repo, "worktree", "add", "-q", "-b", branch, str(tree), base)
        _git(repo, "config", f"branch.{branch}.kestrelbase", base)
    if r.returncode:
        raise WorktreeError(f"git worktree add failed: {r.stderr.strip()}")
    return _setup(repo, tree)


def create(repo: Path) -> dict:
    """A new tab's worktree; sweeps abandoned empty ones first."""
    repo = repo.resolve()
    base = _out(repo, "symbolic-ref", "--short", "HEAD")
    if not base:
        raise WorktreeError("the main checkout isn't on a branch")
    with _lock:
        sweep(repo)
        tree = repo / DIR / f"tab-{secrets.token_hex(4)}"
        notes = _add(repo, tree, base)
        _live[str(tree)] += 1
    return _info(repo, tree, notes)


def for_session(repo: Path, session_id: str) -> dict | None:
    """The worktree a session ran in, rebuilt if removed; None if it ran in the main checkout."""
    repo = repo.resolve()
    if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session_id or ""):
        return None
    prefix = _munge(repo / DIR) + "-"
    name = None
    try:
        for d in CLAUDE_PROJECTS.iterdir():
            if d.name.startswith(prefix) and (d / f"{session_id}.jsonl").exists():
                if NAME.fullmatch(d.name[len(prefix):]):
                    name = d.name[len(prefix):]
                    break
    except OSError:
        return None
    if name is None:
        return None
    tree = repo / DIR / name
    with _lock:
        notes: list[str] = []
        if not (tree / ".git").exists():
            base = _out(repo, "symbolic-ref", "--short", "HEAD") or "main"
            notes = _add(repo, tree, base)
        else:
            notes = _setup(repo, tree)
        _live[str(tree)] += 1
    return _info(repo, tree, notes)


def _trees(repo: Path) -> list[dict]:
    """`git worktree list --porcelain` as [{path, branch, locked}], main first; `locked` is the reason."""
    out = _out(repo, "worktree", "list", "--porcelain") or ""
    trees: list[dict] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            trees.append({"path": Path(line[len("worktree "):]).resolve(),
                          "branch": None, "locked": None})
        elif not trees:
            continue
        elif line.startswith("branch refs/heads/"):
            trees[-1]["branch"] = line[len("branch refs/heads/"):]
        elif line == "locked" or line.startswith("locked "):
            trees[-1]["locked"] = line[len("locked "):].strip() or "locked"
    return trees


def _ours(repo: Path) -> list[Path]:
    """This repo's tab worktrees, as git has them registered."""
    root = (repo / DIR).resolve()
    return [t["path"] for t in _trees(repo)
            if t["path"].parent == root and NAME.fullmatch(t["path"].name)]


def _managed_trees(repo: Path) -> list[Path]:
    """Tab worktrees plus `claude --worktree` ones on their own worktree-<name> branch."""
    root = (repo / DIR).resolve()
    return [t["path"] for t in _trees(repo) if t["path"].parent == root and (
        NAME.fullmatch(t["path"].name) or t["branch"] == NIGHT_PREFIX + t["path"].name)]


def _lock_alive(reason: str) -> bool:
    """Whether a lock's holder lives. Claude Code's names a pid; one without was set by hand and holds."""
    m = LOCK_PID.search(reason or "")
    if not m:
        return True
    try:
        os.kill(int(m.group(1)), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


def _merged(repo: Path, branch: str, base: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", branch, base).returncode == 0


def _dirty(tree: Path) -> list[str]:
    # --no-optional-locks: the tab may be committing here (index.lock).
    return [ln[3:] for ln in (_git(tree, "--no-optional-locks", "status", "--porcelain").stdout
                              or "").splitlines()]


ORPHAN_COMMITS = 5


def branches(repo: Path) -> list[dict]:
    """Every kestrel/tab-* and worktree-* branch: {branch, name, kind ("tab"/"night"), path (or None),
    base, merged, ahead, behind, dirty, locked, held, commits}. `held` = in use; the rest are orphans."""
    repo = repo.resolve()
    trees = _trees(repo)
    main = trees[0]["path"] if trees else repo
    on = {t["branch"]: t for t in trees if t["branch"]}
    refs = _out(repo, "for-each-ref", "--format=%(refname:short)",
                "refs/heads/kestrel/", f"refs/heads/{NIGHT_PREFIX}*") or ""
    out = []
    for b in refs.splitlines():
        if not MANAGED.fullmatch(b):
            continue
        t = on.get(b)
        kind = "tab" if b.startswith(BRANCH_PREFIX) else "night"
        name = b[len(BRANCH_PREFIX):] if kind == "tab" else b[len(NIGHT_PREFIX):]
        path = t["path"] if t else None
        held = (path == main
                or (path is not None and _live[str(path)] > 0)
                # a tab whose tree moved branches still owns this one
                or (kind == "tab" and _live[str(repo / DIR / name)] > 0)
                or bool(t and t["locked"] and _lock_alive(t["locked"])))
        base = _base(repo, b)
        log = _out(repo, "log", f"--max-count={ORPHAN_COMMITS}",
                   "--format=%h%x1f%s%x1f%cr", f"{base}..{b}") or ""
        commits = [dict(zip(("short", "subject", "when"), ln.split("\x1f")))
                   for ln in log.splitlines() if ln.count("\x1f") == 2]
        dirty = _dirty(path) if path is not None and path != main else []
        out.append({
            "branch": b, "name": name, "kind": kind, "repo": str(repo),
            "path": str(path) if path else None, "base": base,
            "merged": _merged(repo, b, base),
            "ahead": int(_out(repo, "rev-list", "--count", f"{base}..{b}") or 0),
            "behind": int(_out(repo, "rev-list", "--count", f"{b}..{base}") or 0),
            "dirty": dirty[:20], "dirty_count": len(dirty),
            "locked": bool(t and t["locked"]), "held": held, "commits": commits,
            "when": _out(repo, "log", "-1", "--format=%cr", b) or "",
        })
    return out


def orphans(repo: Path) -> list[dict]:
    """Unheld branches still holding work (commits beyond base, or uncommitted files)."""
    return [b for b in branches(repo)
            if not b["held"] and not (b["merged"] and not b["dirty"])]


def _drop(repo: Path, b: dict, force: bool = False) -> bool:
    """Remove a branch and its tree; without `force` git refuses a dirty tree."""
    if b["path"]:
        tree = Path(b["path"])
        _unlink(repo, tree)
        args = ["worktree", "remove"] + (["--force", "--force"] if force else [])
        r = _git(repo, *args, str(tree))
        if r.returncode:
            _setup(repo, tree)   # the tree stays, so relink
            return False
    _git(repo, "branch", "-D", b["branch"])
    _git(repo, "config", "--remove-section", f"branch.{b['branch']}")
    return True


def status(tree: Path, repo: Path | None = None) -> dict:
    """Uncommitted files and commits beyond the base."""
    repo = repo or main_checkout(tree)
    info = _info(repo, tree)
    dirty = _dirty(tree)
    ahead = _out(tree, "rev-list", "--count", f"{info['base']}..HEAD")
    behind = _out(tree, "rev-list", "--count", f"HEAD..{info['base']}")
    return {**info, "dirty": dirty, "ahead": int(ahead or 0), "behind": int(behind or 0)}


def main_checkout(tree: Path) -> Path:
    common = _out(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        raise WorktreeError(f"{tree} isn't in a git repo")
    return Path(common).parent


def resolve(repo: Path, value: str | None) -> Path | None:
    """A browser-supplied worktree path → the path if managed, else None. The only way a request reaches one."""
    if not value:
        return None
    want = Path(value).resolve()
    for tree in _managed_trees(repo.resolve()):
        if tree == want:
            return tree
    return None


def _remove(repo: Path, tree: Path) -> bool:
    """Remove a tab's tree, and its own branch if merged (a moved tree can leave it holding work)."""
    _unlink(repo, tree)
    r = _git(repo, "worktree", "remove", str(tree))
    if r.returncode:
        # The tree stays: relink.
        _setup(repo, tree)
        return False
    branch = BRANCH_PREFIX + tree.name
    if _merged(repo, branch, _base(repo, branch)):
        _git(repo, "branch", "-D", branch)
        _git(repo, "config", "--remove-section", f"branch.{branch}")
    return True


def _empty(repo: Path, tree: Path) -> bool:
    s = status(tree, repo)
    return not s["dirty"] and s["ahead"] == 0


def sweep(repo: Path) -> list[str]:
    """Remove every orphan holding nothing (merged, clean); returns what was removed. Caller holds _lock."""
    repo = repo.resolve()
    _git(repo, "worktree", "prune")
    gone = []
    for b in branches(repo):
        if not b["held"] and b["merged"] and not b["dirty"] and _drop(repo, b):
            gone.append(b["branch"])
    for tree in _ours(repo):
        if _live[str(tree)] <= 0 and _empty(repo, tree) and _remove(repo, tree):
            gone.append(tree.name)
    return gone


def sweep_all(repos: list[Path]) -> dict[str, list[str]]:
    """`sweep` across repos (startup); a failing repo doesn't stop the rest."""
    out = {}
    for repo in repos:
        try:
            if eligible(repo):
                with _lock:
                    gone = sweep(repo)
                if gone:
                    out[str(repo)] = gone
        except (OSError, subprocess.SubprocessError):
            continue
    return out


def release(tree: Path) -> bool:
    """A socket using `tree` closed: remove it if last and empty, then sweep; True if removed."""
    key = str(tree)
    with _lock:
        _live[key] -= 1
        if _live[key] > 0:
            return False
        del _live[key]
        try:
            repo = main_checkout(tree)
            gone = _empty(repo, tree) and _remove(repo, tree)
            sweep(repo)
            return gone
        except (WorktreeError, OSError, subprocess.SubprocessError):
            return False
        finally:
            # Under _lock: a waiter never sees the gap between the count dropping and the removal.
            _released.notify_all()


def wait_released(tree: Path | str, timeout: float = 10.0) -> bool:
    """Block until no socket holds `tree` and its release() has swept, or `timeout`; True if so."""
    key = str(tree)
    with _released:
        return _released.wait_for(lambda: _live[key] <= 0, timeout)


def _orphan(repo: Path, branch: str) -> dict:
    if not MANAGED.fullmatch(branch or ""):
        raise WorktreeError(f"{branch!r} isn't a tab's or a --worktree session's branch")
    b = next((x for x in branches(repo) if x["branch"] == branch), None)
    if b is None:
        raise WorktreeError(f"no branch {branch} in {repo.name}")
    if b["held"]:
        raise WorktreeError(f"{branch} is in use (an open tab or a running session)")
    return b


def merge_orphan(repo: Path, branch: str, push: bool = False) -> dict:
    """Left-behind merge: `merge` in its tree, or (no tree) fast-forward if base hasn't moved; then sweep."""
    repo = repo.resolve()
    with _lock:
        b = _orphan(repo, branch)
    if b["path"]:
        res = merge(Path(b["path"]), push)
    elif b["behind"]:
        res = {"ok": False, "base": b["base"],
               "reason": f"{b['base']} moved on and {branch} has no worktree to rebase "
                         "in: open a dev tab on the repo to land it"}
    else:
        with _lock:
            on = _out(repo, "symbolic-ref", "-q", "--short", "HEAD")
            r = _git(repo, "merge", "--ff-only", "-q", branch, timeout=120) if on == b["base"] else None
        if r is None or r.returncode:
            res = {"ok": False, "base": b["base"],
                   "reason": f"the main checkout is on {on}, not {b['base']}" if r is None
                   else "the main checkout refused the fast-forward: "
                        + (r.stderr or r.stdout).strip()[-600:]}
        else:
            res = {"ok": True, "commits": b["ahead"], "base": b["base"], "files": [],
                   "head": _out(repo, "rev-parse", "--short", "HEAD")}
    if res.get("ok"):
        with _lock:
            res["swept"] = sweep(repo)
    return res


def discard(repo: Path, branch: str) -> dict:
    """Left-behind discard: the branch and tree, uncommitted files too. Refuses one in use."""
    repo = repo.resolve()
    with _lock:
        b = _orphan(repo, branch)
        # Forced twice: past uncommitted files, and a dead holder's lock.
        ok = _drop(repo, b, force=True)
    lost = f"{b['ahead']} commit{'s' * (b['ahead'] != 1)}" + (
        f" and {b['dirty_count']} uncommitted file{'s' * (b['dirty_count'] != 1)}"
        if b["dirty_count"] else "")
    return {"ok": ok, "text": f"discarded {branch} ({lost})" if ok
            else f"git wouldn't remove {b['path']}"}


REVIEW_COMMITS = 50
CONFLICT_FILES = 20
CONFLICT_REGIONS = 12
CONFLICT_LINES = 60
CONFLICT_CONTEXT = 3


def review(tree: Path) -> dict:
    """`status` plus the commits `merge` would land (newest first), their diff from the
    merge-base, and `conflicts` (empty unless base moved and a merge would stop). Read-only."""
    from . import console_diff   # lazy: session_start.py loads this file stdlib-only
    tree = tree.resolve()
    s = status(tree, main_checkout(tree))
    base = s["base"]
    mb = _out(tree, "merge-base", base, "HEAD")
    log = _out(tree, "log", f"--max-count={REVIEW_COMMITS}",
               "--format=%H%x1f%h%x1f%s%x1f%cr", f"{base}..HEAD") or ""
    commits = [dict(zip(("sha", "short", "subject", "when"), ln.split("\x1f")))
               for ln in log.splitlines() if ln.count("\x1f") == 3]
    return {**s, "merge_base": mb, "commits": commits,
            "files": console_diff.commit_diff(tree, mb) if mb and s["ahead"] else [],
            "conflicts": preview_conflicts(tree, base, s["branch"])
            if s["ahead"] and s["behind"] else []}


def preview_conflicts(tree: Path, base: str, branch: str) -> list[dict]:
    """[{path, kind, message, regions}] the merge would stop on, via `merge-tree --write-tree`
    (object store only; nothing moves). A preview: `merge` rebases commit by commit, and its answer counts."""
    r = _git(tree, "merge-tree", "--write-tree", "-z", "--name-only", base, branch)
    if r.returncode != 1:            # 0 is a clean merge; anything else can't say
        return []
    parts = r.stdout.split("\0")
    oid, i, paths = parts[0], 1, []
    while i < len(parts) and parts[i]:
        paths.append(parts[i])
        i += 1
    # Then one record per message: <n>\0<path>×n\0<kind>\0<text>\0
    notes: dict[str, list[tuple[str, str]]] = {}
    i += 1
    while i < len(parts) and parts[i].isdigit():
        n = int(parts[i])
        names, rest = parts[i + 1:i + 1 + n], parts[i + 1 + n:i + 3 + n]
        i += n + 3
        if len(rest) == 2 and rest[0].startswith("CONFLICT"):
            kind = re.search(r"\(([^)]*)\)", rest[0])
            for p in names:
                notes.setdefault(p, []).append((kind.group(1) if kind else "", rest[1].strip()))
    out = []
    for p in list(dict.fromkeys(paths))[:CONFLICT_FILES]:
        said = notes.get(p) or [("", "")]
        out.append({"path": p, "kind": said[0][0],
                    "message": " ".join(t for _, t in said if t),
                    "regions": _conflict_regions(tree, oid, p, base, branch)})
    return out


def _conflict_regions(tree: Path, oid: str, path: str, base: str, branch: str) -> list[dict]:
    """The conflicted hunks in the merged file, with context; markers matched with their
    labels. Empty for non-line conflicts (modify/delete, binary)."""
    from . import console_diff
    text = console_diff.blob(tree, oid, path)
    if not text:
        return []
    lines = text.split("\n")
    bare = [ln.removesuffix("\r") for ln in lines]   # a CRLF file's markers end in \r too
    start, end = f"<<<<<<< {base}", f">>>>>>> {branch}"
    regions, i, prev = [], 0, 0
    while i < len(lines) and len(regions) < CONFLICT_REGIONS:
        if bare[i] != start:
            i += 1
            continue
        sides: dict[str, list[str] | None] = {"ours": [], "ancestor": None, "theirs": []}
        side, j = "ours", i + 1
        while j < len(lines) and not (side == "theirs" and bare[j] == end):
            if side == "ours" and bare[j].startswith("||||||| "):
                side, sides["ancestor"] = "ancestor", []
            elif side != "theirs" and bare[j] == "=======":
                side = "theirs"
            else:
                sides[side].append(lines[j])
            j += 1
        if j >= len(lines):
            break                    # never closed: nothing honest to show
        after = []
        for k in range(j + 1, min(len(lines), j + 1 + CONFLICT_CONTEXT)):
            if bare[k] == start:     # the next region's, not context
                break
            after.append(lines[k])
        regions.append({
            "line": i + 1,
            "before": lines[max(prev, i - CONFLICT_CONTEXT):i],
            "after": after,
            "cut": any(len(v) > CONFLICT_LINES for v in sides.values() if v),
            **{k: (v[:CONFLICT_LINES] if v is not None else None) for k, v in sides.items()},
        })
        i = prev = j + 1
    return regions


def merge(tree: Path, push: bool = False) -> dict:
    """Land the tab's branch on its base: rebase it in the tab's tree, then
    fast-forward the main checkout. Never leaves either half-merged.

    Returns {"ok": True, "commits", "files", "head", "pushed"} on success,
    {"ok": True, "commits": 0} when there was nothing to merge, or
    {"ok": False, "reason", ...} with `conflicts` (files) when the rebase
    stopped on one, or `dirty` when there's uncommitted work."""
    tree = tree.resolve()
    with _lock:
        repo = main_checkout(tree)
        s = status(tree, repo)
        base, branch = s["base"], s["branch"]
        if s["dirty"]:
            return {"ok": False, "dirty": s["dirty"][:40],
                    "reason": "uncommitted changes in the tab's worktree: commit "
                              "them (or discard them) first"}
        if s["ahead"] == 0:
            return {"ok": True, "commits": 0, "base": base,
                    "note": f"nothing to merge: {branch} has no commits beyond {base}"}
        if s["behind"]:
            r = _git(tree, "rebase", base, timeout=120)
            if r.returncode:
                conflicts = (_out(tree, "diff", "--name-only", "--diff-filter=U") or "").splitlines()
                _git(tree, "rebase", "--abort")
                return {"ok": False, "conflicts": conflicts, "base": base,
                        "reason": f"{base} moved on and your commits conflict with it. "
                                  f"In your worktree run `git rebase {base}`, resolve "
                                  "each file, `git add` it, `git rebase --continue`, "
                                  "re-run the checks, then merge again. Nothing was "
                                  "changed in the main checkout."}
        on = _out(repo, "symbolic-ref", "-q", "--short", "HEAD")
        if on != base:
            return {"ok": False, "base": base,
                    "reason": f"the main checkout is on {on or 'a detached HEAD'}, "
                              f"not {base}, so it can't be fast-forwarded"}
        files = (_out(repo, "diff", "--name-only", f"{base}..{branch}") or "").splitlines()
        commits = int(_out(repo, "rev-list", "--count", f"{base}..{branch}") or 0)
        # A merge adding a migration to Kestrel's own app.db: snapshot first,
        # or don't merge (merge_guard.py). Imported here: session_start.py
        # loads this file by path, with no package around it.
        from . import merge_guard
        try:
            guard = merge_guard.before_ff(repo, base, branch)
        except (OSError, sqlite3.Error) as e:
            return {"ok": False, "base": base,
                    "reason": f"this merge adds a migration and app.db couldn't be backed "
                              f"up first ({e!r}), so it wasn't merged"}
        r = _git(repo, "merge", "--ff-only", "-q", branch, timeout=120)
        if r.returncode:
            return {"ok": False, "base": base,
                    "reason": "the main checkout refused the fast-forward (usually "
                              "uncommitted edits there to the same files): "
                              + (r.stderr or r.stdout).strip()[-600:]}
        out = {"ok": True, "commits": commits, "files": files, "base": base,
               "head": _out(repo, "rev-parse", "--short", "HEAD")}
        if guard:
            held = merge_guard.arm(guard, repo, base, _out(repo, "rev-parse", "HEAD") or "", push)
            out["guard"] = merge_guard.describe(guard, held)
            if held:
                push = False
                out["pushed"] = False
                out["push_note"] = "held until the reloaded backend passes the merge guard's check"
    if push:
        upstream = _out(repo, "rev-parse", "--abbrev-ref", f"{base}@{{upstream}}")
        if not upstream:
            out["pushed"] = False
            out["push_note"] = f"{base} has no upstream to push to"
        else:
            remote = upstream.split("/", 1)[0]
            r = _git(repo, "push", "-q", remote, base, timeout=120)
            out["pushed"] = r.returncode == 0
            if r.returncode:
                out["push_note"] = (r.stderr or r.stdout).strip()[-400:]
    return out


def describe_merge(res: dict) -> str:
    """A merge result as one paragraph, for the tool and the UI."""
    if not res.get("ok"):
        text = res["reason"]
        if res.get("conflicts"):
            text += "\nConflicting files: " + ", ".join(res["conflicts"])
        if res.get("dirty"):
            text += "\nUncommitted: " + ", ".join(res["dirty"])
        return text
    if not res.get("commits"):
        return res.get("note") or "nothing to merge"
    text = (f"merged {res['commits']} commit{'s' * (res['commits'] != 1)} into "
            f"{res['base']} (now {res['head']}), {len(res['files'])} file"
            f"{'s' * (len(res['files']) != 1)}")
    if "pushed" in res:
        text += "; pushed" if res["pushed"] else f"; not pushed: {res.get('push_note')}"
    if res.get("guard"):
        text += f"\n{res['guard']}"
    return text


def start_line(info: dict) -> str:
    """The worktree's part of the session line. Other tabs' leftovers aren't
    in it: they're the plan view's left-behind panel's (`orphans`)."""
    rel = os.path.relpath(info["path"], info["repo"])
    line = f" · worktree {rel} on {info['branch']} (from {info['base']})"
    for n in info.get("notes") or []:
        line += f" · ⚠ {n}"
    return line
