"""Subagents at work (console_agents): the CLI's task lifecycle messages folded
into the rows the console's strip shows. A background subagent's calls never
reach the parent's stream, so these are all the page ever hears of it."""

from app import console_agents as ca


def test_a_subagent_is_followed_from_start_to_finish():
    agents: dict = {}
    assert ca.apply(agents, "task_started", {
        "task_id": "t1", "description": "Find the brief route", "tool_use_id": "tu1",
        "task_type": "local_agent", "uuid": "u", "session_id": "s"})
    assert ca.running(agents) == 1
    assert ca.apply(agents, "task_progress", {
        "task_id": "t1", "description": "Searching for brief", "last_tool_name": "Grep",
        "usage": {"total_tokens": 12000, "tool_uses": 4, "duration_ms": 9000}})
    row = agents["t1"]
    assert (row["last_tool"], row["tokens"], row["tool_uses"]) == ("Grep", 12000, 4)
    # progress's description is the activity (measured on CLI 2.1.318), not the task
    assert row["description"] == "Find the brief route"
    assert row["activity"] == "Searching for brief"
    assert ca.apply(agents, "task_notification", {
        "task_id": "t1", "status": "completed", "summary": "It's in brief.py",
        "output_file": "/x", "usage": {"total_tokens": 15000, "tool_uses": 6, "duration_ms": 12000}})
    assert ca.running(agents) == 0
    assert agents["t1"]["summary"] == "It's in brief.py"
    assert agents["t1"]["duration_ms"] == 12000


def test_a_finish_reported_only_as_task_updated_still_ends_it():
    agents: dict = {}
    ca.apply(agents, "task_started", {"task_id": "t1", "description": "x"})
    assert ca.apply(agents, "task_updated", {"task_id": "t1", "patch": {"status": "killed"}})
    assert agents["t1"]["status"] == "killed" and ca.running(agents) == 0
    assert agents["t1"]["duration_ms"] is not None


def test_an_unchanged_progress_sends_nothing_and_other_system_messages_are_ignored():
    agents: dict = {}
    ca.apply(agents, "task_started", {"task_id": "t1", "description": "x"})
    assert ca.apply(agents, "task_progress", {"task_id": "t1", "description": "Reading a"})
    assert not ca.apply(agents, "task_progress", {"task_id": "t1", "description": "Reading a"})
    assert not ca.apply(agents, "init", {"task_id": "t9"})
    assert not ca.apply(agents, "task_updated", {"task_id": "t9", "patch": {}})
    assert list(agents) == ["t1"]


def test_finished_rows_are_capped_but_running_ones_are_kept():
    agents: dict = {}
    ca.apply(agents, "task_started", {"task_id": "live", "description": "still going"})
    for i in range(ca.KEEP_FINISHED + 4):
        ca.apply(agents, "task_started", {"task_id": f"d{i}", "description": "x"})
        ca.apply(agents, "task_notification", {"task_id": f"d{i}", "status": "completed",
                                               "summary": "", "output_file": ""})
    assert "live" in agents
    assert sum(1 for r in agents.values() if r["status"] == "completed") == ca.KEEP_FINISHED
    assert "d0" not in agents
