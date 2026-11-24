"""Central config: everything the app needs to know about *your* setup.

Env vars come from backend/.env (see .env.example) plus the shell; a real env
var wins over .env. Secrets belong in .env, never here or in git.
Reasons for the non-obvious values: DESIGN.md → `app/config.py`.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/vault"))).resolve()

# Vault conventions
GENERATED_DIR = "06-Generated"         # agent-created notes land here until reviewed
TASKS_VIEW_FILE = "tasks.md"           # GENERATED read-only view of the tasks DB
BRAIN_DIR = "07-Brain"                 # GENERATED read-only view of app.db brain_facts

# Hours before this count toward the previous day (local time).
DAY_ROLLOVER_HOUR = 4

DEBT_NUDGE_DAYS = int(os.environ.get("DEBT_NUDGE_DAYS", "7"))

# Backend-written export: the webview has downloads off.
EXPENSES_DIR = Path(os.environ.get("EXPENSES_DIR", Path.home() / "Downloads"))

# index.db is derived; app.db is the real data.
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
INDEX_DB = DATA_DIR / "index.db"
APP_DB = DATA_DIR / "app.db"

PROVIDER_REFRESH_SECONDS = int(os.environ.get("PROVIDER_REFRESH_SECONDS", "300"))

# Must match uvicorn --port (baked into the OAuth redirect URI).
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8788"))

# Set by launch_app; absent means another server holds the port.
HELD_RELOAD_ENV = "KESTREL_HELD_RELOAD"
# Write end of launch_app's pipe to the window process; absent: osascript.
NOTIFY_FD_ENV = "KESTREL_NOTIFY_FD"
# Popped, not read: children would inherit the name without the pipe.
NOTIFY_FD = os.environ.pop(NOTIFY_FD_ENV, None)

# None = every calendar on the account.
GOOGLE_CALENDAR_ACCOUNT = "school"
GOOGLE_CALENDAR_IDS: list[str] | None = None

GOOGLE_HEALTH_ACCOUNT = "personal"

GMAIL_ACCOUNT = "school"
# Every *_MODEL is a name model_catalog resolves per call: a tier ("grunt"), family alias, or pinned id.
EMAIL_SUMMARY_MODEL = os.environ.get("EMAIL_SUMMARY_MODEL", "grunt")
EMAIL_SUMMARY_BATCH = 20          # messages per summarizer call
EMAIL_BODY_EXCERPT_CHARS = 1500   # body text sent per message (never stored)
EMAIL_BACKFILL_PER_REFRESH = 60
EMAIL_BODY_FULL_CHARS = 20000

REPLY_DRAFT_MODEL = os.environ.get("REPLY_DRAFT_MODEL", "sonnet")

# Automatic model calls pause at this window fill (percent); agent.interactive() ignores it.
AUTO_PAUSE_PCT = {
    "five_hour": float(os.environ.get("AUTO_PAUSE_5H_PCT", "80")),
    "seven_day": float(os.environ.get("AUTO_PAUSE_7D_PCT", "85")),
}

BRIEF_MODEL = os.environ.get("BRIEF_MODEL", "sonnet")
BRIEF_STALE_GENERATING_SECONDS = 300  # backend died mid-run -> reclaimable

BRAIN_INGEST_MODEL = os.environ.get("BRAIN_INGEST_MODEL", "sonnet")
BRAIN_LINT_MODEL = os.environ.get("BRAIN_LINT_MODEL", "sonnet")
BRAIN_INFERRED_STATUS = "pending"
BRAIN_GROUP_MIN_MINE = 5
BRAIN_BACKFILL_DAYS = 365

# Auto check-in needs BOTH thresholds (0 disables); AZM alone isn't a workout signal.
EXERCISE_HABIT_NAME = os.environ.get("EXERCISE_HABIT_NAME", "Exercise")
EXERCISE_AZM_THRESHOLD = float(os.environ.get("EXERCISE_AZM_THRESHOLD", "30"))
EXERCISE_HR_MAX_THRESHOLD = float(
    os.environ.get("EXERCISE_HR_MAX_THRESHOLD", "180"))

SLEEP_HABIT_NAME = os.environ.get("SLEEP_HABIT_NAME", "Sleep")
SLEEP_HOURS_THRESHOLD = float(os.environ.get("SLEEP_HOURS_THRESHOLD", "9"))

# Plaid client_id/secret live in app.db.
PLAID_COUNTRY_CODES = ["US"]
PLAID_DAYS_REQUESTED = 730   # link-token transaction history window (Plaid max)

# Fallback origin; the US-centre default means "unset".
HOME_LAT = float(os.environ.get("HOME_LAT", "39.8283"))
HOME_LON = float(os.environ.get("HOME_LON", "-98.5795"))

# Empty key or API failure gives leave_by null, never a hard failure.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
LEAVE_BUFFER_MIN = int(os.environ.get("LEAVE_BUFFER_MIN", "10"))
LEAVE_FIX_MAX_MIN = 30
# Appended to a location that geocodes to nothing, retried once; empty = no retry.
LEAVE_GEOCODE_CONTEXT = os.environ.get("LEAVE_GEOCODE_CONTEXT", "")

# Redirect URI must stay the 127.0.0.1 literal (Spotify bans localhost).
SPOTIFY_ACCOUNT = "personal"
SPOTIFY_REDIRECT_URI = f"http://127.0.0.1:{BACKEND_PORT}/api/oauth/spotify"
SPOTIFY_SCOPES = "user-read-recently-played user-top-read user-library-read"

# Unset key = provider "off".
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_ENRICH_PER_CYCLE = int(os.environ.get("LASTFM_ENRICH_PER_CYCLE", "25"))
LASTFM_MIN_TAG_WEIGHT = 10   # 0-100, relative to the artist's own top tag
LASTFM_MAX_TAGS = 8

# chat.db needs Full Disk Access on the process running uvicorn.
CHAT_DB_PATH = Path(os.path.expanduser("~/Library/Messages/chat.db"))
ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")
MESSAGES_BACKFILL_DAYS = 90

# Own numbers/emails, normalized like `messages.handle` (phone: last 10 digits; email: lowercase). Empty: no-op.
MESSAGES_SELF_HANDLES: list[str] = [
    h.strip() for h in os.environ.get("MESSAGES_SELF_HANDLES", "").split(",") if h.strip()]
SELFTEXT_MODEL = os.environ.get("SELFTEXT_MODEL", "grunt")
# A console tab's "regenerate title" (app/session_digest.py): one short turn
# per click.
TAB_TITLE_MODEL = os.environ.get("TAB_TITLE_MODEL", "grunt")

# Deliveries + travel extractors (providers/email_extract.py): the model sees only messages
# matching these hints, never the whole inbox. LIKE is case-insensitive.
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "grunt")
EXTRACT_BATCH = 20
# Food (app/food_estimate.py): once per distinct item name, so a normal day is single digits.
FOOD_ESTIMATE_MODEL = os.environ.get("FOOD_ESTIMATE_MODEL", "grunt")
PACKAGE_SENDER_HINTS: list[str] = [
    "amazon", "usps", "ups", "fedex", "shop", "order", "shipped",
    "delivery", "out for delivery", "tracking",
]
TRIP_SENDER_DOMAINS: list[str] = ["aa.com"]   # aa.com first — the live account

# Matched against sender OR subject; loose on purpose (a miss leaves hours stale).
OFFICE_HOURS_SENDER_HINTS: list[str] = [
    "piazza", "canvas", "instructure", "usc.edu", "blackboard", "gradescope",
    "office hour", "office hours", "oh ", " oh", "section", "recitation",
    "no class", "cancelled", "canceled", "rescheduled", "moved",
]

# MAX_BYTES is not containment. WORKER_MEM is enforced by parent RSS polling (macOS rejects
# rlimits), so the ceiling is this plus one attachments._RSS_POLL_S gap. VISION = 5MB API limit / (4/3).
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENT_WORKER_MEM_BYTES = 1024 * 1024 * 1024
ATTACHMENT_VISION_MAX_BYTES = 3_500_000
ATTACHMENT_EXTRACT_TIMEOUT_S = 30
ATTACHMENT_VISION_MODEL = os.environ.get("ATTACHMENT_VISION_MODEL", "grunt")

# AeroAPI is optional. Poll flights within HORIZON, every INTERVAL until CLOSE, then every cycle.
AEROAPI_KEY = os.environ.get("AEROAPI_KEY")
FLIGHT_POLL_HORIZON_HOURS = 48
FLIGHT_POLL_INTERVAL_HOURS = 1
FLIGHT_POLL_CLOSE_HOURS = 6
FLIGHT_CHECKIN_LEAD_HOURS = 24

# Dry-run defaults off: a channel that never fires tests nothing.
ALARMS_DRY_RUN = os.environ.get("ALARMS_DRY_RUN", "0") != "0"
ALARM_HORIZON_DAYS = int(os.environ.get("ALARM_HORIZON_DAYS", "14"))
# Dedicated list: the reconciler deletes everything in it. Must be the syncing account.
ALARM_REMINDERS_LIST = os.environ.get("ALARM_REMINDERS_LIST", "Kestrel")
ALARM_REMINDERS_ACCOUNT = os.environ.get("ALARM_REMINDERS_ACCOUNT", "iCloud")
# Runaway guard: this many withdrawals at once skips ALL of them this cycle.
ALARM_MAX_WITHDRAW_PER_CYCLE = int(
    os.environ.get("ALARM_MAX_WITHDRAW_PER_CYCLE", "5"))
# Packages have no clock; this hour in the user's zone (tz.zone_on).
PACKAGE_ALARM_HOUR = int(os.environ.get("PACKAGE_ALARM_HOUR", "9"))
# Deadband: leave_by jitters with traffic, and each amendment rewrites the alarm.
LEAVE_ALARM_TOLERANCE_MIN = int(
    os.environ.get("LEAVE_ALARM_TOLERANCE_MIN", "3"))

NOTIFY_ENABLED = os.environ.get("NOTIFY_ENABLED", "1") != "0"
# aw-watcher-afk's idle limit.
NOTIFY_IDLE_MAX_SEC = int(os.environ.get("NOTIFY_IDLE_MAX_SEC", "180"))
# Older than this a notification expires rather than fires stale.
NOTIFY_STALE_MIN = int(os.environ.get("NOTIFY_STALE_MIN", "45"))
# Burst cap; defers the rest to next cycle.
NOTIFY_MAX_PER_CYCLE = int(os.environ.get("NOTIFY_MAX_PER_CYCLE", "5"))

# Needs the sudoers drop-in for `pmset schedule`; absent, apply() logs and retries.
WAKE_DRY_RUN = os.environ.get("WAKE_DRY_RUN", "0") != "0"
WAKE_ENABLED = os.environ.get("WAKE_ENABLED", "1") != "0"
# The only owner we cancel; never `cancelall` (the schedule is shared with the system).
WAKE_OWNER = os.environ.get("WAKE_OWNER", "com.kestrel.alarms")
# Must cover a whole cycle plus iCloud sync.
WAKE_LEAD_MIN = int(os.environ.get("WAKE_LEAD_MIN", "20"))
# Short on purpose: a wake spins up the machine.
WAKE_HORIZON_HOURS = int(os.environ.get("WAKE_HORIZON_HOURS", "36"))
WAKE_COALESCE_MIN = int(os.environ.get("WAKE_COALESCE_MIN", "30"))
# Deadband: each move is a cancel plus an add.
WAKE_TOLERANCE_MIN = int(os.environ.get("WAKE_TOLERANCE_MIN", "5"))
# Truncates the least urgent.
WAKE_MAX_EVENTS = int(os.environ.get("WAKE_MAX_EVENTS", "6"))

# TOKEN is the relay Worker's PULL_TOKEN. Both unset = "off".
LOCATION_RELAY_URL = os.environ.get("LOCATION_RELAY_URL")
LOCATION_RELAY_TOKEN = os.environ.get("LOCATION_RELAY_TOKEN")
# Not 150: HOME's own fixes spread ~127m, which made at_home flicker.
HOME_RADIUS_M = 250

# Slugs must match a "file" entry in the Kite index minus .json.
NEWS_CATEGORIES: list[str] = ["world", "usa", "tech"]
NEWS_HEADLINES_PER_CATEGORY = 5

ATHLETICS_ICS_URL = os.environ.get(
    "ATHLETICS_ICS_URL",
    "https://usctrojans.com/calendar.ashx/calendar.ics?sport_id=0")

# Carries a per-user token: .env only.
BRIGHTSPACE_ICS_URL = os.environ.get("BRIGHTSPACE_ICS_URL", "")

REDDIT_SUBREDDITS: list[str] = ["onebag"]
REDDIT_TOP_PER_DAY = 5
# Stored whole, trimmed on output; --query sees only the first N chars.
REDDIT_SELFTEXT_CHARS = 600
# First-day scores read 1; days inside this window re-sync and report `provisional: true`.
REDDIT_SCORE_MATURE_DAYS = 3
REDDIT_REFRESH_MIN = 60
# Pages of 100; only stops a pagination bug.
REDDIT_MAX_PAGES = 20

GOOGLE_ACCOUNT_SCOPES: dict[str, list[str]] = {
    # calendar.events: routes/calendar.py create/move/RSVP
    "school": [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    "personal": [
        "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
        "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    ],
}

# Console / Claude Agent SDK
#
# `auto` (console.AUTO_MODE), not `acceptEdits`: that runs workspace Bash writes with no card.
PERMISSION_MODE = os.environ.get("CONSOLE_PERMISSION_MODE", "auto")
SETTING_SOURCES = ["user", "project"]  # loads ~/.claude + vault/.claude (your skills)
DEFAULT_MODEL = os.environ.get("CONSOLE_MODEL", "sonnet")
DEV_MODEL = os.environ.get("DEV_CONSOLE_MODEL", "opus")

# The installed binary, not the SDK's bundled one (its WebSearch hangs ~5.5 min); missing falls back.
CONSOLE_CLI_PATH = Path(os.environ.get(
    "CONSOLE_CLI_PATH", os.path.expanduser("~/.local/bin/claude")))

CONSOLE_CHROME = os.environ.get("CONSOLE_CHROME", "1") != "0"
CHROME_PROFILES = ("general", "dev")

# Outside the repo on purpose: the CLI's project is the enclosing git root.
COMPUTER_DIR = Path.home() / ".kestrel" / "computer"
COMPUTER_MODEL = os.environ.get("COMPUTER_MODEL", "sonnet")
COMPUTER_JOB_LIMIT_S = 15 * 60
COMPUTER_SETUP_LIMIT_S = 5 * 60

# SDK allowed_tools skips can_use_tool: only unattended-safe entries. Bash is absent on
# purpose (console_policy.is_safe_read() judges whole commands).
CONSOLE_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "mcp__claude_ai_Google_Calendar__list_events",
    "mcp__claude_ai_Google_Calendar__search_events",
    "mcp__claude_ai_Google_Calendar__get_event",
    "mcp__claude_ai_Google_Calendar__list_calendars",
    "mcp__claude_ai_Gmail__search_threads",
]

# The preset's 30 cost 14,165 tokens/message; these ten are all ever called. ToolSearch must
# stay (deferred tools load through it); `Skill` is absent on purpose (1,990 tokens).
CONSOLE_BUILTIN_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Grep",
    "Glob",
    "Bash",
    "WebSearch",
    "WebFetch",
    "ToolSearch",
    "AskUserQuestion",
]

# TodoWrite is gone from the CLI (2.1.312), hence the Task* tools.
TASK_TOOLS = ["TaskCreate", "TaskUpdate", "TaskList", "TaskGet"]
DEV_BUILTIN_TOOLS = CONSOLE_BUILTIN_TOOLS + ["Agent", "NotebookEdit", "Skill"] + TASK_TOOLS

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

DEV_PROJECT_ROOTS = [Path(os.path.expanduser(p)) for p in os.environ.get(
    "DEV_PROJECT_ROOTS", "~/Projects").split(":") if p]

# Wake word: an openWakeWord name or a .onnx path; falls back to hey_jarvis while WAKE_MODEL
# is missing. The wake threshold also governs barge-in (docs/wake-word.md).
VOICE_STT_MODEL = os.environ.get("VOICE_STT_MODEL",
                                 "mlx-community/parakeet-tdt-0.6b-v3")
WAKE_MODEL = DATA_DIR / "wake" / "hey_kestrel.onnx"
VOICE_WAKE_WORD = os.environ.get("VOICE_WAKE_WORD") or (
    str(WAKE_MODEL) if WAKE_MODEL.is_file() else "hey_jarvis")
VOICE_WAKE_THRESHOLD = float(os.environ.get("VOICE_WAKE_THRESHOLD", "0.5"))
VOICE_VAD_THRESHOLD = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.5"))

# Endpointing: PATIENT silence until SETTLED_SPEECH_MS of speech, then the short one
# (Listener.silence_needed); under MIN_SPEECH_MS is dropped (parakeet invents words for coughs).
VOICE_SILENCE_MS = int(os.environ.get("VOICE_SILENCE_MS", "900"))
VOICE_SILENCE_PATIENT_MS = int(os.environ.get("VOICE_SILENCE_PATIENT_MS", "2200"))
VOICE_SETTLED_SPEECH_MS = int(os.environ.get("VOICE_SETTLED_SPEECH_MS", "2500"))
VOICE_MIN_SPEECH_MS = int(os.environ.get("VOICE_MIN_SPEECH_MS", "300"))
VOICE_MAX_UTTERANCE_S = float(os.environ.get("VOICE_MAX_UTTERANCE_S", "30"))
# Wake fired but nobody spoke; errs long.
VOICE_WAKE_TIMEOUT_S = float(os.environ.get("VOICE_WAKE_TIMEOUT_S", "12"))

# `say` fallback voice: "auto" = first installed of PREFS; empty = system default (DESIGN.md → Voice).
VOICE_TTS_VOICE = os.environ.get("VOICE_TTS_VOICE", "auto")
VOICE_TTS_VOICE_PREFS = ["Serena", "Kate", "Karen", "Moira", "Tessa"]
VOICE_TTS_RATE = int(os.environ.get("VOICE_TTS_RATE", "0"))   # words/min, 0 = default

# `say` is the floor when kokoro can't load; kokoro's G2P language derives from the voice prefix.
VOICE_TTS_ENGINE = os.environ.get("VOICE_TTS_ENGINE", "kokoro")   # kokoro | say
VOICE_TTS_KOKORO_MODEL = os.environ.get("VOICE_TTS_KOKORO_MODEL",
                                        "prince-canuma/Kokoro-82M")
VOICE_TTS_KOKORO_VOICE = os.environ.get("VOICE_TTS_KOKORO_VOICE", "bf_emma")
VOICE_TTS_KOKORO_SPEED = float(os.environ.get("VOICE_TTS_KOKORO_SPEED", "1.0"))

DATA_DIR.mkdir(parents=True, exist_ok=True)


# Baseline window for lib/notable.js: four of each weekday.
DESK_BASELINE_DAYS = 28
