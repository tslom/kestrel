"""The privacy rule for other models, as checks (New Features #86).

Claude is the only model that sees the vault, the Brain, texts, email, money
or contacts. Anything else gets a scrubbed brief, a clean environment and a
sandbox. Each test here is one way that could stop being true.
"""

import ast
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import console, console_tools, sessions
from app.delegates import boundary, registry

PKG = Path(__file__).resolve().parents[1] / "app" / "delegates"

# What each module of the package may import from the app. boundary gets
# config and nothing else: it can't leak what it can't reach. Nothing here may
# import the tools, the Brain, the data providers or the people code.
ALLOWED = {
    "boundary.py": {"config"},
    "registry.py": {"config", "store"},
    "store.py": {"db"},
    "runs.py": {"agent", "config", "boundary", "registry", "store"},
    "api.py": {"registry", "runs", "store"},
    "__init__.py": set(),
}
STARTS_A_PROCESS = {"subprocess", "create_subprocess_exec", "create_subprocess_shell",
                    "Popen", "system", "posix_spawn", "execv", "execvp"}


def _app_imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                names.add(node.module.split(".")[0])
            else:
                names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app":
            names.add("app-absolute:" + node.module)
        elif isinstance(node, ast.Import):
            names.update("app-absolute:" + a.name for a in node.names
                         if a.name.split(".")[0] == "app")
    return names


def test_every_module_is_listed():
    assert {p.name for p in PKG.glob("*.py")} == set(ALLOWED)


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_the_package_reaches_nothing_personal(name):
    extra = _app_imports(ast.parse((PKG / name).read_text())) - ALLOWED[name]
    assert not extra, f"delegates/{name} imports {sorted(extra)}: see ALLOWED in this test"


def _process_starts(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute) and node.attr in STARTS_A_PROCESS:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in STARTS_A_PROCESS:
            found.add(node.id)
    return found


def test_only_the_boundary_runs_an_agent():
    """runs/store/api start no process at all; an outside CLI can only be
    reached through boundary.launch."""
    for name in ("runs.py", "store.py", "api.py"):
        assert not _process_starts(PKG / name), name
    assert "create_subprocess_exec" in _process_starts(PKG / "boundary.py")


