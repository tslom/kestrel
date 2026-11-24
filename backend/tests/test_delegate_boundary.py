"""The privacy rule for other models, as checks.

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

# What each module may import from the app: boundary gets config only (it can't leak what
# it can't reach); none may import the tools, Brain, providers or people code.
ALLOWED = {
    "boundary.py": {"config"},
    "registry.py": {"config", "store", "boundary"},
    "store.py": {"db"},
    "accounts.py": {"boundary", "registry", "store"},
    "runs.py": {"agent", "config", "accounts", "boundary", "registry", "store"},
    "api.py": {"accounts", "boundary", "registry", "runs", "store"},
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
    """runs/store/api/accounts start no process; an outside CLI is reached only via boundary.launch or start_login."""
    for name in ("runs.py", "store.py", "api.py", "accounts.py"):
        assert not _process_starts(PKG / name), name
    assert "create_subprocess_exec" in _process_starts(PKG / "boundary.py")


def test_the_registry_only_probes():
    """registry only asks version/sign-in: one subprocess.run in _probe_run, constant args, boundary's env allowlist."""
    tree = ast.parse((PKG / "registry.py").read_text())
    funcs = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    runs_at = [f for f in funcs.values() for n in ast.walk(f) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert [f.name for f in runs_at] == ["_probe_run"]
    assert "boundary.clean_env()" in ast.unparse(funcs["_probe_run"])
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_probe_run"]
    assert len(calls) >= 3
    for call in calls:
        argv = call.args[0]
        assert isinstance(argv, ast.List)
        assert all(isinstance(a, ast.Constant) for a in argv.elts[1:]), ast.unparse(call)
    assert _process_starts(PKG / "registry.py") == {"subprocess"}


def test_probes_get_the_allowlist_not_the_backend_env(monkeypatch):
    monkeypatch.setenv("PLAID_SECRET", "s3cret")
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    seen = {}

    def run(argv, **kw):
        seen[argv[0]] = kw["env"]
        return subprocess.CompletedProcess(argv, 0, "x 1.0", "")
    monkeypatch.setattr(registry.subprocess, "run", run)
    registry._version("agy")
    assert "s3cret" not in seen["agy"].values() and "sk-test" not in seen["agy"].values()


def test_api_keys_go_to_their_own_cli_only(monkeypatch, tmp_path):
    """A key reaches only the CLI that uses it, by the name it reads (codex: CODEX_API_KEY, not OPENAI_API_KEY)."""
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    env_of = {pid: boundary.clean_env(p.env_keys, tmp_path, p.env_set)
              for pid, p in registry.PROVIDERS.items() if p.outside}
    assert env_of["codex"].get("CODEX_API_KEY") == "sk-codex"
    for pid, env in env_of.items():
        assert "sk-openai" not in env.values(), pid
        if pid != "codex":
            assert "sk-codex" not in env.values(), pid
    assert "g-key" not in env_of["codex"].values()
    assert registry._codex_signed_in()[0]
    for p in registry.PROVIDERS.values():
        assert not any(k.endswith("KEY") for k, _ in p.env_set), p.id


def test_antigravity_runs_in_a_home_of_its_own(tmp_path):
    """agy gets its own HOME and keychain; the login keychain stays shut."""
    p = registry.PROVIDERS["antigravity"]
    env = boundary.clean_env(p.env_keys, tmp_path, p.env_set)
    assert env["HOME"] == str(registry.AGY_HOME) != str(Path.home())
    assert p.config_dir == registry.AGY_HOME
    assert Path.home() in registry.AGY_HOME.parents
    keychains = Path.home() / "Library" / "Keychains"
    assert keychains not in registry.AGY_HOME.parents and registry.AGY_HOME != keychains
    argv = p.argv("/brief", tmp_path, "gemini-x")
    assert argv[:2] == ["agy", "--print=/brief"]
    assert {"--disable-slash-commands", "--output-format", "stream-json"} <= set(argv)
    assert "--sandbox" not in argv and "--remote-agent" not in argv


def test_launch_takes_only_a_scrubbed_brief(tmp_path):
    with pytest.raises(boundary.BoundaryError, match="scrubbed"):
        asyncio.run(boundary.launch(lambda t: ["true"], "raw text", tmp_path))
    with pytest.raises(boundary.BoundaryError):
        boundary.Brief("made by hand", ())


# ---------- the gate: what kind of work may leave at all ----------

def test_launch_takes_only_a_brief_that_passed_the_gate(tmp_path):
    """scrub without a purpose is for Claude's own variants; launch refuses it."""
    with pytest.raises(boundary.BoundaryError, match="passed the gate"):
        asyncio.run(boundary.launch(lambda t: ["true"], boundary.scrub("tidy this"), tmp_path))


@pytest.mark.parametrize("purpose", ["", "design", "feature-idea", "personal", "essay"])
def test_work_outside_the_purposes_is_refused(purpose):
    """A project's own ideas can't be read off the text, so anything that
    isn't one of the grunt-work kinds stays with Claude."""
    with pytest.raises(boundary.BoundaryError, match="purpose must be one of"):
        boundary.scrub("Plan how the new feature should work", purpose=purpose)


def test_a_study_tab_sends_nothing():
    with pytest.raises(boundary.BoundaryError, match="study tab"):
        boundary.scrub("Reformat this JSON", purpose="format",
                       origin=boundary.Origin(profile="study"))


@pytest.mark.parametrize("brief", [
    "Write a 1,200-word draft for WRIT 150 on remote work",
    "Solve question 3 of the CSCI-103 lab",
    "Here's my homework: integrate x^2 sin x",
    "Finish my problem set on graph search",
    "Draft an essay on urban heat islands",
    "Summarise the rubric and what the grader wants",
    "Make flashcards from these Brightspace module notes",
    "It's due date is Friday, write the reflection",
    "Tidy the notes in 02-Areas/usc/fall-2026/MATH-229.md",
])
def test_coursework_is_refused(brief):
    with pytest.raises(boundary.BoundaryError, match="coursework"):
        boundary.scrub(brief, purpose="format")


def test_the_users_own_course_codes_count(tmp_path, monkeypatch):
    """A subject the fixed list doesn't know still counts once the vault has
    a course note for it."""
    term = tmp_path / "02-Areas" / "usc" / "spring-2027"
    term.mkdir(parents=True)
    (term / "ZOOL-301.md").write_text("private")
    monkeypatch.setattr(boundary, "VAULT_PATH", tmp_path)
    assert "ZOOL" in boundary.course_codes()
    with pytest.raises(boundary.BoundaryError, match="ZOOL 301"):
        boundary.scrub("Make a table for ZOOL 301 species", purpose="format")


@pytest.mark.parametrize("brief", [
    "Refactor every destructuring assignment in this file to named imports",
    "Boilerplate for an HTTP 404 handler in FastAPI",
    "Summarise RFC 9110's caching section in ten bullets",
    "Write pytest scaffolding for a function that parses ISO 8601 dates",
])
def test_grunt_work_passes(brief):
    assert boundary.scrub(brief, purpose="boilerplate").purpose == "boilerplate"


@pytest.mark.parametrize("brief, what", [
    ("Write a birthday message for Jordan Lee", "person"),
    ("Turn [[2026-11-20]] into a bullet list", "vault link"),
    ("Format this: deliver to 1420 Ocean Avenue by noon", "street address"),
    ("Clean up the table in 01-Projects/plan", "vault folder"),
])
def test_personal_details_left_after_the_scrub_are_refused(brief, what):
    with pytest.raises(boundary.BoundaryError, match=f"personal details.*{what}"):
        boundary.scrub(brief, names=("Jordan Lee",), purpose="format")


def test_identifiers_are_still_scrubbed_not_refused():
    """An email or a path is replaced, as before: it's an identifier, not the
    point of the work (sample data in a test scaffold)."""
    brief = boundary.scrub("Validate addresses like sam@example.com", purpose="tests")
    assert "[email]" in brief.text and brief.redactions == ("1 email",)


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
    env = boundary.clean_env(registry.PROVIDERS["antigravity"].env_keys, tmp_path)
    assert env["GEMINI_API_KEY"] == "theirs"
    assert "s3cret" not in env.values()
    assert set(env) <= {*boundary.ENV_KEEP, *registry.PROVIDERS["antigravity"].env_keys,
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
        assert all(isinstance(w, str) and w for w in p.login)
    assert "--ignore-user-config" in registry.PROVIDERS["codex"].argv("b", tmp_path, None)
    assert {p.id for p in registry.PROVIDERS.values() if p.outside} == {"antigravity", "codex"}


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
    """The profile run for real: vault, data dir and home are walled off; the scratch dir (inside data) is open."""
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
def test_the_login_keychain_stays_shut_with_a_config_dir_open(tmp_path):
    """With agy's home open, the login keychain stays unreadable (file or /usr/bin/security), even with HOME pointed back."""
    login = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    if not login.exists():
        pytest.skip("no login keychain here")
    scratch = tmp_path / "s"
    scratch.mkdir()
    rules = boundary.profile(scratch, (registry.AGY_HOME,))
    env = boundary.clean_env((), scratch)        # HOME is the real one here

    def inside(*argv):
        return subprocess.run([boundary.SANDBOX_EXEC, "-p", rules, *argv], cwd=scratch,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)

    assert inside("/bin/cat", str(login)).returncode != 0
    assert inside("/usr/bin/security", "show-keychain-info", str(login)).returncode != 0
    assert str(login) not in inside("/usr/bin/security", "list-keychains").stdout


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
        boundary.scrub("mail a@b.co about it", purpose="format"), scratch, stdin_brief=True, timeout=20))
    assert done.code == 0, done.stderr
    assert done.stdout.count("mail [email] about it") == 2
    assert "secret=[]" in done.stdout and "walled" in done.stdout
    assert "private note" not in done.stdout
    assert (scratch / "made.txt").exists()
    assert os.path.realpath(scratch) in boundary.profile(scratch)


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
@pytest.mark.parametrize("cap", ["turns", "time"])
def test_an_agent_that_keeps_going_is_cut_off(tmp_path, monkeypatch, cap):
    """A never-ending stand-in is killed at the turn cap or the clock; output kept, cut named."""
    monkeypatch.setattr(boundary, "DATA_DIR", tmp_path / "data")
    scratch = tmp_path / "data" / "delegates" / "b" / "v1"
    loop = "i=0; while true; do i=$((i+1)); echo turn $i; sleep 0.05; done"
    done = asyncio.run(boundary.launch(
        lambda text: ["sh", "-c", loop], boundary.scrub("go", purpose="variants"), scratch,
        timeout=60 if cap == "turns" else 1,
        ends_turn=(lambda line: line.startswith("turn")) if cap == "turns" else None,
        max_turns=5))
    assert done.cut == cap and done.code is None
    assert done.stdout.startswith("turn 1\n")
    if cap == "turns":
        # A turn's tool calls run after its closing line, so the sixth's line is the stop.
        assert done.stdout.splitlines()[-1] == "turn 6"


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
def test_the_last_allowed_turns_writes_land(tmp_path, monkeypatch):
    """agy writes after a turn's line: a cap of 2 keeps both writes, the third turn is cut."""
    monkeypatch.setattr(boundary, "DATA_DIR", tmp_path / "data")
    scratch = tmp_path / "data" / "delegates" / "b" / "v1"
    script = ("for i in 1 2 3; do echo DONE $i; sleep 0.1; echo part $i >> notes.md; done; "
              "sleep 5")
    done = asyncio.run(boundary.launch(
        lambda text: ["sh", "-c", script], boundary.scrub("go", purpose="variants"), scratch, timeout=30,
        ends_turn=lambda line: line.startswith("DONE"), max_turns=2))
    assert done.cut == "turns"
    assert (scratch / "notes.md").read_text().splitlines()[:2] == ["part 1", "part 2"]


