"""A dev tab's branch review (console_worktree.review, preview_conflicts;
console_diff.commit_diff, blob) and its two routes, on real git repos in tmp."""

import subprocess
from pathlib import Path

import pytest

from app import console, console_diff, console_profiles
from app import console_worktree as cw


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "CLAUDE_PROJECTS", tmp_path / "claude-projects")
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "_live", cw.Counter())
    r = tmp_path / "Projects" / "demo"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def tree(repo):
    return Path(cw.create(repo)["path"])


def commit(cwd, name, text, msg="change"):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-qm", msg)


def snapshot(*cwds):
    """What a read must leave exactly as it found it, in each tree."""
    return [(git(c, "rev-parse", "HEAD"), git(c, "status", "--porcelain"),
             sorted(p.name for p in Path(c).iterdir())) for c in cwds]


def test_review_is_the_branch_commits_and_their_diff_not_uncommitted_work(repo, tree):
    commit(tree, "b.txt", "new\n", "add b")
    commit(tree, "a.txt", "one\nTWO\nthree\nfour\nfive\nsix\nseven\n", "edit a")
    (tree / "c.txt").write_text("not committed\n")
    res = cw.review(tree)
    assert res["ahead"] == 2 and res["behind"] == 0 and res["dirty"] == ["c.txt"]
    assert [c["subject"] for c in res["commits"]] == ["edit a", "add b"]
    assert res["commits"][0]["sha"] == git(tree, "rev-parse", "HEAD")
    assert res["merge_base"] == git(repo, "rev-parse", "main")
    by = {f["path"]: f for f in res["files"]}
    assert set(by) == {"a.txt", "b.txt"}
    assert by["b.txt"]["status"] == "added" and by["a.txt"]["lines"][0].startswith("@@")
    assert "-two" in by["a.txt"]["lines"] and "+TWO" in by["a.txt"]["lines"]
    assert res["conflicts"] == []


def test_an_empty_branch_has_nothing_to_review(tree):
    res = cw.review(tree)
    assert res["ahead"] == 0 and res["files"] == [] and res["commits"] == []


def test_base_moving_on_elsewhere_previews_clean(repo, tree):
    commit(tree, "b.txt", "tab\n")
    commit(repo, "c.txt", "main\n")
    res = cw.review(tree)
    assert res["behind"] == 1 and res["conflicts"] == []
    # The diff is the tab's own change, not main's c.txt read backwards.
    assert [f["path"] for f in res["files"]] == ["b.txt"]


def test_conflict_preview_shows_both_sides_and_moves_nothing(repo, tree):
    commit(tree, "a.txt", "one\nTAB\nthree\nfour\nfive\nsix\nseven\n", "tab work")
    commit(repo, "a.txt", "one\nMAIN\nthree\nfour\nfive\nsix\nseven\n", "main work")
    before = snapshot(repo, tree)
    res = cw.review(tree)
    assert snapshot(repo, tree) == before
    assert not (Path(git(tree, "rev-parse", "--git-dir")) / "rebase-merge").exists()
    [c] = res["conflicts"]
    assert c["path"] == "a.txt" and c["kind"] == "contents"
    assert "Merge conflict in a.txt" in c["message"]
    [region] = c["regions"]
    assert region["ours"] == ["MAIN"] and region["theirs"] == ["TAB"]
    assert region["ancestor"] is None
    assert region["before"] == ["one"] and region["after"] == ["three", "four", "five"]
    # The merge itself agrees, and still leaves main alone.
    assert cw.merge(tree)["conflicts"] == ["a.txt"]
    assert snapshot(repo, tree) == before


def test_a_diff3_conflict_style_carries_the_ancestor(repo, tree):
    git(repo, "config", "merge.conflictStyle", "zdiff3")
    commit(tree, "a.txt", "one\nTAB\nthree\nfour\nfive\nsix\nseven\n")
    commit(repo, "a.txt", "one\nMAIN\nthree\nfour\nfive\nsix\nseven\n")
    [region] = cw.review(tree)["conflicts"][0]["regions"]
    assert (region["ours"], region["ancestor"], region["theirs"]) == (["MAIN"], ["two"], ["TAB"])


