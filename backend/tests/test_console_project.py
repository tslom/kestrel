""".kestrel/project.json (console_project.py): the config, a port per
worktree, and the runs a dev tab's buttons start."""

import json
import socket
import time

import pytest

from app import console_project as cp


def write(tree, cfg):
    (tree / ".kestrel").mkdir(exist_ok=True)
    (tree / ".kestrel" / "project.json").write_text(json.dumps(cfg))


@pytest.fixture(autouse=True)
def clean():
    cp._ports.clear()
    yield
    for r in list(cp._runs.values()):
        r.stop()
    cp._runs.clear()
    cp._ports.clear()


def wait(pred, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_no_file_is_none(tmp_path):
    assert cp.load(tmp_path) is None
    assert cp.start_note(tmp_path) == ""


def test_load_normalises(tmp_path):
    write(tmp_path, {"scripts": [{"id": "dev", "command": "npm run dev", "icon": "RUN"},
                                 {"name": "test", "command": "npm test", "icon": "flask"},
                                 {"id": "empty", "command": "  "}],
                     "previewUrl": "http://localhost:$PORT/", "autoOpenPreview": 1,
                     "runOnWorktreeCreate": "npm install"})
    cfg = cp.load(tmp_path)
    assert [(s["id"], s["icon"]) for s in cfg["scripts"]] == [("dev", "run"), ("test", "run")]
    assert cfg["autoOpenPreview"] is True
    assert cfg["runOnWorktreeCreate"] == ["npm install"]


@pytest.mark.parametrize("bad", ["{not json", "[]",
                                 json.dumps({"scripts": [{"id": "a", "command": "x"},
                                                         {"id": "a", "command": "y"}]})])
def test_bad_config_says_so(tmp_path, bad):
    (tmp_path / ".kestrel").mkdir()
    (tmp_path / ".kestrel" / "project.json").write_text(bad)
    with pytest.raises(cp.ProjectError):
        cp.load(tmp_path)
    assert cp.describe(tmp_path)["error"]


def test_fill():
    assert cp.fill("http://localhost:$PORT/a ${PORT} {port} $PORTX", 41001) == \
        "http://localhost:41001/a 41001 41001 $PORTX"


def test_ports_differ_per_tree_and_are_stable(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    pa, pb = cp.port_for(a), cp.port_for(b)
    assert pa != pb
    assert cp.PORT_BASE <= pa < cp.PORT_BASE + cp.PORT_SPAN
    assert cp.port_for(a) == pa
    cp._ports.clear()
    assert cp.port_for(a) == pa     # the hash, not the order asked in


def test_a_port_held_by_someone_else_is_skipped(tmp_path, monkeypatch):
    want = cp.port_for(tmp_path)
    cp._ports.clear()
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        held.bind(("127.0.0.1", want))
    except OSError:
        pytest.skip("the hashed port is in use already")
    held.listen()
    monkeypatch.setattr(cp.console_profiles, "dev_servers", lambda tree, skip=(): [])
    try:
        assert cp.port_for(tmp_path) != want
    finally:
        held.close()


def test_a_run_gets_its_port_and_output_and_stops(tmp_path):
    row = cp.start(tmp_path, "dev", "dev", "echo port=$PORT; sleep 30", "run")
    assert row["running"] and row["port"] == cp.port_for(tmp_path)
    assert wait(lambda: f"port={row['port']}" in cp.runs(tmp_path)[0]["output"])
    # A second click on a running script is the same run, not a second server.
    assert cp.start(tmp_path, "dev", "dev", "echo again", "run")["id"] == row["id"]
    assert cp.stop(row["id"])
    assert wait(lambda: not cp.runs(tmp_path)[0]["running"])
    assert not cp.stop("nope")


def test_runs_are_per_tree_and_stop_with_it(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    cp.start(a, "dev", "dev", "sleep 30")
    cp.start(b, "dev", "dev", "sleep 30")
    assert len(cp.runs(a)) == 1 and len(cp.runs(b)) == 1
    cp.stop_tree(a)
    assert wait(lambda: not cp.runs(a)[0]["running"])
    assert cp.runs(b)[0]["running"]


def test_output_is_capped_and_ansi_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "OUTPUT_BYTES", 100)
    row = cp.start(tmp_path, "x", "x", r"printf '\033[32mgreen\033[0m '; head -c 500 /dev/zero | tr '\0' y")
    assert wait(lambda: cp.runs(tmp_path)[0]["code"] is not None)
    out = cp.runs(tmp_path)[0]["output"]
    assert len(out) <= 100 and "\x1b" not in out
    assert row["code"] is None


def test_on_created_runs_the_setup_in_order(tmp_path):
    write(tmp_path, {"scripts": [{"id": "deps", "command": "echo two >> log"}],
                     "runOnWorktreeCreate": ["echo one > log", "deps"]})
    row = cp.on_created(tmp_path)
    assert row and row["name"] == "setup"
    assert wait(lambda: cp.runs(tmp_path)[0]["code"] == 0)
    assert (tmp_path / "log").read_text() == "one\ntwo\n"


def test_on_created_without_config_or_broken_is_quiet(tmp_path):
    assert cp.on_created(tmp_path) is None
    (tmp_path / ".kestrel").mkdir()
    (tmp_path / ".kestrel" / "project.json").write_text("{")
    assert cp.on_created(tmp_path) is None


def test_describe_and_start_note(tmp_path):
    write(tmp_path, {"scripts": [{"id": "dev", "command": "true"}],
                     "previewUrl": "http://localhost:{port}"})
    d = cp.describe(tmp_path)
    port = cp.port_for(tmp_path)
    assert d["preview_url"] == f"http://localhost:{port}" and d["port"] == port
    assert d["config"]["scripts"][0]["id"] == "dev" and d["listening"] is False
    assert str(port) in cp.start_note(tmp_path)


def test_routes_run_only_named_scripts(tmp_path, monkeypatch):
    from fastapi import HTTPException
    write(tmp_path, {"scripts": [{"id": "dev", "command": "sleep 30"}]})
    monkeypatch.setattr(cp, "folder", lambda project, worktree: tmp_path)
    import asyncio
    row = asyncio.run(cp.project_run({"project": "x", "script": "dev"}))
    assert row["running"]
    with pytest.raises(HTTPException) as e:
        asyncio.run(cp.project_run({"project": "x", "script": "rm -rf /"}))
    assert e.value.status_code == 404
    assert asyncio.run(cp.project_stop({"id": row["id"]})) == {"ok": True}
