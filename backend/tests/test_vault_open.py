"""Turning what the console *said* about a note back into the note itself.

The console writes about files in four shapes — a wikilink, a wikilink with an
alias or a heading, a vault-relative path, an absolute one — and the dashboard
has to reach the same file from all of them before it can hand anything to
Obsidian. These tests pin the two edges that matter more than the happy path:

- a name that isn't a note in the vault must *miss*, never fall back to
  something nearby, because a link that opens the wrong note is worse than a
  link that says it can't find one. `README.md` is the real case — this vault
  has several, and a bare name that fits four notes fits none of them;
- nothing outside the vault resolves, whatever the text does with `..` or a
  leading slash.
"""

import sqlite3

import pytest

from app import indexer, macos, vault_views


NOTES = [
    "01-Projects/voron-build/README.md",
    "01-Projects/acme-website/README.md",
    "06-Notes/only-one-of-these.md",
    "05-Daily/2026-09-01.md",
]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault on disk plus the index over it — resolution reads both."""
    for rel in NOTES:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# note\n", encoding="utf-8")
    (tmp_path / "Attachments").mkdir()
    (tmp_path / "Attachments/scan.pdf").write_bytes(b"%PDF-1.4\n")

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")
    con.executemany("INSERT INTO notes VALUES (?,?,'{}')",
                    [(rel, rel.rsplit("/", 1)[-1][:-3]) for rel in NOTES])

    monkeypatch.setattr(vault_views, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(indexer, "connect", lambda: con)
    return tmp_path


@pytest.mark.parametrize("target, expect", [
    ("06-Notes/only-one-of-these.md", "06-Notes/only-one-of-these.md"),
    # A wikilink drops the extension, and names the file from anywhere.
    ("[[only-one-of-these]]", "06-Notes/only-one-of-these.md"),
    ("only-one-of-these", "06-Notes/only-one-of-these.md"),
    # Alias, heading and block id are display/position, not part of the name.
    ("[[only-one-of-these|that note]]", "06-Notes/only-one-of-these.md"),
    ("[[only-one-of-these#Costs]]", "06-Notes/only-one-of-these.md"),
    ("[[only-one-of-these^abc123]]", "06-Notes/only-one-of-these.md"),
    # A full path stays a full path even when the name is shared.
    ("01-Projects/voron-build/README.md", "01-Projects/voron-build/README.md"),
    ("2026-09-01", "05-Daily/2026-09-01.md"),
    # Obsidian opens attachments too, and the console links the PDF it just
    # filed as readily as the note about it. Only the *index* fallback is
    # markdown-only, because that's the only part that guesses.
    ("Attachments/scan.pdf", "Attachments/scan.pdf"),
])
def test_resolves(vault, target, expect):
    assert vault_views.resolve_note(target) == expect


def test_absolute_path_inside_the_vault_resolves(vault):
    assert (vault_views.resolve_note(str(vault / "05-Daily/2026-09-01.md"))
            == "05-Daily/2026-09-01.md")


@pytest.mark.parametrize("target", [
    "README.md",              # two notes carry this name — ambiguous is a miss
    "README",
    "no-such-note.md",
    "",
    "[[]]",
    "scan",                   # the name fallback is markdown-only, so a bare
    "scan.pdf",               # attachment name doesn't resolve without its folder
    "../../../etc/hosts",
    "/etc/hosts",
    "06-Notes/../../../etc/hosts",
])
def test_misses(vault, target):
    with pytest.raises(FileNotFoundError):
        vault_views.resolve_note(target)


def test_ambiguity_says_so(vault):
    """The message is the link's tooltip, so it has to name the problem."""
    with pytest.raises(FileNotFoundError, match="names 2 notes"):
        vault_views.resolve_note("README")


def test_uri_carries_vault_file_and_anchor(vault):
    uri = vault_views.obsidian_uri("06-Notes/only-one-of-these.md")
    assert uri.startswith(f"obsidian://open?vault={vault.name}&file=")
    assert "06-Notes%2Fonly-one-of-these.md" in uri
    assert "heading=" not in uri

    with_head = vault_views.obsidian_uri("06-Notes/a b.md", "#Costs & Fees")
    assert "file=06-Notes%2Fa%20b.md" in with_head
    assert with_head.endswith("&heading=Costs%20%26%20Fees")

    assert vault_views.obsidian_uri("06-Notes/a.md", "^abc123").endswith("&block=abc123")


@pytest.mark.parametrize("target, anchor", [
    ("[[note#Costs]]", "#Costs"),
    ("[[note#Costs|the costs]]", "#Costs"),
    ("[[note^abc123]]", "^abc123"),
    ("[[note|alias]]", ""),          # an alias names no position in the note
    ("06-Notes/note.md", ""),
])
def test_anchor(target, anchor):
    assert vault_views.note_anchor(target) == anchor


def test_open_hands_the_uri_to_the_os(vault, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return type("P", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(vault_views.sys, "platform", "darwin")
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    out = vault_views.open_in_obsidian("[[only-one-of-these#Costs]]")

    assert out["path"] == "06-Notes/only-one-of-these.md"
    # `-g`: the click shouldn't pull focus off the dashboard when Obsidian is
    # already up on another space.
    assert seen["cmd"][:2] == ["open", "-g"]
    assert seen["cmd"][2] == out["uri"]
    assert "heading=Costs" in out["uri"]