def test_each_region_gets_its_own_sides_and_no_markers_as_context(repo, tree):
    lines = [f"l{n}" for n in range(1, 13)]
    def with_(two, eight):
        return "\n".join(["l1", two, *lines[2:7], eight, *lines[8:]]) + "\n"
    commit(repo, "a.txt", with_("l2", "l8"))
    git(tree, "rebase", "-q", "main")
    commit(tree, "a.txt", with_("TAB2", "TAB8"))
    commit(repo, "a.txt", with_("MAIN2", "MAIN8"))
    regions = cw.review(tree)["conflicts"][0]["regions"]
    assert [(r["ours"], r["theirs"]) for r in regions] == [(["MAIN2"], ["TAB2"]),
                                                            (["MAIN8"], ["TAB8"])]
    assert regions[0]["before"] == ["l1"] and regions[1]["after"] == ["l9", "l10", "l11"]
    for r in regions:
        assert not any(ln[:7] in ("<<<<<<<", "=======", ">>>>>>>") for ln in r["before"] + r["after"])


def test_a_marker_lookalike_in_the_file_is_not_a_region(repo, tree):
    text = "<<<<<<< not a marker\nx\n=======\ny\n>>>>>>> nope\n"
    commit(repo, "m.txt", text + "base\n")
    git(tree, "rebase", "-q", "main")
    commit(tree, "m.txt", text + "tab\n")
    commit(repo, "m.txt", text + "main\n")
    [region] = cw.review(tree)["conflicts"][0]["regions"]
    assert region["ours"] == ["main"] and region["theirs"] == ["tab"]


def test_modify_delete_is_named_and_has_no_lines_to_show(repo, tree):
    git(tree, "rm", "-q", "a.txt")
    git(tree, "commit", "-qm", "drop a")
    commit(repo, "a.txt", "changed on main\n")
    [c] = cw.review(tree)["conflicts"]
    assert c["kind"] == "modify/delete" and c["regions"] == []
    assert "deleted in kestrel/" in c["message"]


def test_blob_reads_a_commit_and_nothing_it_shouldnt(repo, tree, monkeypatch):
    commit(tree, "a.txt", "changed\n")
    mb = cw.review(tree)["merge_base"]
    assert console_diff.blob(tree, mb, "a.txt").startswith("one\ntwo")
    assert console_diff.blob(tree, "HEAD", "a.txt") == "changed\n"
    for rev, path in [("HEAD", "nope.txt"), ("HEAD", "../a.txt"), ("HEAD", "/etc/hosts"),
                      ("HEAD", "-a.txt"), ("HEAD~1", "a.txt"), ("--output=x", "a.txt"),
                      ("main", "a.txt"), ("", "a.txt"), ("HEAD", "")]:
        assert console_diff.blob(tree, rev, path) is None, (rev, path)
    (tree / "bin.dat").write_bytes(b"\x00\x01\x02")
    commit(tree, "big.txt", "x" * 64)          # commits bin.dat with it
    assert console_diff.blob(tree, "HEAD", "bin.dat") is None
    monkeypatch.setattr(console_diff, "BLOB_MAX_BYTES", 10)
    assert console_diff.blob(tree, "HEAD", "big.txt") is None


def test_commit_diff_takes_only_a_revision(tree):
    commit(tree, "b.txt", "new\n")
    assert console_diff.commit_diff(tree, "--output=/tmp/kestrel-x", "HEAD") == []
    assert console_diff.commit_diff(tree, "HEAD", "--stat") == []
    assert not Path("/tmp/kestrel-x").exists()


@pytest.fixture
def client(router_client, repo, monkeypatch):
    monkeypatch.setattr(console_profiles, "DEV_PROJECT_ROOTS", [repo.parent])
    return router_client(console.router)


def test_review_route_reads_only_a_tab_worktree(client, repo, tree, tmp_path):
    commit(tree, "b.txt", "new\n")
    r = client.get("/api/console/review", params={"project": "demo", "worktree": str(tree)})
    assert r.status_code == 200
    body = r.json()
    assert body["branch"] == f"kestrel/{tree.name}" and [f["path"] for f in body["files"]] == ["b.txt"]
    for bad in (str(repo), str(tmp_path), "/etc"):
        r = client.get("/api/console/review", params={"project": "demo", "worktree": bad})
        assert r.status_code == 404, bad


def test_blob_route_answers_null_rather_than_failing(client, repo, tree):
    commit(tree, "b.txt", "new\n")
    q = {"project": "demo", "worktree": str(tree), "path": "b.txt"}
    assert client.get("/api/console/blob", params={**q, "rev": "HEAD"}).json()["text"] == "new\n"
    assert client.get("/api/console/blob", params={**q, "rev": "HEAD^"}).json()["text"] is None
    # The main checkout shares the object store, so a commit reads there too.
    sha = git(tree, "rev-parse", "HEAD")
    r = client.get("/api/console/blob", params={"project": "demo", "rev": sha, "path": "b.txt"})
    assert r.json()["text"] == "new\n"
