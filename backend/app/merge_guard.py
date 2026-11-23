"""The merge guard: back up app.db before a merge that adds a migration,
check the reloaded backend, and roll both back if it doesn't come up.

app.db holds things nothing can rebuild: alarms (the only handle on an item
sitting on the phone), location history, tasks. A migration is the one kind
of merge that rewrites it, and the monthly snapshot (backup.py) can be weeks
old. So when a dev tab's worktree_merge lands new entries in
`db.MIGRATIONS` on Kestrel's own repo, three things happen:

  1. **before the fast-forward** (`before_ff`, called by console_worktree.merge):
     a snapshot, `backups/premerge-<head>-<time>.db`. If it can't be taken the
     merge is refused: an unbacked migration is what this exists to stop.
  2. **a marker** (`backups/merge-check.json`, `arm`): the heads before and
     after, the snapshot, whether to push. When the launcher supervises the
     server (GUARD_ENV set) the push waits for the check: a merge that rolls
     back must not already be on origin.
  3. **at the restart** (launch_app.Backend.restart, `verify_restart`): the old
     server stopped, so nothing is writing, the launcher takes a fresh
     snapshot (so a rollback loses nothing written since the merge) and
     starts the new code. It must answer `/api/healthz?db=1`, which opens
     app.db and so runs the migration. If it doesn't within HEALTH_S, the
     server is stopped, the main checkout reset to the old head (`git reset
     --keep`, which refuses to overwrite uncommitted edits), app.db restored
     from the snapshot, and the old code started again. The tab's branch keeps
     its commits, so nothing is lost: fix, re-merge.

The result goes to `backups/merge-check-last.json` and a banner. Read that
file in a later turn to learn how a merge went: within the merging turn the
server is still the old one (the reloader holds the restart until turns end).

A server run without the launcher (terminal uvicorn) gets steps 1 and 2 only,
and pushes at once: there is nobody to run step 3.

Stdlib at import: the launcher imports this, and it's the one process that
must not fail to.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

GUARD_ENV = "KESTREL_MERGE_GUARD"
APP_REPO = Path(__file__).resolve().parents[2]
DB_FILE = "backend/app/db.py"
MARKER = "merge-check.json"
LAST = "merge-check-last.json"
KEEP = 5
HEALTH_S = 90.0


def _backups() -> Path:
    from . import backup
    return backup.backups_dir()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=120)


def migration_prints(repo: Path, rev: str) -> set[str] | None:
    """Fingerprints of `db.MIGRATIONS` as of `rev`, or None when the file or
    the list can't be read (then the guard assumes a migration changed)."""
    from . import db
    r = _git(repo, "show", f"{rev}:{DB_FILE}")
    if r.returncode:
        return None
    try:
        tree = ast.parse(r.stdout)
    except SyntaxError:
        return None
    for node in tree.body:
        target = (node.target if isinstance(node, ast.AnnAssign)
                  else node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
        if isinstance(target, ast.Name) and target.id == "MIGRATIONS":
            value = node.value
            if not isinstance(value, ast.List):
                return None
            out = set()
            for el in value.elts:
                if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
                    return None
                out.add(db.fingerprint(el.value))
            return out
    return None


def new_migrations(repo: Path, base: str, branch: str) -> int:
    """How many migrations `branch` adds over `base` (-1: couldn't tell, but
    db.py changed)."""
    changed = _git(repo, "diff", "--name-only", f"{base}..{branch}", "--", DB_FILE).stdout.strip()
    if not changed:
        return 0
    old, new = migration_prints(repo, base), migration_prints(repo, branch)
    if old is None or new is None:
        return -1
    return len(new - old)


def snapshot(tag: str) -> Path:
    from . import backup
    d = _backups()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"premerge-{tag}-{datetime.now():%Y%m%d-%H%M%S}.db"
    n = 1
    while dest.exists():
        dest = d / f"premerge-{tag}-{datetime.now():%Y%m%d-%H%M%S}-{n}.db"
        n += 1
    backup.copy_to(dest)
    for old in sorted(d.glob("premerge-*.db"), key=lambda p: p.stat().st_mtime)[:-KEEP]:
        old.unlink(missing_ok=True)
    return dest


def before_ff(repo: Path, base: str, branch: str) -> dict | None:
    """Called with the merge lock held, before the fast-forward. None: not
    this repo, or no migration. Else the guard's state, snapshot taken.
    Raises OSError / sqlite3.Error if the snapshot fails."""
    if repo.resolve() != APP_REPO:
        return None
    n = new_migrations(repo, base, branch)
    if n == 0:
        return None
    old_head = _git(repo, "rev-parse", base).stdout.strip()
    snap = snapshot(old_head[:8])
    return {"old_head": old_head, "migrations": n, "snapshot": str(snap)}


def supervised() -> bool:
    return os.environ.get(GUARD_ENV) == "1"


def arm(guard: dict, repo: Path, base: str, new_head: str, push: bool) -> bool:
    """Write the marker for the launcher. True when the push should wait for
    the check."""
    hold = push and supervised()
    marker = {**guard, "repo": str(repo), "base": base, "new_head": new_head,
              "push": hold, "at": datetime.now().isoformat(timespec="seconds")}
    (_backups() / MARKER).write_text(json.dumps(marker, indent=1))
    return hold


def describe(guard: dict, held: bool) -> str:
    n = guard["migrations"]
    what = "a change to db.MIGRATIONS" if n < 0 else f"{n} new migration{'s' * (n != 1)}"
    text = f"merge guard: {what}; app.db backed up to {Path(guard['snapshot']).name}"
    if supervised():
        text += ("; when the backend reloads (after this turn) the launcher checks it and "
                 "rolls code and app.db back if it fails — read "
                 f"backend/data/backups/{LAST} in a later turn for the result")
        if held:
            text += "; the push waits for that check"
    else:
        text += "; no launcher here to check the reload, so no automatic rollback"
    return text


# --- the launcher's half ----------------------------------------------------

def pending() -> dict | None:
    try:
        return json.loads((_backups() / MARKER).read_text())
    except (OSError, ValueError):
        return None


def refresh_snapshot(check: dict) -> dict:
    """With the old server stopped: a newer snapshot, so a rollback keeps
    what was written between the merge and the restart. The merge-time one
    stays if this fails."""
    try:
        fresh = snapshot(check["old_head"][:8])
    except Exception as e:  # the merge-time snapshot still stands
        print(f"[merge-guard] fresh snapshot failed, keeping the first: {e!r}", flush=True)
        return check
    first = Path(check["snapshot"])
    if first != fresh:
        first.unlink(missing_ok=True)
    return {**check, "snapshot": str(fresh)}


def restore(snapshot_path: Path, db_path: Path) -> None:
    """Put the snapshot back as app.db. The server must be stopped. The WAL
    and shared-memory files go first: they belong to the database being
    replaced, and SQLite would otherwise replay the failed migration's
    pages over the restored file."""
    from . import backup
    part = db_path.with_name(db_path.name + ".restore")
    part.unlink(missing_ok=True)
    backup.copy_to(part, src_path=snapshot_path)
    for ext in ("-wal", "-shm"):
        Path(str(db_path) + ext).unlink(missing_ok=True)
    os.replace(part, db_path)


def rollback_code(check: dict) -> str | None:
    """Reset the main checkout to the head before the merge. None on
    success, else why not."""
    repo = Path(check["repo"])
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != check["new_head"]:
        return f"main moved on since the merge (now {head[:8]}); left it alone"
    r = _git(repo, "reset", "--keep", check["old_head"])
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-300:]


