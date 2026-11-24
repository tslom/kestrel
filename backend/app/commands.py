"""Slash-command discovery for the console, zero tokens: scans ~/.claude and
VAULT_PATH/.claude for commands/*.md and skills/*/SKILL.md (frontmatter only) for
`GET /api/commands`. The SDK init message stays the authority on what a live
session loaded (built-ins like /compact); the frontend merges both.
"""

from pathlib import Path

import frontmatter

from .config import VAULT_PATH

DESCRIPTION_CAP = 300  # skills can carry essay-length descriptions; trim for UI


def _meta(path: Path) -> dict:
    try:
        return frontmatter.load(path).metadata or {}
    except Exception:
        return {}


def _skill_files(skills_dir: Path):
    """*/SKILL.md, tolerating lowercase skill.md (old-vault convention)."""
    seen: set[Path] = set()
    for pattern in ("*/SKILL.md", "*/skill.md"):
        for f in skills_dir.glob(pattern):
            if f.parent not in seen:
                seen.add(f.parent)
                yield f


def scan_commands() -> list[dict]:
    """[{name, description, argument_hint, scope, kind}] — project shadows user."""
    found: dict[str, dict] = {}
    sources = (("user", Path.home() / ".claude"),
               ("project", VAULT_PATH / ".claude"))
    for scope, root in sources:
        cdir = root / "commands"
        if cdir.is_dir():
            for f in sorted(cdir.rglob("*.md")):
                name = ":".join(f.relative_to(cdir).with_suffix("").parts)
                meta = _meta(f)
                found[name] = {
                    "name": name,
                    "description": str(meta.get("description", "")).strip()[:DESCRIPTION_CAP],
                    "argument_hint": str(meta.get("argument-hint", "")).strip(),
                    "scope": scope,
                    "kind": "command",
                }
        sdir = root / "skills"
        if sdir.is_dir():
            for f in _skill_files(sdir):
                meta = _meta(f)
                name = str(meta.get("name") or f.parent.name).strip()
                found[name] = {
                    "name": name,
                    "description": str(meta.get("description", "")).strip()[:DESCRIPTION_CAP],
                    "argument_hint": str(meta.get("argument-hint", "")).strip(),
                    "scope": scope,
                    "kind": "skill",
                }
    return sorted(found.values(), key=lambda e: e["name"])