def test_a_config_dirs_parents_get_metadata_only(tmp_path):
    """SQLite lstats each path component: the home's parents get file-read-metadata literals, no more."""
    rules = boundary.profile(tmp_path, (registry.AGY_HOME,))
    meta = next(line for line in rules.splitlines() if line.startswith("(allow file-read-metadata"))
    for parent in registry.AGY_HOME.parents:
        assert f'(literal "{os.path.realpath(parent)}")' in meta
    assert f'(subpath "{os.path.realpath(registry.AGY_HOME.parent)}")' not in rules


# ---------- signing in ----------

# Reports the sandbox's verdict: seatbelt checks before the kernel, so EADDRINUSE means
# allowed and only EPERM is a refusal (1455 is busy while a real codex sign-in runs).
_BIND = ("import socket,sys,errno\nfor p in sys.argv[1:]:\n s=socket.socket()\n try:\n"
         "  s.bind(('127.0.0.1',int(p)));print(p,'ok')\n except OSError as e:\n"
         "  print(p,'ok' if e.errno==errno.EADDRINUSE else 'denied' if e.errno==errno.EPERM"
         " else errno.errorcode.get(e.errno,e.errno))\n s.close()")


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _in_login_sandbox(tmp_path, ports, browser, argv):
    home = tmp_path / "home"
    (home / ".login").mkdir(parents=True, exist_ok=True)
    rules = boundary.login_profile(home, argv[0], ports, browser)
    return subprocess.run([boundary.SANDBOX_EXEC, "-p", rules, *argv], capture_output=True,
                          text=True, timeout=30, cwd=home / ".login").stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
