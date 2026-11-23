"""What holds the tool descriptions down without spending a model call. The
model half is evals/tool_choice/run.py, to run when a description changes."""

import json
from pathlib import Path

from app import console_tools as ct

CASES = json.loads((Path(__file__).parents[1] / "evals/tool_choice/cases.json").read_text())

# Every session carries all of these. They were 30k until 11-06, when the eval
# showed 19.5k routes just as well. Raising this is fine, with a fresh eval run.
# 11-17: +1.8k for run_script, whose description is the bridged functions'
# signature list (console_code.signature). +300 for trip_delete and
# email_archive, with an eval run. 11-18: +1.1k for run_script's table
# summary and "return the answer" rule (code_mode eval, --repeat 3).
# 11-18: +200 for message_tab (a parent's follow-up to its child tab), with
# an eval run.
# +700 for delegate, fan_out and shortlist (other models on errands), with
# an eval run.
# +800 for theme_set (colour roles; a third of it is the role names), with
# an eval run.
DESCRIPTION_BUDGET = 25_500


def test_every_case_names_real_tools():
    known = {t.name for t in ct.TOOLS} | {ct.use_computer.name}
    assert not {t for c in CASES for t in c["ok"]} - known


def test_cases_are_unique():
    prompts = [c["p"] for c in CASES]
    assert len(prompts) == len(set(prompts))


def test_descriptions_stay_inside_the_budget():
    assert sum(len(t.description) for t in ct.TOOLS) <= DESCRIPTION_BUDGET


def test_run_script_schema_hint_matches_the_schema(con):
    """The table summary in run_script's description stops the model probing
    sqlite_master, so a renamed column there would send it querying a column
    that doesn't exist. Held to the migrated schema."""
    from app import console_code as cc
    for table, cols in cc.SCHEMA_TABLES.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        assert have, f"{table} isn't a table"
        assert not set(cols.split(", ")) - have, table
    assert cc.SCHEMA in ct.run_script.description
