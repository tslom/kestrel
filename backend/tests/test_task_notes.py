"""task_notes: the CLI's <task-notification> messages become a row, not a
bubble of XML in the user's voice (live stream and replay alike)."""

import json

from app import sessions, task_notes

STOPPED_AGENTS = """<task-notification>
<task-id>a01c03abf930a5e47</task-id>
<task-id>a6f8645ee2812b1bc</task-id>
<status>stopped</status>
<summary>2 background agents didn't finish before the previous session ended: "Time-boxed local AI video test" (a01c03abf930a5e47), "Analyze reference sites, write notes" (a6f8645ee2812b1bc).</summary>
<note>No completion record was found for them in the previous session.</note>
</task-notification>"""

STOPPED_SHELL = """<task-notification>
<task-id>b8aukrd2b</task-id>
<tool-use-id>toolu_01RBzdqJDM9fDRphRWDeLYQX</tool-use-id>
<status>stopped</status>
<summary>Background shell command didn't finish before the previous session ended</summary>
<note>Check the output file for partial results before assuming it completed.</note>
</task-notification>"""

FAILED = """<task-notification>
<task-id>bdid3a426</task-id>
<output-file>/private/tmp/x/tasks/bdid3a426.output</output-file>
<status>failed</status>
<summary>Background command "npx next start -p 4319 &gt; /tmp/hd-next.log 2&gt;&amp;1" failed with exit code 1</summary>
</task-notification>"""


def test_cut_off_agents_are_named_and_said_plainly():
    notes, rest = task_notes.parse(STOPPED_AGENTS)
    assert rest == ""
    (n,) = notes
    assert n["status"] == "stopped"
    assert n["title"] == ("2 background agents stopped when the app quit: "
                          "Time-boxed local AI video test, Analyze reference sites, write notes")
    assert n["task_ids"] == ["a01c03abf930a5e47", "a6f8645ee2812b1bc"]
    assert n["note"].startswith("No completion record")


def test_a_cut_off_shell_and_a_failure_keep_their_details():
    (shell,), _ = task_notes.parse(STOPPED_SHELL)
    assert shell["title"] == "A background command stopped when the app quit"
    assert shell["tool_use_id"] == "toolu_01RBzdqJDM9fDRphRWDeLYQX"
    (failed,), _ = task_notes.parse(FAILED)
    assert failed["status"] == "failed"
    assert failed["title"] == ('Background command "npx next start -p 4319 > '
                               '/tmp/hd-next.log 2>&1" failed with exit code 1')
    assert failed["output_file"] == "/private/tmp/x/tasks/bdid3a426.output"


def test_text_around_a_notification_stays_text():
    notes, rest = task_notes.parse(f"{STOPPED_SHELL}\n{FAILED}\ncontinue")
    assert [n["status"] for n in notes] == ["stopped", "failed"]
    assert rest == "continue"
    assert task_notes.parse("just words") == ([], "just words")


def test_replay_shows_a_row_not_a_user_bubble(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "project_dir", lambda cwd=None: tmp_path)
    monkeypatch.setattr(sessions, "_session_dirs", lambda cwd=None: [tmp_path])
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "text", "text": STOPPED_AGENTS}]}},
        {"type": "user", "uuid": "u3", "message": {"role": "user", "content": STOPPED_SHELL}},
    ]) + "\n")
    out = sessions.session_transcript("s")
    assert [e["kind"] for e in out] == ["user", "tasknote", "tasknote"]
    assert not any("<task-notification>" in (e.get("text") or "") for e in out)