def test_a_sign_in_listens_only_where_it_may(tmp_path):
    """codex binds its two callback ports only; agy any loopback port; device-code none. Real ports, not random: seatbelt refuses some literal allows (boundary.login_profile)."""
    a, b = registry.CODEX_LOGIN_PORTS
    other = _free_port()
    py = "/usr/bin/python3"     # the venv's lives under the home directory, walled off
    said = _in_login_sandbox(tmp_path, (a, b), False, [py, "-c", _BIND, str(a), str(b), str(other)])
    assert said.split() == [str(a), "ok", str(b), "ok", str(other), "denied"]
    said = _in_login_sandbox(tmp_path, None, True, [py, "-c", _BIND, str(other)])
    assert said.split() == [str(other), "ok"]
    said = _in_login_sandbox(tmp_path, (), False, [py, "-c", _BIND, str(a)])
    assert said.split() == [str(a), "denied"]


@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox is macOS seatbelt")
def test_a_sign_in_kestrel_opens_cant_open_the_browser_itself(tmp_path):
    """The sign-in can't reach LaunchServices (read-only probe: which app opens a URL)."""
    script = ("ObjC.import('AppKit');var a=$.NSWorkspace.sharedWorkspace"
              ".URLForApplicationToOpenURL($.NSURL.URLWithString('https://example.com'));"
              "console.log(a.isNil()?'none':'app')")
    js = ["/usr/bin/osascript", "-l", "JavaScript", "-e", script]
    shut = subprocess.run([boundary.SANDBOX_EXEC, "-p", boundary.login_profile(
        tmp_path, "osascript", (), False), *js], capture_output=True, text=True, timeout=30)
    assert "none" in shut.stderr + shut.stdout


@pytest.mark.parametrize("url", [
    "https://evil.example/oauth", "http://auth.openai.com/x", "file:///etc/passwd",
    "https://auth.openai.com.evil.example/x", "javascript:alert(1)", ""])
def test_kestrel_opens_only_sign_in_pages(url, monkeypatch):
    ran = []
    monkeypatch.setattr(boundary.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(boundary.BoundaryError):
        boundary.open_sign_in(url)
    assert not ran
