"""What holds the tool descriptions down without spending a model call. The
model half is evals/tool_choice/run.py, to run when a description changes."""

import json
from pathlib import Path

from app import console_tools as ct

CASES = json.loads((Path(__file__).parents[1] / "evals/tool_choice/cases.json").read_text())

# Every session carries all of these. The eval routed as well at 19.5k as at 30k;
# raising this budget needs a fresh eval run.
DESCRIPTION_BUDGET = 25_600


def test_every_case_names_real_tools():
    # "none": the right move is no tool, e.g. work Claude keeps instead of delegating.
    known = {t.name for t in ct.TOOLS} | {ct.use_computer.name, "none"}
    assert not {t for c in CASES for t in c["ok"]} - known


def test_cases_are_unique():
    prompts = [c["p"] for c in CASES]
    assert len(prompts) == len(set(prompts))


def test_descriptions_stay_inside_the_budget():
    assert sum(len(t.description) for t in ct.TOOLS) <= DESCRIPTION_BUDGET


def test_run_script_schema_hint_matches_the_schema(con):
    """run_script's table summary matches the migrated schema."""
    from app import console_code as cc
    for table, cols in cc.SCHEMA_TABLES.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        assert have, f"{table} isn't a table"
        assert not set(cols.split(", ")) - have, table
    assert cc.SCHEMA in ct.run_script.description
