"""PR watch (console_pr.py): which PR moves wake the tab, and which don't."""

import pytest

from app import console_pr


def raw(**over):
    base = {"number": 12, "url": "https://github.com/acme/site/pull/12",
            "title": "Hero copy", "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
            "headRefName": "try/hero", "headRefOid": "a" * 40, "baseRefName": "main",
            "reviewDecision": "", "statusCheckRollup": [], "reviews": [], "comments": []}
    return console_pr.shape({**base, **over})


def run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def test_first_snapshot_is_not_news():
    after = raw(statusCheckRollup=[run("build", conclusion="FAILURE")])
    assert console_pr.changes(None, after) == []


def test_check_verdicts():
    pending = raw(statusCheckRollup=[run("build", status="IN_PROGRESS", conclusion=""),
                                     run("visual", conclusion="SKIPPED")])
    assert pending["verdict"] == "pending"
    passed = raw(statusCheckRollup=[run("build"), run("visual", conclusion="SKIPPED")])
    assert passed["verdict"] == "pass"
    failed = raw(statusCheckRollup=[run("build", conclusion="FAILURE"), run("visual")])
    assert failed["verdict"] == "fail"
    status_ctx = raw(statusCheckRollup=[{"__typename": "StatusContext", "context": "deploy",
                                         "state": "ERROR"}])
    assert status_ctx["checks"] == {"deploy": "fail"}

    assert console_pr.changes(pending, passed) == ["Checks passed on aaaaaaa (2)."]
    notes = console_pr.changes(pending, failed)
    assert len(notes) == 1 and notes[0].startswith("Checks failed on aaaaaaa: build.")
    # Nothing new: the same verdict on the same commit.
    assert console_pr.changes(passed, passed) == []
    assert console_pr.changes(failed, failed) == []


def test_a_new_push_that_passes_again_is_news():
    before = raw(statusCheckRollup=[run("build")])
    after = raw(headRefOid="b" * 40, statusCheckRollup=[run("build")])
    assert console_pr.changes(before, after) == ["Checks passed on bbbbbbb (1)."]


def test_reviews_and_comments_but_not_your_own():
    before = raw()
    after = raw(reviews=[{"id": "R1", "author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
                          "body": "The hero  image\nis too dark"},
                         {"id": "R2", "author": {"login": "me"}, "state": "COMMENTED", "body": "ok"}],
                comments=[{"id": "C1", "author": {"login": "reviewer"}, "body": "also the footer"},
                          {"id": "C2", "author": {"login": "me"}, "body": "on it"}])
    notes = console_pr.changes(before, after, me="me")
    assert notes == ["reviewer requested changes: “The hero image is too dark”",
                     "reviewer commented: “also the footer”"]
    # Seen once, never again.
    assert console_pr.changes(after, after, me="me") == []


def test_review_without_body_points_at_inline_comments():
    after = raw(reviews=[{"id": "R1", "author": {"login": "reviewer"}, "state": "APPROVED", "body": ""}])
    [note] = console_pr.changes(raw(), after)
    assert note.startswith("reviewer approved it (inline comments:") and "pulls/12/comments" in note


def test_conflict_merge_and_close():
    assert console_pr.changes(raw(), raw(mergeable="CONFLICTING")) == [
        "PR #12 now conflicts with main: rebase onto it and resolve."]
    assert console_pr.changes(raw(mergeable="CONFLICTING"), raw(mergeable="CONFLICTING")) == []
    merged = raw(state="MERGED")
    assert console_pr.changes(raw(), merged) == ["PR #12 was merged into main."]
    assert console_pr.final(merged) and console_pr.final(raw(state="CLOSED"))
    assert not console_pr.final(raw())


def test_another_pr_is_a_new_baseline():
    assert console_pr.changes(raw(number=3), raw(mergeable="CONFLICTING")) == []


def test_long_bodies_are_cut():
    after = raw(comments=[{"id": "C1", "author": {"login": "m"}, "body": "x" * 1000}])
    [note] = console_pr.changes(raw(), after)
    assert len(note) < console_pr.BODY_CHARS + 30


@pytest.mark.parametrize("ref,ok", [("12", True), ("#12", True), ("", True),
                                    ("https://github.com/a/b/pull/9", True), ("try/hero-v2", True),
                                    ("12; rm -rf /", False), ("--web", False), ("-w", False)])
def test_refs(ref, ok):
    if ok:
        console_pr._ref(ref)
    else:
        with pytest.raises(console_pr.PrError):
            console_pr._ref(ref)


def test_poll_passes_the_ref_and_diffs(monkeypatch, tmp_path):
    seen = []

    def fake_gh(cwd, *args):
        seen.append(args)
        if args[:2] == ("api", "user"):
            return "me\n"
        import json
        return json.dumps({"number": 12, "state": "OPEN", "headRefOid": "c" * 40,
                           "mergeable": "CONFLICTING", "baseRefName": "main"})

    monkeypatch.setattr(console_pr, "_gh", fake_gh)
    monkeypatch.setattr(console_pr, "_viewer", None)
    first = console_pr.poll(tmp_path, "", None)
    assert first["notes"] == [] and not first["final"]
    assert seen[0][:2] == ("pr", "view") and seen[0][2] == "--json"
    # Later polls name the PR by its URL, not the branch the tree is on now.
    last = {**first["pr"], "url": "https://github.com/a/b/pull/12", "mergeable": "MERGEABLE"}
    again = console_pr.poll(tmp_path, "", last)
    assert any(a[2] == "https://github.com/a/b/pull/12" for a in seen if a[:2] == ("pr", "view"))
    assert again["notes"] == ["PR #12 now conflicts with main: rebase onto it and resolve."]
