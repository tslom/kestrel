"""Each dev tab in its own git worktree, on its own branch.

Two dev tabs sharing one checkout sweep up each other's edits with a `git
stash` and leave the tree unbuildable. A tab's own worktree ends that: a
stash, reset or checkout in one can't reach another's files. JetBrains Air does the same (`.air/worktree.json`); this copies its
model, as `.kestrel/worktree.json`.

- **Where:** `<repo>/.claude/worktrees/tab-<hex>`, on branch `kestrel/tab-<hex>`
  cut from the branch the main checkout is on (its *base*, kept in git config
  as `branch.<branch>.kestrelbase`). The directory is listed in the repo's
  `.git/info/exclude`, so the main checkout's status and rg never see it.
- **Setup:** what `.kestrel/worktree.json` names. `link` symlinks gitignored
  paths from the main checkout (a venv, node_modules, the app's data dir: one
  copy of each, and for data, one source of truth); `setup` runs commands in
  the new tree. With no file, `.claude/settings.local.json` is linked, the
  allow-rules a dev tab already reads.
- **What runs:** the backend and frontend keep serving the main checkout. A
  tab's work goes live when `merge` fast-forwards the base branch there.
- **Merging back:** `merge` rebases the tab's branch onto the base in the
  tab's own tree, then fast-forwards the main checkout. A conflict never
  reaches the main checkout: the rebase is aborted and the files are handed
  back to the tab, which resolves them where its change lives.
- **Reviewing first:** `review` is what the diff pane's branch view reads:
  the commits `merge` would land, their diff against where the branch left
  its base, and (once the base has moved) the files a merge would stop on,
  worked out in the object store by `git merge-tree` so neither tree moves.
- **A tab with nothing to keep** (clean tree, no commits beyond the base) has
  its worktree and branch removed when its socket closes. A resumed session gets its tree back at
  the same path (Claude Code files a session's transcript under its cwd, so
  the path is how a resume finds it, `for_session`); the memory stays the
  main checkout's, which Claude Code shares across worktrees.
- **Orphans** (`branches`, `sweep`): every `kestrel/tab-*` branch, and every
  `worktree-*` one Claude Code's own `--worktree` sessions (the night jobs)
  leave, whose tab or session is gone. On startup, on a tab's close and when
  a tree is made, the fully merged ones go (tree and branch); the rest are
  what the plan view's "left behind" panel lists, with merge and discard.
  Gone means: no socket here holds the tree, and no live process holds its
  git lock (Claude Code locks a `--worktree` tree while its session runs,
  naming the pid). They are listed there, not as a ⚠ in whatever unrelated
  tab opens next on that repo.

Stdlib only, and importable by macOS's python3 (3.9): the SessionStart hook
(.claude/hooks/session_start.py) loads it outside the venv.
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
# The branches the orphan sweep may touch: a tab's, or a `claude --worktree`
# session's (its tree is `.claude/worktrees/<name>`, its branch worktree-<name>).
MANAGED = re.compile(r"kestrel/tab-[0-9a-f]{8}|worktree-[\w.-]+")
LOCK_PID = re.compile(r"\bpid (\d+)\b")
# KESTREL_DEV_WORKTREES=0 turns it off: dev tabs open on the main checkout.
ENABLED = os.environ.get("KESTREL_DEV_WORKTREES", "1") != "0"

_lock = threading.Lock()
# Worktrees a live socket is using. One that's in here is never removed.
_live: Counter[str] = Counter()
# Notified (under _lock) each time a release() has finished its removal and
# sweep, for wait_released.
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
    # Claude Code's project-directory name for a cwd: every non-alphanumeric
    # becomes "-" (console_profiles.memory_dir).
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def eligible(repo: Path) -> bool:
    """A repo a tab can branch from: the main worktree (not itself a linked
    one), on a branch, with a commit to branch from."""
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
    """The worktrees directory and every link, in .git/info/exclude (shared
    by all worktrees). Links need their own line: `.venv/` in .gitignore
    matches a directory, and a symlink to one is a file to git, so without
    this the tab's `git add -A` would commit it."""
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
    """Symlink the configured gitignored paths from the main checkout into
    `tree`, kept out of git. Returns the ones it linked; a path already there
    (or missing from the main checkout) is left alone. Also what
    .claude/hooks/session_start.py runs, for a worktree Kestrel didn't make."""
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
    """Links and setup commands. Returns what went wrong, for the start line:
    a tab whose venv didn't link can still edit, so this never refuses."""
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
    """Take the links out before git removes the tree, so nothing it does can
    follow one into the main checkout's data."""
    try:
        links = _config(repo)["link"]
    except WorktreeError:
        links = list(DEFAULT_LINKS)
    for rel in links:
        p = tree / rel
        if p.is_symlink():
            p.unlink()


def _base(repo: Path, branch: str) -> str:
    """Where a branch lands: the base it was cut from, else the branch the
    main checkout is on."""
    return _out(repo, "config", f"branch.{branch}.kestrelbase") or \
        _out(repo, "symbolic-ref", "--short", "HEAD") or "main"


