"""No reaching into another module's `_private` names.

No allowlist: make the name public or move the helper. Two forms count:
`from .x import _y`, and `x._y` on a module imported from app.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _internal(node: ast.ImportFrom) -> bool:
    return bool(node.level) or (node.module or "").split(".")[0] == "app"


def scan() -> tuple[set[str], set[str]]:
    imports, attributes = set(), set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        tree = ast.parse(path.read_text())
        modules = set()     # names bound to app modules here
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _internal(node):
                for a in node.names:
                    if _private(a.name):
                        imports.add(f"{rel}: {'.' * node.level}{node.module or ''}.{a.name}")
                    else:
                        modules.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == "app":
                        modules.add(a.asname or a.name.split(".")[0])
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in modules and _private(node.attr)):
                attributes.add(f"{rel}: {node.value.id}.{node.attr}")
    return imports, attributes


def test_no_private_names_across_modules():
    imports, attributes = scan()
    found = sorted(imports | attributes)
    assert not found, (
        "another module's private name: make it public in the module that "
        "defines it (drop the underscore, or wrap it in a real API), or move "
        "the shared helper to a shared module, then use that:\n  "
        + "\n  ".join(found))