def test_the_registry_only_probes():
    """registry runs the CLIs too, but only to ask their version and whether
    they're signed in: every command line there is constants."""
    tree = ast.parse((PKG / "registry.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert calls
    for call in calls:
        argv = call.args[0]
        assert isinstance(argv, ast.List)
        assert all(isinstance(a, ast.Constant) for a in argv.elts[1:]), ast.unparse(call)
    assert _process_starts(PKG / "registry.py") == {"subprocess"}


def test_launch_takes_only_a_scrubbed_brief(tmp_path):
    with pytest.raises(boundary.BoundaryError, match="scrubbed"):
        asyncio.run(boundary.launch(lambda t: ["true"], "raw text", tmp_path))
    with pytest.raises(boundary.BoundaryError):
        boundary.Brief("made by hand", ())


def test_scrub_takes_out_identifiers(monkeypatch, tmp_path):
    monkeypatch.setattr(boundary, "VAULT_PATH", tmp_path / "vault")
    home = str(Path.home())
    brief = boundary.scrub(
        "Write a hero for Acme Dental. Contact jamie.lee@example.com or (213) 555-0142, "
        f"+1 213-555-0199. Notes are in {tmp_path}/vault/02-Areas/clients.md and "
        f"{home}/Desktop/a.png and ~/Documents/x.pdf. Routing 021000021. "
        "Maya Okafor signed off; maya okafor's logo is 1920 1080, #0f172a, 2026-11-22.",
        names=("Maya Okafor", "Mo"))
    t = brief.text
    for gone in ("jamie.lee", "555-0142", "555-0199", "clients.md", "Desktop", "Documents",
                 "021000021", "Okafor", "okafor", home):
        assert gone not in t, gone
    # What a design brief is made of survives.
    for kept in ("Acme Dental", "1920 1080", "#0f172a", "2026-11-22", "hero"):
        assert kept in t, kept
    assert {r.split()[1].rstrip("s") for r in brief.redactions} == {
        "email", "phone", "path", "number", "person"}


def test_scrub_leaves_single_names_and_plain_text_alone():
    brief = boundary.scrub("Mo wants five hero sections, 3 columns, 16px type.", names=("Mo",))
    assert brief.text.startswith("Mo wants") and brief.redactions == ()


def test_scrub_refuses_empty_and_oversized():
    with pytest.raises(boundary.BoundaryError):
        boundary.scrub("  ")
    with pytest.raises(boundary.BoundaryError):
        boundary.scrub("x" * (boundary.BRIEF_MAX_CHARS + 1))


def test_the_environment_is_an_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAID_SECRET", "s3cret")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "s3cret")
    monkeypatch.setenv("GEMINI_API_KEY", "theirs")
    env = boundary.clean_env(registry.PROVIDERS["gemini"].env_keys, tmp_path)
    assert env["GEMINI_API_KEY"] == "theirs"
    assert "s3cret" not in env.values()
    assert set(env) <= {*boundary.ENV_KEEP, *registry.PROVIDERS["gemini"].env_keys,
                        "TMPDIR", "NO_COLOR"}
    assert Path(env["TMPDIR"]).parent == tmp_path


def test_outside_command_lines_carry_no_tools_of_ours(tmp_path):
    for p in registry.PROVIDERS.values():
        if not p.outside:
            continue
        argv = p.argv("the brief", tmp_path, None)
        joined = " ".join(argv)
        assert "vault" not in joined.lower() and "mcp-config" not in joined
        assert p.config_dir is not None and p.install and p.login
    assert "kestrel-none" in registry.PROVIDERS["gemini"].argv("b", tmp_path, None)
    assert "--ignore-user-config" in registry.PROVIDERS["codex"].argv("b", tmp_path, None)


def test_the_tools_are_not_bridged_into_scripts():
    """run_script composes reads; a brief leaving the machine isn't one."""
    names = {t.name for t in console_tools.DELEGATE_TOOLS}
    assert names == {"delegate", "fan_out", "shortlist"}
    assert names <= {t.name for t in console_tools.ACTING_TOOLS}
    assert {n.rsplit("__", 1)[1] for n in console.DELEGATE_TOOL_NAMES} == names
    assert {n.rsplit("__", 1)[1] for n in console.DELEGATE_TOOL_NAMES} == {
        s.strip("_") for s in sessions.DELEGATE_TOOLS}


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
def test_the_sandbox_holds_against_real_files(tmp_path, monkeypatch):
    """The profile, run for real: from inside it the vault, the data
    directory and the home directory can't be read or written, and the
    scratch directory (inside the data directory) can."""
    vault, data = tmp_path / "vault", tmp_path / "data"
    scratch = data / "delegates" / "b" / "v1"
    for d in (vault, scratch):
        d.mkdir(parents=True)
    (vault / "note.md").write_text("private note")
    (data / "app.db").write_text("money")
    (scratch / "in.txt").write_text("fine")
    monkeypatch.setattr(boundary, "VAULT_PATH", vault)
    monkeypatch.setattr(boundary, "DATA_DIR", data)
    rules = boundary.profile(scratch)

    def inside(*argv):
        return subprocess.run([boundary.SANDBOX_EXEC, "-p", rules, *argv], cwd=scratch,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              env=boundary.clean_env((), scratch))

    assert inside("/bin/cat", "in.txt").stdout == "fine"
    assert inside("/bin/sh", "-c", "echo made > out.txt").returncode == 0
    assert (scratch / "out.txt").read_text() == "made\n"
    for private in (vault / "note.md", data / "app.db", Path.home() / ".zshrc",
                    Path.home() / "Library" / "Messages" / "chat.db"):
        got = inside("/bin/cat", str(private))
        assert got.returncode != 0 and "private" not in got.stdout and "money" not in got.stdout
    assert inside("/bin/ls", str(Path.home())).returncode != 0
    assert inside("/bin/ls", str(vault)).returncode != 0
    assert inside("/bin/sh", "-c", f"echo x > {vault}/planted.md").returncode != 0
    assert not (vault / "planted.md").exists()
    assert inside("/bin/sh", "-c", f"echo x > {data}/planted").returncode != 0


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
def test_launch_runs_a_cli_inside_it(tmp_path, monkeypatch):
    """launch end to end with a stand-in CLI: it gets the scrubbed text, a
    clean environment and the sandbox."""
    vault, data = tmp_path / "vault", tmp_path / "data"
    scratch = data / "delegates" / "b" / "v1"
    vault.mkdir()
    (vault / "note.md").write_text("private note")
    monkeypatch.setattr(boundary, "VAULT_PATH", vault)
    monkeypatch.setattr(boundary, "DATA_DIR", data)
    monkeypatch.setenv("PLAID_SECRET", "s3cret")
    script = (f"cat; echo; echo \"$1\"; echo \"secret=[$PLAID_SECRET]\"; "
              f"cat {vault}/note.md 2>/dev/null || echo walled; echo hi > made.txt")
    done = asyncio.run(boundary.launch(
        lambda text: ["sh", "-c", script, "sh", text],
        boundary.scrub("mail a@b.co about it"), scratch, stdin_brief=True, timeout=20))
    assert done.code == 0, done.stderr
    assert done.stdout.count("mail [email] about it") == 2
    assert "secret=[]" in done.stdout and "walled" in done.stdout
    assert "private note" not in done.stdout
    assert (scratch / "made.txt").exists()
    assert os.path.realpath(scratch) in boundary.profile(scratch)
