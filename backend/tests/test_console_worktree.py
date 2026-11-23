"""Dev-tab worktrees (console_worktree) against real git repos in tmp."""

import json
import subprocess
from pathlib import Path

import pytest

from app import console_worktree as cw
from app import sessions


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "CLAUDE_PROJECTS", tmp_path / "claude-projects")
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "_live", cw.Counter())
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text("data/\n.venv/\n")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def commit(tree, name, text, msg="change"):
    (Path(tree) / name).write_text(text)
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", msg)


def test_create_gives_a_branch_off_main_hidden_from_main_status(repo):
    info = cw.create(repo)
    tree = Path(info["path"])
    assert tree.parent == repo / ".claude" / "worktrees"
    assert info["branch"] == f"kestrel/{tree.name}" and info["base"] == "main"
    assert git(tree, "branch", "--show-current") == info["branch"]
    assert git(repo, "status", "--porcelain") == ""


def test_links_point_at_main_and_never_get_committed(repo):
    (repo / "data").mkdir()
    (repo / "data" / "app.db").write_text("real")
    (repo / ".venv").mkdir()
    (repo / ".kestrel").mkdir()
    (repo / ".kestrel" / "worktree.json").write_text(json.dumps(
        {"link": ["data", ".venv"], "setup": ["echo hi > setup-ran.txt"]}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cfg")
    tree = Path(cw.create(repo)["path"])
    assert (tree / "data").is_symlink() and (tree / "data" / "app.db").read_text() == "real"
    assert (tree / "setup-ran.txt").exists()
    # The links are excluded: only the setup's own output is untracked.
    assert git(tree, "status", "--porcelain") == "?? setup-ran.txt"


def test_removing_a_tree_leaves_the_linked_data_alone(repo):
    (repo / "data").mkdir()
    (repo / "data" / "app.db").write_text("real")
    (repo / ".kestrel").mkdir()
    (repo / ".kestrel" / "worktree.json").write_text(json.dumps({"link": ["data"]}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "cfg")
    tree = Path(cw.create(repo)["path"])
    assert cw.release(tree) is True
    assert not tree.exists()
    assert (repo / "data" / "app.db").read_text() == "real"
    assert "kestrel/" not in git(repo, "branch")


def test_wait_released_answers_once_the_close_has_swept(repo):
    """The page asks for the left-behind list right after closing a tab
    (/api/console/orphans?released=); the answer has to wait for that
    socket's release(), not guess at how long it takes."""
    import threading
    tree = Path(cw.create(repo)["path"])
    got = []
    waiter = threading.Thread(target=lambda: got.append(cw.wait_released(tree, 5)))
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive() and tree.exists()     # still held: waiting
    assert cw.release(tree) is True
    waiter.join(5)
    assert got == [True]
    assert cw.wait_released(tree, 0.1) is True     # nothing holds it: at once
    held = Path(cw.create(repo)["path"])
    assert cw.wait_released(held, 0.1) is False    # never released: gives up


def test_a_tab_with_work_is_kept_and_listed_as_an_orphan(repo):
    first = Path(cw.create(repo)["path"])
    (first / "a.txt").write_text("edited\n")
    assert cw.release(first) is False and first.exists()
    second = Path(cw.create(repo)["path"])   # its sweep must not take `first`
    assert first.exists()
    # `second` is held by its socket, so only `first` is an orphan.
    assert [o["name"] for o in cw.orphans(repo)] == [first.name]
    [o] = cw.orphans(repo)
    assert o["dirty"] == ["a.txt"] and o["kind"] == "tab" and not o["held"]
    # …and the start line no longer carries it into unrelated tabs.
    assert "⚠" not in cw.start_line(cw._info(repo, second))


def night_tree(repo, name, locked=None):
    """A `claude --worktree <name>` tree: .claude/worktrees/<name> on worktree-<name>."""
    tree = repo / ".claude" / "worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", f"worktree-{name}", str(tree), "main")
    if locked:
        git(repo, "worktree", "lock", "--reason", locked, str(tree))
    return tree.resolve()


def test_sweep_removes_merged_orphans_and_keeps_the_rest(repo):
    merged = night_tree(repo, "night-done")           # nothing beyond main
    work = night_tree(repo, "night-work")
    commit(work, "w.txt", "w\n", "night work")
    git(repo, "branch", "kestrel/tab-0000aaaa")       # a treeless, merged tab branch
    git(repo, "branch", "kestrel/tab-0000bbbb")
    git(repo, "checkout", "-q", "kestrel/tab-0000bbbb")
    commit(repo, "t.txt", "t\n", "treeless work")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "try/other")                  # not ours: never touched
    with cw._lock:
        gone = cw.sweep(repo)
    assert sorted(gone) == ["kestrel/tab-0000aaaa", "worktree-night-done"]
    assert not merged.exists() and work.exists()
    left = {o["branch"]: o for o in cw.orphans(repo)}
    assert set(left) == {"kestrel/tab-0000bbbb", "worktree-night-work"}
    assert left["worktree-night-work"]["ahead"] == 1
    assert left["worktree-night-work"]["commits"][0]["subject"] == "night work"
    assert left["kestrel/tab-0000bbbb"]["path"] is None
    assert "try/other" in git(repo, "branch")


def test_a_live_lock_holds_and_a_dead_one_doesnt(repo):
    import os
    alive = night_tree(repo, "night-live", locked=f"claude session x (pid {os.getpid()} start now)")
    dead = night_tree(repo, "night-dead", locked="claude session y (pid 999999 start then)")
    by = {b["name"]: b for b in cw.branches(repo)}
    assert by["night-live"]["held"] and not by["night-dead"]["held"]
    with cw._lock:
        cw.sweep(repo)
    # Merged and clean, but a live session holds it; the dead lock doesn't
    # save the other (a plain `worktree remove` refuses a locked tree, so the
    # merged-and-clean sweep leaves it for discard).
    assert alive.exists()
    with pytest.raises(cw.WorktreeError):
        cw.discard(repo, "worktree-night-live")
    assert cw.discard(repo, "worktree-night-dead")["ok"] and not dead.exists()


def test_merge_orphan_lands_a_night_branch_and_sweeps_it(repo):
    work = night_tree(repo, "night-work")
    commit(work, "w.txt", "w\n", "night work")
    commit(repo, "m.txt", "m\n", "main moved")
    res = cw.merge_orphan(repo, "worktree-night-work")
    assert res["ok"] and res["commits"] == 1 and (repo / "w.txt").exists()
    assert res["swept"] == ["worktree-night-work"] and not work.exists()
    assert "worktree-night-work" not in git(repo, "branch")


def test_merge_orphan_without_a_tree_fast_forwards_only(repo):
    git(repo, "branch", "kestrel/tab-0000cccc")
    git(repo, "checkout", "-q", "kestrel/tab-0000cccc")
    commit(repo, "t.txt", "t\n", "treeless work")
    git(repo, "checkout", "-q", "main")
    assert cw.merge_orphan(repo, "kestrel/tab-0000cccc")["ok"]
    assert (repo / "t.txt").exists() and "kestrel/" not in git(repo, "branch")
    # One whose base moved on has nowhere to rebase: refused, nothing moves.
    git(repo, "branch", "kestrel/tab-0000dddd")
    git(repo, "checkout", "-q", "kestrel/tab-0000dddd")
    commit(repo, "u.txt", "u\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "v.txt", "v\n")
    head = git(repo, "rev-parse", "HEAD")
    res = cw.merge_orphan(repo, "kestrel/tab-0000dddd")
    assert not res["ok"] and "moved on" in res["reason"]
    assert git(repo, "rev-parse", "HEAD") == head


def test_discard_takes_uncommitted_work_and_refuses_other_branches(repo):
    tree = Path(cw.create(repo)["path"])
    commit(tree, "b.txt", "b\n")
    (tree / "c.txt").write_text("untracked\n")
    with pytest.raises(cw.WorktreeError):           # its socket is still open
        cw.discard(repo, f"kestrel/{tree.name}")
    cw.release(tree)
    res = cw.discard(repo, f"kestrel/{tree.name}")
    assert res["ok"] and "1 commit and 1 uncommitted file" in res["text"]
    assert not tree.exists() and "kestrel/" not in git(repo, "branch")
    with pytest.raises(cw.WorktreeError):
        cw.discard(repo, "main")


def test_a_tab_tree_on_another_branch_merges_that_branch(repo):
    tree = Path(cw.create(repo)["path"])
    git(tree, "checkout", "-q", "-b", "try/thing")
    commit(tree, "x.txt", "x\n", "on try")
    assert cw.status(tree)["branch"] == "try/thing"
    res = cw.merge(tree)
    assert res["ok"] and (repo / "x.txt").exists()


def test_merge_fast_forwards_main(repo):
    tree = Path(cw.create(repo)["path"])
    commit(tree, "b.txt", "new\n")
    res = cw.merge(tree)
    assert res["ok"] and res["commits"] == 1 and res["files"] == ["b.txt"]
    assert (repo / "b.txt").read_text() == "new\n"
    assert git(repo, "log", "-1", "--format=%s") == "change"
    # And it's now empty, so closing the tab removes it.
    assert cw.release(tree) is True


def test_merge_with_nothing_is_a_no_op(repo):
    tree = Path(cw.create(repo)["path"])
    res = cw.merge(tree)
    assert res == {"ok": True, "commits": 0, "base": "main",
                   "note": f"nothing to merge: kestrel/{tree.name} has no commits beyond main"}


def test_merge_refuses_uncommitted_work(repo):
    tree = Path(cw.create(repo)["path"])
    (tree / "a.txt").write_text("dirty\n")
    res = cw.merge(tree)
    assert not res["ok"] and res["dirty"] == ["a.txt"]


def test_merge_rebases_when_main_moved(repo):
    tree = Path(cw.create(repo)["path"])
    commit(tree, "b.txt", "tab\n", "tab work")
    commit(repo, "c.txt", "elsewhere\n", "main work")
    res = cw.merge(tree)
    assert res["ok"] and res["commits"] == 1
    assert git(repo, "log", "--format=%s", "-3").splitlines() == ["tab work", "main work", "init"]


def test_conflict_goes_back_to_the_tab_and_main_is_untouched(repo):
    tree = Path(cw.create(repo)["path"])
    commit(tree, "a.txt", "tab\n", "tab work")
    commit(repo, "a.txt", "main\n", "main work")
    head = git(repo, "rev-parse", "HEAD")
    res = cw.merge(tree)
    assert not res["ok"] and res["conflicts"] == ["a.txt"]
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "a.txt").read_text() == "main\n"
    # The rebase was aborted: the tab is back on its own commit, clean.
    assert git(tree, "status", "--porcelain") == ""
    assert (tree / "a.txt").read_text() == "tab\n"
    assert "Conflicting files: a.txt" in cw.describe_merge(res)


def test_merge_refuses_when_main_checkout_is_dirty_on_the_same_file(repo):
    tree = Path(cw.create(repo)["path"])
    commit(tree, "a.txt", "tab\n")
    (repo / "a.txt").write_text("uncommitted in main\n")
    res = cw.merge(tree)
    assert not res["ok"] and "refused the fast-forward" in res["reason"]
    assert (repo / "a.txt").read_text() == "uncommitted in main\n"


def test_resume_finds_the_sessions_tree_and_rebuilds_a_removed_one(repo):
    info = cw.create(repo)
    tree = Path(info["path"])
    pdir = cw.CLAUDE_PROJECTS / cw._munge(tree)
    pdir.mkdir(parents=True)
    (pdir / "abcd1234-0000.jsonl").write_text("{}\n")
    assert cw.release(tree) is True and not tree.exists()
    back = cw.for_session(repo, "abcd1234-0000")
    assert back["path"] == str(tree) and (tree / ".git").exists()
    # A session that ran in the main checkout resumes there.
    assert cw.for_session(repo, "ffff0000-1111") is None


def test_resolve_accepts_only_this_repos_tab_trees(repo, tmp_path):
    tree = Path(cw.create(repo)["path"])
    assert cw.resolve(repo, str(tree)) == tree
    assert cw.resolve(repo, str(repo)) is None
    assert cw.resolve(repo, str(tmp_path)) is None


def test_not_eligible_without_a_commit_or_on_a_detached_head(repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert not cw.eligible(empty)
    assert cw.eligible(repo)
    git(repo, "checkout", "-q", "--detach")
    assert not cw.eligible(repo)


def test_session_listing_includes_worktree_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path)
    repo = Path("/x/proj")
    main = sessions.project_dir(repo)
    wt = tmp_path / (main.name + "--claude-worktrees-tab-0123abcd")
    main.mkdir(parents=True)
    wt.mkdir()
    rec = json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})
    (main / "s1.jsonl").write_text(rec + "\n")
    (wt / "s2.jsonl").write_text(rec + "\n")
    assert {s["id"] for s in sessions.list_sessions(cwd=repo)} == {"s1", "s2"}
    assert sessions._session_file("s2", repo) == wt / "s2.jsonl"


def test_dev_prompt_names_the_worktree_only_when_there_is_one():
    from app import console_prompt
    wt = {"path": "/r/.claude/worktrees/tab-0123abcd", "branch": "kestrel/tab-0123abcd",
          "base": "main", "repo": "/r"}
    text = console_prompt.render_append(profile="dev", worktree=wt)
    assert "kestrel/tab-0123abcd" in text and "worktree_merge" in text and "{base}" not in text
    assert "worktree_merge" not in console_prompt.render_append(profile="dev")