def settle(check: dict, ok: bool, detail: str = "") -> dict:
    """Record the outcome, push if the merge held and asked to, clear the
    marker."""
    result = {**check, "ok": ok, "detail": detail,
              "checked_at": datetime.now().isoformat(timespec="seconds")}
    if ok and check.get("push"):
        repo = Path(check["repo"])
        up = _git(repo, "rev-parse", "--abbrev-ref", f"{check['base']}@{{upstream}}").stdout.strip()
        if up:
            r = _git(repo, "push", "-q", up.split("/", 1)[0], check["base"])
            result["pushed"] = r.returncode == 0
            if r.returncode:
                result["push_note"] = (r.stderr or r.stdout).strip()[-300:]
    d = _backups()
    (d / LAST).write_text(json.dumps(result, indent=1))
    (d / MARKER).unlink(missing_ok=True)
    return result


def verify_restart(check: dict, start, stop, healthy, db_path: Path, log=print) -> tuple[object, dict]:
    """The launcher's check, with its own process handling passed in:
    start() → proc, stop(proc), healthy(proc) → bool. Returns (the proc now
    serving, the settled result)."""
    proc = start()
    if healthy(proc):
        res = settle(check, True, "the reloaded backend answered with app.db open")
        log(f"[merge-guard] {check['new_head'][:8]} came up healthy"
            + (" — pushed" if res.get("pushed") else ""))
        return proc, res
    log(f"[merge-guard] {check['new_head'][:8]} failed its health check — rolling back")
    stop(proc)
    why = rollback_code(check)
    if why:
        # Code can't go back, so app.db stays: restoring it under the new
        # code would only re-run the migration that just failed.
        res = settle(check, False, f"health check failed; code not rolled back: {why}; "
                                   f"app.db left as is, snapshot at {check['snapshot']}")
        return start(), res
    restore(Path(check["snapshot"]), db_path)
    proc = start()
    back = healthy(proc)
    res = settle(check, False,
                 f"health check failed; main reset to {check['old_head'][:8]} and app.db "
                 f"restored from {Path(check['snapshot']).name}"
                 + ("" if back else "; the old code didn't come up either — look at the log"))
    return proc, res
