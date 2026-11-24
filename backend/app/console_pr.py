"""PR watch: a dev tab linked to its pull request, woken when the PR moves.

Stateless: the console holds the last snapshot per tab and sends it with each
poll; `poll` returns the new snapshot and the notes between the two. `gh` does the
talking, run in the tab's folder so `gh pr view` finds the branch's PR.
Design: DESIGN.md → Dev tabs: project scripts, a port per worktree, PR watch.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

GH_TIMEOUT = 20
FIELDS = ("number,url,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
          "headRefName,headRefOid,baseRefName,statusCheckRollup,reviews,comments")
BODY_CHARS = 280
# A number, `#12`, a github.com URL, or a head branch (no leading "-": gh reads it as a flag).
REF = re.compile(r"#?\d{1,7}|https://github\.com/[\w.-]+/[\w.-]+/pull/\d{1,7}|\w[\w./-]{0,120}")

# Checks fold to pass/fail/pending; neutral/skipped count as passing (a skipped job isn't a failure).
_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
         "STARTUP_FAILURE", "STALE"}
_PENDING_STATUS = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}

_viewer: str | None = None


class PrError(RuntimeError):
    pass


def _gh(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=GH_TIMEOUT)
    except FileNotFoundError:
        raise PrError("gh isn't installed")
    except subprocess.TimeoutExpired:
        raise PrError("gh timed out")
    if r.returncode:
        said = (r.stderr or r.stdout).strip().splitlines()
        raise PrError(said[-1][:300] if said else f"gh exited {r.returncode}")
    return r.stdout


def viewer(cwd: Path) -> str:
    """The logged-in GitHub user; their own comments don't wake the tab (it would wake itself)."""
    global _viewer
    if _viewer is None:
        try:
            _viewer = _gh(cwd, "api", "user", "-q", ".login").strip()
        except PrError:
            return ""
    return _viewer


def _check_state(c: dict) -> str:
    """One rollup entry (a CheckRun or a legacy StatusContext) as pass/fail/pending."""
    if c.get("__typename") == "StatusContext":
        s = str(c.get("state") or "").upper()
        return "fail" if s in ("FAILURE", "ERROR") else "pending" if s in ("PENDING", "EXPECTED") else "pass"
    if str(c.get("status") or "").upper() in _PENDING_STATUS:
        return "pending"
    return "fail" if str(c.get("conclusion") or "").upper() in _FAIL else "pass"


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= BODY_CHARS else text[:BODY_CHARS - 1].rstrip() + "…"


def shape(raw: dict) -> dict:
    """gh's JSON cut to what the notes are worked out from and the chip shows."""
    checks = {}
    for c in raw.get("statusCheckRollup") or []:
        name = str(c.get("name") or c.get("context") or "check")
        checks[name] = _check_state(c)
    states = set(checks.values())
    verdict = ("none" if not checks else "pending" if "pending" in states
               else "fail" if "fail" in states else "pass")
    reviews = [{"id": str(r.get("id") or r.get("submittedAt") or i),
                "author": str((r.get("author") or {}).get("login") or "someone"),
                "state": str(r.get("state") or "COMMENTED").upper(),
                "body": _clip(r.get("body"))}
               for i, r in enumerate(raw.get("reviews") or [])]
    comments = [{"id": str(c.get("id") or c.get("createdAt") or i),
                 "author": str((c.get("author") or {}).get("login") or "someone"),
                 "body": _clip(c.get("body"))}
                for i, c in enumerate(raw.get("comments") or [])]
    return {
        "number": int(raw.get("number") or 0), "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""), "state": str(raw.get("state") or "OPEN").upper(),
        "draft": bool(raw.get("isDraft")), "head": str(raw.get("headRefName") or ""),
        "base": str(raw.get("baseRefName") or ""), "sha": str(raw.get("headRefOid") or ""),
        "mergeable": str(raw.get("mergeable") or "UNKNOWN").upper(),
        "review": str(raw.get("reviewDecision") or "").upper(),
        "checks": checks, "verdict": verdict,
        "reviews": reviews, "comments": comments,
    }


def changes(before: dict | None, after: dict, me: str = "") -> list[str]:
    """The notes between two snapshots, each a line the tab is woken with.
    None before: the link was just made, so nothing is news yet."""
    if not before or before.get("number") != after["number"]:
        return []
    notes = []
    if after["state"] != before.get("state"):
        if after["state"] == "MERGED":
            notes.append(f"PR #{after['number']} was merged into {after['base'] or 'its base'}.")
        elif after["state"] == "CLOSED":
            notes.append(f"PR #{after['number']} was closed without merging.")
        elif after["state"] == "OPEN":
            notes.append(f"PR #{after['number']} was reopened.")
    # A settled verdict that differs from the last one for this commit; a new push resets
    # checks, so a pass after a push is news even if the previous commit passed.
    same = after["sha"] == before.get("sha")
    if after["verdict"] in ("pass", "fail") and (not same or after["verdict"] != before.get("verdict")):
        if after["verdict"] == "fail":
            bad = sorted(n for n, s in after["checks"].items() if s == "fail")
            notes.append(f"Checks failed on {after['sha'][:7]}: {', '.join(bad)}. "
                         "`gh pr checks` and `gh run view --log-failed` show why.")
        elif not same or before.get("verdict") in ("pending", "fail", "none"):
            notes.append(f"Checks passed on {after['sha'][:7]} ({len(after['checks'])}).")
    seen = {r["id"] for r in before.get("reviews") or []}
    for r in after["reviews"]:
        if r["id"] in seen or (me and r["author"] == me):
            continue
        verb = {"APPROVED": "approved it", "CHANGES_REQUESTED": "requested changes",
                "DISMISSED": "had a review dismissed"}.get(r["state"], "reviewed it")
        notes.append(f"{r['author']} {verb}" + (f": “{r['body']}”" if r["body"] else
                                                " (inline comments: `gh api repos/{owner}/{repo}/"
                                                f"pulls/{after['number']}/comments`)."))
    seen = {c["id"] for c in before.get("comments") or []}
    for c in after["comments"]:
        if c["id"] not in seen and not (me and c["author"] == me):
            notes.append(f"{c['author']} commented: “{c['body']}”")
    if after["mergeable"] == "CONFLICTING" and before.get("mergeable") != "CONFLICTING":
        notes.append(f"PR #{after['number']} now conflicts with {after['base'] or 'its base'}: "
                     "rebase onto it and resolve.")
    return notes


def final(snap: dict) -> bool:
    return snap["state"] in ("MERGED", "CLOSED")


def _ref(value: Any) -> str:
    ref = str(value or "").strip()
    if ref and not REF.fullmatch(ref):
        raise PrError("name the PR by number or its github.com URL")
    return ref.lstrip("#")


def snapshot(folder: Path, ref: Any = "") -> dict:
    """The PR now. No ref: the one for the branch `folder` is on."""
    args = ["pr", "view"] + ([r] if (r := _ref(ref)) else []) + ["--json", FIELDS]
    try:
        raw = json.loads(_gh(folder, *args))
    except ValueError as e:
        raise PrError(f"gh's answer didn't parse: {e}")
    return shape(raw)


def poll(folder: Path, ref: Any, last: dict | None) -> dict:
    snap = snapshot(folder, ref or (last or {}).get("url") or "")
    notes = changes(last, snap, viewer(folder) if last else "")
    return {"pr": snap, "notes": notes, "final": final(snap)}


async def poll_async(folder: Path, ref: Any, last: dict | None) -> dict:
    return await asyncio.to_thread(poll, folder, ref, last)
