"""Which built-in tools the console is handed — and why the list is a list.

`tools=` is easy to drop when editing build_options, and nothing fails when you
do: the session still works, every vault tool still loads, the tests still pass.
The only symptom is the floor going back to 28,561 tokens and staying there,
paid again on every assistant message, which no test would otherwise notice.

So what's pinned here is the omission, not the inclusion: the preset's twenty
unused tools must stay out, and ToolSearch must stay in.
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
    """The 31 vault tools and the claude.ai connectors are deferred — ToolSearch
    is the only way a session reaches them. Without it every writer is stranded,
    and the failure looks like the model claiming the capability doesn't exist."""
    assert "ToolSearch" in CONSOLE_BUILTIN_TOOLS


def test_the_clis_own_furniture_stays_out():
    """These are what the trim is for. A console over a vault cannot spawn
    agents, manage worktrees or write crontabs, and no session on record has
    tried; carrying their schemas costs ~4.7k tokens on every message."""
    for tool in ("Task", "TaskOutput", "TaskStop", "CronCreate", "CronList",
                 "EnterWorktree", "ExitWorktree", "EnterPlanMode",
                 "Monitor", "SendMessage", "NotebookEdit", "DesignSync"):
        assert tool not in CONSOLE_BUILTIN_TOOLS


def test_skill_stays_out():
    """Dropping Skill is what drops the Skills block: all 14 load through
    setting_sources and 13 are this repo's dev skills. The vault's own
    daily-brief is built backend-side by brief.py, not from a console turn.
    If that ever changes this assert is the thing to delete, knowingly."""
    assert "Skill" not in CONSOLE_BUILTIN_TOOLS


def test_allowed_tools_is_a_subset_of_what_exists():
    """CONSOLE_ALLOWED_TOOLS names built-ins that skip the approval card. Any
    built-in there that `tools=` no longer hands out is a dead entry, and reads
    as a permission that is granted when it is simply unreachable."""
    opts = _options()
    granted = set(opts.tools) | {t for t in opts.allowed_tools if t.startswith("mcp__")}
    assert set(opts.allowed_tools) <= granted
