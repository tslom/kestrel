"""Central config. Everything the app needs to know about *your* setup lives here.

Env vars come from backend/.env (see .env.example for the full list) plus
whatever's already in the shell — a real env var always wins over .env
(dotenv's default), so `VAULT_PATH=... uvicorn ...` still overrides it
per-invocation like before. .env is gitignored; only .env.example is
committed. Secrets (LOCATION_RELAY_TOKEN, AEROAPI_KEY) belong in .env, not
in this file or in git.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Absolute path to your (new, greenfield) Obsidian vault.
VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/vault"))).resolve()

# Vault conventions
GENERATED_DIR = "06-Generated"         # agent-created notes land here until reviewed
TASKS_VIEW_FILE = "tasks.md"           # GENERATED read-only view of the tasks DB
BRAIN_DIR = "07-Brain"                 # GENERATED read-only view of app.db brain_facts

# Day boundary: local time, hours before this count toward the PREVIOUS day.
DAY_ROLLOVER_HOUR = 4

# How long a chased debt stays quiet before it's your move again. A week is
# long enough not to nag someone who said "next payday" and short enough that
# an ignored ask doesn't vanish — the failure this exists to prevent is the
# debt you mentioned once in July and never thought about again.
DEBT_NUDGE_DAYS = int(os.environ.get("DEBT_NUDGE_DAYS", "7"))

# Where the expenses export (app/expenses.py) is written. The backend writes
# it rather than the page downloading it, because the desktop shell's webview
# has downloads switched off.
EXPENSES_DIR = Path(os.environ.get("EXPENSES_DIR", Path.home() / "Downloads"))

# App-owned data. index.db = derived cache (disposable). app.db = real data
# (tasks, habits) — this one gets backed up.
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
INDEX_DB = DATA_DIR / "index.db"
APP_DB = DATA_DIR / "app.db"

# Context providers
PROVIDER_REFRESH_SECONDS = int(os.environ.get("PROVIDER_REFRESH_SECONDS", "300"))

# Must match the uvicorn --port — it's baked into the OAuth loopback redirect
# URI (http://127.0.0.1:<port>/api/oauth/callback) the Connect button uses.
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8788"))

# Set by launch_app on the server it starts, whose reloader waits for console
# turns to finish. Absent means some other server holds the port — one that
# restarts the instant backend/app changes, mid-turn.
HELD_RELOAD_ENV = "KESTREL_HELD_RELOAD"
# Also set by launch_app: the write end of a pipe to the window process, which
# posts notifications as Kestrel (desktop_shell.Notifier). Absent means no
# window to post through, and notify.py falls back to osascript.
NOTIFY_FD_ENV = "KESTREL_NOTIFY_FD"
# Taken out of the environment as the server starts, not just read: every
# process the server starts (each tab's CLI, the commands it runs, a Python
# script importing app.notify) would otherwise inherit the name without the
# pipe, and write banners into whatever that fd number is in them.
NOTIFY_FD = os.environ.pop(NOTIFY_FD_ENV, None)

# Calendar provider: which token row and which calendars to pull.
# None = every calendar on the account's calendarList (subscribed ones too);
# set a list of calendar ids (e.g. ["primary"]) to narrow it.
GOOGLE_CALENDAR_ACCOUNT = "school"
GOOGLE_CALENDAR_IDS: list[str] | None = None

# Health provider: which token row holds the Google Health grant.
GOOGLE_HEALTH_ACCOUNT = "personal"

# Email provider: which token row (needs gmail.modify), and the
# model the review-queue summarizer runs on — via the Claude Code CLI on the
# subscription login, same as the console (never the raw API).
GMAIL_ACCOUNT = "school"
EMAIL_SUMMARY_MODEL = os.environ.get("EMAIL_SUMMARY_MODEL", "haiku")
EMAIL_SUMMARY_BATCH = 20          # messages per summarizer call
EMAIL_BODY_EXCERPT_CHARS = 1500   # body text sent per message (never stored)
# Mail outside the inbox still owed a label + search terms (older summaries,
# mail archived straight from Gmail) — worked through this many per refresh,
# so the backfill trickles behind the poller instead of stalling it.
EMAIL_BACKFILL_PER_REFRESH = 60
# `--body <msg_id>` reads one message whole, for a person or the console
# chasing something a summary dropped (a call link, an address). Bigger cap:
# nothing is being fed to a model here. Still never stored.
EMAIL_BODY_FULL_CHARS = 20000

# Reply drafter: candidate replies for a thread waiting on you,
# on demand from the People panel only — never from the poller, and it never
# sends. Same subscription-CLI posture as email triage.
REPLY_DRAFT_MODEL = os.environ.get("REPLY_DRAFT_MODEL", "sonnet")

# Automatic model calls (the poller's triage/extraction/self-texts/food
# estimates, the brain's catch-up, the lazy brief) stand down once a
# subscription window is this full, so the rest of it is left for the console
# and anything you click. Keyed like claude_quota's windows; percent. Paths a
# person asked for run inside agent.interactive() and ignore these.
AUTO_PAUSE_PCT = {
    "five_hour": float(os.environ.get("AUTO_PAUSE_5H_PCT", "80")),
    "seven_day": float(os.environ.get("AUTO_PAUSE_7D_PCT", "85")),
}

# Daily brief: the one-shot brief writer, via the Claude Code
# CLI on the subscription login like email triage — never the raw API.
BRIEF_MODEL = os.environ.get("BRIEF_MODEL", "sonnet")
BRIEF_STALE_GENERATING_SECONDS = 300  # backend died mid-run -> reclaimable

# The Brain (app/brain.py): the model that reads a window of sources and
# proposes facts, and the one that tidies pages afterwards. Both run on the
# subscription CLI through agent.ask, like the brief.
BRAIN_INGEST_MODEL = os.environ.get("BRAIN_INGEST_MODEL", "sonnet")
BRAIN_LINT_MODEL = os.environ.get("BRAIN_LINT_MODEL", "sonnet")
# What a fact read off a source (not told to Kestrel) starts as. "pending"
# is usable by the console but labelled unconfirmed until the user ✓s it:
# everything read off a source needs a ✓.
BRAIN_INFERRED_STATUS = "pending"
# Group chats count as a source only where the user actually talks: at least
# this many of their own messages in the backfill window. 1:1s always count.
BRAIN_GROUP_MIN_MINE = 5
# How far back the first ingest reaches, and how far messages are pulled in
# from chat.db to cover it (a year, groups and 1:1s alike).
BRAIN_BACKFILL_DAYS = 365

# Auto exercise check-in: on each health refresh, a day whose Active Zone
# Minutes reach EXERCISE_AZM_THRESHOLD *and* whose peak HR reaches
# EXERCISE_HR_MAX_THRESHOLD auto-checks the EXERCISE_HABIT_NAME habit
# (source='auto'). Never overwrites a manual check/un-check. Set either
# threshold to 0 (or rename the habit away) to disable. AZM threshold picked to
# clear ordinary active days (~<26 AZM) and catch real workouts (60+); Fitbit's
# own default daily AZM goal is 22 if you want it looser.
#
# The peak-HR gate exists because AZM alone is not a workout signal: anything
# holding the heart rate in the cardio zone earns 2 AZM/min, so a long evening
# of elevated resting HR (alcohol, illness, stress) can out-score a real
# session — observed here at 179-315 AZM on days that never broke 177 bpm,
# against 111-152 AZM on genuine workouts that all hit 180+. Volume can't tell
# those apart; peak effort can. Tune to your own workout peaks, not a formula.
EXERCISE_HABIT_NAME = os.environ.get("EXERCISE_HABIT_NAME", "Exercise")
EXERCISE_AZM_THRESHOLD = float(os.environ.get("EXERCISE_AZM_THRESHOLD", "30"))
EXERCISE_HR_MAX_THRESHOLD = float(
    os.environ.get("EXERCISE_HR_MAX_THRESHOLD", "180"))

# Auto sleep check-in: same mechanism, keyed on sleep_hours (which lands on the
# wake-up day). A night reaching SLEEP_HOURS_THRESHOLD hours auto-checks the
# SLEEP_HABIT_NAME habit (source='auto'). Never overwrites a manual check/
# un-check. Set the threshold to 0 (or rename the habit away) to disable.
SLEEP_HABIT_NAME = os.environ.get("SLEEP_HABIT_NAME", "Sleep")
SLEEP_HOURS_THRESHOLD = float(os.environ.get("SLEEP_HOURS_THRESHOLD", "9"))

# Finance provider (archive/PLAID_PLAN.md): non-secret knobs only — the Plaid
# client_id/secret live in app.db (python -m app.providers.plaid setup).
PLAID_COUNTRY_CODES = ["US"]
PLAID_DAYS_REQUESTED = 730   # link-token transaction history window (Plaid max)

# Weather provider: Open-Meteo, no key/auth. Home coordinate is the fallback
# origin; get() prefers the latest location fix < 6h old over this. Defaults to the geographic centre of the contiguous
# US, a placeholder that means "unset" — set it per-machine in .env.
HOME_LAT = float(os.environ.get("HOME_LAT", "39.8283"))
HOME_LON = float(os.environ.get("HOME_LON", "-98.5795"))

# Time-to-leave: Google Maps Routes v2 (traffic-aware drive time)
# + Geocoding (event location string -> coords). Solo use stays inside the
# free tier easily. leave_by = event start − drive − LEAVE_BUFFER_MIN. Origin
# prefers a location fix younger than LEAVE_FIX_MAX_MIN, else HOME. An
# empty key (or any API failure) degrades: /api/leave still lists the events,
# just with leave_by null — never a hard failure.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
LEAVE_BUFFER_MIN = int(os.environ.get("LEAVE_BUFFER_MIN", "10"))
LEAVE_FIX_MAX_MIN = 30
# Appended to an event location that geocodes to nothing on its own, then
# retried once. A university calendar writes half its rooms bare ("KDC 240"),
# which Google resolves only with the campus attached. Empty = no retry.
LEAVE_GEOCODE_CONTEXT = os.environ.get("LEAVE_GEOCODE_CONTEXT", "")

# Spotify provider: authorization-code + PKCE, public client (no
# client secret — Spotify's dev dashboard just needs the redirect URI below
# registered exactly). Token row lives in app.db `tokens`
# (provider='spotify', account=SPOTIFY_ACCOUNT, client_secret=''). Spotify
# banned 'localhost' and non-loopback HTTP redirect URIs in Nov 2025, so
# this must stay the 127.0.0.1 IP literal, matching BACKEND_PORT — the
# callback is a real FastAPI route (see app/routes/spotify.py), not a CLI
# loopback server, since the redirect URI can't be a throwaway ephemeral port.
SPOTIFY_ACCOUNT = "personal"
SPOTIFY_REDIRECT_URI = f"http://127.0.0.1:{BACKEND_PORT}/api/oauth/spotify"
SPOTIFY_SCOPES = "user-read-recently-played user-top-read user-library-read"

# Last.fm — genre/mood tags per artist, because Spotify stopped returning the
# `genres` field on its artist object. Free key, no OAuth, no secret: last.fm/api/account/create.
# Unset = the provider reports "off" and never calls out.
# Enrichment is a trickle: PER_CYCLE artists per poll, most-played first, so
# ~9.8k artists fill in over a day or so without hammering a free API. Raise it
# (or run `python -m app.providers.lastfm backfill --limit N`) to hurry.
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_ENRICH_PER_CYCLE = int(os.environ.get("LASTFM_ENRICH_PER_CYCLE", "25"))
LASTFM_MIN_TAG_WEIGHT = 10   # 0-100, relative to the artist's own top tag
LASTFM_MAX_TAGS = 8

# Messages provider (archive/ROUND34_PLAN.md §9): chat.db is truth;
# unlike Spotify's `plays`, app.db's `messages` IS a genuine cache — chat.db
# keeps full local history, so a cache drop only costs a re-sync. No OAuth:
# reading chat.db needs Full Disk Access granted to whichever process runs
# uvicorn (System Settings -> Privacy & Security -> Full Disk Access; test
# with a manual sqlite3 read first, per CLAUDE.md's Run section).
CHAT_DB_PATH = Path(os.path.expanduser("~/Library/Messages/chat.db"))
ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb")
MESSAGES_BACKFILL_DAYS = 90

# Self-texts (app/selftexts.py): your own numbers/iCloud emails — a message
# you send to yourself (chat.db logs these like any other 1:1 chat) is routed
# to a task, event, food entry, list item or note. Normalized the same way as
# `messages.handle` before comparing (phone: last 10 digits; email:
# lowercase) — e.g. "+1 (555) 123-4567" or "me@icloud.com". Set in .env as a
# comma-separated MESSAGES_SELF_HANDLES. Empty: routing is a silent no-op.
MESSAGES_SELF_HANDLES: list[str] = [
    h.strip() for h in os.environ.get("MESSAGES_SELF_HANDLES", "").split(",") if h.strip()]
SELFTEXT_MODEL = os.environ.get("SELFTEXT_MODEL", "haiku")
# A console tab's "regenerate title" (app/session_digest.py): one short turn
# per click.
TAB_TITLE_MODEL = os.environ.get("TAB_TITLE_MODEL", "haiku")

# Deliveries + travel email extractors (archive/ROUND34_PLAN.md §15/§16): shared
# pipeline in providers/email_extract.py, same subscription-CLI posture as
# email_triage — the model only ever sees candidate messages (matched by
# these hints), never the whole inbox. LIKE matching is case-insensitive.
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "haiku")
EXTRACT_BATCH = 20
# Food: dining-hall dishes → ingredients + grams, then a pick among SR28 matches
# (app/food_estimate.py). Once per distinct item name, so a cold start is a few
# batches and a normal day is single digits.
FOOD_ESTIMATE_MODEL = os.environ.get("FOOD_ESTIMATE_MODEL", "haiku")
PACKAGE_SENDER_HINTS: list[str] = [
    "amazon", "usps", "ups", "fedex", "shop", "order", "shipped",
    "delivery", "out for delivery", "tracking",
]
TRIP_SENDER_DOMAINS: list[str] = ["aa.com"]   # aa.com first — the live account

# Office-hours amendments arriving by mail. Matched against sender OR
# subject, same as the package hints. Course mail is the case where a *subject*
# hint carries most of the weight: a TA emailing from a personal address about
# "OH moved to Friday" has nothing recognisable in the from line, while the
# course platforms are recognisable there and say nothing useful in the
# subject. Deliberately loose — a false candidate costs one cheap extraction
# that answers `relevant: false` and is never retried, while a miss means the
# hours quietly go stale, which is the failure this exists to prevent.
OFFICE_HOURS_SENDER_HINTS: list[str] = [
    "piazza", "canvas", "instructure", "usc.edu", "blackboard", "gradescope",
    "office hour", "office hours", "oh ", " oh", "section", "recitation",
    "no class", "cancelled", "canceled", "rescheduled", "moved",
]

# Attachment extraction (providers/attachments.py). Never runs on its own —
# no poller hook — because every image costs model tokens and every PDF parses
# bytes an outside party chose.
#
# ATTACHMENT_MAX_BYTES caps what's read at all — about not slurping a 400MB
# video, not about containment.
#
# ATTACHMENT_WORKER_MEM_BYTES is the real bomb defense, and it's enforced by
# the parent polling the child's RSS rather than by an rlimit: macOS accepts
# RLIMIT_AS/DATA/RSS and then rejects every attempt to lower them, so the
# in-child memory cap silently does nothing on this platform. Generous enough
# for a legitimate 25MB scanned PDF's page tree, small enough that a file
# inflating toward gigabytes is killed before the machine swaps.
#
# Read this number together with attachments._RSS_POLL_S: the real ceiling is
# this cap PLUS whatever the child can allocate in one poll gap, because a
# sample can only catch what has already happened. A 0.15s gap measured
# ~0.84 GB past the cap; at 30ms the stated cap and the effective one are
# within ~17%.
#
# The vision cap is the API's, not ours: base64 inflates by 4/3 and the request
# limit is 5MB, so ~3.5MB of raw bytes is the real ceiling.
#
# The timeout is wall-clock and belongs to the parent. RLIMIT_CPU in the child
# can't cover a process blocked in a syscall, which burns no CPU at all.
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ATTACHMENT_WORKER_MEM_BYTES = 1024 * 1024 * 1024
ATTACHMENT_VISION_MAX_BYTES = 3_500_000
ATTACHMENT_EXTRACT_TIMEOUT_S = 30
ATTACHMENT_VISION_MODEL = os.environ.get("ATTACHMENT_VISION_MODEL", "haiku")

# Live flight status: FlightAware AeroAPI (aeroapi.flightaware.com,
# Personal tier). Optional — trip extraction from email works without a
# key; only poll_flight_status() (live status/gate, the T-24h check-in
# task, delay/gate-change desktop notifications) needs it. Poll cost
# control (decided in archive/ROUND34_PLAN.md): only flights departing within
# FLIGHT_POLL_HORIZON_HOURS, re-checked at most every
# FLIGHT_POLL_INTERVAL_HOURS until FLIGHT_POLL_CLOSE_HOURS out, then every
# poller cycle (PROVIDER_REFRESH_SECONDS, well under an hour).
AEROAPI_KEY = os.environ.get("AEROAPI_KEY")
FLIGHT_POLL_HORIZON_HOURS = 48
FLIGHT_POLL_INTERVAL_HOURS = 1
FLIGHT_POLL_CLOSE_HOURS = 6
FLIGHT_CHECKIN_LEAD_HOURS = 24

# Alarms. Dry-run is off: a channel that never fires is indistinguishable from
# one that works, so leaving it on tests nothing. Set to 1 to go quiet.
ALARMS_DRY_RUN = os.environ.get("ALARMS_DRY_RUN", "0") != "0"
# How far ahead to bother writing alarms. A horizon exists so the Reminders
# list stays readable and a far-future flight doesn't sit there for a season collecting rewrites.
ALARM_HORIZON_DAYS = int(os.environ.get("ALARM_HORIZON_DAYS", "14"))
# Delivery target. A dedicated list, never an existing one: everything in it is
# written and deleted by the reconciler, so pointing this at a list with real
# reminders in it would put them in reach of a withdrawal. The account must be
# the syncing one — a local-only list reaches the phone never, which is the
# entire point of the feature.
ALARM_REMINDERS_LIST = os.environ.get("ALARM_REMINDERS_LIST", "Kestrel")
ALARM_REMINDERS_ACCOUNT = os.environ.get("ALARM_REMINDERS_ACCOUNT", "iCloud")
# Runaway guard. The three-state source protocol is what *should* stop a mass
# withdrawal, so tripping this means that failed: a plan asking to pull this
# many alarms at once is likelier to be a bug than a day's worth of changes.
# Withdrawals are skipped wholesale for the cycle rather than partly applied.
ALARM_MAX_WITHDRAW_PER_CYCLE = int(
    os.environ.get("ALARM_MAX_WITHDRAW_PER_CYCLE", "5"))
# Packages carry an `eta_day` and no clock, so the alarm needs an hour of its
# own. Morning, in the *user's* zone (tz.zone_on) — unlike a flight, whose
# departure clock belongs to the airport.
PACKAGE_ALARM_HOUR = int(os.environ.get("PACKAGE_ALARM_HOUR", "9"))
# A leave_by is recomputed from live traffic every cycle, so it jitters by a
# minute or two while nothing about the day has changed. Without a deadband
# every cycle would count as an amendment, and an amendment withdraws the
# alarm off the phone and writes it again. Only a real move should reach it.
LEAVE_ALARM_TOLERANCE_MIN = int(
    os.environ.get("LEAVE_ALARM_TOLERANCE_MIN", "3"))

# The at-desk channel (app/notify.py). On by default: a local banner has no
# blast radius worth a dry run, and a channel that never fires is
# indistinguishable from a quiet week. Off is available; off is not the default.
NOTIFY_ENABLED = os.environ.get("NOTIFY_ENABLED", "1") != "0"
# How long since the last keypress or mouse move still counts as "at the desk".
# Three minutes is aw-watcher-afk's number for the same judgement: the
# threshold where "still here" stops being true.
NOTIFY_IDLE_MAX_SEC = int(os.environ.get("NOTIFY_IDLE_MAX_SEC", "180"))
# A notification means *now*. Held past this, it stops being news and starts
# being a lie ("leave in 5 minutes", delivered an hour late), so it expires
# instead of firing. This is the one place this channel deliberately loses data,
# and the opposite call from the alarm writer, which retries all day — an alarm
# carries its own fire time and is still true whenever it lands.
NOTIFY_STALE_MIN = int(os.environ.get("NOTIFY_STALE_MIN", "45"))
# Burst cap. Unlike ALARM_MAX_WITHDRAW_PER_CYCLE this defers rather than
# refuses: half a withdrawal is worse than none (it strands alarms), while half
# the news beats none of it. The rest keep their turn next cycle.
NOTIFY_MAX_PER_CYCLE = int(os.environ.get("NOTIFY_MAX_PER_CYCLE", "5"))

# The scheduled wake (app/wake.py). The alarm writer hands the phone an
# alarm hours in advance; what it can't do is keep that alarm *true*
# across a night of sleep, because the reconciler only runs while the lid is
# open. An OS-level wake shortly before each alarm buys one more cycle.
# Dry-run is off: scheduling reaches root through a sudoers
# drop-in scoped to `/usr/bin/pmset schedule` (see wake.py). Until that file is
# installed the two write verbs fail on `sudo -n` and apply() logs and retries,
# which is the same soft failure as any other pmset error — a missed wake costs
# freshness, never the alert. Set to 1 to plan without touching the schedule.
WAKE_DRY_RUN = os.environ.get("WAKE_DRY_RUN", "0") != "0"
WAKE_ENABLED = os.environ.get("WAKE_ENABLED", "1") != "0"
# The owner string stamped on every event we schedule, and the *only* one we
# will ever cancel. `pmset -g sched` is shared with the system — this machine
# already carries com.apple.alarm events for the calendar travel engine and Do
# Not Disturb — so `pmset schedule cancelall` is never used and each cancel
# names its exact type/date/owner. Same argument as the `kind:` prefix on an
# alarm key: a partition is what keeps a reconciler's deletions on its own land.
WAKE_OWNER = os.environ.get("WAKE_OWNER", "com.kestrel.alarms")
# How long before the alarm to wake. It has to cover a whole cycle — every
# provider, the reconcile, the Reminders write — *and* iCloud's own sync out to
# the phone, which is not ours and not instant. Hence generous rather than tight.
WAKE_LEAD_MIN = int(os.environ.get("WAKE_LEAD_MIN", "20"))
# Much shorter than ALARM_HORIZON_DAYS (14), and not the same kind of number. A
# Reminders row two weeks out costs nothing to leave sitting there; a wake event
# is a machine spinning up. There is also no point scheduling one for a
# Thanksgiving flight — the laptop will be open many times before then, and will
# schedule it when it comes near. 36 hours covers tonight and tomorrow, which is
# the span a sleeping laptop actually spans.
WAKE_HORIZON_HOURS = int(os.environ.get("WAKE_HORIZON_HOURS", "36"))
# Alarms cluster (a check-in and a leave_by for one trip), and one wake refreshes
# every alarm in the cycle it runs, not just the one it was scheduled for. So
# wakes within this window collapse to the earliest of them.
WAKE_COALESCE_MIN = int(os.environ.get("WAKE_COALESCE_MIN", "30"))
# Deadband, for the same reason _FIRE_DEADBAND exists one module over — but
# applied to every kind rather than just `leave`. Coalescing makes a wake time
# the minimum over a cluster, so *any* member jittering by a minute would drag
# it, and every move here is a cancel plus an add against global state.
WAKE_TOLERANCE_MIN = int(os.environ.get("WAKE_TOLERANCE_MIN", "5"))
# Cap on events we hold at once. Unlike ALARM_MAX_WITHDRAW_PER_CYCLE, which
# refuses the whole batch, this one simply truncates: the list is sorted by time,
# so what falls off the end is the least urgent, and a later cycle schedules it.
WAKE_MAX_EVENTS = int(os.environ.get("WAKE_MAX_EVENTS", "6"))

# Location provider (archive/ROUND34_PLAN.md §10): OwnTracks (iOS, HTTP mode)
# posts fixes to the location relay (relay/, a Cloudflare Worker), never to
# this machine; providers/location_relay.py pulls from it each poller cycle.
# URL = the Worker's https://…workers.dev origin; TOKEN = the Worker's
# PULL_TOKEN secret (read + delete — the phone holds a different, write-only
# one). Both unset = no ingest, and location's health reads "off".
LOCATION_RELAY_URL = os.environ.get("LOCATION_RELAY_URL")
LOCATION_RELAY_TOKEN = os.environ.get("LOCATION_RELAY_TOKEN")
# "at home" threshold for time_at_home_min / first_leave / last_return.
# 250, not 150: HOME is a dorm building whose own fix cluster spreads ~127m
# around its centroid, so a 150m radius put the far side of the building
# within 20m of the boundary and made at_home flicker across one building.
HOME_RADIUS_M = 250

# News provider: Kagi Kite (kite.kagi.com/kite.json), no key.
# Slugs must match a "file" entry in the index (minus .json) — CLI --list
# on the index if unsure. refresh() fetches once per day_key.
NEWS_CATEGORIES: list[str] = ["world", "usa", "tech"]
NEWS_HEADLINES_PER_CATEGORY = 5

# Athletics provider: Sidearm's public iCal feed of every USC sport (no key),
# so kickoff times come from here rather than from web searches.
ATHLETICS_ICS_URL = os.environ.get(
    "ATHLETICS_ICS_URL",
    "https://usctrojans.com/calendar.ashx/calendar.ics?sport_id=0")

# Coursework provider: the user's Brightspace (D2L) calendar feed — Calendar →
# Subscribe. The URL carries a per-user token, so it lives in .env only; unset
# means the provider answers with nothing rather than failing.
BRIGHTSPACE_ICS_URL = os.environ.get("BRIGHTSPACE_ICS_URL", "")

# Reddit provider: Arctic Shift (arctic-shift.photon-reddit.com), no key —
# Pushshift's successor, and the only anonymous route left. Reddit's own
# `.json` endpoints answer 403 to any script regardless of User-Agent, and
# old.reddit redirects to a login; its `.rss` feeds all answer 404.
REDDIT_SUBREDDITS: list[str] = ["onebag"]
REDDIT_TOP_PER_DAY = 5
# Selftext is stored whole but trimmed on the way out, so a range read stays
# token-cheap for the console. Note --query filters the payload, so it only
# sees these first N characters — the untrimmed column is there for --search
# and for anything that reads the table directly.
REDDIT_SELFTEXT_CHARS = 600
# Arctic Shift ingests a post at creation and revisits it later, so a post's
# score is worth ~nothing for its first day: measured, every post
# from the previous day still read score=1, while day-2 and older carried real
# values (52, 465, 214). Days inside this window are re-synced on every refresh
# and reported `provisional: true` — never charted as final.
REDDIT_SCORE_MATURE_DAYS = 3
# The poller runs every 5 minutes; re-fetching three days per subreddit that
# often would be rude to a free third-party archive for data that moves hourly.
REDDIT_REFRESH_MIN = 60
# Pages of 100 per subreddit per sync window. 20 covers a very busy sub's day;
# the cap only exists so a pagination bug can't walk the whole archive.
REDDIT_MAX_PAGES = 20

# Google OAuth: one token row per (provider, account). Scope presets per
# account so `python -m app.providers.oauth authorize --account school` needs
# no flags. Tokens live in app.db — localhost-only, never in frontend code.
GOOGLE_ACCOUNT_SCOPES: dict[str, list[str]] = {
    # school Workspace account — calendar widget + gmail review queue
    # calendar.readonly is what lists the calendars themselves; calendar.events
    # is read/write on their contents, and is what routes/calendar.py needs to
    # create, move, cancel and RSVP. Neither one can create or delete a whole
    # calendar — that would take the unscoped `auth/calendar`.
    "school": [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    # personal account — Google Health / Fitbit Air. Restricted-tier
    # scopes: expect scary consent screens on your own External/Testing app.
    "personal": [
        "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
        "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    ],
}

# Console / Claude Agent SDK
#
# `auto` (console.AUTO_MODE), not the SDK's `acceptEdits`, which the picker no
# longer offers. acceptEdits reads as the cautious middle option and isn't one:
# the CLI settles Bash writes inside the workspace under it without consulting
# the backend, so `rm <vault>/…` runs with no card while `task_create` stops
# for one. `auto` inverts both halves of that — the writes it runs unattended are the reversible ones, and what can't
# be taken back still asks. Set CONSOLE_PERMISSION_MODE to `default` for a
# console that asks about everything.
PERMISSION_MODE = os.environ.get("CONSOLE_PERMISSION_MODE", "auto")
SETTING_SOURCES = ["user", "project"]  # loads ~/.claude + vault/.claude (your skills)
# Sonnet by default — most console chats are drafting/quick questions and the
# per-session picker can still opt into a bigger model.
DEFAULT_MODEL = os.environ.get("CONSOLE_MODEL", "sonnet")
# A dev tab is real coding, where the strongest model earns its cost. "opus"
# is the CLI's alias for the newest Opus (claude-opus-5-5 on CLI 2.1.312).
DEV_MODEL = os.environ.get("DEV_CONSOLE_MODEL", "opus")

# Which `claude` binary the SDK drives. The SDK ships its own copy under
# claude_agent_sdk/_bundled/, and that copy pins whatever version the SDK
# release was cut against — 2.1.191 for sdk 0.2.110. That build's WebSearch
# hangs ~5.5 minutes behind the ANTHROPIC_BASE_URL proxy and then fails the
# turn with `502 Unable to safely convert buffered response to SSE`; the same
# search on the installed CLI, same proxy, same model, returns in ~13s. So
# point at the installed one instead. ~/.local/bin/claude is a symlink the
# installer repoints on upgrade, so this tracks the current version rather
# than pinning a second stale one — but it is a *host* path: if it goes
# missing, fall back to the bundled binary rather than failing to start.
CONSOLE_CLI_PATH = Path(os.environ.get(
    "CONSOLE_CLI_PATH", os.path.expanduser("~/.local/bin/claude")))

# Claude in Chrome (`--chrome`) in these tab profiles. Tools appear whenever the
# flag is set; they only work with Chrome open and the extension connected.
CONSOLE_CHROME = os.environ.get("CONSOLE_CHROME", "1") != "0"
CHROME_PROFILES = ("general", "dev")

# Computer use, as a job a tab hands off (console_terminal.py). Its own folder,
# because `/mcp` enables computer-use per project and it's one enable this way.
# Outside this repo on purpose: the CLI's project is the enclosing git root, so
# in here the enable lands on the repo, and the job would load its CLAUDE.md.
COMPUTER_DIR = Path.home() / ".kestrel" / "computer"
COMPUTER_MODEL = os.environ.get("COMPUTER_MODEL", "sonnet")
COMPUTER_JOB_LIMIT_S = 15 * 60
COMPUTER_SETUP_LIMIT_S = 5 * 60

# Read-only tools console sessions may use without an approval round-trip.
# Passed as SDK allowed_tools so it applies regardless of the vault cwd
# (the console never loads this repo's .claude/settings.json).
#
# allowed_tools means auto-allowed — the SDK executes these WITHOUT calling
# can_use_tool, so nothing in this list ever reaches the browser approval
# card. Only entries safe to run unattended belong here.
#
# Bash is deliberately absent, and the omission is the point. It used to hold
# prefix patterns like `Bash(curl -s http://localhost:8788/*)`, but a prefix
# wildcard matches everything *after* the URL too — `... /api/tasks -X POST
# -d @-` is still the same prefix. That auto-approved reads and writes alike
# across ~25 mutating endpoints. A prefix string cannot express "and no write
# flags", so Bash policy moved to console_policy.is_safe_read(), which sees the whole
# command and can say no. Anything it doesn't recognize prompts as usual.
#
# The MCP entries stay: each names one read-only operation outright, with no
# wildcard and no argument surface to smuggle a write through.
#
# Read, Grep and Glob are here because leaving them out backfired. They cannot
# write, but they were still reaching can_use_tool and prompting — so the model
# reached for Bash `grep -ril`, `find` and `cat` instead, which prompted too and
# arrived with none of these tools' scoping. Auto-allowing them is also what
# ordinary Claude Code does. Exfiltration is unaffected: it needs a write or an
# outbound request, and both of those still stop at a card.
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

# The built-in tools the console is given at all — a different axis from
# CONSOLE_ALLOWED_TOOLS above, which only says which of them skip the approval
# card. Left unset, `tools=` defaults to the whole claude_code preset: 30
# built-ins whose schemas cost 14,165 tokens, re-read on every single assistant
# message.
#
# Measured against every console session on record: it has ever called ten of
# those thirty — ToolSearch, Bash, WebSearch, Grep, AskUserQuestion, Read,
# WebFetch, Glob, Write, Edit. The other twenty are the CLI's own furniture
# (Task/TaskOutput/TaskStop, Cron*, EnterWorktree/ExitWorktree, PlanMode,
# Monitor, SendMessage, NotebookEdit, ReportFindings, DesignSync, …) and are
# meaningless in a vault console that cannot spawn agents or manage worktrees.
# Naming the ten costs 6,440 tokens/message less than taking the preset
# (28,561 -> 22,121 measured via get_context_usage), which is ~16% of console
# spend for tools no session has reached for.
#
# ToolSearch has to stay: the 31 vault tools and the 51 claude.ai connector
# tools are *deferred*, so they cost nothing until asked for, and ToolSearch is
# how a session asks. Dropping it would strand every mcp__vault__ writer.
#
# Skill is deliberately absent, and it is the one real behaviour change here.
# Its omission is what drops the Skills block (1,990 of the 6,440): all 14
# skills load through setting_sources, and 13 are this repo's dev skills
# (dataviz, frontend-design, artifact-*, code-review) leaking into a vault
# console that has never once invoked one. The 14th is the vault's own
# daily-brief — which the console does not drive either: the brief is built
# backend-side by brief.py through agent.py, and that path does not go through
# here. If a console session ever needs to run a skill, add "Skill" back and
# expect ~2k/message for it.
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

# A dev tab (console_profiles.py) is Claude Code on a repo, so it gets the
# furniture the comment above strips from the vault console: subagents, the
# task list, notebooks, and Skill — a repo's own skills (this one's `verify`)
# are the point there, not leakage.
# The task list is TaskCreate/TaskUpdate/TaskList/TaskGet: TodoWrite is gone
# from the CLI (2.1.312 drops it from the tool list silently — the model went
# looking for it with ToolSearch and found nothing).
TASK_TOOLS = ["TaskCreate", "TaskUpdate", "TaskList", "TaskGet"]
DEV_BUILTIN_TOOLS = CONSOLE_BUILTIN_TOOLS + ["Agent", "NotebookEdit", "Skill"] + TASK_TOOLS

# `--effort` levels the start message may ask for; anything else is dropped
# and the CLI's own default stands.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Where a dev tab's projects live: each immediate subdirectory is offered in
# the new-tab picker, and nothing outside these is accepted as a cwd.
DEV_PROJECT_ROOTS = [Path(os.path.expanduser(p)) for p in os.environ.get(
    "DEV_PROJECT_ROOTS", "~/Projects").split(":") if p]

# Voice: both transducers are ours, and
# both run on this machine. Audio never leaves the box — only the transcript
# does, and only along the path a typed message already takes.
#
# The wake word is either one of openWakeWord's six pretrained models (alexa,
# hey_jarvis, hey_mycroft, hey_rhasspy, timer, weather) or a path to one we
# trained — the chosen name is **hey kestrel**, and docs/wake-word.md is the
# recipe. Drop the trained model at WAKE_MODEL and it is picked up on the next
# restart; the phrase the UI shows is the file's stem, so the name lives in
# the filename and nothing here has to be edited. Until that file exists it
# falls back to a pretrained phrase, because a missing model file would
# otherwise take the mic socket down with it. That fallback is not a
# half-configured state: `voice.wake_label()` reads the spec actually in use, so
# while the file is missing every surface says "hey jarvis" and the mic answers
# to "hey jarvis" — one name, whichever model is loaded.
# Thresholds are the two knobs worth having: the wake threshold trades false
# summons against missed ones — and it applies while the console is talking
# too, since the wake word is also how you interrupt a reply — while the VAD
# threshold decides what counts as speech for endpointing.
VOICE_STT_MODEL = os.environ.get("VOICE_STT_MODEL",
                                 "mlx-community/parakeet-tdt-0.6b-v3")
WAKE_MODEL = DATA_DIR / "wake" / "hey_kestrel.onnx"
VOICE_WAKE_WORD = os.environ.get("VOICE_WAKE_WORD") or (
    str(WAKE_MODEL) if WAKE_MODEL.is_file() else "hey_jarvis")
VOICE_WAKE_THRESHOLD = float(os.environ.get("VOICE_WAKE_THRESHOLD", "0.5"))
VOICE_VAD_THRESHOLD = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.5"))

# Endpointing. A turn ends after VOICE_SILENCE_MS of non-speech, or at
# VOICE_MAX_UTTERANCE_S regardless — the cap is what stops a stuck-open mic
# from handing the model a ten-minute recording of the room. An utterance
# carrying less than VOICE_MIN_SPEECH_MS of actual speech is dropped rather
# than transcribed, because parakeet will happily invent a word for a cough.
#
# Endpointing is two thresholds, not one, because the pause after a finished
# sentence and the pause in the middle of "so, um... the Ellis thread" are the
# same signal to a VAD and opposite things to a person. One number has to be
# either impatient at the start of an utterance or sluggish at the end of it.
# VOICE_SILENCE_PATIENT_MS applies while you're still assembling the sentence;
# once VOICE_SETTLED_SPEECH_MS of *speech* has accumulated (not elapsed time —
# hunting for a word never advances it) a pause probably means you're done and
# the shorter VOICE_SILENCE_MS takes over. Raise the patient one if it still
# cuts you off; raise VOICE_SETTLED_SPEECH_MS to stay patient for longer into
# the turn.
VOICE_SILENCE_MS = int(os.environ.get("VOICE_SILENCE_MS", "900"))
VOICE_SILENCE_PATIENT_MS = int(os.environ.get("VOICE_SILENCE_PATIENT_MS", "2200"))
VOICE_SETTLED_SPEECH_MS = int(os.environ.get("VOICE_SETTLED_SPEECH_MS", "2500"))
VOICE_MIN_SPEECH_MS = int(os.environ.get("VOICE_MIN_SPEECH_MS", "300"))
VOICE_MAX_UTTERANCE_S = float(os.environ.get("VOICE_MAX_UTTERANCE_S", "30"))
# Wake fired but nobody said anything: give up and go back to spotting.
# Same reasoning: this is the pause between "hey kestrel" and saying anything
# at all, i.e. exactly when you're deciding what to ask. Giving up costs a
# re-summon, waiting costs nothing — the recording is dropped by the
# minimum-speech floor either way, so it errs long.
VOICE_WAKE_TIMEOUT_S = float(os.environ.get("VOICE_WAKE_TIMEOUT_S", "12"))
# Barge-in has no threshold of its own: interrupting a reply is saying the
# wake word again, so VOICE_WAKE_THRESHOLD covers it. Bare speech used to be
# the trigger, which meant a room with any noise in it re-opened the mic at the
# end of every answer.

# The `say` voice (the fallback engine below): free, offline and already
# installed.
#
# "auto" walks VOICE_TTS_VOICE_PREFS and takes the first voice installed;
# naming one outright pins it; empty means the system default. The list is in
# preference order and every entry is a female en_GB/en_AU voice, because the
# system default is the thing people turn the speaker off to escape.
#
# The catch that makes this a list rather than one name: macOS ships every
# voice in a *compact* build and keeps the good ones behind a download
# (System Settings -> Accessibility -> Spoken Content -> System Voice ->
# Manage Voices). Compact Serena and Serena (Premium) are not the same voice
# and only the second is worth listening to, so voice.tts_voice() prefers the
# Premium/Enhanced spelling of whichever name it lands on.
#
# Two things are deliberately *not* in this list, both verified rather than
# assumed on macOS 25.5:
#
#   - **Siri voices.** They install from the same pane and sound far better
#     than anything here, but Apple keeps them first-party: a downloaded Siri
#     voice appears in neither `say -v '?'` nor AVSpeechSynthesisVoice
#     .speechVoices(), so no third-party synthesiser can reach one. Naming
#     it here would silently resolve to nothing.
#   - **A premium Australian female.** Matilda and Catherine are Siri voices,
#     and the downloadable-voice catalogue at
#     /System/Library/AssetsV2/com_apple_MobileAsset_VoiceServices_Combined-
#     VocalizerVoices offers exactly one en-AU voice, Lee, who is male. So
#     Karen compact is the whole Australian option and the upgrade path is
#     British — which is the tradeoff, not an oversight.
VOICE_TTS_VOICE = os.environ.get("VOICE_TTS_VOICE", "auto")
VOICE_TTS_VOICE_PREFS = ["Serena", "Kate", "Karen", "Moira", "Tessa"]
VOICE_TTS_RATE = int(os.environ.get("VOICE_TTS_RATE", "0"))   # words/min, 0 = default

# Which synthesiser actually runs. `say` is the floor: always installed, never
# fails, and the fallback whenever the chosen engine can't load — speech-out
# degrading is annoying, speech-out crashing the reply stream is not
# acceptable, so no engine here is allowed to be load-bearing.
#
# `kokoro` is Kokoro-82M through mlx-audio, on Metal. It is the swap DESIGN.md
# always planned for and it is not close: `say` is concatenative synthesis with
# a prosody rule set bolted on and no emotion model to reach for, while Kokoro
# is 82M parameters of learned prosody. Measured on this M5 after warm-up it
# runs 16-17x faster than real time (~0.25s for a sentence), so it is cheaper
# per reply than the network hop that produced the text.
#
# Voices are Kokoro's own names, not the system's: the `bf_`/`bm_` prefixes are
# British, `af_`/`am_` American, and VOICE_TTS_KOKORO_LANG is derived from that
# prefix rather than configured, because a British voice reading with the
# American G2P is a bug nobody would think to look for.
VOICE_TTS_ENGINE = os.environ.get("VOICE_TTS_ENGINE", "kokoro")   # kokoro | say
VOICE_TTS_KOKORO_MODEL = os.environ.get("VOICE_TTS_KOKORO_MODEL",
                                        "prince-canuma/Kokoro-82M")
VOICE_TTS_KOKORO_VOICE = os.environ.get("VOICE_TTS_KOKORO_VOICE", "bf_emma")
VOICE_TTS_KOKORO_SPEED = float(os.environ.get("VOICE_TTS_KOKORO_SPEED", "1.0"))

DATA_DIR.mkdir(parents=True, exist_ok=True)


# How much history a desk card reads a reading against (lib/notable.js).
# Four weeks rather than one, and the same number for every card on purpose:
# "unusual" has to mean one thing across the desk or the word stops carrying
# information. Four weeks also carries four of each weekday, so a body (or a
# laptop) with a weekday/weekend shape doesn't have that shape mistaken for
# the signal — a seven-day baseline calls every Saturday remarkable.
DESK_BASELINE_DAYS = 28