def _info(repo: Path, tree: Path, notes: list[str] | None = None) -> dict:
    # The branch the tree is on, which isn't always the one it was made on: a
    # tab can check out another (a site repo's tab trees sit on try/* branches),
    # and merging the made-on one then lands the wrong commits.
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
    """A new tab's worktree. Sweeps the abandoned empty ones first."""
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
    """The worktree a session ran in, put back if it was removed. None: the
    session ran in the main checkout (it predates worktrees, or they were
    off), and resumes there."""
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
    """`git worktree list --porcelain` as [{path, branch, locked}], the main
    checkout first. `locked` is the lock's reason ("locked" if it gave none)."""
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
    """The tab worktrees, and the `claude --worktree` ones on their own
    worktree-<name> branch — the trees the left-behind panel can open."""
    root = (repo / DIR).resolve()
    return [t["path"] for t in _trees(repo) if t["path"].parent == root and (
        NAME.fullmatch(t["path"].name) or t["branch"] == NIGHT_PREFIX + t["path"].name)]


def _lock_alive(reason: str) -> bool:
    """Whether a worktree lock still has its holder. Claude Code's says
    "claude session <name> (pid N …)"; a lock that names no pid was put there
    by hand, on purpose, so it holds."""
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
    # No optional locks: status would otherwise refresh the index under
    # index.lock, and the tab may be committing in this tree right now.
    return [ln[3:] for ln in (_git(tree, "--no-optional-locks", "status", "--porcelain").stdout
                              or "").splitlines()]


ORPHAN_COMMITS = 5


def branches(repo: Path) -> list[dict]:
    """Every kestrel/tab-* and worktree-* branch in the repo and where it
    stands: {branch, name, kind ("tab"/"night"), path (its tree, or None),
    base, merged, ahead, behind, dirty, locked, held, commits}. `held`: a
    socket here or a live process (by its git lock) is using it, so nothing
    may touch it; the rest are orphans."""
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
                # a tab whose tree moved to another branch still owns this one
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
    """The branches whose tab or session is gone and which still hold work:
    commits their base doesn't have, or uncommitted files in their tree."""
    return [b for b in branches(repo)
            if not b["held"] and not (b["merged"] and not b["dirty"])]


def _drop(repo: Path, b: dict, force: bool = False) -> bool:
    """Remove a branch and its tree. Without `force` git refuses a tree with
    uncommitted or untracked files, which is the point."""
    if b["path"]:
        tree = Path(b["path"])
        _unlink(repo, tree)
        args = ["worktree", "remove"] + (["--force", "--force"] if force else [])
        r = _git(repo, *args, str(tree))
        if r.returncode:
            _setup(repo, tree)   # the tree stays, and so must its venv
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
    """A worktree path from the browser → the path, if it is one of this
    repo's tab worktrees (or a `--worktree` session's on its own branch).
    The only way a request reaches into one."""
    if not value:
        return None
    want = Path(value).resolve()
    for tree in _managed_trees(repo.resolve()):
        if tree == want:
            return tree
    return None


def _remove(repo: Path, tree: Path) -> bool:
    """Remove a tab's tree, and the tab's own branch if that's merged (a
    tree moved to another branch can leave its made-on one holding work)."""
    _unlink(repo, tree)
    r = _git(repo, "worktree", "remove", str(tree))
    if r.returncode:
        # Put the links back: the tree stays, and so must its venv.
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
    """Remove every orphan that holds nothing: a branch fully merged into its
    base with no uncommitted files in its tree, and tab trees on some other
    branch in the same state. Returns the branches (or trees) removed.
    Caller holds _lock."""
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
    """`sweep` across repos (startup). A repo that can't branch is skipped,
    and one that fails doesn't stop the rest."""
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
    """A socket using `tree` closed. Removes it if that was the last and it
    holds nothing, then sweeps the repo's other orphans; True if it was
    removed."""
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
            # Still under _lock, so a waiter sees the sweep's result, never
            # the moment between the count dropping and the removal.
            _released.notify_all()


def wait_released(tree: Path | str, timeout: float = 10.0) -> bool:
    """Block until no socket holds `tree` and its last release() has swept
    the repo, or `timeout` passes; True if it did. For the page that just
    closed a tab and wants the left-behind list as that close leaves it (its
    socket's close reaches release() on its own schedule)."""
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
    """The left-behind panel's merge: `merge` in the branch's tree, or, for a
    branch with no tree, a fast-forward of the main checkout when its base
    hasn't moved. Either way a clean landing only; then the sweep."""
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
    """The left-behind panel's discard: the branch and its tree, uncommitted
    files and all. Refuses one in use; the UI asks before it gets here."""
    repo = repo.resolve()
    with _lock:
        b = _orphan(repo, branch)
        # Forced twice: past uncommitted files, and past a lock whose holder
        # is dead (a live one made it `held`, which _orphan refused).
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
    """The tab's branch as the diff pane's review shows it: `status`, plus
    the commits `merge` would land (newest first), their diff against the
    commit the branch left its base at, and `conflicts` — empty unless the
    base moved on and a merge of the two would stop. Only reads: the tab may
    be mid-turn in this tree."""
    from . import console_diff   # here, not at the top: session_start.py loads
                                 # this file standalone, so its imports stay stdlib
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
    """The files merging `branch` into `base` would stop on, found without
    moving a tree or a ref: `merge-tree --write-tree` merges in the object
    store only. [{path, kind, message, regions}], `kind` git's own word
    ("contents", "modify/delete", …).

    A preview: `merge` rebases, replaying commit by commit, so it can stop on
    a change a later commit undid. Its answer is the one that counts."""
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
    """The conflicted hunks git wrote into the merged file: the base's lines
    and the tab's (and the ancestor's, under a diff3 conflictStyle), a little
    context either side. The markers are matched with their labels, so a
    line that merely starts with <<<<<<< isn't one. Empty for a conflict
    that isn't about lines (modify/delete, binary)."""
    from . import console_diff   # see review()
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
