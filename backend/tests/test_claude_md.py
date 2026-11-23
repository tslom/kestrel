"""CLAUDE.md's rule → enforcer table names things that exist.

The table is what an agent reads to learn that a rule is checked (the
`/correct` habit from the 11-17 "All Code Is Slop" report). A row whose
enforcer was renamed or deleted would claim a check that no longer runs.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_enforcer_in_the_table_exists():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "| Rule | Enforced by |" in text
    rows = text.split("| Rule | Enforced by |", 1)[1].split("\n\n", 1)[0].splitlines()[2:]
    assert rows
    for row in rows:
        paths = re.findall(r"`([^`]+)`", row.strip().strip("|").rsplit("|", 1)[1])
        assert paths, row
        for rel in paths:
            assert (ROOT / rel).exists(), f"CLAUDE.md names {rel}, which isn't there"
