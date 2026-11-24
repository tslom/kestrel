"""Which built-in tools the console is handed — and why the list is a list.

Dropping `tools=` from build_options breaks nothing visible; the floor just returns to
28,561 tokens per message. Pins the omission: the preset's twenty unused tools stay
out, ToolSearch stays in.
"""

from app import console
from app.config import CONSOLE_BUILTIN_TOOLS


async def _deny(*a, **k):
    return {"behavior": "deny", "message": "test"}


async def _noop(*a, **k):
    return {}


def _options():
    return console.build_options(None, None, "default", _deny, _noop)


def test_build_options_passes_the_trimmed_list():
    """Unset, `tools=` means the whole claude_code preset. It has to be named."""
    assert _options().tools == CONSOLE_BUILTIN_TOOLS


def test_toolsearch_survives():
    """Vault tools and connectors are deferred; ToolSearch is the only way to reach them."""
    assert "ToolSearch" in CONSOLE_BUILTIN_TOOLS


def test_the_clis_own_furniture_stays_out():
    """The CLI's agent/worktree/cron tools stay out (~4.7k tokens per message)."""
    for tool in ("Task", "TaskOutput", "TaskStop", "CronCreate", "CronList",
                 "EnterWorktree", "ExitWorktree", "EnterPlanMode",
                 "Monitor", "SendMessage", "NotebookEdit", "DesignSync"):
        assert tool not in CONSOLE_BUILTIN_TOOLS


def test_skill_stays_out():
    """Dropping Skill drops the Skills block (13 of 14 are dev skills; daily-brief is built by brief.py)."""
    assert "Skill" not in CONSOLE_BUILTIN_TOOLS


def test_allowed_tools_is_a_subset_of_what_exists():
    """CONSOLE_ALLOWED_TOOLS has no entry `tools=` no longer hands out (a dead permission)."""
    opts = _options()
    granted = set(opts.tools) | {t for t in opts.allowed_tools if t.startswith("mcp__")}
    assert set(opts.allowed_tools) <= granted
