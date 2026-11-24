"""PostToolUse hook: ruff (backend) / eslint errors (frontend/src) on the edited file, then
comment_checks.py; exit 2 hands the errors back inside the same turn. Per-repo on purpose:
console sessions load the global settings. A missing linter passes silently.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comment_checks  # a sibling file, found through the path set just above

ROOT = Path(__file__).resolve().parents[2]   # the checkout (or worktree) this is in
TIMEOUT = 30


def lint(path: Path) -> tuple[int, str]:
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return 0, ""
    parts = rel.parts
    if parts[0] == "backend" and path.suffix == ".py":
        tool, cwd = ROOT / "backend" / ".venv" / "bin" / "ruff", ROOT / "backend"
        cmd = [str(tool), "check", "--quiet", "--force-exclude", "--output-format", "concise",
               str(path)]
    elif parts[:2] == ("frontend", "src") and path.suffix in (".js", ".jsx"):
        tool, cwd = ROOT / "frontend" / "node_modules" / ".bin" / "eslint", ROOT / "frontend"
        cmd = [str(tool), "--quiet", "--format", "stylish", str(path)]
    else:
        return 0, ""
    if not tool.exists():
        return 0, ""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return 0, ""
    return r.returncode, (r.stdout + r.stderr).strip()


def _at_head(rel: str) -> str:
    try:
        r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def check_comments(path: Path) -> str:
    """What comment_checks finds in this file, as an error message ("" if clean)."""
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
        text = path.read_text()
    except (ValueError, OSError, UnicodeDecodeError):
        return ""
    if not comment_checks.in_scope(rel):
        return ""
    out = []
    for ln, c in comment_checks.bad_noqa(path, text):
        out.append(f"{rel}:{ln} noqa without a code and reason: {c}")
    if out:
        out.append(comment_checks.NOQA_FIX)
    head = _at_head(rel)
    now = comment_checks.dated(path, text)
    before = comment_checks.dated(path, head)
    if len(now) > len(before):
        old = {c for _, c in before}
        new = [f"{rel}:{ln} {c}" for ln, c in now if c not in old] or [f"{rel}: {len(now) - len(before)} more"]
        out += [f"dated comment added (the file has {len(now)}, HEAD had {len(before)}):"] + new
        out.append(comment_checks.DATED_FIX)
    longs = comment_checks.long_blocks(path, text)
    longs_before = comment_checks.long_blocks(path, head)
    if len(longs) > len(longs_before):
        old = {c for _, _, c in longs_before}
        new = [f"{rel}:{ln} {n} lines: {c[:60]}" for ln, n, c in longs if c not in old]
        out += [f"comment block over {comment_checks.MAX_BLOCK} lines added:"] + new
        out.append(comment_checks.LENGTH_FIX)
    n, ceiling = comment_checks.comment_lines(path, text), comment_checks.allowed_lines(rel, text)
    if n > ceiling:
        out += [f"{rel} has {n} comment lines; its ceiling is {ceiling} (comments_baseline.json). "
                "Cut a comment to make room, or move the reasoning to DESIGN.md. "
                "Raising the ceiling is a decision to state in the commit message.",
                comment_checks.LENGTH_FIX]
    return "\n".join(out)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    file_path = (event.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0
    code, out = lint(Path(file_path))
    comments = check_comments(Path(file_path))
    if code == 0 and not comments:
        return 0
    if code:
        print(f"Lint errors in {file_path} (fix them now; the rules and why are in "
              f"backend/ruff.toml / frontend/eslint.config.js):\n{out}", file=sys.stderr)
    if comments:
        print(f"Comment rules (.claude/hooks/comment_checks.py), fix them now:\n{comments}",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
