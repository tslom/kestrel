"""Notes search, one note's links, and Obsidian's bookmarks — the plan view's
⌘K notes group and Vault notes view. Built by the real indexer.rebuild() over
a throwaway vault, so the link resolution under test is the one that runs."""

import json
import os
from contextlib import contextmanager

import pytest

from app import indexer, vault_views, widgets

NOTES = {
    "01-Projects/Lease.md": "---\ntags: [home]\n---\n# Lease\n\nRenewal due Oct 1. "
                            "See [[Landlord]] and [[CSCI 170|the class]].\n",
    "02-Areas/people/Landlord.md": "Park Ave management. Back to [[Lease#Terms]].\n",
    "03-Courses/CSCI 170.md": "Office hours Mon 9:30. Proofs by induction.\n",
    "03-Courses/notes/week1.md": "Relative link: [intro](../CSCI%20170.md). "
                                 "Missing: [[Nowhere]]. Image: ![[scan.png]].\n",
    "05-Misc/README.md": "one readme\n",
    "04-Other/README.md": "second readme, and [[README]] is ambiguous\n",
    "06-Generated/lease-summary.md": "Generated: the lease renews Oct 1.\n",
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    for rel, body in NOTES.items():
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text(body)
    # mtimes: Lease newest, so it wins recency ties
    for i, rel in enumerate(NOTES):
        os.utime(v / rel, (1_700_000_000 + i, 1_700_000_000 + i))
    os.utime(v / "01-Projects/Lease.md", (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(indexer, "VAULT_PATH", v)
    monkeypatch.setattr(indexer, "INDEX_DB", tmp_path / "index.db")

    # conftest's autouse `empty_index` stubs session() with a bare notes
    # table; these tests want the real one, over the throwaway index.db.
    @contextmanager
    def session():
        c = indexer.connect()
        try:
            yield c
        finally:
            c.close()
    monkeypatch.setattr(indexer, "session", session)
    monkeypatch.setattr(vault_views, "VAULT_PATH", v)
    indexer.rebuild()
    return v


def paths(notes):
    return [n["path"] for n in notes]


def test_link_targets_skip_embeds_that_are_not_notes():
    body = "[[A]] ![[pic.png]] [[B#h|alias]] [x](C.md) [y](https://e.com/D.md) [[A]]"
    assert indexer.link_targets(body) == ["A", "B", "C.md"]


def test_a_table_cells_escaped_pipe_is_not_part_of_the_name():
    assert indexer.link_targets("| [[usc/CSCI-103\\|CSCI 103]] |") == ["usc/CSCI-103"]


def test_search_ranks_title_over_body_and_needs_every_word(vault):
    # Title hit, then the path hit, then the body-only hit ([[Lease#Terms]]).
    assert paths(vault_views.search_notes("lease")) == [
        "01-Projects/Lease.md", "06-Generated/lease-summary.md",
        "02-Areas/people/Landlord.md"]
    assert paths(vault_views.search_notes("lease oct")) == [
        "01-Projects/Lease.md", "06-Generated/lease-summary.md"]
    assert vault_views.search_notes("lease zebra") == []


def test_search_under_two_letters_is_empty_not_the_whole_vault(vault):
    assert vault_views.search_notes("a") == []
    assert vault_views.search_notes("  ") == []


def test_search_snippet_shows_the_body_hit(vault):
    [hit] = vault_views.search_notes("induction")
    assert hit["path"] == "03-Courses/CSCI 170.md"
    assert "induction" in hit["snippet"]
    assert hit["folder"] == "03-Courses"


def test_note_links_both_ways_with_obsidian_resolution(vault):
    lease = vault_views.note_detail("[[Lease]]")
    assert paths(lease["links_to"]) == ["02-Areas/people/Landlord.md", "03-Courses/CSCI 170.md"]
    assert paths(lease["linked_from"]) == ["02-Areas/people/Landlord.md"]
    assert lease["frontmatter"] == {"tags": ["home"]}
    assert lease["excerpt"].startswith("Renewal due Oct 1.")
    assert not lease["generated"]

    course = vault_views.note_detail("03-Courses/CSCI 170.md")
    # a relative, url-encoded markdown link counts as a backlink
    assert paths(course["linked_from"]) == ["01-Projects/Lease.md", "03-Courses/notes/week1.md"]


def test_unresolved_and_ambiguous_links_are_kept_as_names(vault):
    week1 = vault_views.note_detail("03-Courses/notes/week1.md")
    assert week1["unresolved"] == ["Nowhere"]
    other = vault_views.note_detail("04-Other/README.md")
    assert other["unresolved"] == ["README"]   # two READMEs: names neither
    assert other["links_to"] == []


def test_recent_lists_a_folder_newest_first(vault):
    # Lease has the newest mtime of all; within 03-Courses, notes/week1 was
    # written after CSCI 170 (insertion order sets the mtimes).
    assert paths(vault_views.recent_notes("03-Courses")) == [
        "03-Courses/notes/week1.md", "03-Courses/CSCI 170.md"]
    assert paths(vault_views.recent_notes("06-Generated/")) == ["06-Generated/lease-summary.md"]
    assert vault_views.recent_notes("06-Generated")[0]["generated"] is True
    assert paths(vault_views.recent_notes(limit=1)) == ["01-Projects/Lease.md"]


def test_recent_treats_the_folder_as_a_literal_prefix(vault):
    assert vault_views.recent_notes("0_-Courses") == []     # `_` is not a wildcard


def test_generated_notes_say_so(vault):
    assert vault_views.note_detail("06-Generated/lease-summary.md")["generated"]


def test_missing_note_is_404(vault, router_client):
    c = router_client(widgets.router)
    assert c.get("/api/vault/note", params={"path": "nope.md"}).status_code == 404
    assert c.get("/api/vault/note", params={"path": "[[Landlord]]"}).json()["title"] == "Landlord"
    assert paths(c.get("/api/vault/search", params={"q": "park"}).json()["notes"]) == [
        "02-Areas/people/Landlord.md"]


def test_pinned_reads_obsidian_bookmarks_flattening_groups(vault):
    assert vault_views.pinned_notes() == {"pinned": []}      # no file yet: ordinary
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/bookmarks.json").write_text(json.dumps({"items": [
        {"type": "file", "path": "01-Projects/Lease.md"},
        {"type": "group", "title": "School", "items": [
            {"type": "file", "path": "03-Courses/CSCI 170.md"},
            {"type": "folder", "path": "03-Courses"},
            {"type": "search", "query": "tag:#home"}]},
        {"type": "file", "path": "moved-away.md"},
        {"type": "file", "path": "01-Projects/Lease.md"}]}))
    assert vault_views.pinned_notes() == {"pinned": [
        {"path": "01-Projects/Lease.md", "title": "Lease"},
        {"path": "03-Courses/CSCI 170.md", "title": "CSCI 170"}]}


def test_unreadable_bookmarks_say_why_instead_of_raising(vault):
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/bookmarks.json").write_text("{not json")
    out = vault_views.pinned_notes()
    assert out["pinned"] == [] and "bookmarks.json" in out["error"]
