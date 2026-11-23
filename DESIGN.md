# Design doc — Kestrel (v2)

Solo, localhost-first web app: a fresh greenfield vault + app-owned database,
with an embedded Claude Code console. Old vault is a read-only import source.

**Contents** — [Architecture](#architecture) ·
[Source-of-truth map](#source-of-truth-map) · [Day boundary](#day-boundary) ·
[Context providers](#context-providers-appproviders) (one `###` per source) ·
[The desk](#the-desk-frontend) · [Route map](#route-map) ·
[Console](#console) · [Reaching the user](#reaching-the-user-21--alarmspy-reminderspy-notifypy-wakepy) ·
[Voice](#voice-appvoicepy-frontendsrclibvoicejs) ·
[Recurring lessons](#recurring-lessons) · [Concurrency](#concurrency) ·
[Security posture](#security-posture) · [Deferred](#deferred)

## Architecture

```
Browser (React/Vite, :5173)
 ├── widgets ── REST /api/* ─────────────┐
 └── console ── WS /ws/console ──┐       │
                                 ▼       ▼
                       FastAPI backend (:8788, binds localhost only)
                        ├── console.py ── Claude Agent SDK ── claude CLI
                        │     cwd = vault · subscription OAuth · your skills
                        │     loaded · resume against ~/.claude/projects
                        │     (same store as terminal /resume) · PostToolUse
                        │     hook stamps provenance frontmatter
                        ├── providers/ ── external context: REST + CLI +
                        │     app.db cache + asyncio poller + oauth helper
                        ├── db.py ─────── app.db (WAL + busy_timeout)
                        ├── indexer.py ── vault *.md → index.db (read-only scan)
                        ├── watcher.py ── vault change → debounced rebuild (watchfiles)
                        ├── vault_views ─ regenerates tasks.md; generated inbox
                        └── widgets.py ── REST endpoints
                                 │
        ┌────────────────────────┴────────────────────────────┐
        ▼                                                     ▼
  NEW Obsidian vault (markdown truth for NOTES)        backend/data/
  ├── 06-Generated/            (agent notes, unreviewed)├── app.db    TRUTH: tasks,
  ├── tasks.md                 GENERATED VIEW of app.db │             habits, checkins
  └── atomic notes, [[wikilinks]]                       ├── index.db  derived cache,
                                                        │             disposable
                                                        └── backups/  monthly
                                                                      app.db snapshot
```

### Where a file goes

Both trees are grouped by *what a module is*, not by feature, and in both the
top level is reserved for the things that own something.

- `backend/app/` — modules that own state or rules stay here (`db`, `console`,
  `widgets`, `people`, `lists`, `brief`, `leave`, `trends`, `voice`, the §21
  channels). `app/routes/` is thin HTTP glue over something that already
  exists; `app/providers/` is the registry (flat on purpose — the CLI
  `python -m app.providers <source>` addresses it by filename, so a
  subdirectory would be a second naming scheme for the same set).
- `frontend/src/` — `lib/` is everything with no JSX, `ui/` is presentation
  shared by more than one feature, `console/` is the console tree, `widgets/`
  is one file per card and its sheet (plus `cards.js`, the hooks' registry,
  and `views.jsx`, which draws a card or a sheet from its id), and `plan/` is
  the layout around them. Only `main.jsx` and `styles.css` sit at the root.

The rule that keeps this from rotting: a module that carries a router does not
move into `routes/` just because it has one, and a widget's private component
does not move into `ui/` just because it could — filing logic under a name that
claims it's only plumbing is how the grouping stops meaning anything.

## Source-of-truth map

| Data | Truth | Other format |
|---|---|---|
| Notes | markdown | indexed into index.db (derived) |
| Courses (§22 — syllabus facts, office hours, instructor, LMS) | markdown, one note per class under `02-Areas/usc/<term>/` with `type: course` frontmatter | the `Office hours` card reads it straight out of index.db's frontmatter JSON — no app.db table, so a course edited in Obsidian is right on the next reindex. `office_hours` is a list of `{who, role, when, where}`; `when` is `"Mon 09:30-10:30"` (days may be comma/&/slash-joined) and anything else falls through as literal text rather than being dropped. An entry may carry `except:`, a list of `{date, when, cancelled, note}` overriding single dates — the standing rule and a one-off change are different facts, so amending the rule for "2pm just this Tuesday" is the bug the list exists to prevent. The week view answers "when are the hours" and shows the **rule**, annotated with the date; today's rows answer "can I walk in now" and carry the **effective** hours. Written by the console's `course_hours` / `course_except` / `course_set` tools (§22), which rewrite the note and reindex. One person holds several blocks, so `course_hours` addresses a block by *who plus weekdays*: the same weekdays amend that block, different weekdays append another, and only two blocks on the same days ask for `slot`. Matching on `who` alone is what silently folded a TA's four blocks into one on 09-01 — a write that can't find its block adds, never overwrites |
| Course schedule & exams (§22 study mode, `app/study.py`) | the extracted syllabus text (`syllabi/text/`, `type: syllabus-text`), falling back to the course note's dated tables | none — `course_context` reads it on demand and `/api/courses` carries an exam ≤14 days out as `study`. A *reader*, not a parser of record: rows are the syllabus's own words with a date resolved beside them, in the three shapes the extractions come in (dated rows, `Week N` blocks, prose sentences). A syllabus it can't date is shown verbatim, never aligned by guesswork (CSCI 103's topic table extracts as two unaligned columns). Week of term = Monday of the earliest scheduled day across the term's courses |
| Course review log (§22 study mode) | markdown, one note per course, `type: course-review`, created in `06-Generated/<CODE>-review.md` and found by frontmatter wherever it's filed | written only by the console's `course_review_add` (appends a `### date — concept` entry with `Got wrong`/`Correct`/`Before` (the next exam)/`Session` bullets; the same concept again adds an `Again` line instead of a duplicate) and read back by `course_review`. Notes, not app.db: it's study material the user reads and annotates in Obsidian, no card joins against it, and it isn't a Brain fact (the Brain is the user's life, not coursework). The calling session reaches the in-process tool through `console_tools.SESSION`, a ContextVar console.py sets before `connect()` |
| Documents dropped on the console (PDFs the answer can cite by page) | the file, `backend/data/docs/<sha256>.pdf` | `docs` row in app.db (name, size, pypdf's per-page text — derived from the file); its `d…` ref in `cite_refs`. See Citations → Documents |
| Tasks (incl. subtasks, triage fields, provenance — archive/TASKS_PLAN.md) | app.db | tasks.md regenerated as read-only view, subtasks nested under parents |
| Habits + checkins (incl. Exercise) | app.db | |
| Daily notes (mood/energy/workout journaling) | **retired** — mood tracking dropped; energy is now a derived trends series (sleep/RHR/HRV from the health cache), workout derives from the Exercise habit | |
| Provider data (`calendar`, `health`, `email`, …) | external service | app.db cache tables (`events`+`calendar_days`+`calendars`, `health_days`+`health_sync`+`health_hours`+`sleep_segments`, `emails`+`email_days`), droppable. A calendar's *name* lives in `calendars`, one row per calendar; `events.calendar_name` is a copy the sync drags along, so renaming a calendar in Google reaches cold rows too |
| Email triage (AI summaries + your review decisions) | app.db `email_triage` | summaries regenerable at token cost; the mailbox state actions produce (labels/archive) lives in Gmail |
| Daily brief (AI morning summary) | app.db `daily_brief` | regenerable at token cost; DB-only, never written into the vault — the /daily-brief skill is a thin wrapper over GET /api/brief |
| OAuth tokens | app.db `tokens`, keyed (provider, account) | never in frontend code or API responses |
| Finance (balances + transactions + recurring + liabilities) | Plaid | app.db cache `fin_accounts`+`fin_transactions`+`fin_recurring`+`fin_liabilities`, droppable; `plaid_items` holds item access tokens — credentials like `tokens`, NOT droppable |
| Weather (forecast) | Open-Meteo | app.db cache `weather_days`+`geo_cache`, droppable; no credentials |
| Basemap tiles (§24) | openstreetmap.org | app.db cache `tile_cache`, droppable; no credentials. No TTL on purpose — street geometry doesn't move, and `fetched_at` is for reading, not expiring. The only blob table in the app |
| News | Kagi Kite (kite.kagi.com) | app.db cache `news_items`, droppable; no credentials; CC BY-NC content |
| Subreddit posts | Arctic Shift (arctic-shift.photon-reddit.com); Reddit `.rss` was a today-only fallback until every feed 404'd (removed 11-22) | app.db cache `reddit_posts`+`reddit_sync` (fetched-day markers), droppable together; no credentials — Reddit's own `.json` is 403 to anonymous clients. `score`/`num_comments` are NULLABLE: the archive backfills scores over ~3 days and `.rss` carries none, so unknown stays NULL rather than a real-looking 0. `search()` is the live face — one subreddit, uncached, never written (the archive 400s a query scoped to neither a subreddit nor an author, so Reddit-wide search is not available at all); it is the console's `reddit_search` and `--search`/`--sub` on the CLI. A 422 "Timeout. Maybe slow down a bit" is the archive under load rather than a bad query, so `_api` retries once and then raises `ArchiveBusy`, which callers distinguish from a 400 |
| Listening history | Spotify | app.db `plays` — NOT droppable (recently-played only ever returns the last 50 tracks, so this table is the only durable record between polls) |
| Artist genres | Last.fm | app.db `artist_tags` — a genuine cache, keyed on `plays.artist_key` (artist name). Replaced `artist_genres` in v35: Spotify stopped returning `genres` at all |
| Messages | `~/Library/Messages/chat.db` (local, no API) | app.db `messages`, droppable — chat.db keeps full local history, so a drop only costs a re-sync (`sync_state` ROWID watermark, `MESSAGES_BACKFILL_DAYS` back on first run) |
| Packages + trips (extracted from email) | app.db `packages`+`trips` | NOT droppable — the only record of order/flight state this app keeps; `email_extract` (raw per-message model output, keyed by msg_id) IS a genuine cache, droppable at model-cost only |
| People (birthday, watch cadence, notes, day-one tag + crew) | app.db `people` | NOT droppable — real facts with no other source; last-contact columns are computed live from `emails`/`messages`/`events` (never stored), which stay the sources of truth for contact history. `day_one`/`crew` are hand-set (migrated from the old vault's `day-ones.md`); a day one with no message or email history is a standalone row keyed `manual:<name>` |
| Expenses claimed from Dad | app.db `expenses` + `expense_batches` + `expense_reviewed` | NOT droppable. A claim snapshots day, merchant and amount; `transaction_id` is a soft link into the droppable `fin_transactions`. Sent rows are frozen to match the spreadsheet. Reviewed is kept per transaction, not as a date watermark, because Plaid backdates late-posting charges. A pending charge can be claimed: `finance._sync_item` calls `expenses.follow_posted` with Plaid's `added` rows, and a posted charge's `pending_transaction_id` moves the claim onto it in the same write. Unsent claims take the posted amount (whole claims) or are capped by it (partial). Sent ones stay frozen and record `posted_amount` for the card to flag |
| Debts (personal IOUs) | app.db `debts` | NOT droppable — migrated from the old vault's `debts.md`, which is no longer read. Distinct from `fin_liabilities` (Plaid's institutional debt): one row per debt, not per person, so each reason settles on its own |
| User lists (shopping, packing, …) | app.db `lists` + `list_items` | NOT droppable — migrated from `shopping.md`, no longer read. v41 generalized the single `shopping_items` table into one row per list: Shopping is seeded and holds the old rows at their old ids, and the user makes the rest. Deleting a list cascades to its items |
| Watchlist | app.db `watchlist` | NOT droppable — migrated from `watchlist.md`, no longer read; `services` records where a title streams |
| Location (raw fixes) | app.db `location_points` (pulled from the location relay, §10) | NOT droppable — the only record of location history; `location_days` is the derived/droppable feature cache |
| Named places, **and home** (`app/places.py`) | markdown, one note per place with `type: place` frontmatter (`02-Areas/places/`) | no app.db table — read straight out of index.db's frontmatter JSON, the §22 courses arrangement, so a place edited in Obsidian is right on the next reindex. Fields: `lat`, `lon`, `radius_m` (default 250), `kind` (`home` is the only kind with behaviour), and `from`/`until` day_keys, both inclusive. **Home is dated, not a constant**: `at_home`, `time_at_home_min`, `first_leave` and `last_return` are scored against the home in effect on *that day*, and a day no entry covers has **no** home — those four metrics are absent rather than 0, because a zero there is a measurement of the config rather than of the day. A single fixed `HOME_LAT`/`HOME_LON` is what scored a month of days at the previous home against the new address after a cross-country move, put twelve false zeros into the feature matrix, and manufactured a `steps ~ time_at_home_min` correlation that dissolves once the days are scored correctly (IDEAS). With no `type: place` note anywhere the module falls back to the config constants, so the notes are an upgrade path and never a prerequisite. Two declared places whose radii overlap while both are in effect report `ambiguous_with` rather than resolving — at campus density the gym is ~15m from the dorm and accuracy is p50 17m, so the honest answer is that a fix there could be either |
| Features (§11 cross-provider day matrix) | app.db `features` | fully derived/droppable — a denormalization of the tables above (health/screen/spotify/messages/location/finance/events/tasks/weather), recomputed by `providers/features.py` with SQL/python only |
| Alarms on the phone (§21) | app.db `alarms` | NOT droppable — `external_id` is the only handle on a Reminders item, and a Reminders item is invisible from here, so a dropped row strands an alarm that fires forever. The *desired* set is recomputed every cycle; the row records what was actually written (`written_at`) |
| At-desk notifications (§21) | app.db `notifications` | droppable — a delivery log keyed on the fact, kept so a fact isn't announced twice and so an expired row still records that the channel had something to say while you were out |
| Activity log — what the backend changed on its own (`app/activity.py`) | app.db `activity` | NOT droppable — the only record of what an automation wrote and the handle its Undo needs (`ref`: a task id, a calendar event id, food_log ids, a note path). "Seen" is `sync_state.activity_seen_at`, holding the newest row id you said "looks fine" to |
| Self-text bursts (`app/selftexts.py`) | app.db `selftext_bursts` + `selftext_messages` | NOT droppable while routing runs: `selftext_messages` is the "already routed" mark, so dropping it re-files every text since `sync_state.selftext_since` |
| Scheduled wakes (§21) | the OS (`pmset -g sched`) | no table at all, on purpose — the OS list is authoritative and shared with the system, so anything kept here could only drift from it; `WAKE_OWNER` partitions our rows from `com.apple.alarm`'s |
| Contacts (name/phone/email cards) | Contacts.app → CardDAV → iCloud | app.db keeps none. Reads go through the local AddressBook sqlite (`providers/messages.py`); **writes go out through osascript only** — writing back through the read path would corrupt the sync engine's private store, and osascript is the one direction that ends anywhere but this machine |
| Attachments (files sent by text/email) | chat.db + Gmail | app.db `attachments` (metadata) + `attachment_text` (extracted text), both droppable — metadata rebuilds from the sources on the next sync, text at model/parse cost |
| The Brain — facts about the user (§ The Brain) | app.db `brain_facts` + `brain_pages` | `07-Brain/` in the vault, a generated read-only view (the tasks.md arrangement). Facts are *derived* from sources that stay where they live (transcripts, `messages`, `emails`, notes) and rebuildable from them — except `verdict`, the user's ✓/✗, which is NOT droppable. `brain_runs` is the ingest ledger and watermark, droppable |
| Food log, foods, targets inputs, weigh-ins, closed days (§25, `app/food.py`) | app.db `food_log` + `foods` + `food_profile` + `food_targets` + `weigh_ins` + `food_days` | NOT droppable. A closed day also writes the `Log food` habit check-in (summary in its note) — the check-in is the habit's truth, `food_days` the food side's. `foods` rows with source `usc_estimate` are regenerable at token cost, except ones the user corrected (confidence 1), which aren't |
| USC dining menus + campus venues (§25) | hospitality.usc.edu (menu JSON, location pages) | app.db `usc_menu_items` + `usc_menu_days` + `food_venues` + `food_estimates`, droppable. `food_venues.dining_dollars` is set on insert and never overwritten, so a hand correction survives |
| Food reference data (§25) | USDA SR28 (SR Legacy), checked in as `backend/app/food_data/sr28.json.gz` (`scripts/build_sr28.py`); Open Food Facts per barcode | read-only; a food that gets logged is copied into `foods`, so a rebuilt SR28 never moves a past day |
| Campus café menus (§25, `app/food_seed.py`) | `food_data/coffee_bean.json` (CBTL's PDF) + `food_data/campus_menus.json` (Panda as recalled; guide item names to estimate) | loaded into `foods` as source `custom`, key `venue\|name` — the same key `food_create` uses, so the user's entry and the seed's are one row. The seed rewrites only rows marked `detail.seed` and not corrected |

Rules: one truth per datum; the other format is generated, read-only, and
regenerable. Sync is one-way only. Never parse a generated file back in.

## Food (§25 — `app/food.py`, `app/food_estimate.py`, `providers/usc_dining.py`)

Spec and the decisions behind it: `FOOD_PLAN.md`. What a later session needs:

- **Unknown is not zero.** A food's nutrients are per serving and a nutrient
  it doesn't carry is absent; day totals report `missing[k]` (how many entries
  lack k), and the UI marks those totals as floors. A custom Panda bowl with
  only macros must not read as "no vitamin C today".
- **Dining-hall nutrition is estimated once per item name** (USC's API has
  names, allergens and diet tags only): decompose → SR28 candidates → pick,
  with the model's own macros for an unmatched component (confidence 0.5) so
  nothing sums to zero. Rows are only ever *inserted*; a user correction via
  `food_fix` is confidence 1 and permanent. The poller estimates up to 150 new
  names a cycle; the menus read kicks a background run.
- **Targets are per day, not per goal.** Protein 1.8 g/kg every day; energy is
  maintenance on a rest day and maintenance + session + surplus on a training
  day (Exercise checked; a gym-ish calendar event counts only until the habit
  is checked either way). Weekly weigh-in trend moves maintenance ±100 kcal at
  most once a week — lazily, on the day read, not on a schedule.
- **Suggestions plan a meal, not the day.** Every term is against this meal's
  share of what's left, capped at 1.4× a normal meal of that kind; a future
  day uses the normal share. One venue per plate, a penalty per extra entrée
  (≥20 g protein). The first version put three entrées and 117 g of protein on
  one lunch.
- **Late night is a snack.** 22:00–03:59 defaults to `snack`; it's still the
  previous day-key, which is where a 1am Taco Taco run belongs.
- **Logging never asks.** The food write tools are in `console_changes._VAULT`
  with no guard, so auto mode logs without a card; everything they write is
  editable or deletable from the sheet.

## Day boundary

`db.day_key()` — local time, 4am rollover (00:00–03:59 → previous day). It is
the ONLY way any code (or skill) computes "today" for checkins/daily keys.

## Context providers (`app/providers/`)

One module per external source, common interface `get(rng, metric)`,
registered in `providers/__init__.REGISTRY`. Three faces over the same code:

1. REST `GET /api/{source}?range=&metric=` (routes generated from the registry;
   `GET /api/providers` describes it)
2. CLI `python -m app.providers {source} --range this-week [--metric m]` —
   what skills and the console call. `--body <id>` (email only, duck-typed on
   a provider-level `body()` like `--extract` is on `extract_pending`) reads
   one message whole instead of a range, for when a summary dropped the thing
   you actually needed — a call link, an address. Live fetch, never stored
3. app.db cache tables, refreshed by an asyncio poller in the FastAPI lifespan
   (`PROVIDER_REFRESH_SECONDS`, default 300; failures log, never kill the loop)

Range vocabulary (`today`…`last-year`, `next-*`, plus explicit dates —
`2026-06-15` or inclusive spans `2026-06-01..2026-06-15`, capped at 366 days)
parses ONLY in `providers/ranges.py`, anchored on `db.day_key()`. Providers
without `ALLOW_FUTURE` reject any range ending past today — `next-*` by name,
explicit dates by date. `GET /api/providers` advertises the grammar
(`"custom"`). Cache tables are keyed per day, so explicit dates read the same
rows named ranges do; days the poller doesn't keep warm are fetched on demand
in `get()` and backfill the cache. `providers/weather.py` is the smallest
copyable template (keyless, metric-shaped, no OAuth).

### §2 Calendar

`providers/calendar.py` is the first real provider (§2): school
Google Calendar, `ALLOW_FUTURE`, events keyed to every plain local date they
touch (no day_key rollover — a 1am event is on its own date), with
`calendar_days` marking synced days so empty ≠ never-fetched.

Writes live OUTSIDE the provider in `app/routes/calendar.py`, the same split
`routes/email.py` draws: a provider that mutates its own upstream stops being a
cache of it. Create / move / delete / RSVP are four operations, not three —
delete is Google's own `DELETE …/events/{id}`, because blanking an event's
fields leaves a ghost and `status=cancelled` is the same delete spelled in a way
only organizers may use, while an RSVP answers the organizer and removes
nothing. Each write re-pulls only the days it touched (`calendar.sync_days`,
clustered so an August→December move doesn't re-fetch the autumn) instead of
`refresh()`'s six-week window, which would miss an event outside it anyway.
Three rules are enforced here rather than trusted to the caller: a bare clock
time is in the zone the user is standing in **on the event's date**
(`tz.zone_on`, never this machine's and never the zone the conversation has
been discussing); an all-day `end` is inclusive going in and exclusive going
out (Google stores the day after, and inverting that drops the last day of
every trip); and a reschedule always PATCHes start and end together, keeping
the event's existing duration when only one is given. Guests are never emailed
unless `notify` is set.

A repeat is a property of an event, not a fifth operation: `_rrule` turns words
a person says ("weekly", "weekdays", "until 2026-12-12") into the RRULE Google
stores on one master event and expands, so a semester of lectures is one write.
Two failures there are silent rather than loud, so both are handled before the
POST: `UNTIL` is inclusive and becomes the last *instant* of that local day
(written as a bare midnight, the last class of the semester disappears), and it
has to match DTSTART's form — a DATE for an all-day series, a UTC DATE-TIME for
a timed one. A rule that can't be built never reaches Google, because the
alternative is a non-repeating event on the calendar and a 400 about RRULE
syntax. The cache stores the *expansion*, so `match` always resolves to one
occurrence and every write takes `scope`: "this" (default) or "all", which
redirects to the master behind it. There is no "this and following" — it is a
split, not an edit, and the recipe (end the series with `repeat_until`, create
a new one) is in the refusal. A series write re-pulls its occurrences' days
only where `calendar_days` already has them: four months of Tuesdays would
otherwise fetch an autumn the cache never held, and an unsynced day can't be
stale. Writing needs `calendar.events` on top of the read-only
scope, so the stored scopes are checked before the call — Google's own answer
is a 403 about `insufficientPermissions`, which reads like a malformed request
rather than a consent never given.

`free_slots()` (console tool `free_time`) is the read that belongs beside those
writes: waking hours minus the day's timed events, per day, through `get()` so
it inherits the backfill, zone conversion and duplicate collapsing. All-day
events are listed beside the gaps rather than blocking them — a birthday would
otherwise mark a whole day busy. Without it the console answered "when am I
free" by printing a day's events and eyeballing the holes, which is where the
spill-over event and the cross-zone hour go wrong.

### §3 Health

`providers/health.py` (§3) is Google Health API v4 (Fitbit Air) on the
personal account: daily metrics (steps, resting/avg/max HR, sleep hours +
stage minutes, active-zone minutes) via `dataPoints:dailyRollUp` (civil
ranges, chunked — 14-day cap for heart-rate, 90 for the rest) and filtered
`dataPoints` lists for sleep sessions, daily resting HR and daily HRV; one
`health_days` row per (civil day, metric) — weekly trends are SQL — with
`health_sync` markers so "watch not worn" ≠ "never fetched". Sleep belongs
to the wake-up date. `refresh()` also keeps a short trailing window of
**intraday HR** warm (§12): the `heart-rate/dataPoints:rollUp` endpoint
(`{"range":{startTime,endTime}, "windowSize":"3600s"}`, physical-time,
14-day cap) buckets to one hourly mean bpm per UTC hour, converted to the
local tz and stored one `health_hours` row per (local day, hour). It's
best-effort — wrapped so an intraday failure never sinks the daily sync —
and consumed only by `features.py`'s `hr_var_day`.

### §5 Email

`providers/email.py` (§5) is Gmail (`gmail.modify`, school account): message
metadata only — bodies are fetched transiently for summarization, never
stored — keyed to the plain local date received, with `email_days` sync
markers. `email.body(msg_id)` (CLI `--body`) reads one message whole on the
same transient terms, because a one-sentence summary regularly drops the
detail the message was opened for; the no-storage rule is what's load-bearing
here, not no-bodies. Over `EMAIL_BODY_FULL_CHARS` it drops the quoted reply
chain (`email_triage.strip_quotes`) before it truncates — a forward keeps its
link at the bottom, so a blind cut loses exactly what you came for — and
flags which happened via `quoted_trimmed`/`truncated`. `refresh()` also reconciles inbox membership, reconciles `unread`
per synced day-window (so archived mail's read state tracks Gmail too, not
just the inbox), and hands new inbox
messages to `providers/email_triage.py`: batched summary + filing label +
search terms + an action guess on a cheap model via
`claude_agent_sdk.query()` — subscription login like the console, never the
raw API — cached in `email_triage` by message id so nothing is summarized
twice. The prompt is vault-blind (messages, the label set, this term's course
codes). Write actions live OUTSIDE the provider in `app/routes/email.py`: the
review queue (`GET /api/email/queue` = inbox minus already-reviewed) and three
actions — archive, unsubscribe (RFC 8058 one-click POST → `mailto:` via the
Gmail send API → hand back the https link for the browser; then archive),
keep — each of which **files** the message under its label.

**Review files; search finds.** Once reviewed, the only thing a message is
still for is being found again, so review is organised around that. Every
message gets a label from a closed set (`email_triage.TAXONOMY`: Courses,
USC, Housing, Career, Career/Applications, Zeta Psi, Clubs, Receipts,
Finance, Travel, Health, Accounts, Dev, Newsletters, Personal — plus
`Courses/<CODE>` per term from the vault, plus anything added under `Filed/`
in Gmail), all real Gmail labels under one `Filed/` parent, created on first
use with their parents so they nest. Closed because the free-form "bins" it
replaced forked into near-duplicates (`USC Orientation` vs `USC
Orientation/Weekly Updates`) within weeks; a model label outside the set is
dropped, never created. The card arrives labelled — the sender's record
first (`email_learn.predict_label`: the last two filings agree), the model's
pick otherwise — and correcting it is picking another chip before acting.
What was applied lands in `email_triage.user_label` (real data, undo clears
it and takes the Gmail label back off), so two corrections move a sender.
Delete and bin are gone: 163 deletes were of mail nobody would search for,
and filing it costs nothing a trash didn't.

`terms` is what makes the search good without breaking the no-bodies rule:
the model pulls out the handles someone would type months later (order and
confirmation numbers, amounts, dates, people, course codes) and those, not
the body, are stored. `providers/email_search.py` ranks every cached message
through an FTS5 index (`email_fts`: subject, sender, summary, terms, label;
bm25 weighted toward subject and terms; every word a prefix match, hence no
porter stemmer — it indexes "Dining" as "dine", which "dini*" can't reach),
then falls through to Gmail's own `q=` search, whose hits are cached as
metadata so the backfill picks them up. The index is derived and rebuilt
whole when a signature over `emails`/`email_triage` moves, instead of
being kept in step by the five places that write those tables. Faces: the
search box atop the email sheet (`GET /api/email/search`), the console's
`email_search` tool, and `python -m app.providers email --search TEXT`.
Summarizing isn't inbox-only any more: `pending()` takes the inbox, then
`EMAIL_BACKFILL_PER_REFRESH` older messages per refresh, so the whole cache
gets labels and terms (all 1,195 did within a few hours of shipping).

**The recommendation on the card is not the model's.** Measured over the
first 875 recorded decisions the model agreed 28.9% of the time, where always
saying "archive" agrees 78.6% — it recommended `keep` 331 times against 8
actual keeps. A prior drawn from the model's idea of a person is not a prior
about this person, so `providers/email_learn.py` takes it from the record
instead: `email_sender_stats` aggregates `email_triage.user_action` by sender
and by domain, `record()` folds in each action, `unrecord()` backs out each
undo, and `ensure_warm()` rebuilds the whole table from `email_triage` when
it is empty. Derived and droppable like every other cache here.

(Since review went to three actions the learner reads the retired `delete`
and `bin` as `archive` — `email_learn.FOLD`, at read time, so the record
stays as it happened — and the dwell rule below is gone: its only verdict
was "delete", which now means the default. The history is kept for why.)

Two signals carry it. A sender decided twice the same way is decided: 91%
precise over 349 calls, and 97% over the 289 of those that say "archive"
(`delete` is the weak one at 60%). A domain needs three decisions, never
speaks over an address already known, and lands at 77%. Then dwell — how long
a card has sat — the weakest of the three at 52%, which runs
opposite to intuition: mail acted on inside six hours is 92% archived, mail
still sitting after three days is 57% *deleted*. Time in the queue never
meant "I am keeping this", it meant "I did not care". Dwell is unavailable
when mail arrives, so a fresh card is judged on its sender alone and the call
sharpens as it goes stale.

The counted evidence (`obs`) and the recency weighting (`n`) are separate
columns on purpose: a decayed count cannot be compared against a
whole-number floor, since two decisions a day apart sum to 1.992 and two a
quarter apart to 1.5. Evidence does not evaporate; only the question of which
action is current decays (90-day half-life, chosen by backtest).

The model keeps the summary, and one thing it is genuinely good at: it
flagged 28 of the 30 messages actually unsubscribed from (93% recall, 28%
precision). Too imprecise to recommend, too useful to drop — it rides along
as `unsubscribable`, an offer beside the recommendation rather than the
headline. Its own pick stays on the card as `model_action` so the
disagreement is visible. Every card states which decisions it reasoned from,
the same rule §24 follows for the place shortlist — something learned from
two clicks says so before it is trusted. Hover-↵ runs the recommendation,
filed under whatever the label chip says; with delete gone every action it
can take is ⌘Z-undoable, bar the unsubscribe request itself.

`POST /api/email/draft` (console tool `email_draft`) writes a reply into the
mailbox as a **draft** and has no send path: the draft waits in Gmail until a
human sends it, the same line `reply_draft.py` draws for messages (§17 —
suggest, don't act). Given `reply_to` it fetches the parent for its Message-ID
and sets `In-Reply-To`/`References` as well as `threadId` — Gmail threads on
the headers, so a draft carrying only `threadId` opens a fresh thread on the
recipient's side. This exists because it must not depend on the claude.ai
Gmail connector: on 2026-08-16 that connector stopped loading into console
sessions altogether (`needs-auth` in every non-interactive session while
`claude mcp list` reports it connected), with nothing local having changed.
The backend's own `gmail.modify` token covers `drafts.create`, so the console
keeps the capability regardless of what the connector is doing.

**Scheduled send** (`app/email_schedule.py`, console `email_schedule` /
`email_scheduled` / `email_unschedule`, `/api/email/scheduled`) is the one
deliberate exception to "a human sends it". Gmail's API has no scheduled send,
so the message is an ordinary draft and the time is a `scheduled_emails` row
(source of truth); a 30-second loop in the app calls `drafts.send` at
`send_at`, and `wake.py` asks pmset to wake the machine a minute before. The
human check moves to the approval card — `email_schedule` is guarded in
`console_changes`, so it cards even in auto mode. Editing the draft in Gmail
changes what goes; deleting it cancels (the send 404s → `cancelled`). Sends
are claimed `scheduled → sending` before the call, and a row stuck in
`sending` becomes `failed` ("check Sent") rather than being sent twice.

### §18 Finance (Plaid)

`providers/finance.py` (archive/PLAID_PLAN.md) is Plaid: per-item cursor loop over
`/transactions/sync` (pages accumulate in memory; rows + cursor persist only
after `has_more` goes false, so a mutation-during-pagination error can
restart cleanly from the stored cursor) plus cached balances from
`/accounts/get`. Amounts keep Plaid's sign — positive = money OUT — and flip
only at display layers; `day` is Plaid's civil date, like calendar/health.
No webhooks: localhost app, the 300s poller is the freshness budget. Metrics:
`transactions` (each row carries Plaid enrichment when present — brand
`logo_url`, in-store `location` "City, ST", merchant `website`, and
`counterparties` pre-joined to the non-merchant payment parties like
"DoorDash"/"Toast", dropping the merchant itself and bank-rail
`financial_institution` entries as noise; all NULL-until-cursor-reset derived
cache), `spend` (daily outflow sum, transfers/loan payments excluded
so card payments don't double-count), `balances` (range-independent
snapshot), `recurring` (§18 — active `/transactions/recurring/get` streams,
`predicted_next_date` is Plaid's own prediction, not recomputed),
`bills_due` (§18 — `fin_liabilities` due ≤14d + recurring outflow predicted
≤7d, merged one list). All Plaid HTTP goes through `providers/plaid.py` —
raw `urllib` POSTs, no plaid-python.

§18 finance deeper: `/transactions/recurring/get` needs no re-link, called
regardless of item status. It and `/liabilities/get` are **once-a-day legs** —
both re-fetch their whole set and neither dataset moves faster than daily, so
running them on the 300s cycle spent ~570 identical calls a day. A
`sync_state` row per `finance:{leg}:{item_id}` holds them to one run per
`day_key`, set only after a fetch lands: a failed or skipped leg stays due, so
it retries next cycle and `POST /api/plaid/sync` still heals immediately.
The transactions cursor is incremental and stays on every cycle.
`/liabilities/get` does need
consent — items originally linked with `products: ["transactions"]` only
raise `PRODUCTS_NOT_SUPPORTED`/`ADDITIONAL_CONSENT_REQUIRED`, caught and
surfaced as `plaid_items.status='needs_consent'` (a fourth status alongside
`ok`/`login_required`/`error`). FinanceWidget's reconnect banner already
fires on `login_required`; it now also fires on `needs_consent` (same Link
update-mode flow — `create_link_token` always requests
`additional_consented_products: ["liabilities"]`, on both fresh connects
and reconnects). A successful `/liabilities/get` self-heals the status back
to `ok`; `POST /api/plaid/sync` resets both parked statuses so a reconnect
takes effect immediately instead of waiting for the poller. Once a card's
`due_day` lands in `[due_day-5, due_day]`, `_maybe_create_bill_task`
auto-creates ONE task ("Pay `<mask>` — min $X due `<date>`"), deduped on
`source_kind='finance'`+`source_ref=account_id:due_day` via
`db.task_exists_for_source()` — the shared dedupe helper §16's check-in
auto-task will reuse. Subscriptions/price-creep stay brief lines only,
never tasks.

### §6 Weather

`providers/weather.py` (§6) is Open-Meteo — no key/auth. `ALLOW_FUTURE=True`
(it's a forecast); one row per (civil day, metric) in `weather_days` — no
sync-marker table like calendar/health, since every cached day carries a
real value for all metrics, so a gap always just means "fetch it" (cheap,
keyless, safe to retry). One API call covers both directions via Open-Meteo's
`past_days`/`forecast_days` params. Origin is `HOME_LAT`/`HOME_LON` by
default; `_origin()` guard-imports `providers/location.py` (§10)
and prefers a fix < 6h old when it exists. `geocode(name)` resolves
place names via Open-Meteo's geocoding API, cached in `geo_cache`. The daily
brief pulls `weather.day_summary()` (all metrics for today) and, for today's
calendar events with a `location` string, an ad hoc `weather.forecast_at()`
per geocoded event — both skip silently on a miss rather than failing the
brief.

`current()` is the glance strip's right-now chip and the only read here with a
fallback: two attempts a beat apart, then the last good reading served with
`stale: True` and its own `as_of`, then nothing (the route's 502). **A
fallback is only as good as what fills it**, and for its first week nothing
did. The cache was written by `current()` alone — i.e. by a page load — while
a served-stale reading expires after an hour, so it could cover a blip during
a *repeat* visit and never the first load of the day. The 502s kept coming
after the fix shipped (four more, 09-16..09-19, each mid-desk-load) because
the first load of the day is exactly when they landed. `refresh()` now takes
the reading too, so the poller keeps it one cycle fresh whether or not anyone
is looking. The shape worth remembering: *a cache whose only writer is the
request it protects cannot protect the first request.*

### §7 Screen time — REMOVED 2026-09-13

Gone entirely: `providers/screen.py`, `screen_days`/`screen_apps`/`screen_sync`
(migration v46), the card, the sheet, `AW_HOSTS`, `DOOMSCROLL_APPS` and the
ActivityWatch LaunchAgent. It could only ever measure this laptop, and the
laptop is not where the time goes; the phone half has no route (no Screen Time
API, and `knowledgeC.db` is undocumented, wants Full Disk Access the app bundle
lacks, and changes schema between releases). A decision about the measurement,
not about the code.

The one idea that outlived it is live elsewhere and needs no §7 to read: **a
sensor's silence is only a zero if the sensor was watching**, now
`features._COVERAGE_GATES` (stated under "Recurring lessons → On absence", and
argued in that module's own comments). Day-keying behavioral sources is the
day-boundary rule above.

Design as it stood, data, and the iPhone not-feasible evidence:
`archive/SCREEN_TIME.md`.

### §20 News

`providers/news.py` (§20) is Kagi Kite — no key/auth. Kite has no per-day
historical API (it republishes one edition per category, ~daily), so unlike
every other provider `day` in `news_items` is the **day_key of the fetch**,
not an article date: `get()` only ever backfills *today* on demand (a
pre-refresh fetch just tags whatever Kite is currently serving under
today's day_key rather than erroring), and `refresh()` no-ops once a
day_key already has rows. `NEWS_CATEGORIES` (config, slugs matching Kite's
file names minus `.json`) fan out from the `kite.json` index, which also
supplies the live host (`kite.kagi.com` redirects to `news.kagi.com` —
resolved via the index fetch's final URL rather than hardcoded). Metrics:
`headlines` (top `NEWS_HEADLINES_PER_CATEGORY` clusters per category),
`stories` (every cluster, full `short_summary`). `top_headlines()`
round-robins categories for the brief's `world_news` context key, which
renders as a `## World` "5 things to know" section, title-only. Content is
CC BY-NC — personal use; `sources` (JSON list of contributing domains) is
kept for attribution.

### §8 Spotify

`providers/spotify.py` (§8) is authorization-code + PKCE against
`accounts.spotify.com` — public client, no client secret, raw `urllib` like
`plaid.py` rather than an SDK. Day is **day_key**, a behavioral source like
§7/§20. Spotify banned `localhost` (non-loopback-IP) redirect URIs in Nov
2025 and requires the URI registered *exactly*, so unlike the Google Connect
flow the CLI can't spin up its own ephemeral loopback server: the redirect
always lands on a real FastAPI route, `app/routes/spotify.py`'s
`GET /api/oauth/spotify` (fixed at `http://127.0.0.1:<BACKEND_PORT>/api/
oauth/spotify`), which does the code exchange. `python -m
app.providers.spotify authorize [--client-id ID]` is a thin client of that
route's `/start` and `/status` siblings — POST to start (stores the PKCE
verifier in-memory, in the running backend process, keyed by `state`), open
the consent URL in a browser, poll status until connected. Token row:
`tokens` (provider='spotify', account='personal', client_secret=''). Polling
(`refresh()`) pulls `/me/player/recently-played?limit=50` every cycle —
well above anything that can accumulate in `PROVIDER_REFRESH_SECONDS`, so
nothing is missed between polls — and upserts into `plays`, deduped on
`played_at`. Unlike every other provider, `get()` never
backfills a missing day on demand: the recently-played endpoint can only
answer "the last 50," not a specific past day, so history *not* caught by a
poll is gone until the (unbuilt) GDPR-export importer backfills it — see
`plays`' migration comment. Metrics: `plays` (raw list), `listen_min`
(ms summed per day), `novelty` (share of a day's plays whose artist's
all-time-first play falls within `NOVELTY_WINDOW_DAYS` of that day),
`bracket` (first/last `played_at` per day), `top_repeat` (that day's most-
repeated track + count). No brief/widget integration yet — backend + CLI
only, matching §7's rollout. The extended-streaming-history importer
(`scripts/import_spotify_history.py`) is deferred until the GDPR export
(requested, arrives in days) lands — see IDEAS.md.

**Artist identity across the import seam (v35).** Group artists on
`plays.artist_key`, never `artist_id`. `artist_id` holds Spotify's real id on
rows the poller wrote and a synthetic `sha:`+sha1(name) on the ~266k rows the
importer wrote, because neither GDPR export carries an artist id — so grouping
on it splits every artist in two at the seam. That pinned `novelty` at ~1.0 for
the twelve days after 2026-07-14 (a six-year favourite counted as brand-new the
first time the poller saw it) and fed that into `features` → `correlations` →
the brief's Patterns line. `spotify.artist_key(name)` is the fix: the artist's
name, whitespace-collapsed and **casefolded in Python** — not a SQL expression,
because SQLite's `lower()` is ASCII-only and would leave "Vîęzë Ãsbæk" split
anyway. `spotify.ensure_artist_keys(con)` backfills and self-heals it (one
indexed probe once filled) and is called from `get()`/`summary()`/`refresh()`,
plus `features.py` and `lastfm.py`, which read `plays` directly.
`top_repeat`/`_top_track` still group on `track_id`, which was already
consistent across the seam.

### §8 Last.fm tags

`providers/lastfm.py` (§8) supplies the genre/mood tags Spotify no longer will.
The v16 `artist_genres` table never held a row: it was fed by the batch
`GET /artists?ids=`, which **403s** for this app (only single-resource GETs are
permitted), and it threw on every poll that saw a new artist, intermittently
breaking `spotify.refresh()`. Worse, the artist object no longer carries a
`genres` field at all — verified live 2026-07-26, the response is down to
id/name/images/urls, and `/recommendations/available-genre-seeds` is 404 and
documented as deprecated. So genres come from Last.fm's `artist.getTopTags`,
which is **name-keyed** — joined on `artist_key`, no Spotify id involved. Tags
are crowd-sourced: weights are 0-100 relative to the artist's own top tag, the
long tail is listener chatter (`TAG_BLOCKLIST`), and weights are treated as an
optional refinement since the documented sample response omits `count`
entirely. Enrichment is a bounded trickle, not a batch —
`LASTFM_ENRICH_PER_CYCLE` artists per poll, **recently played first, then
most-played**, so what you actually listen to is covered within minutes and the
~9.8k tail fills in over a day. Recency has to outrank play count: ordering by
plays alone queues an artist you tried once today behind the entire backlog, so
the current day's `coverage` sits at 0% for days — the one day anything reads.
An artist Last.fm doesn't know is marked `missing=1` rather than retried
forever, and that includes the not-found *error* (code 6), which is a settled
answer wearing a failure's clothes. Metrics: `genres` (ranked over the range; a play votes for each of its
artist's tags, so shares don't sum to 1), `artists`, `coverage` (share of a
day's plays whose artist is tagged — how much to trust that day's mix). No
`FRESHNESS` entry: `artist_tags` has no day column, so `health()` reports
coverage instead, and no `LASTFM_API_KEY` means state `off`, never red.

Each row keeps Last.fm's untouched reply in `artist_tags.raw` alongside the
cleaned `tags` (v36). The cleaning rules — blocklist, weight floor, tag cap,
spelling de-dupe, dropping an artist's own name — are judgement calls that get
revised as tag noise shows itself, and without the raw reply every revision
would mean refetching ~9.8k artists for data Last.fm already sent.
`python -m app.providers.lastfm reclean` re-derives every row offline instead;
pre-v36 rows have no raw and are skipped until next fetched.

`lastfm.day_mix(con, day)` folds today's top genres into `spotify.summary()`
(imported lazily inside the function — lastfm imports spotify for
`artist_key`, so a module-level import is a cycle), so MusicWidget gets genres
without a second endpoint or a second fetch. The mix always travels with the
`coverage` it was computed from: three genres off 20% of a day is a different
claim from three off 95%, and the genre list alone can't distinguish them.

**genre_variety** (§11 feature) is the normalized Shannon entropy of a day's
genre mix — 0 = one sound all day, 1 = evenly spread. It's what survives of
"genre as mood" after inspecting the vocabulary: these are genre words, not
mood words (bare `mellow`/`upbeat`/`romantic` occur once each across ~2k
artists; `sad` and `happy` never), so a tag→mood lexicon would fire on one or
two artists and report it as a mood. Gated on `GENRE_MIN_COVERAGE` /
`GENRE_MIN_PLAYS`, gap-not-zero. Its `FEATURE_DOMAIN` is **spotify**, not
lastfm: it's derived from the same `plays` rows, so pairing it with
`listen_min`/`novelty` would surface the play count against itself.
Deliberately *not* raw entropy or a distinct-genre count — both have more
spread, and a distinct-genre count is the only version that reaches
significance, but both are play volume in disguise, which is how you'd get a
confident `calendar_events ~ genres` that only means "busy days are listening
days".

**Listening shape** (§8 features, `spotify.listen_shape_by_day`) is the other
half of the same question: `listen_min` says how much you listened, these say
how. **repeat_ratio** is the share of a day's plays taken by its single
most-played *track* — obsession, keyed on track because an album played front
to back is not one song eleven times and grouping by artist can't tell them
apart. **listen_sessions** counts separate sittings, splitting on
`LISTEN_GAP_MIN` (30) of silence — above any single track, so a normal album
never splits — and **longest_listen_min** is the longest unbroken sitting.
Sessions are measured **between play markers only, never marker plus `ms`**:
the poller stores Spotify's recently-played instant and the GDPR importer
stores the export's `ts` (when the stream *ended*), so a marker is trustworthy
as a point in time while the two together are not. Gated on `SHAPE_MIN_PLAYS`
(5), gap-not-zero: over two plays `repeat_ratio` can only be 0.5 or 1.0. All
three are `FEATURE_DOMAIN` **spotify**, and the provider owns the math with
features.py calling it — the `novelty` drift (v35) came from a second copy.

Unlike `genre_variety` and `reply_latency`, these read: over real history
`repeat_ratio ~ resting_hr` is r=+0.411 (n=45, p=0.005), the strongest
cross-domain pair in the matrix, and `contacts ~ listen_sessions` r=+0.366
(n=101, p=0.0002) with its mirror `contacts ~ longest_listen_min` r=−0.339.
Note that sessions and longest are two views of one shape, not independent
evidence.

### §9 Messages

`providers/messages.py` (§9) reads `~/Library/Messages/chat.db` directly —
no API, no OAuth, just Full Disk Access granted to whichever process runs
uvicorn (System Settings → Privacy & Security). Day is **day_key**, a
behavioral source like §7/§8/§10. Unlike every other cache table, `messages`
IS genuinely droppable even though it holds real content — chat.db already
keeps full local history, so a drop only costs a re-sync, not lost data —
but `messages` and `sync_state` have independent lifecycles, so a clean
re-sync means dropping/clearing both, not just `messages` (see the v17
migration comment in `db.py`). Sync is incremental: a `sync_state` row
(generic `(key, value)` table, not messages-specific) tracks the last
`message.ROWID` synced; first run backfills `MESSAGES_BACKFILL_DAYS` (90) by
`date` instead. Read receipts (`is_read`/`read_at`, `is_delivered`/
`delivered_at` — outbound: they read yours; inbound: you read theirs) are
stamped onto chat.db rows *in place* after the row exists, which the ROWID
watermark never revisits, so each cycle re-reads the last 7 days by `date`
and writes only rows whose state changed (`_sync_receipts`). Rows are filtered to `associated_message_type=0` — tapback
reactions (2000s/3000s) and stickers (1000) carry synthesized pseudo-text
("Loved 'yes'") that would otherwise inflate `sent`/`received`/`contacts`
and could self-capture a spurious task. Contact names
come from every `~/Library/Application Support/AddressBook/Sources/*/
AddressBook-v22.abcddb` (Z_PK-joined phone/email tables), matched to chat.db
handles by normalizing both sides the same way (phone → last 10 digits,
email → lowercase) — `normalize_handle` is the single source of truth for
that comparison, used for contacts, chat.db handles, and
`MESSAGES_SELF_HANDLES` alike. `message.text` is frequently NULL on modern
macOS (content lives in `attributedBody`, an NSKeyedArchiver typedstream
blob); `_extract_text` isn't a general typedstream parser, just the one
shape needed — after the `NSString` class marker, a `+` byte introduces a
length-prefixed UTF-8 payload (one length byte, or `0x81` + a little-endian
uint16 for longer strings). Validated the parsing logic against 300 real
chat.db rows where both `text` and `attributedBody` happened to be
populated: 300/300 exact matches (the naive "decode + keep the longest
printable run" version this replaced managed ~3%, tripped up by typedstream
framing bytes and unrelated `NSString` instances like rich-text
attribute-run metadata). Caveat: that sample is necessarily simple
single-run messages, since `text` had to be non-null to serve as ground
truth — exactly the case where `_extract_text` short-circuits and never
reaches the parser; the parser's actual production use (`text IS NULL` rows
— quoted replies, rich attribute runs) is unvalidated and fails soft (empty
or, in principle, a wrong earlier `NSString`) rather than raising. Metrics:
`messages` (raw list, `--query` searches it), `sent`/`received` (per-day
counts), `contacts` (distinct handles per day), `unanswered` (every chat's
true last message, ignoring the query range on purpose — a chat gone quiet
for days should surface even if `rng` is "today" — where that message is
inbound and >`UNANSWERED_HOURS` old; for §17), `reply_latency` (median
minutes to answer, per day). `reply_latency_by_day()` is a module function
so features.py can call it instead of re-expressing the math in SQL (the
duplication that let `novelty` drift for twelve days, v35). Each inbound
message opens a debt in its 1:1 chat and your next outbound closes it;
samples land on the day of **your reply**, so a finished day is never
revised by a late answer. Group chats are excluded (nobody owes a group a
reply), the oldest unanswered inbound starts the clock (five texts and one
answer = as long as the *first* waited), pairs beyond `REPLY_MAX_GAP_MIN`
(48h) are a new conversation rather than a slow reply — retried against
their most recent message first, which on real history rescued 61 of 111
otherwise-dropped pairs — and a day under `REPLY_MIN_SAMPLES` (3) stays a
gap, since one slow answer isn't a habit. Never-answered messages
contribute nothing; that's what `unanswered` is for. Texts to yourself
(`MESSAGES_SELF_HANDLES`) are synced like any other message and no longer
become tasks here; `app/selftexts.py` routes them — see "Unattended writes".
Texting yourself logs both an outgoing and an incoming copy in chat.db, so
routing keys off the outgoing one only.

### §15 packages / §16 travel (extraction)

`providers/email_extract.py` (§15 packages + §16 travel) is shared
infrastructure, not a provider — no METRICS/get(), not in REGISTRY. It runs
inside `email.refresh()`'s cadence, called last (same place
`email_triage.summarize_pending` already runs): candidates from the synced
`emails` table by from/subject heuristics (`PACKAGE_SENDER_HINTS`,
`TRIP_SENDER_DOMAINS` in config.py) → body fetched transiently by msg_id
(reuses `email_triage.body_excerpt`, never stored) → batched
`EXTRACT_MODEL` (haiku) extraction via `claude_agent_sdk.query()`, mirroring
`email_triage` exactly → cached per msg_id in `email_extract` so nothing is
re-parsed → typed upserts into `packages`/`trips`. Package identity is
`merchant:order_id` when both are known (stable across an order's whole
lifecycle), falling back to `tracking_no` only when `order_id` is still
unknown — with a fold-in step that renames a stray tracking-only row onto
the canonical key once `order_id` shows up, so one real order can't fork
into two rows (hit this live on a real Vuori order: a "shipped" email had
only `tracking_no`, the later "delivered" email only `order_id`; fixed and
reverified against the same two emails). When an email carries *neither* —
a reply in the order's thread saying only what changed — it attaches to the
merchant's one open order, matching on a normalized merchant so the
confirmation's "Campus Ready" and the reply's "campusready.com" are the same
seller, and abstaining when that merchant has none or several open (guessing
which of two orders a bare date belongs to is worse than a blank card). Such
an email is trusted for what changed (eta, status, carrier, tracking) but not
for what the order *is*: its `merchant`/`item_desc` are dropped rather than
COALESCEd in, since a follow-up describes the order in passing while the
confirmation that opened the row named it properly. Found live: Campus
Ready's pickup-date reply was parsed correctly, keyed to nothing, and
discarded, so the card showed no ETA for two days while the right date sat
in the `email_extract` cache. Because that cache is keyed by msg_id and the
upsert runs only at extraction time, a change to these rules reaches nothing
already parsed — `backfill_packages()` (`python -m app.providers.email_extract
--backfill-packages`) replays the cache through them, stamping each row with
its own email's `received_at` so a long-delivered package isn't shoved back
into the card's three-day return window. `packages.status` only moves
forward through `ordered → shipped → out_for_delivery → delivered` on the
email path; `update_package()` — the console's `package_update` tool — may
move it backward, because forward-only exists to survive out-of-order email,
not to overrule the person holding the box. `create_package()` (the console's
`package_add`) opens a row with no email behind it at all: every path in
`refresh()` starts at a message in *this* mailbox, so an order placed on a
family member's account, a store pickup, or a tracking number read out over
the phone could otherwise never reach the card — found live when a backpack
and a shoe spray ordered on someone else's Amazon login went onto the shopping
list marked bought, their arrival dates surviving only as prose in a note. It
routes through `_upsert_package()` rather than its own INSERT, handing it a
synthetic `order_id` of `manual-<n>` (so the key reads `Merchant:manual-<n>`
and cannot collide with a real order id) unless a tracking number is given —
then that keys the row, exactly as on the email path, so the carrier's own
later notification lands here instead of forking a second card. That sharing
is why `last_msg_id` COALESCEs on conflict: a hand-write passes `msg_id=None`
and must not erase the pointer to the email that opened the row.
`trips` keys on `(pnr, flight_no, dep_day)`; email upserts touch itinerary
fields but never `status`/`gate` once AeroAPI (below) starts owning them.
Read routes: `GET /api/packages` (active = not delivered, or delivered
< 3 days), `GET /api/trips?hours=` — `app/routes/extract.py`. The daily brief
gets a Travel section (`packages_arriving`: ETA today/tomorrow +
return-window lines; `trips_soon`: near-term flight status).

`TravelWidget.jsx` puts both on the desk (added 2026-07-29 — until then the
data reached the brief and nowhere else, so a package arriving today was
visible only if that morning's prose mentioned it). One card, not two: each
feed is empty most days, and two permanently-blank cards is the equal-weight
noise the salience pass exists to end. NEEDS when a parcel lands today or a
flight is inside 48h or a trip's status isn't nominal, HAS when anything is
in transit, QUIET otherwise. Two horizons on purpose: `upcoming_trips()`
keeps its 48h default for the brief ("am I flying tomorrow?"), while the
route defaults to a year for the card ("what's booked?") — at 48h a flight
bought four months out is invisible until the night before, which is exactly
when a card about it has stopped being useful. `/api/packages` returns the
backend's `day_key` alongside the rows, because `eta_day` is a day_key and a
browser-side `new Date()` disagrees with it between midnight and 4am.

`poll_flight_status()`, same module, polls FlightAware AeroAPI for live
status on trips departing within `FLIGHT_POLL_HORIZON_HOURS` (48) —
`AEROAPI_KEY` unset is a quiet no-op, trip extraction from email works
without it. Poll cadence: at most every `FLIGHT_POLL_INTERVAL_HOURS` (1)
until `FLIGHT_POLL_CLOSE_HOURS` (6) out, then every poller cycle. A delay
or gate change fires a local desktop notification (`osascript`, no push
service).

That 48h horizon is not only a poll-cost choice: AeroAPI rejects a `start`
bound **more than 2 days out** (400 `INVALID_ARGUMENT`), so it is also the
furthest the endpoint can see. Raising it buys nothing but 400s. The `end`
bound has no such cap, which is what lets the window bracket the local day.
`_match_flight()` then picks our operation out of the several an ident flies:
the window must be wider than one UTC day (a trip row stores `dep_local`, and
21:00 at LAX is 04:00Z the *next* day — a one-UTC-day window comes back empty
for such a departure, and the poller then stamps `last_checked_at` on a flight
it never actually read: checked, and never looked at), and once it is, the
response's newest-first ordering makes `flights[0]` some other day's flight —
plus some rows carry `scheduled_out: null` and status `"result unknown"`.
Matching uses the **origin airport's** zone as the response reports it,
deliberately not `tz.zone_on()`: that answers where the *user* is that day, and
on a moving day the two disagree — a cross-country flight departs in Eastern
on a date `TIMELINE` files under Pacific. A 400 does not mark the source
degraded (it is the request being wrong, not FlightAware being down); 401/403
still does, since a dead credential behind a quiet no-op stays dead for a week.

`ensure_checkin_tasks()` creates the T-24h (`FLIGHT_CHECKIN_LEAD_HOURS`)
"Check in AA1234" task — `source='travel'`, deduped on
`source_ref=pnr:flight_no:dep_day` via `task_exists_for_source`, the same
pattern as finance.py's bill auto-task. It is a **sibling** of the poller,
not a step inside it: as one call in `poll_flight_status()`'s loop it sat
behind that function's `if not AEROAPI_KEY: return 0`, so the one piece of
§16 needing no external service was gated on the credential for the one that
does, and no check-in task had ever been created. Its window lives in the
SQL rather than a per-row hours check, so "departing soon" has one
definition here instead of two that can drift.

`create_trip()` opens a `trips` row from what the user says, reached by the
console's `trip_add`. Same hole `create_package()` closed, one store over:
every path into `trips` starts at a message in *this* mailbox, so a booking
made on someone else's account or made before the mail sync's history starts
has no way in — a cross-country move flight sat on the calendar and
in no other store, and the travel card read "2 trips booked" meaning
Thanksgiving. It routes through `_upsert_trip` so the COALESCE rules, the
derived `dep_day` and the status/gate protection stay written once, and a
real `pnr` keys the row so a later schedule-change email amends it instead of
forking a card — the job `tracking_no` does for packages. `dep_local`
requires a time, not just a date: a bare date parses to midnight, which dates
the check-in task a day early. **A flight on the calendar is not in `trips`**
— the two stores are unrelated, and only `trips` drives the card, the 48h
warning and the check-in task. Verified against the real pipeline (Vuori package
extracted/merged live; trip candidate query, upsert-forward, AeroAPI status
apply, notification, and T-24h task creation all exercised with a synthetic
trip row, since the account currently has no AA mail in the synced window).

### §17 People

`app/people.py` (§17) is a router, not a provider — no range/metric axis,
no refresh(). `people` holds the only real facts (birthday,
`watch_cadence_days`, notes; keyed on a normalized email/phone
`person_key`, same normalization as `providers/messages.py`'s
`normalize_handle` so a person lines up across `emails.from_addr`/
`to_addr` and `messages.handle` without a join table) — everything else is
computed live from `emails`/`messages`/`events`, which stay the sources of
truth for contact history. Sent-mail sync piggybacks on the existing
day-window sync in `email.py`: Gmail's `after:/before:` search isn't
inbox-scoped, so every message in range was already being fetched — the
change was just persisting the `SENT` label (`is_sent`) and a `To` header
(`to_addr`, first recipient only) that were already sitting in the API
response. `GET /api/people` (merged view), `GET /api/people/search?q=`
(name substring — person_key is opaque, this is how you find it), `GET
/api/people/{person_key}` (full history for one person: every email/
message/best-effort-matched calendar event, plus first/last-contact times
and counts — works even for a person_key with no `people` row yet, and
normalizes the path param the same way messages.py handles are, so a raw
phone number in any format resolves), and `GET /api/people/waiting`
(outbound email threads and iMessage chats — reusing `messages.py`'s
`unanswered` metric — with no later reply, > 3 days; console drafts
`waiting` tasks from it, nothing here auto-creates), and `GET
/api/people/signals` (birthdays + watch-due, same computation `brief.py`
pulls in, exposed directly for the widget). `PATCH /api/people/{person_key}`
upserts `name`/`birthday`/`watch_cadence_days`/`notes` (partial —
`exclude_unset`, same semantics as `PATCH /api/tasks/{id}`); it's how the
dashboard's edit form and manual console/SQL edits both land, and it's also
how a stub person (looked up but never before edited) gets its first real
`people` row. Birthdays come from `scripts/import_birthdays.py`, a rerunnable
AddressBook importer (`ZABCDRECORD.ZBIRTHDAY`, Apple epoch; stores bare
MM-DD when Apple's sentinel no-year value shows up, else YYYY-MM-DD).
Brief gets a new People section: birthdays within 7 days, contacts past
their own watch cadence — suggestions only, never auto-tasks (decided).

Two ways to act on a waiting row short of replying, both suggestion-only:
`POST /api/people/waiting/remind` turns it into a task due `db.day_key()`
(the mirror image of dismissing — dismiss says "this needs nothing", remind
says "not right now"). Its `source_ref` carries the message's own
timestamp, exactly like a dismissal's `through`, so the dedupe means "you
already have a reminder for *this* message" — `task_exists_for_source`
matches completed and dropped tasks too, so a person-keyed ref would make
answering someone once block ever being reminded about them again.
`POST /api/people/waiting/draft` returns candidate replies from
`app/reply_draft.py` (not a provider — no range/metric axis, nothing
cached, the `leave.py` precedent): the last `DRAFT_CONTEXT_MESSAGES` (20)
turns of that thread, oldest first, through `claude_agent_sdk.query()` on
the subscription CLI like email triage, `REPLY_DRAFT_MODEL` (sonnet — one
call per button press, and matching someone's voice is worth more than the
batch-triage economics). **It never sends**: the output is text to copy,
because an auto-sent reply in a real conversation is the one mistake here
that can't be taken back. Called only from the router, never the poller, so
nothing reads conversations in the background.

Frontend: `PeopleWidget.jsx` (card — waiting-on count + birthday peek;
either being nonzero is what makes it `NEEDS`) and `PeopleExpand.jsx`
(slide-over: name search, birthdays/watch-due/waiting-on lists, and a
per-person detail view with an inline edit form over `PATCH` and the
merged email/message/meeting history). Waiting rows carry four actions —
✎ draft (candidates open in place under the row, click one to copy), ⏰
remind, ✓ dismiss, 🔇 mute. Vault notes are deliberately not
part of this view — texts/emails/events/meetings only, per the user's
explicit scoping.

Verified live: real AddressBook import (11 birthdays), a full `emails`
resync confirmed `is_sent`/`to_addr` backfill onto previously-cached rows
(6 real sent messages picked up correctly), `/api/people/waiting`
surfaced real stale threads from both email and Messages, and the
dashboard itself (search → detail → edit → save → pin → back, via
Playwright against the running app) round-tripped correctly with no
console errors and no backend tracebacks.

### Attachments

`providers/attachments.py` is the files half of email/Messages, split in two
because the halves have opposite risk. **Metadata** (`attachments`) is written
by the iMessage and Gmail syncs — filename, mime, size, and a source-shaped
`locator` (an absolute local path for chat.db, an opaque Gmail attachmentId for
email). It's cheap, always on, and makes a file addressable without anything
having opened it. **Text** (`attachment_text`) is extraction, and it never runs
automatically: the module deliberately defines no `refresh()`, since the poller
calls that on every module that has one, so `--extract N` on the provider CLI is
the only way in.

Routing by mime *is* the security design. PDFs and text parse locally but never
in-process — a hardened child (`providers/_extract_worker.py`, run under
`python -I` with `RLIMIT_FSIZE=0`/`CPU`/`NPROC`) whose realistic failure mode,
a decompression bomb, is killed by the parent's RSS watchdog in `_run_guarded`.
Wallet passes (`.pkpass`) go to the same child, which unzips exactly one member
— `pass.json`, read to a byte cap rather than to the size the archive claims —
and renders it as the ticket a person would read: organization, style, dates,
every label/value field front to back, and the barcode message. The artwork
beside it is left alone for the same reason images are, and the signature isn't
checked, because "did the issuer write this" is a different question from "what
does it say". Passes are also routed by *suffix* when the declared type says
nothing (`_mime_for`): Apple Mail attaches them as `application/octet-stream`,
so mime alone would read half of them and shrug at the rest.
That watchdog exists because **macOS silently refuses to lower `RLIMIT_AS`,
`RLIMIT_DATA` and `RLIMIT_RSS`** — the API accepts the call and then rejects
every attempt, so an in-child memory cap does nothing on Darwin and the ceiling
has to be enforced from outside. Images are **never decoded locally**: raw bytes
go to the model as image blocks (subscription CLI, `tools=[]`, one turn), which
reads the picture without this machine running libjpeg/libwebp/libtiff over a
file a stranger sent. HEIC — the most common thing in chat.db — is therefore
unsupported on purpose; converting it would mean running exactly that decoder.

Images split by who chose them. A stored attachment arrived on its own, so it
gets `VISION_PROMPT` from a tool-free reader and only text goes further. An
image the user dropped, pasted or took off the Desktop tray goes to the session
as an image block (`console.turn_input`), held by id between upload and send:
a description lost what the question was about, reducing a CAD sketch to its
four numbers (11-03), and `Read` on an image path already put pixels in a
session that has tools, so the boundary only ever covered this one door. The
user's decision, 11-04. History keeps the image, and an image the API refuses
would fail every later turn, so a drop must open under sips (`_long_edge`) and
is capped to `TRANSCODE_LONG_EDGE`.

Extracted text is attacker-authored by construction, so `fenced()` wraps it in
`<untrusted_attachment>` before it reaches any prompt, and `email_triage`'s
system prompt names the block as data. Gmail bytes are fetched only on demand
(`email.fetch_attachment`), so syncing a mailbox of hostile PDFs never writes
one to disk.

`decorate(con, rows)` hangs a message's files off the message itself, and is
what puts them in a log — `messages.get()` and both of `people.py`'s history
queries call it. It's not redundant with the `"(photo)"` fallback in
`_message_text`: that only fires when a file arrived with *nothing typed*, so
before this a file sent **with a caption** was invisible ("here is the packet",
no packet). Chips render dim until the text is extracted, because searchable vs
not is the difference between the console answering a question about the file
and not. Stickers and `dyn.*` plugin payloads are excluded here for the same
reason `get()` drops them.

### §11 Feature matrix

`providers/features.py` (§11, the keystone) is a registered provider with no
external source: `METRICS=None` (a free-form feature axis — any feature name is
a valid `--metric` filter), `ALLOW_FUTURE=False`. It's a pure denormalization —
`refresh()` recomputes today+yesterday, `get()` backfills any never-computed
past day on demand (like `location.py`) — of the other providers' **cache
tables** into one tall `features(day, feature, value, PK(day,feature))` table,
using SQL/python only: no external calls, no LLM, and deliberately **not** the
sibling providers' `get()` (those trigger on-demand syncs). The `*_days` tables
(health/screen/location/weather) passthrough with a rename where needed
(`tmax`→`weather_tmax`); the raw-event tables aggregate to match each provider's
own metric exactly (`plays`→listen_min/novelty, `messages`→msgs_sent/received/
contacts plus `reply_latency` via the provider's own
`reply_latency_by_day()` — responsiveness, which the volume features can't
tell from chattiness, and delegated rather than re-expressed so the two can
never drift, `fin_transactions`→spend with the same exclusions as `finance.spend`,
`events`→calendar_events/busy_min over timed events clamped to their civil day,
`tasks`→tasks_done folded on day_key, `health_hours`→`hr_var_day`, the
population stdev of a day's hourly mean HR — a same-day physiological-load
proxy (§12), gapped below 12 covered hours so partial wear can't fake a spread).
`day` stays each source's own key (day_key
for behavioral sources + tasks, civil date for health/weather/calendar/finance);
values are not re-bucketed, so a feature always matches what that provider's own
endpoint reports. **An absent source is a gap, never a 0** — a real zero and "no
signal" must stay distinguishable for correlation.

**A day is recomputed when its sources move, not only when it's missing.**
`_stale_days` compares each source's own `refreshed_at` against
`MIN(features.refreshed_at)` for the day, so a late arrival — a provider
backfill, a lazily-fetched artist tag — reaches a day already computed. Every
source is watched from one registry (`_STALE_SOURCES`), including the three
whose timestamp doesn't sit beside their day: calendar through `calendar_days`
(v3's synced-marker), tasks through the `tasks_touch` trigger's `updated_at`
(which `datetime('now','localtime')` writes space-separated, so it sorts before
every ISO-T stamp of the same date and must be normalized before comparison),
and Last.fm tags through the `plays` that reference the artist. Pollers re-stamp
their whole trailing window each cycle whether or not a value changed, so that
window goes stale once per poll — **every arm must converge in one recompute**,
or every read recomputes forever.

**A constant is not a source, so moving one reaches nothing.** The complement
of the rule above, and the way it fails is quiet: `_stale_days` watches tables,
so changing a *threshold* leaves every already-computed day exactly as it was,
still carrying features the new threshold would have refused. Moving location's
gate to coverage (08-30) would have left the 8 days it disqualifies in the
matrix indefinitely — the same confidently-wrong reading the gate was built to
stop, arrived at by doing nothing. A threshold move ships with the re-stamp
that makes its days stale: `python -m app.providers.location
--recompute-history`, which rewrites `location_days` from the raw points and
lets the next read rebuild the features through the new gate.

**The inverse of gap-not-zero: a source that can no longer answer at all.**
`_compute_days` rebuilds a day from scratch, so recomputing a day the source has
since dropped would republish it as *having had no data* — the same
confidently-wrong reading, reached from the opposite direction. Only the
calendar has that shape: `get()`/`_sync` rewrite whole spans of `events` from
the API, so a past day can lose its rows while `features` holds the last record
of it. `_RETENTION_GATES` (calendar → `calendar_days`) carries those features
forward instead of deleting them, re-stamped with `now` so the day doesn't read
stale forever. A day the calendar *does* cover with zero events stays a real
gap, or a cancelled event would live in the matrix forever. The brief's `_gather` adds
`features_14d` (`features.matrix(day)` → `{feature: {day: value}}` over the
trailing 14 days) and the SYSTEM prompt gains an optional one-line **Signals**
instruction (surfaced only when a 14-day pattern is striking). The weekly
correlation-vs-checkins pass stays a console/skill job (lazy rule), not backend.

### OAuth and Plaid credentials

OAuth (`providers/oauth.py`): loopback redirect + PKCE, auto-refresh; needs a
**Desktop app** client (Web-app clients fail with redirect_uri_mismatch).
Tokens in app.db keyed (provider, account); accounts: `school` (Workspace —
calendar/gmail, authorized) and `personal` (Google Health `googlehealth.*
.readonly` scopes, pinned in config — Restricted tier, expect consent
warnings). Consent
screen is External/Testing → Google expires refresh tokens after ~7 days;
when that bites, publish the app or go Internal + a second project for the
personal account. CLI: `python -m app.providers.oauth authorize|status|refresh`.
Re-auth is a dashboard affair: `app/routes/oauth.py` (`/api/oauth/start|
callback`) + StatusPanel's connect button drive the same PKCE flow through
the backend's own callback (`http://127.0.0.1:<BACKEND_PORT>/api/oauth/
callback` — Desktop-app clients allow any loopback port/path), reusing the
client creds stored by the first CLI authorize. A refresh that dies with
`invalid_grant` clears the dead refresh token so status flips to
needs-connect (red row + connect button in StatusPanel) instead of erroring
every poll.
Bootstrap (first auth per account) stays on the CLI — it needs the client
JSON. Token values still never leave the backend.

Plaid (`providers/plaid.py`) follows the same posture, minus the CLI-only
bootstrap: OAuth needs a downloaded client JSON, but Plaid credentials are
two strings, so the widget's Set up form POSTs them to `/api/plaid/setup`
(stored in the `tokens` table, one env at a time; the CLI
`python -m app.providers.plaid setup|status|sandbox-item|remove` does the
same). Connecting a bank: the FinanceWidget lazily loads the
Plaid Link script, POSTs `/api/plaid/link_token` (with `item_id` for update
mode when an item hits `ITEM_LOGIN_REQUIRED`), and hands Link's public_token
to `/api/plaid/exchange`. Item access tokens live in `plaid_items` and never
leave the backend; `/api/plaid/status` returns institution names and sync
times only.

### Source status (`/api/status`)

Every provider has three independent ways to be broken, and until §21 each was
only visible somewhere else: auth (a token row), the last refresh (a stderr
line, thrown away) and the cache (a widget looking wrong). `routes/status.py`
joins them into one row per source.

The poller persists each outcome through `providers/heartbeat.py:record()` into
`provider_health` (one row per provider, overwritten every cycle,
derived/droppable): `ok`, the `error` repr, the refresh's own `info` as JSON,
`last_attempt_at`, `last_ok_at` (COALESCEd, so it survives later failures),
`duration_ms`, and `fails` = *consecutive* failures. It's written inside the
poller's worker thread — `_refresh_one` wraps a synchronous `_refresh_sync` —
so a dozen simultaneous sqlite writes can't stall the event loop.

`provider_health` is the *current* cycle only, so on its own it forgets: a
source that broke at 3am and recovered by 9 leaves nothing behind (`fails`
resets on the first success), and a sub-step failure the provider caught and
carried on from never appears at all — `refresh()` returned success, so the row
says ok. `provider_events` (v34, derived/droppable, trimmed to
`EVENT_LOG_KEEP`) is the append-only companion. `record()` writes a
`broke`/`recovered` row on *transitions only* — a source down all night would
otherwise log 288 identical rows, though a changed error text while still down
does earn one, since "429 became invalid_grant" is a different bug. Swallowed
sub-step failures call `heartbeat.log_event(provider, "degraded", …)`
explicitly: finance's two Plaid legs, messages' chat.db schema drift,
email_extract's AeroAPI fetch. `/api/status` attaches the newest few per source
and the panel renders them under the expanded row. Before this the only record
was a `print` into `/tmp/vault-dashboard-app.log` (app bundle) or whatever the
terminal run redirected to — unrotated, and `/tmp` is periodically cleaned.

Four states, and the two extra ones carry the weight:

- **stale** — `heartbeat.FRESHNESS` maps each source to its cache table and the
  gap that makes a human suspicious (3 days for messages: you can go three days
  without texting). This is the only check that catches a refresh
  that *succeeds and writes nothing* — chat.db unreadable, a listener silent —
  which is invisible to every error-based signal. An empty table is stale too.
  Future days (weather forecasts) clamp to 0 days behind, never stale.
- **off** — never configured: Plaid without creds, an `AW_HOSTS` machine that
  isn't up, a Google account never bootstrapped through the CLI. Grey, not red.
  A panel that cries wolf is a panel you stop reading, so this state exists
  purely to keep the red ones meaningful.

Providers may add an optional `health()` probe for what no cache can show, and
it gets the last word: `messages` opens chat.db read-only to prove *this*
process still has Full Disk Access, `location` reports the age of the newest
pushed fix (its refresh only recomputes local rows, so it succeeds forever
whether or not the phone is still sending) — **weighted by where that fix
was**, because Overland reports movement and a phone sitting at home produces
no fixes at all, so silence at home is "still there" and only silence that
ends away from home is stale (`HOME_QUIET_MAX_MIN`, a week, is the ceiling
past which even a stay in is worth a look). A source that cries broken every
time you spend a weekend at home is how a status panel gets ignored. A probe
that raises is caught and reported, never a 500.

Providers also report partial trouble inside their own `info` dict rather than
raising. `unreachable` implies stale (the day synced short); `skipped` is a
note only — news' "already fetched today" is a normal daily no-op, and nothing
can generically tell a benign skip from a damning one.

Frontend: `StatusChip` in the glance strip is the whole indicator — a quiet
green dot when all-clear, the failing source names when not — and opens
`StatusPanel`, worst-first, each row expanding to origin, sync age, newest
cached day, row count and the last error verbatim. Re-auth buttons reuse the
existing oauth/spotify/Plaid-Link flows. It replaced `AccountsBar`, which only
ever spoke for Google and only about auth.

### Faults (`app/failures.py`)

The argument above — a failure that exists only as a stderr line is not
recorded — was made for the poller and acted on only there. Three layers
around it still failed into the backend's stderr log — `/tmp/vault-dashboard-app.log`
today, `/private/tmp/backend.log` when this was written — which `/tmp` cleaning
eventually eats either way:

- **api** — an on-demand read raising. `providers/api.py` maps `ValueError` to
  400 and lets everything else become an unhandled 500, so the *read* path
  (the twenty-odd registry endpoints the desk hits on every load) had none of
  the instrumentation the refresh path has had since v33. `failures.middleware`
  catches both shapes: an exception propagating out of a route, and a response
  that comes back 5xx because a route raised `HTTPException` by hand. It
  re-raises always — Starlette still returns the 500 — so this is a copy, not
  a handler. 4xx is deliberately not recorded: that is the caller being wrong,
  and logging it would bury the real ones within a day.
- **desk** — a widget throwing during render. There was no error boundary
  anywhere in `frontend/src`, so one bad card unmounted the whole tree: 27
  working cards replaced by a blank page, with the only trace in a devtools
  console nobody had open. `ui/ErrorBoundary.jsx` now wraps each drawn card
  and each full view — around the card, not around the app, because the point
  is that the rest survives. (A card's *hook* runs above any boundary, so
  `widgets/cards.js` catches its throws itself.) `lib/report.js` posts the throw to
  `POST /api/failures` and installs the two handlers a boundary structurally
  cannot replace: `window.onerror` for a throw outside the render tree (an
  event handler, a timer, a websocket callback) and `unhandledrejection`,
  which is what every un-awaited `fetchJSON` in a `useEffect` becomes. It
  calls `fetch` directly rather than `fetchJSON` — a report must never travel
  through the helper whose failures it exists to record, or a backend that is
  down turns one error into a rejection that reports, fails, and rejects again.
- **console** — a turn coming back `is_error`. `console.py` streamed that flag
  to the browser and dropped it, so "how often does a turn fail, and on what"
  was answerable only by replaying transcripts by hand — which is how both of
  the "On reach" lessons below were actually found. Grouped on the CLI's own
  `subtype` (`error_max_turns`, `error_during_execution`), which is the part
  worth counting — with two corrections the first four rows it ever collected
  earned, both in `_turn_fault`:

  - Three of the four were `error_during_execution` with a **NULL detail**,
    because `msg.result` is the only thing that was recorded and the CLI
    frequently sends none. A table whose stated job is "on what" answering
    with the subtype and nothing else is the question restated. When there is
    no result text the turn's last moments stand in — the tool it was running
    and what that came back with, both tracked through the stream for exactly
    this.
  - The fourth was a session limit filed under the label **`success`**: a
    quota stop arrives with `is_error` set and `subtype: "success"`, so
    recording the subtype verbatim produced a failure whose name argues
    against its own table. It still belongs in the log — "how often am I cut
    off" is a real question — under the label `limit`. *A field is not a
    name just because it is a string next to one.* The CLI later renamed its
    windows ("5-hour limit", "weekly limit") and they went back under
    `success`, so the match now covers those, a lost login is `auth`, and
    `success` is never a label. Limit rows fingerprint without the reset time:
    one row per window, `count` answering how often.

`failures` (v44) is keyed by **fingerprint** — origin + label + detail, hashed
— rather than appended per occurrence, because the failure it most exists to
catch is a card that throws on every render, and an event stream would be
thousands of identical rows an hour. One row per distinct failure, `first_at`
/`last_at` bracketing it, `count` carrying the volume; that also makes "what
breaks most" an `ORDER BY` instead of a `GROUP BY`. The fingerprint takes the
detail but not the trace (the same bug reached two ways is one thing to fix,
and a stack differing by a line number would split it into two rows that each
look rare) and not the label alone (one card has more than one way to break).

`last_at` carries **microseconds**, unlike every other timestamp in app.db,
because it is not only displayed — the trim orders by it. At second precision
a render loop puts every row in the same second, SQLite breaks the tie by
rowid (i.e. by *first* insert), and the trim then kept the oldest rows and
deleted what had just happened. Derived/droppable like `provider_events`,
trimmed to `FAILURE_KEEP` by recency — a long-standing failure that still
fires today is the one most worth keeping, and it holds the oldest row.

Same never-raise contract as `heartbeat.record()`, on both sides: a log that
can fail the request it instruments is worse than no log, and `recent()`
swallowing its own errors is what lets the status panel still render its
source list when the fault log is the thing that's broken. The panel shows the
section only when it is non-empty — an always-present "0 faults" heading is
one more thing to learn to skim past — and `clear` is how "is this still
happening" gets an answer that isn't reading timestamps.

### Usage (`app/usage.py`)

`salience.js` sorts seventeen cards into three bands every morning and nothing
had ever checked its work. From inside the app a card abandoned in July looks
exactly like one read daily: both render, both sit there, neither says
anything. `usage` (v45) counts four things — `seen` (rendered, with the band
it was rendered in), `open` (you unfolded a card the desk had folded), `sheet`
(the full slide-over), `command` (⌘K) — and `UsageSheet` reads them back.

The obvious version of this measures clicks and is wrong. Most cards here that
work are read at a *glance* and never touched: a line saying "8 to review" has
done its whole job before you decide not to click it, so an interaction count
alone convicts precisely the cards doing their job best. Two things keep the
number honest:

- **Days, not totals.** "Still used?" is a question about habit, and a running
  total can't tell fifty opens last March from five a week since. The window is
  also counted back from the newest *recorded* day, not from today — otherwise
  a fortnight with the machine off shrinks every denominator and the whole desk
  reads as abandoned.
- **The band it was sitting in.** A card the desk folded into `quiet` every day
  for a month and that you never opened is furniture. A card sitting in
  `needs`/`has` that you never opened is being read exactly as designed. This
  is the same shape as "silence is a signal, but only if the source would have
  sent something", one level up: an untouched card is a signal only if the desk
  thought it had something to say. `idle` is the one verdict computed
  automatically and it needs both halves — quiet on every recorded day *and*
  never opened — plus a seven-day floor, so a card added this week can't be
  convicted by its own newness.

The impression is gated on `settled`, which is load-bearing rather than tidy:
a card that hasn't reported yet counts as `HAS` so the desk can render in
order while it waits, so without the gate every card logs a `has` impression
in the first second of every session, `days_active` is never zero, and `idle`
can never fire. It logged 28 impressions for 17 cards before the gate went in.

Buffered client-side and flushed on a timer or `pagehide`: the desk re-renders
on every websocket tick, and a POST per render would measure the refresh loop
and call it attention. `seen` is deduped per (label, band) for the life of the
tab for the same reason — it is an impression, not a counter. ⌘K counts itself
inside `buildCommands`, not at the two call sites in `CommandPalette`, so a
command added later can't forget to be counted and Enter can't drift from
click. Derived/droppable, same never-raise contract as `failures.py`.

### Model usage (`app/token_usage.py`, `widgets/TokenUsageSheet.jsx`)

Tokens and API-equivalent cost per tab, model, mode, project or day, over a
range that starts at the live 5h / weekly window when `claude_quota` knows its
reset. It exists to answer two questions: does the tiering (Fable plans, Opus
decides, Haiku grinds) save anything, and which tabs used up the window.

- **The source is the transcripts**, every `~/.claude/projects/*/*.jsonl` and
  `*/subagents/*.jsonl`. Usage is deduped by `message.id` per file, taking
  the max of each field, because a streamed message logs its cumulative usage
  on every chunk. A subagent's calls count to its parent session.
- **No table.** A full scan reads ~3.6GB in about five seconds, and a
  per-file (size, mtime) cache means later scans only re-read files that
  grew. The numbers are derived by construction.
- **Cost is list price, not a bill.** `PRICES` holds input / output / cache
  read per model, and writes are 1.25x (5m) or 2x (1h) input. Haiku 5.5 bills
  a prompt over 100K tokens at 5x. "Caching saved" is the same calls with
  every prompt token at input price, minus actual. The tiering line reprices
  every call on one model. It ignores tokenizer differences, and says so.
- **Who a session was** comes from its cwd and entrypoint: a project or
  worktree means dev, `night-*` worktrees mean night, `cli` means terminal,
  and sdk calls from `backend/` are Kestrel's one-shots. A vault session
  could be a general or a study tab, and only the console knew which:
  `console_sessions` (written by `console_work.remember_session` on every
  work mark) keeps it. Vault sessions from before that table read
  "general/study". A tab's label is the console's if it recorded one, else
  the session's first prompt.
- **Gemini and Codex** come from the delegates log (`delegate_runs`, via
  `delegates.store.finished_runs`), one row per finished run, each its own
  provider. Claude's fan-out variants are skipped there because their
  transcripts are already scanned. A run counts toward the Claude session
  that sent it, so a tab's row includes its errands. They have no `PRICES`
  rows, so they show in tokens and are unpriced, and the tiering line reprices
  only Claude's calls against Claude's own cost (`claude_cost`). If the log
  can't be read, the page loses those rows and nothing else.

## The desk (`frontend/`)

What the providers above feed. **Look:** one token system (type, spacing,
colour, radius, shadow, motion) at the top of `styles.css`; the research, the
rules and their sources are `docs/ui-overhaul.md`. The accent (blue since
round 2 of the look lab, 11-14) means what amber used to: a fill is context
flowing to the agent, an edge is "this wants you"; nothing else wears it.
New CSS uses the tokens, never a raw size, radius, colour or shadow.

Three decisions, and only the first is about
data: what a card is *for* (rank), how the page is laid out, and when a card
goes and fetches again.

### Desk rank (`lib/salience.js`)

The sidebar used to render every widget, always, in a fixed order at equal
weight — a day with nothing in it looked exactly as busy as a day with
everything in it. The desk now applies the brief's "sections are earned" rule
(`brief.py`'s SYSTEM): each widget reports what it currently holds, and App
ranks and collapses on that.

Frontend-only, deliberately: salience is derived from the payload each widget
has *already fetched*, so what you see on a card is what decided its rank —
there's no second copy of the rule server-side to drift out of agreement with
the card. No new endpoint, no new table.

- **Levels** — `NEEDS` is narrow: a console turn blocked on you, a clock (a
  leave-by inside 30 minutes, a task overdue or due today), a broken source,
  or a person or decision waiting (mail to review, a reply owed, a debt to
  chase, an office-hours change to confirm, a wire to match). `HAS` is
  anything that merely exists today: events ahead, undated open tasks,
  unticked habits, open list items, notes to file. `QUIET` is nothing today.
  Habits and lists were `NEEDS` from 2026-09-13 to 2026-09-25; the plan view
  moved them back ("today, unless it's like a task that needs to be done that
  day"), and snooze is what makes a `NEEDS` card livable when one is.
  When Messages can't be read, the People row and that fault are one row
  (Open, Fix access), sorted as a broken source. A widget passes a `null`
  level while its fetch is out; silence is read as `HAS`, never as quiet, so a
  slow card is never buried before its data lands.
- **Ranking** — the plan view shows `NEEDS` as rows in the "needs you" list
  and the other two as one line per card under `today` and `quiet` (folded by
  default), each band in `DESK` order (`widgets/cards.js`). The brief is exempt
  and always leads:
  it summarises everything below it.
- **Never NEEDS** — some cards are informational by construction and say so in
  code: `LocationWidget` imports only `HAS`/`QUIET`. Where you were yesterday
  is not a thing to act on today, and a card that claims the top band for a
  number you can't do anything about is how the fixed-order desk got noisy in
  the first place. `CoursesWidget` was in this list until §22 (09-13) gave it
  something that *is* owed — a proposed office-hours change waiting on your
  approval — which is the shape of the exception: a card earns NEEDS by
  gaining an action, not by gaining a number.
- **Summaries are live, not empty-state** — a card's one line reads the same
  data the card would (`4,676 steps · 7.1 h`), never "nothing today". A line
  holding real numbers that announced an empty state is worse than no line.
- **Card hooks own the data, widgets are views** (2026-09-25) — each
  `*Widget.jsx` exports `useXCard()`: the fetch, the refresh subscription,
  card-level mutations, and `{ level, summary }` (level `null` while loading).
  `widgets/cards.js`'s `useCards()` runs all 17 once in `PlanApp`, which lists
  from what they return and hands each widget its `card` prop when it's drawn
  — so a card's data and salience exist without drawing it, and opening a card
  never refetches. Pure UI state (an input, a show-all toggle, a picked chip)
  stays in the view. A hook makes every React hook call before deriving
  anything, so `useCards` can try/catch each one: a payload the derivation
  can't read turns that one card into an error instead of blanking the app,
  which the per-card `ErrorBoundary` can't cover for hooks. `useLastWord`
  keeps each card's last non-null `{ level, summary }` so a reload doesn't
  hop bands, and `Console` is memoised (`ConsolePane`) because every fetch
  re-renders the app.
- **Asks: the plan view's "needs you" rows** (`lib/asks.js`) — a hook also
  returns `asks`, one row per thing you could act on: an overdue task, the
  review queue, an office-hours proposal, a debt to chase. Each has a stable
  `id` (the snooze key), a `tier` (blocking a turn → clock → broken source →
  owed) and actions that either run in place or open a sheet or the card.
  `useCards` keeps the list honest: a card not at `NEEDS` has no rows whatever
  its hook built, and a `NEEDS` card that built none, or is broken, gets one
  generic row that opens it. So the list is exactly the cards that need you.
  `plan/PlanApp.jsx` orders them by tier then desk order.
- **A card with an error is `NEEDS`** — `useCards` overrides the level and
  folds the line through `salience.errorSummary`, so a dead credential reads
  "google/personal needs reconnecting" instead of a bare label that reads as a
  broken widget.

### Card salience (`lib/notable.js`)

Desk rank answers "which cards matter today". It does not answer "which number
on this card matters today", and until this pass nothing did: a ranked card was
still a fixed inventory — four health stats, six screen metrics, a four-cell
location grid — laid out identically whether or not the day had anything in it.
Ranking the cards while every card still printed everything moved the reading
work around rather than removing it.

So the same rule now runs one level down. Each measurement card declares its
readings, `score()` judges each one against that metric's own recent history,
and `lead()` returns the single most unusual. `Lede` draws it big; everything
else compresses to one muted line; on an ordinary day only that line shows and
the card reports `QUIET` and folds. A card earns its headline the same way a
brief section earns its place.

- **Median and MAD, not mean and standard deviation** — four weeks of a
  person's behaviour contains outliers by construction (one 21k-step Saturday,
  one night at 1.5h), and both statistics that outliers move are exactly the
  ones a test for outliers must not use. The median and the median absolute
  deviation don't move, so the 21k day is measured against the ordinary days
  rather than against a mean it dragged upward itself.
- **Three gates, and the practical one goes first** — `floor` is per-metric and
  declared by the widget (3,000 steps, 1 hour of sleep, 3 bpm, $150), because
  statistical significance is not the same claim as "worth your morning": a
  +2 bpm move can be six sigma on a steady baseline and still mean nothing.
  Then `Z_FLOOR` (2.0), then a **rarity** gate — the share of past days that
  went at least as far *in the same direction*, suppressed above 10%.
- **Why the rarity gate exists** — calibration was a replay of 28 real days,
  not a guess. At `Z_FLOOR` 1.5 the scorer fired on 46% of them, four of those
  on separate ~21k-step days: MAD measures the dense mode of a bimodal
  distribution, so a recurring habit reads as a repeated anomaly. Rarity took
  it to 39%; raising the floor to 2.0 took it to 25%. The floor is 2.0 rather
  than 1.7 because the steps firings all sat at z 1.6–1.7 while every genuine
  event (a 1.5h night, RHR at the month's high, a 233-minute zone day) sat at
  z 2.7+ — a clean gap, so the cut went in the gap.
- **A flat baseline is notable, not a divide-by-zero** — when MAD is 0 every
  past day read the same, and `z` is `Infinity` on purpose. The floor gate has
  already run, so what reaches it is a metric that has never varied and just
  did, which is the most notable thing a metric can do. (Doomscroll minutes
  are 0 on every observed day; the 45-minute floor is what keeps a 9-minute
  day from leading the card.)
- **Eight days or it says nothing** — `MIN_BASELINE` is a refusal, not a
  fallback. Screen's most extreme day in the window (466 active minutes) does
  not lead, because at that point aw-server had four observed days behind it
  and four days cannot support the word "unusual". A metric spends its first
  fortnight quiet, and that is the correct output, not a gap to paper over.
- **A missing day is a missing point, never a zero** — health, screen and
  location all null their thin and unsynced days rather than zero-filling them,
  or a week the sensor was off would become the baseline that makes every
  ordinary day remarkable. Spotify is the one exception, and only because a day
  with no plays there is a real zero rather than an unobserved one.
- **One window for the whole desk** — `DESK_BASELINE_DAYS` (28) lives in
  `app/config.py` and every card reads it. "Unusual" has to mean one thing
  across the desk or the word stops carrying information; 28 days also holds
  four of each weekday, so a body with a weekday/weekend shape doesn't have
  that shape mistaken for the signal the way a 7-day window would.
- **Frontend-only, for the reason `lib/salience.js` is** — the backend gained
  series (`/api/health/summary`, and a `series` block on the screen, location
  and spotify summaries), not verdicts. The judgement stays next to the card
  that renders it, so there is no second copy of the rule to drift.
- **The card is quiet; the pin is not** — nothing was deleted. The numbers that
  no longer print are still in the sheet and still on the end of the card's
  pin, which is the surface that wants all of them. The console reads data; you
  read conclusions.

### Folding a queue (`ui/Folded.jsx`)

The lede form does not port to the cards that hold things owed — tasks, email,
office hours, parcels. You *work* those: a row you can't see is a row you can't
tick, so compressing them to one reading would remove function rather than
noise. What they had instead was a subtler version of the same fault. They
spent equal height on the part of the queue that had already happened or hadn't
come near yet: a nine-meeting day whose next meeting sat in the middle of eight
ended ones, office hours that ran out at 11 rendered like hours you can still
walk into, a flight in December drawn like a flight tomorrow.

So the rows fold instead of collapsing. `Folded` draws one muted line saying how
many, and one click brings them back — nothing is dropped, and the count is
always visible, so the fold is never a claim that the rows don't exist.

- **The split is "can you act on it now", not "is it recent"** — calendar and
  courses split on the backend's own `state`/end time; travel splits on the
  same predicates that already drove its attention dot (`landingToday`,
  `departingSoon`, `offNominal`), so the fold and the dot can't disagree.
- **`above` for a boundary, plain for a footer** — earlier events and spent
  office hours belong *before* what's live, so the line renders above its own
  rows and keeps the day chronological when expanded. Travel's later trips and
  in-transit parcels are a footer, and render after. This is the whole of what
  the flag means; getting it backwards puts 4pm above 8:30am.
- **Folded rows keep their affordances** — the pin, the mark-delivered and drop
  buttons, the drag payload. Folding is a height decision, not a permissions
  one.
- **The fold summary is counts, not the nearest item** — "2 in transit · 3
  trips booked" rather than naming one and hiding the rest, because a parcel
  and a flight aren't comparable enough for one of them to stand for both.

### Layout: the plan view (`plan/PlanApp.jsx`)

One window that never scrolls; only the conversation, a long list and a sheet
body do. Across the top, the glance strip and the brief. Below it three
columns: the console (ink) with the prompt under it, the map, and a 340px paper
column holding the "needs you" list, then `today` as one line per card and
`quiet · N` folded. **Rest shows what bears on you, and reach shows the rest**
(2026-11-04, after "way too cluttered"): a control you set and leave folds
(the console bar's ⋯, a sheet's custom dates, the Trends metrics), a tool you
reach for appears on hover (sync, pin, the brief's links), and help for a mode
waits until you're in it. A fold may never hide a warning: a broken source's
ring and name stay up, and the start line's mode sentence and `⚠` still say
their piece before the first message. **Nothing is ever drawn over the console or the map** (2026-11-03,
replacing a sheet over the map and a slide-over): they're grid siblings in
`.plan-stage`, and the side column takes width rather than floating. It
replaced the rail desk (a ranked column of foldable cards beside the console,
chosen over a grid by A/B in August) on 2026-09-25; git history holds that,
and the reasoning for what folded where is in its commits.

A card has three depths: its row or line (does it need you), its detail in
the side column (the widget itself, over the same hook result; the column
widens to a 520px reading width for it), and its full view (the sheet).

**Card anatomy** (2026-09-27). The widgets were drawn for the rail, eighteen
to a 340px column, and kept that shape when the pane became the only place
they render: a bordered box at the top of an otherwise empty half-window,
lists cut at three or six rows with "+N more", an expand icon and a
header-click in the card *and* a "Full view" button over it, and folds that
could hide everything the card held. The contract now, for every card:

- **The pane is the frame.** In `.side-body.detail` a `.widget` draws no box.
  Its header is the pane's title (17px, sentence case), and its controls
  (refresh, pin, chips) stay visible. The pane head holds the rest: `← Needs
  you`, the band pill and, only for a card with a sheet (`PANEL_FOR`), `⤢ Full
  view`. That button is the single way to the third depth. Cards don't
  carry their own expand icon.
- **The card is whole.** No cut for space: every row is drawn and the pane
  scrolls. A cut that *means* something stays, such as Tasks' "N more in all
  tasks" (open tasks not on today's board). A card without a sheet (debts,
  expenses, lists, generated, watchlist, office hours, deliveries) has
  nothing further to show, which is why it has no Full view.
- **Folds hide the secondary part, never the whole.** A `Folded` whose rows
  are all the card has starts open (`open`): an evening with only ended
  events, or three trips booked with nothing landing today.
- **Every state is drawn.** Loading is skeletons, empty is a `.widget-empty`
  line that says what would appear and how to add it, and an error is
  `WidgetError`. A card that renders nothing reads as broken.
- **Width is a reading measure, not a share of the window.** Wide material
  (charts, grids, history) belongs in the sheet, which gets the whole window.
- **The card stacks over the list; it doesn't replace it.** Most cards are
  three to eight lines, so a card that took the column's full height left
  most of it empty. The card panel now hugs its content up to 62% of the
  column and scrolls past that. The "needs you" list keeps the rest (180px
  floor), with the open card's row marked, so moving to the next card is a
  click. Close is ✕ or Esc, not "← Needs you". Sources opens the same way.

**Full views are tabs**, the dock carried over: every sheet you open stays in
a strip with its body mounted and its data warm, newest last, and takes the
stage and the side column. `openFull` is the one call behind a row's action,
the pane's Full view and ⌘K, so re-opening one shows it where it sits
instead of adding a copy. Esc (`lib/escape.js`, which also keeps the old
dock's fullscreen-exit race) unwinds the newest tab, then closes an open card
or Sources; `← Back` closes every tab. The console stays mounted under
a full view with only its prompt showing: it holds a live session, and the
prompt is the one thing that's always on screen. Sheets were drawn for a
~500px column, so inside a tab they keep that measure, centred.

**As the window narrows.** Two independent steps, so the stage's shape follows
the room it actually has — opening a card narrows it the same way shrinking
the window does:

- *The stage* is a `@container` on `.plan-work`: 800px or wider, console and
  map side by side; under that, map above console.
- *The side column* is a `@media` at 1200px (`NARROW` in `PlanApp.jsx`): below
  it the window is the console and the map alone, and an "N need you" chip on
  the strip opens the column **in the map's place**, the console staying where
  it is. Crossing into narrow closes it, and × there only closes it: it isn't
  remembered as "put away", or the column would be gone on widening. On a wide
  window × puts the column away (per browser), and the chip or a node brings
  it back beside the stage.

**Notes are full views too.** The Vault node and ⌘K's "Vault notes" open a
search over titles, paths and text, then Obsidian's own bookmarks, then what
the console wrote (`06-Generated/`). A note opens as a tab keyed
`note:<path>`: title, path, edited, the opening prose rendered by the
console's markdown renderer, and its links both ways as chips that open
further tabs. Editing stays in Obsidian — "Open in Obsidian" goes through
`openNote()` → `/api/vault/open`, never a plain `obsidian://` link (Chrome
prompts on every click and the app ignores it). A row's note pill opens the
tab; ⌘K lists matching notes as a Notes group from the second letter typed.
⌘K also carries "Use the light / dark theme" (`lib/theme.js`): it sets
`data-theme` on `<html>`, remembered per browser, and choosing what the system
would pick anyway clears the override.

### The "needs you" list (`plan/NeedsList.jsx`)

Each row says what, since when, and what to do, and has one way out: act,
snooze ("later"), or open. A dot, the title, a reason line ending in an age,
an optional note pill, the actions; "later" is invisible until the row is
hovered, focused or under the keyboard cursor, and always shown on touch.

- **Snooze** (`/api/snoozes`) — options fitted to the ask, at most four: a
  leave-by gets 5 or 10 minutes (`ask.clock`), an ask with a deadline gets the
  morning before it (`ask.deadline`), anything else the end of your next event
  today, tonight (before 8 PM) or tomorrow; the presets resolve server-side
  against the day key. A snoozed row leaves the list, its node goes dashed and
  blue, and "snoozed · N" at the bottom brings any back. A console card
  waiting on you can't be snoozed.
- **Undo** (`lib/undo.js`, `plan/UndoToast.jsx`) — every one-click write
  offers its inverse for 8s: a draining bar, paused while the toast is
  hovered or focused and while the tab is hidden; ⌘Z while it shows (never in
  a field). Hooks offer it themselves, so the list and the card behave the
  same: a task reopens (and so do the subtasks its close swept up — they carry
  the parent's exact `completed_at`), a habit check-in flips back, a batch goes
  unpaid, an office-hours confirm leaves the course note again
  (`POST /api/courses/proposals/{id}/undo`), a snooze wakes, an email comes
  back to the inbox (`POST /api/email/{id}/undo`, which already existed). An
  unsubscribe can't be recalled, so it isn't sent until the toast goes; undo
  just brings the card back. The review sheet keeps its own stack and button
  for walking back a run; ⌘Z is the toast's.
- **Cleared today** — an undoable action that finishes an ask carries its
  `askId`, which marks it cleared (`/api/usage/cleared`); undo takes it back
  out. Snoozing never counts.
- **Draft nudge** replaces the debt "ask" button on a row: the prompt and the
  debt as a pill go to the composer, and nothing is sent or marked.
- **The settle rule** — after an action the list holds its order, the
  finished row in place marked done (or "until …"), until the pointer leaves
  or 6s pass.
- **Keyboard** — j/k move, ↵ does the first action, s then 1–4 snoozes, o
  opens the card, Esc backs out; only on the list, never while typing, and a
  waiting approval card keeps ↵ and Esc.
- **Note pills** — `asks.noteFor`: a course code in the title names that
  course's note (click opens it, drag pins it). Titles only: the review
  queue's reason line quotes its oldest email, which is not what the queue is
  about.
- **All clear** — a check, "Nothing needs you", today's cleared count, the
  next thing with a clock, and how many are snoozed.

### The console sheet (`plan/StatusBar.jsx`, `plan/Timeline.jsx`)

**Claude's view** (`plan/ViewPane.jsx`) takes the map's grid area, with the map
mounted behind it, because this is an editor's preview pane and a thumbnail was
too small to be that. It holds the last 8 images tools gave the model this turn
(`result_image`, on the `tool_result` frame; raster types, capped; cleared when
the next turn starts, never stored or replayed), and in a dev tab a Preview: a
frame on the project's dev server, found by `console_profiles.dev_servers`
matching each listener's cwd to the project. Localhost only. It refuses this
app's own origin, since a frame there would share `localStorage` and reconnect
this window's sessions. A new image brings the pane up mid-turn unless it was
closed during that turn. The bar's thumbnail, or its `view` button when there's
no image yet, toggles it.

**Claude in Chrome** is on in general and dev tabs (`CONSOLE_CHROME`). It drives
the user's real Chrome with their logins from sessions that read untrusted mail,
so `console_policy.browser_needs_card` decides in a PreToolUse hook, not in
`can_use_tool`, which never-ask drops. Looking passes, and acting cards in every
mode, with no "don't ask again". New tools default to carding. The `--chrome`
flag working under the SDK is measured (11-04), not documented.

**Computer use** is a job a tab hands off (`use_computer`, `console_terminal.py`).
The CLI's `computer-use` server only exists interactively: 0 tools in an SDK
session even with it enabled, 24 on a pty (measured 11-04). So the tool runs a real
`claude` on a pty in `~/.kestrel/computer`, with its own `/mcp` enable, and returns
the answer to the conversation. That folder is outside this repo on purpose: the
CLI's project is the enclosing git root, so in here the enable lands on the repo
and the job loads its CLAUDE.md. A card asks first in every mode, through the same
PreToolUse gate as Chrome. One job at a time, since the CLI holds a single lock.
Stop and a closing tab end it. It takes the screen: the CLI has only full-screen
tools, and it hides other apps while it works.

The job's transcript is all that's read. Never scrape its screen: a misread prompt
is a wrong click on the Mac. Three facts about that transcript, all found the hard
way. The CLI logs a call only once it's answered, so an "allow this app?" prompt
looks like silence: the live terminal comes up in Claude's view (xterm.js, attached
to the running job; that socket can't start one) whenever the transcript goes quiet,
and the user answers there. A final message is logged a block at a time with
`end_turn` on each, so a thinking block says end_turn seconds before the answer
exists: a turn ends on `system`/`turn_duration` or an `end_turn` holding text. And
an inherited `CLAUDE_CODE_CHILD_SESSION` turns transcript saving off in the child.
Both console sockets check Origin, since CORS doesn't cover WebSockets.

**Composer**, as the CLI's. One box that grows with what's in it to 45% of the
window, then scrolls. `!` runs a shell command at the tab's cwd with no card
in any mode, because you typed it, then Claude sees the output and answers it.
`ShellBlock` keeps its own scroller on the newest line until you scroll up in it,
and on resume `sessions._shell_turn` reads `shell_prompt`'s wrapper back into the
command and output, so that wrapper's format is a contract between the two.
Ctrl+B moves it to the background, as does outliving 2 minutes, since a held turn
locks the tab: it runs on in Tasks, Stop there kills its process group, and Claude
hears how it ended with your next message. `@` completes a path, `?` on an empty
prompt is the shortcuts, `\` then Enter is a new line. Enter mid-turn steers or
queues, per the ⋯ menu's "Enter mid-turn" (one setting, default steer), and
⌥Enter does the other (#92). *Steer* hands the message to the CLI now, read at
the next tool boundary; *queue* holds it in app.db (`app/console_queue.py`,
keyed by tab id) for a turn of its own when this one ends. Held ones are a
numbered list over the composer: click to edit, ↑↓ to reorder, "steer now" (or
"send now" when idle) to hand one in, ↶ back to the prompt, × to drop; every
verb is a socket frame answered with the whole list (`held`, sticky on the
Link, so a reloaded page or a reloaded backend gets it back). The drain is
read_session's `hold_next`: after a turn that ended clean (not stopped, not an
error or usage cut, nothing still steering) it puts `held_next` on the Link's
inbox, so the oldest goes through the same path as a typed message, and the
page hears `replay_user` with `held: true`. After a reload the list waits for
a turn to end or "send now"; images in a held message's pills don't survive
one (they live in memory). Up takes back the newest held one, then the last
steered; Ctrl+Enter stops the turn and sends now; Stop returns both lists to
the prompt, because a Stop means the plan changed. Up and
Ctrl+R are prompt history, per folder in localStorage. `/btw` is a one-turn,
tool-free model given what's on screen, so it works mid-turn and the session never
hears of it. Ctrl+S stashes, Shift+Tab cycles modes, Esc Esc clears (Up recovers
it) or, from empty, rewinds the conversation after a confirm. Not added: `:emoji:`.
`#` and `&` aren't documented prefixes.

**`@` a tab or a past session** (#94). The `@` popup lists other tabs (off
the stored tab list, so the memoised panes aren't re-rendered for it), then
files, then past sessions from `/api/sessions/search` (chat_search's search,
this tab's project, from 2 letters, minus any session already shown as a
tab). Picking a tab or session drops the mention and adds a `session` pill;
⌘-dragging a row out of the sessions panel does the same through `dragPin`.
Every way in goes through `usePills.pinContext`, which sees `{ sessionRef }`
and fetches `/api/sessions/{id}/digest`. The digest is extractive
(`app/session_digest.py`): the asks in order (first four and newest ten past
fourteen), files Edit/Write touched, the last reply, 6k characters at most.
A model summary would be a claim about the session and a charge per attach.
`build_prompt` gives it its own `<past_session>` block, not a `<widget>`,
because it quotes a conversation that may itself have quoted mail.

Prompt suggestions are wired and silent. `--prompt-suggestions` is accepted, but the
CLI emitted none here on Haiku or Sonnet, even with `promptSuggestionEnabled`
(11-04), so it's gated upstream. The SDK's parser drops that message type, so
`_with_suggestions` reads the SDK's private `_query` ahead of it. Don't replace it
with our own model call: that's a charge on every turn for a guess.

The view pane also holds **Files** (read and edit under the tab's folder; a save
refuses if the file changed since it opened, and generated `tasks.md` is read only),
**Plan** and **Tasks**. Plan mode writes its plan with `Write` under
`~/.claude/plans/` and may never reach ExitPlanMode, so that's where the pane reads
it. **Auto-verify** is off by default and per project, since it costs usage: it adds
an `<auto_verify>` note to each message, the Preview's localhost URL checked by
regex because the page sent it. Navigating to localhost needs no Chrome card, but
closing a tab still does. A tab that finishes while you aren't looking posts through
`notify`, whose `at_desk()` decides the rest.

`Console.jsx` is reused whole beside the map, in one of three states — `read`
(sharing the stage, where it opens), `peek` (folded to the status bar, the map
has the room), `full` (it takes the window). The prompt is not in the sheet:
`Console` renders its composer through a portal (`composerTarget`) into
`.prompt-bar`, so it's on screen in all three and over a full view; React
events bubble through the portal, so a drop on the prompt is still the
console's drop. ⌘J or a click on the bar goes between peek and read;
double-click or ⤢ for full, Esc back (unless a card is waiting). A reply doesn't
raise a folded console: a turn that ends while it's at peek leaves an unread mark — the bar's dot pings blue ("replied · new reply ·
⌘J to read") and a blue badge pings on Kestrel's hub — cleared when the sheet
leaves peek or the next turn starts.

- **Its toolbar is one row.** Model, mode, context and the session verbs stay;
  what you set and leave (effort, style, MCP, CLI update, session id, `bar-opt`)
  folds behind ⋯ under 760px of console, the width it always has beside the map.
  The connection dot only shows once the socket is gone: the status bar already
  says what the console is doing.
- **Status bar** — state (idle / working / waiting on you / writing /
  error / replied), the call in flight or the reply streaming, elapsed, tokens in · out,
  and one usage chip rotating between the 5-hour and 7-day windows every 4s
  (`UsageChips rotate`; still while the tab is hidden, both on hover, a window
  past its reset void as before). On a permission the bar turns to the accent and
  carries Yes / No; the same ask heads the "needs you" list as a `blocking`
  row, and its line on the map goes dashed. A question card can only be
  answered in the conversation, so it raises the sheet instead.
- **The turn outside the console** — `lib/turn.js` is a module-level bus:
  Console emits (call, done, waiting, answered, text, end with tokens, rate
  limits) and registers the one action others may take (answer a
  permission). A bus rather than props because `ConsolePane` is memoised and
  a turn changes state many times a second.
- **Timeline** — every call of the turn on one time axis, the time a call
  spent waiting on you striped; the idle bar's "N failed" opens it filtered to
  the failures.
- **Drops** — the prompt, the sheet and Kestrel on the map all take a pill;
  every drop lands in the prompt's pills.
- **Tabs** (`lib/tabs.js`, `plan/ConsoleTabs.jsx`) — up to six consoles,
  each its own socket and session (`console_session:<tab id>`; the old
  single `console_session` became the first tab's on first load). Every tab
  stays mounted, hidden by `.tab-hidden` rather than unmounted, because
  unmounting closes the socket and a background turn is the point. `active`
  gives one tab what the window has one of: the composer portal, the
  speakers (a background tab's `speech` is dropped), push-to-talk, ⌘K's
  focus. `lib/turn.js` events carry `tab`; `useTurnTrace(active)` keeps a
  trace per tab and returns the showing one, so the map, bar and timeline are
  that tab's, while the strip and the "needs you" list read `trace.tabs` —
  a background tab's card is a list row (`· tab N`) whose Yes/No answer into
  that tab (`answerConsole(id, allow, tab)`). Unread is per tab: a turn
  ending out of sight (tab hidden, or sheet at peek) marks it; seeing the
  conversation clears it. A tab mid-turn won't close (closing would cut the
  turn); closing drops only the tab's key — the session stays in resume.
  ⌥T / ⌥W / ⌥1–9, since Chrome keeps ⌘T/⌘W/⌘1. **A session id is stored
  only once a turn has finished in it**: the CLI announces the id at init
  but writes no transcript until a turn lands, so resuming an unused one
  (a reload, or a model/mode switch — `reconnectKeeping`) exits 1 with "No
  conversation found" and the socket dies on the next send.
- **Arranging tabs** (`lib/tabRest.js`, `plan/ConsoleTabs.jsx`; #91, #102)
  — right-click a tab for its menu: pin (first in the bar, no ×), move
  left/right, Later, settle, regenerate title. Top-level tabs also drag to
  reorder, and a drop never crosses the pin line. **Later** snoozes it with
  the desk's choices (an hour, tonight, tomorrow 8am, next Monday; 4am
  rollover); **settle** puts it on a Settled list. Both unmount the tab like
  a done child (socket closed, session in resume, worktree kept), so both
  are refused mid-turn, and both leave the strip for one dashed ☾ N button
  at its end that lists them and brings one back (going to it resumes it).
  A minute `useTicker` wakes due snoozes and settles tabs by themselves:
  one whose worktree merged (the `worktree_merge` tool or the bar's merge
  sets `merged`; typing clears it), once you've read its last reply and
  are looking elsewhere, or one with no send or look for 7 days. Never a
  pinned tab, the one on screen, a child (it has `done`), or one with a
  brief still to send. Tabs from before `lastActive` count from first load,
  or they'd all settle at once. **Regenerate title** is the one model call
  here: `POST /api/sessions/{id}/title`, one Haiku turn over the extractive
  digest (`app/session_digest.py`), stored as the tab's `name`, which the
  first message no longer overrides. The strip's menus portal to
  `document.body`: an ancestor of the bar is a containing block for
  `position: fixed`, which put them off to the side and under the rooms.
- **⌘Enter** (#93) sends the prompt, and its pills, as the first message of
  a new tab in the same mode, project and model, opened behind; the composer
  clears. It's the user's tab, not a child: typed, engaged, no parent. The
  brief rides `brief` + `briefPills`, so it goes once the socket's up and
  survives a reload before then.
- **Typing at an approval card declines it** (#99), Claude Code's "no, and
  tell Claude what to do differently". On the focused card any letter that
  isn't a shortcut opens the reason box holding it; from the composer, with
  the card shown in the log, Enter (the button reads "Decline & redirect")
  sends the text as the denial's reason instead of queueing it behind a call
  nobody approved. The reason reaches the model verbatim as the call's
  result, so the same turn changes course.
- **Modes** (`app/console_profiles.py`, `console/ModePicker.jsx`) — a tab is
  general, study or dev, picked when it opens (⌥T → G / S / D → project),
  because cwd, prompt and tools are fixed at session start: a guess from
  the first message would run a whole session in the wrong directory. Each
  mode keeps its own permission mode and model (`console_mode[:<profile>]`,
  general inherits the old keys) and starts at `DEFAULT_MODE` — dev asks.
  *general* is the console as it was; *study* adds `STUDY_APPEND` (tutor,
  don't solve); *dev* is a different `build_options`: cwd at a repo under
  `DEV_PROJECT_ROOTS` (validated by `resolve_project`, the one gate between
  a string and a cwd), the stock prompt + `DEV_APPEND` (no vault rules, no
  Brain), `DEV_BUILTIN_TOOLS` (Agent, the Task* list, Skill…), setting sources
  incl. `local`, Claude Code's own auto-memory left **on** (the Brain
  doesn't cover repos), and of the vault server only `open_tab` and
  `project_info` (other projects, listed or briefed: CLAUDE.md + its
  `@imports`, auto-memory index, recent commits). Other projects are
  readable, not writable: `can_use_tool` passes Read/Grep/Glob under any
  project or its memory dir (`is_project_path`) without a card, and nothing
  is added as a working directory, so an edit there still asks. The picker's
  **N** makes a new project (`create_project`: an empty `git init`ed dir under
  a root, `POST /api/console/projects`) and opens the tab on it. **M** moves
  the selected project into a folder (`file_project`,
  `POST /api/console/projects/folder`, one level, blank for none): a row in
  `project_folders`, nothing on disk, because hardcoded paths and
  `~/.claude/projects` history key on the path. `list_projects` deliberately
  doesn't see folders, so `resolve_project`, `open_tab` and `project_info`
  find a project by name wherever it's filed; only the picker
  (`with_folders`) and `project_info`'s output show them. Its
  sessions are the repo's (`sessions.project_dir(cwd)`), so the resume list
  is the one `claude --resume` shows there. Bash keeps `is_safe_read` and
  auto's guard list, which already stops `rm`, `git push/reset/clean`, `gh`.
- **Handoff** (`console_tools.open_tab`) — the model, not a separate
  classifier, notices a request belongs in another mode, or in a dev tab on
  another project (`HANDOFF_APPEND`), and calls `open_tab(mode, project, brief)`. The tool reaches the page
  through `SessionState.open_tab` → `{"type":"open_tab"}` → the window
  answers (`PlanApp.handOff`: ≤10 tabs, ≤5 *other* tabs mid-turn — one
  5-hour window) → `tab_opened`, which becomes the tool's result. The tab
  opens *behind* yours and sends the brief as its first message, without
  the shared prompt's pills, once (`briefSent` clears it). One level on its
  own: a spawned tab has the tool, but `SessionState.open_tab` stays empty
  until a message the page marks `typed` arrives (`unlock_handoff`), so a
  brief can't chain, and any longer chain has you behind each link. A client
  that doesn't mark stays locked, and a reload locks it again. A spawned dev tab
  starts in ask until you pick a mode for that tab: the page sends no mode
  for it until then, and `start_mode` must honour a pick or the tab could
  never leave ask (every switch reconnects through it). The tab remembers
  its `parent`; when the brief's turn ends (that turn only — later ones are
  yours), its last reply plus the diff's file list goes back
  (`turn.reportConsole`): a card in the parent's log, an unread mark if
  you're elsewhere, and a `<handoff_report>` on the parent's next message
  (`build_prompt`) so its model hears it too. Held in memory, not stored —
  a reload before the next send drops it from the model's view.
- **Child tabs** (`lib/children.js`, `plan/ConsoleTabs.jsx`, 11-18) — a tab
  open_tab made is its parent's, not a peer. **In the bar:** the parent's
  chip, then a badge (a dot per child, most urgent first, and the most
  urgent count, "1 needs you" before "4 running"); the badge drops a menu
  open under the parent, one row per child with its state (running / needs
  you + why / queued #n / done), done ones last. Inline children were built
  first and dropped: the strip gets ~170px in the plan view. The child on
  screen is drawn beside its parent. ⌥1–9 count in this order (`barOrder`).
  **Queue:** past 5 other tabs mid-turn, `admit` queues the brief instead of
  refusing (`tab_opened` carries `queued: n`, the tool says "queued (#n) …
  don't open it again"); a queued tab is unmounted, and `nextQueued`, run in
  PlanApp's render, starts the oldest when a slot frees. A tab about to send
  its brief holds its slot. Clicking a queued one starts it now. 10 mounted
  tabs is still a refusal. **Done:** when a child's report fires and the user
  hasn't typed in it, `landing` reads its worktree status: no commits beyond
  base and nothing uncommitted (it merged itself, per its repo's CLAUDE.md,
  which SPAWNED_APPEND tells it to follow, never deploying) and no `Needs
  you:` line in its report → `finish`: done, unmounted (its socket close
  removes the empty tree), resumable by a click, 12 kept. Otherwise `needs`
  holds why, and the parent's report says so. A child stopping on a question
  tells its parent too. Closing a parent takes its done children; live ones
  move up a level. **The parent wakes** (Console `wake`): a report reaching
  an idle parent starts a turn nobody typed, carrying the held reports
  (`typed: false`, so it unlocks nothing), as a finished background task
  wakes Claude Code; one that lands mid-turn goes when the turn ends. Six in
  a row without the user is the most, so a parent and child can't bat work
  back and forth unwatched. **Follow-ups** (`message_tab`, 11-18): the parent
  names a child by the id open_tab returned (also in each report's `from`);
  `followUp` lets it only at its own children, and the text becomes the
  child's next brief, so it goes when the child is idle, reopens a done one
  (queued past the cap), and reports back like the first.
- **Dev model** — `DEV_MODEL` (default `opus`, the CLI's alias for the
  newest Opus) is what "default" means in a dev tab; the vault modes keep
  `DEFAULT_MODEL` (sonnet). The backend names it in the `mode` frame, so
  the picker reads `default · opus` rather than guessing.
- **A worktree per dev tab** (`app/console_worktree.py`, 11-17) — on
  2026-08-12 two dev tabs in one checkout each ran `git stash` and wiped
  each other's edits. A new dev tab on a repo that can branch (a commit, on
  a branch) gets `<repo>/.claude/worktrees/tab-<hex>` on branch
  `kestrel/tab-<hex>`, cut from the branch the main checkout is on (its
  base, kept as `branch.<b>.kestrelbase` in git config). The tree is in
  `.git/info/exclude`. Its setup comes from `.kestrel/worktree.json` (after
  Air's `.air/worktree.json`): `link` symlinks gitignored paths from the
  main checkout (here the venv, `.env`, `backend/data` — one app.db, not a
  copy — node_modules, settings.local.json), each also excluded, because a
  symlink to a directory is a file to git and `.venv/` wouldn't match it;
  `setup` runs commands. The servers keep running the main checkout: a
  tab's edits go live only when merged. **Merging** (`worktree_merge` tool
  in the tab, or the bar's **merge → main**) refuses uncommitted work,
  rebases the branch onto the base *in the tab's tree*, then `merge
  --ff-only` in the main checkout (optionally pushes). **Conflicts never
  reach the main checkout:** the rebase is aborted and the files go back to
  the tab — as the tool's error, or from the button as a status line plus a
  drafted "resolve these" message — since the tab knows its own change; a
  judgment call goes to you. A refused fast-forward (uncommitted edits in the
  main checkout on the same files) is reported the same way. **Lifecycle:**
  when the socket closes, a tree holding nothing (clean, no commits beyond
  base) is removed with its branch, links unlinked first. **Orphans**
  (`branches` / `sweep` / `orphans`, 11-18): every `kestrel/tab-*` branch and
  every `worktree-*` one a `claude --worktree` session (the night jobs) left,
  unless a socket here holds its tree or a live pid holds its git lock. On
  startup (`main.lifespan`), on every tab's close and when a tree is made, the
  fully merged and clean ones go, tree and branch; the rest are the plan
  view's **Left behind** panel (a row in the needs list → `OrphansPanel`):
  Review opens the review pane on the tree with no tab behind it, Merge is
  the same rebase-and-fast-forward (`merge_orphan`; a treeless branch only
  fast-forwards), Discard asks inline, then forces. Before 11-18 each new
  tab's start line carried them as a ⚠, in whatever unrelated tab opened
  next on that repo. A tab's tree on another branch (a client site's `try/*`)
  is status'd and merged as that branch. **Resume:** Claude
  Code files a transcript under its cwd, so `for_session` finds the
  session's tree by its transcript directory and rebuilds it at the same
  path if it was removed; auto-memory stays the main checkout's (Claude Code
  shares it across worktrees, checked 11-17). The resume list reads the
  worktree directories too (`sessions._session_dirs`). The diff pane, Files
  and `@` take `worktree=`, which must be one of the project's registered tab
  trees (`console_worktree.resolve`). Sessions from before stay in the main
  checkout. `KESTREL_DEV_WORKTREES=0` turns it off.
- **Per-turn diff** (`app/console_diff.py`, `console/DiffView.jsx`) — read
  off git, not rebuilt from Edit inputs, because Bash writes too. A user
  turn snapshots the tree with `git stash create` (a dangling commit; files,
  index and stash list untouched) plus untracked files' contents; the
  result diffs against it, so work uncommitted *before* the turn isn't
  charged to it. The card lists files; the pane (also the bar's **diff**,
  everything uncommitted vs HEAD) shows hunks with line numbers. Hunk lines
  are taken by position after the first `@@`, never by prefix: deleting a
  `-- comment` line *is* the line `--- comment`. Capped per file and per
  turn, marked cut rather than dropped.
- **Reviewing a tab's branch** (`console_worktree.review`, `lib/review.js`,
  `lib/syntax.js`, 11-18) — in a worktree tab the bar's **review** opens the
  pane's third view, **branch**: what `merge` would land, i.e. the commits in
  `base..HEAD` and their diff from the merge-base (committed only; an
  uncommitted file gets a band, since the merge refuses it, and a button
  asking the tab to commit). The strip under the header has **Merge**, the
  same `mergeWorktree` the bar's button and the `worktree_merge` tool run
  (the pane asks it not to draft the composer, and shows the answer), with
  a remembered per-project **push** box, off by default. **Conflicts are
  shown before the merge, not discovered by it:** once the base has moved,
  `git merge-tree --write-tree` merges the two tips in the object store (no
  tree, index or ref moves, so it is safe mid-turn) and each conflicted
  file is marked in the list, named in a red band, and drawn above its diff
  as git would leave it: base lines, tab lines, labelled markers (a diff3
  `conflictStyle` adds the ancestor). It's a preview: the merge rebases
  commit by commit and can stop where the tip merge doesn't, so its own
  refusal also lands as a band, worded for you rather than in the tool's
  words to the tab. **Line comments** work in all three views: the gutter's
  + on a line, shift-click a second for a range; each is kept (per tab
  folder, in localStorage, until sent) with the lines it quotes, and finds
  its row again by side, number and text, so one whose line moved is shown
  as outdated rather than misplaced. **Send to the tab** makes them one
  message — `path:line`, the quoted lines, the comment, an optional overall
  note — sent as the tab's next message (mid-turn it goes in at the next
  step, like typing; no pills ride along), and closes the pane so you
  watch it land. A turn ending re-reads the open pane. **Syntax colour**
  shares the shell/run_script `sh-*` palette (lifted toward the ink text
  for the add/remove tints): a tokenizer per language family that carries
  state across lines (docstrings, block comments, templates, JSX tags and
  the prose between them), fed the whole file at both revisions
  (`GET /api/console/blob`) so a hunk that opens mid-docstring still knows
  it; a row whose text no longer matches the file falls back to its hunk's
  own lines. Checked by joining tokens back over every file in the repo
  (identical, and every JSX file ends balanced).
- **Subagents** — their messages share the stream, tagged
  `parent_tool_use_id`. Their calls show as `↳Name` (so a run tallies
  `Agent ↳Read×12`); their prose and partial deltas are dropped, since that
  is the subagent talking to the parent. Their writes reach the change
  ledger and the turn's diff like the parent's (checked live 10-28: a
  general-purpose subagent's Write + Edit both listed).

### The map (`plan/PlanMap.jsx`)

The map is every card at a fixed spot in one of six areas (Comms, Plans, Body,
Media, Money, Machine), Kestrel at the centre; clicking Kestrel raises the
console if it's folded. Spots are on a 1000 × 660 plan spread to the pane's
shape, not letterboxed: the pane is tall beside the console and short above it,
and a fitted picture came out at half size with unreadable labels. So the SVG
is drawn in real pixels, nodes shrink at most to 0.8, and any that would land on
each other's labels are pushed apart (`spread`). Under 340px tall the per-node status line
goes; hover still has it. The map *shows*: where a problem lives,
and what a console turn touched. The list stays the place to act, so a node
opens its card (double-click: its full view) rather than doing anything.

- **Node** — size and colour are the band (needs 17px in the accent with a badge
  counting its asks; today 14px ink; quiet a bare 4px grey dot, named on
  hover or in the key). A summary line only on
  accent nodes, first clause only; the whole summary shows on hover or focus,
  drawn on a top layer so no neighbour covers it. Hovering a node lights its
  list row and the other way round.
- **Ring** — 24 segments: the card's provider's last 24 refreshes,
  `provider_health.recent` (`heartbeat.record` appends one character per
  cycle: `o` ok · `x` failed · `.` skipped). A source is broken only when the
  last two failed (`heartbeat.broken`): one bad cycle in a row of good ones is
  weather. Drawn only for a broken source, the node under the pointer, or the
  key: 24 healthy segments on nine nodes was the map's loudest texture and
  said nothing. Cards with no provider (tasks, lists…) have no ring.
- **Lines** — hidden at rest except the last turn's (faint). The console
  reports each call over the socket (`tool_use` carries an `id`, and a
  `tool_result` follows it with `is_error`); `lib/turn.js` maps a call to its
  node — custom tools by name, the provider tool and provider CLI by source,
  other Bash to Shell, file tools to Vault — and a call lights its line, dashed
  while a permission card waits, red if it failed. The key shows every line,
  weighted by how often the console has gone there (counted per browser).
- **Dots** — one per console tab mid-turn, circling Kestrel while it thinks
  and flying out along a line to circle the node of its newest call. A
  subagent is a smaller dot of its own (`parent` on the socket's `tool_use`
  says whose call a call is): it circles its tab's dot, or goes to the node of
  its own newest call, and the tab's dot stays put meanwhile. All of the
  movement is one frame loop (`plan/dotMotion.js`) writing `transform`
  directly, never through React and only while a dot exists; a CSS flight and
  an SMIL orbit couldn't see each other, so a dot jumped whenever either
  changed. A dot sits at least 900ms where it lands and waits 280ms with
  nothing to do before heading home, because calls come in bursts and following
  them literally reverses it mid-flight and it arrives nowhere. Waiting on you
  or under the pointer, it stops circling. A finished dot flies home and folds
  into Kestrel before it's removed. Under reduced motion they sit still, spread
  round whatever they're at. Lines light in 120ms and fade in 450ms, so a burst
  doesn't flicker them.
- **Machine nodes** (Vault, app.db, Shell, Web) fade to 35% until a call uses
  them.
- **Drag** — a node drags its card as a pill onto the console or Kestrel.
  SVG can't take part in HTML5 drag-and-drop, so it's pointer events, a
  floating pill and a hit test on release.
- **Touch** — the first tap shows a node's label, the second opens it, and a
  tap on open map puts the label away.
- **The ground** (`plan/MapTerrain.jsx`, 2026-11-04) is the state, not a texture:
  a height field with a hill per card, as tall as it needs you, so a heavy day is
  a range and a clear one goes flat. Peaks warm to the accent on the same rule as the
  rest of the app: the accent means this wants you (red for a broken source). A call
  sends a ripple from its node and a running turn pulses from Kestrel, so the map's
  motion means something is happening; the only other movement answers the
  pointer. It's a canvas under the SVG, redrawn at 24fps while moving and 6fps at
  rest, not at all when hidden, and once per change under reduced motion.

### Sheets are written to the subject, not the provider

The generic expansion (`ProviderExpand`, since removed once every source had a
sheet and nothing could reach it) rendered any registry source: a chip
row of `METRICS`, a range picker, and a renderer picked by the shape of the
rows. That was the right *fallback* and the wrong *destination* — it shows a
provider's API surface, so `finance` arrived as seven chips (`networth`,
`bills_due`, `recurring`, …) that were really halves of one question, and
"how is my money doing" cost four clicks plus memory of which chip you last
pressed.

`MoneySheet` is the pattern that replaces it: the chips become **bands**, all
on screen at once, ordered the way the question is actually asked — what I
have (*Worth*), what's moving (*Flow*), what's owed (*Due soon*), where it went
(*Where it went*), then the receipts (*Transactions*). Six reads in parallel,
one range spine shared with every other sheet, and each band drag-pinnable on
its own so a section can go to the console without the whole sheet. Two rules
carried over from it: the headline number lives in the band header (the body is
then free to be a chart, a list, or receipts), and any client-side arithmetic
that mirrors backend logic — the category breakdown re-derives
`SPEND_EXCLUDED_CATEGORIES` from transaction rows — is placed directly under
the total it must agree with, so a drift shows up as bars that don't add up
rather than as a quiet wrong number.

Seven sheets now follow it — money, calendar, health, music, screen time,
location, email — and what they share is a kit, not a layout: `lib/sheet.js`
(`useSheetData`: one `Promise.all` over the range, one sequence guard, one
"all of them failed" rule), `ui/Band.jsx`, `ui/SheetHead.jsx`, and the chart
primitives in `ui/charts.jsx` (`Stat`, `Bars`, `RankBars`, `SeriesRow`,
`SpanBars`). The primitives exist because the same shape kept recurring across
subjects that have nothing else in common: doomscroll inside screen time and
sleep stages inside a night are both a stack whose segments sum to the bar;
top artists, top senders and spending categories are all a sorted comparison;
resting HR and app switches both live in a narrow band where a zero baseline
would draw five identical columns. Everything a primitive draws drag-pins the
row behind it, so a bar is a thing you can hand to the console rather than a
picture of one.

Three decisions the sheets are worth reading for:

- **Say what the number is measured from.** `first_active` / `first_leave`
  are minutes since the 4am rollover, not since midnight, so `SpanBars` takes
  an `originHour` and labels its axis 4a→4a. A midnight axis would have put
  every morning four hours early.
- **Never draw a zero nobody observed.** `finite()` drops null days before any
  arithmetic, and location says outright that a day with no fixes is
  unmeasured rather than empty,
  because a phone that stays put sends nothing.
- **Don't answer a question twice.** The health sheet computes no wellness
  score (`/api/trends` already derives `energy` from the same cache), and the
  calendar sheet never calls `/api/leave` (billable Google Routes; the card
  already pays for it). A second, differently derived answer to a question the
  app already answers is worse than no answer.

### When cards reload (`lib/refresh.js`)

Widgets each own their fetch; `lib/refresh.js` owns the *when* — on mount, on a
sync broadcast, on day rollover, and on **return to the window** after being
away `AWAY_MS` (5 min). The last one is the shape of refresh-on-hover that
survived review: one fire on return rather than one per card the cursor
crosses on its way to the console. The away gate matters because a refresh
isn't free — `/api/health`, `/api/calendar` and `/api/email` run provider
syncs on read, `/api/leave` makes billable Google Routes calls, `/api/status`
opens chat.db to prove Full Disk Access — so flicking to the editor and back
must cost nothing.

That cost is also why `broadcastRefresh` takes an optional **topic**. Closing
the Tasks or Habits sheet has to put its card straight — you just ticked things
off in it — but a bare broadcast would charge a round of provider syncs for a
change to one table, so those sheets broadcast `"tasks"` / `"habits"` and only
`useLoad(load, topic)` subscribers with that name reload. A widget without a
topic still reloads on a plain `broadcastRefresh()`; the topic only ever adds a
way to be called.

**Kestrel's own writes use the same topics.** `console_changes._CARD_TOPICS`
says which cards each change scope moves, and `_stream_messages` sends a
`refresh` frame once the call's result comes back clean, so a denied or failed
call reloads nothing. `refreshSoon` coalesces them (250ms), so parallel calls
read each card once. The "needs you" list has no feed of its own: card hooks
build their asks, so reloading the card moves the list, and the brief line
follows the asks. A second `refresh` goes out for the whole turn just before its
result, because some changes land after the call does: the connector hooks'
re-pulls, and `/api/generated`, which reads `index.db` behind the note watcher.
SQL and shell calls could have touched anything, so they name every card topic
(all local reads, never a bare broadcast). In a dev tab only `db` and calls to
the dashboard's own API count. Full sheets that load themselves subscribe with
`useTopic`; a `useSheetData` sheet's topic is its source name. A new scope needs
a row in `_CARD_TOPICS` (empty if no card shows it), and
`test_console_changes.py` fails without one, or if nothing in `frontend/src`
listens to a topic. Computer-use pty jobs aren't classified, so they send none.

Away-ness is tracked from window `blur`/`focus` as well as
`visibilitychange`: switching *apps* (the normal case for a localhost
dashboard sitting behind an editor) leaves the tab's `visibilityState`
"visible", so `visibilitychange` alone would never fire. The rollover check
runs first and reports whether it already broadcast, so a return that also
crossed 4am reloads once rather than twice.

### Themes Kestrel makes (`app/looks.py`, `app/webshot.py`, `lib/look.js`)

"Make it warmer" is a console request (New Features #105), and the lesson it
was built on came from another tool: an agent that set named colour roles got
it right every time, an agent that guessed CSS got it wrong and said "fixed"
anyway. So two rules, both held by code rather than by the prompt.

**Colour roles only.** `theme_set` takes `{role: "#RRGGBB"}` per scheme, dark
first. There are 21 roles (`looks.ROLES`), each one colour token from the
`:root` block of styles.css; nothing else about the page is reachable. No
selector, property or free CSS is accepted anywhere on the path: the tool's
schema allows only role names and hex strings, `looks.problems` checks again,
and `lib/look.js` re-checks each value and token name before writing the
rule. Hex and not `oklch()` for the old reason: the map's canvas parses
tokens with `hexToRgb`. A role the theme leaves out keeps the baseline
(`looks.BASELINE`, a copy of styles.css that `test_looks.py` holds equal).
Every set is contrast-checked on the resolved palette against the pairs the
baseline was built to (`looks.CHECKS`: 7:1 body text, 4.5:1 secondary text,
accent and status, 3:1 incidental text, boundaries and data marks). A failing
set is refused and nothing is stored; the refusal names the pair, both hexes,
the ratio and the bar, so the next call can fix exactly that.

**The answer is a picture.** The tool's result carries a WebKit screenshot
of the theme, so the model has seen it before it can say anything. There is
no separate "take a screenshot" step to skip. `webshot.py` renders a
loopback URL in an off-screen `WKWebView` (the .app's engine) in a subprocess
of its own, because AppKit wants a main thread. If the picture can't be
taken the theme is still applied, and the result says in so many words that
it has not been seen and that the reply must say so.

What it shoots is the **specimen** (`/#specimen/<scheme>/<id>`,
`ui/ThemeSpecimen.jsx`): every role in use, drawn from the real stylesheet,
with each swatch's hex read back off the rendered page. Not the desk: a desk
load opens a console socket (a CLI per load) and syncs providers on read,
and its picture would carry the day's mail. The cost is that the specimen
can't show a component that ignores the tokens (a hard-coded colour on the
map, say). Two WebKit facts found on the way: a view outside a window
snapshots blank, and a window that is never shown runs no
`requestAnimationFrame`, so the page's ready flag can't wait on a frame.

A theme is a look like the others: `data-look="theme-<slug>"`, a chip in the
Look lab's Colour row, votes in `look_votes` under its id. Unlike them it is
data (`look_themes`), and which one is on is the server's (`active`), not a
per-browser key, so the one Kestrel just set is the one the window shows.
The page learns of a change from the `looks` refresh topic and keeps the
last answer in localStorage to paint from before the fetch lands. Picking a
built-in colour takes the theme off. `theme_set` needs no approval card
(colour only, checked, undone from the lab) but is recorded as a change, so
the turn's summary names it.

## Unattended writes (`app/activity.py`, `app/selftexts.py`)

Anything the backend writes without a click goes through
`activity.record(source, kind, summary, ref, origin)` once the write lands.
The "While you were away" card (`ActivityWidget.jsx`) reads it back and puts
an Undo on every row. The card is NEEDS while any row is newer than the last
"looks fine", HAS for a day after that, and QUIET otherwise. The rule exists
because a bill task, a flight check-in task and a self-text task each used
to appear with nothing saying where they came from. `test_activity.py` fails
on any file that calls `db.create_task(` outside the user-initiated sites
(widgets, people, console_tools) without also calling `activity.record(`.
Logged today: self-texts, `finance` bill tasks, `travel` check-in tasks.
Not logged yet: package/trip upserts and scheduled email sends (a sent mail
has no undo; it would be a row without the button).

Undo reverses only what the row made, and refuses rather than guesses when
the thing has moved on since: a task you finished is yours now (409, "you've
already finished that task"), and a 06-Generated note you've marked reviewed
stays. Something already deleted by hand counts as undone. Undo goes
task → dropped, event → Calendar delete, food → delete the log rows (the
custom food stays, harmless), watch/list_item → delete the row, note →
delete the file.

**Self-texts apply first; you review after.** That's the user's call
(2026-10-26): a proposal queue was the other design, and "I just want to see
changes in case I want to undo" chose this one. Texts are grouped into a
burst while they arrive within `BURST_GAP` (10 min) of each other, and a
burst is routed once `QUIET` (3 min) has passed since its last text. So a
photo and its caption are one thing, and so are "Buy batteries" and "and
tape". The poller's last step (`selftexts`) does this every cycle.
Photos and PDFs go through `attachments.extract` (vision for images), and
their text reaches the router fenced as untrusted, since a flyer is someone
else's words. Haiku (`SELFTEXT_MODEL`) answers with actions from a closed
set: task, event (own calendar, never attendees), food (dish names run
through `food_estimate.estimate_dishes` → a custom food → `food.log`,
source `selftext`), watch, list_item, note (06-Generated, `generated_by:
selftext`), or keep. An earlier burst within 30 min is passed in as
context, so a follow-up lands in the same place. An action that fails to
apply becomes a `keep` row saying why. A burst whose routing fails three
times becomes a `keep` row too, so nothing disappears silently. A usage
limit leaves the burst pending with no attempt counted. A burst is marked
done *before* its actions are written, because a crash halfway through must
not write the first half twice. "Redo as…" undoes one row and routes the
same texts again with the kind forced.

Only texts newer than `sync_state.selftext_since` (set on the first run) are
routed. Everything before that was already captured as a task by the old
rule.

## Route map

| Route | Purpose |
|---|---|
| `GET/POST /api/tasks`, `GET /api/tasks/today`, `PATCH /api/tasks/{id}`, `POST /api/tasks/{id}/status` | tasks CRUD (regenerates tasks.md); `/today` returns triage groups (overdue/due_today/available/waiting_stale + counts) plus the `today` day key it triaged against. A due date is two columns: `due` is the day key it always was, `due_time` the optional `'HH:MM'` on it (`due` may be POSTed as `YYYY-MM-DDTHH:MM` and is split server-side). Read-time only, never stored: `due_at`, the instant the task comes due, via `db.day_instant()` — the inverse of `day_key()`, so 01:00 resolves to the calendar day *after* its key. A timed task moves from `due_today` to `overdue` once its hour passes; an untimed one waits for the day itself to roll. The frontend counts down off `due_at` inside 24h (`ui/TaskDue.jsx`) |
| `GET/POST /api/habits`, `POST /api/habits/{id}/checkin` | habits + idempotent checkins (value 0 = un-check, kept as a tombstone). `habit_checkins.source` records who wrote the row: 'ui'/'console' vs 'auto'. Signal-driven `db.auto_checkin()` fills only untouched days and never overwrites a manual check/un-check. `health.refresh()` runs two such legs off a shared `_auto_habit` helper: `active_zone_minutes` ≥ `EXERCISE_AZM_THRESHOLD` → Exercise, and `sleep_hours` ≥ `SLEEP_HOURS_THRESHOLD` (default 9, keyed on the wake-up day) → Sleep |
| `GET /api/sessions` | Claude Code sessions for this vault (shared with terminal /resume) |
| `GET /api/sessions/search?q=` | the same rows, narrowed to sessions whose transcript mentions `q`, each carrying the lines that matched (user text, replies, tool calls by name and argument). Same function behind the console's `chat_search` tool |
| `GET /api/courses?term=` | §22 office hours: every `type: course` note for the term (default = the term today falls in), each with its office hours placed in the week, plus `today`, `open_now`, `next` (with `in_min`) and `unscheduled` (courses whose hours aren't posted yet). Dated exceptions are applied: a today row carries the effective `start`/`end`, its `exception`, and `moved_from` (the weekday it normally falls on, when that isn't today); a cancelled slot stays on the card with `state: "cancelled"` but never reaches `open_now`, and `next` skips it. Reads index.db only — no writes, no table; the console's course tools write the vault note instead. `study` (study mode): per course, an exam ≤14 days out read off the syllabus and the count of review-log items; a parse failure empties it rather than failing the hours |
| `GET /api/generated`, `POST /api/generated/promote` | unreviewed agent notes; mark reviewed / adopt as curated / move |
| `POST /api/vault/open` | open a note in Obsidian. `target` is whatever the console wrote — a wikilink (alias, `#heading` and `^block` all understood), a vault-relative path, or an absolute one — and the backend resolves it against the vault and index.db before shelling out to `open -g obsidian://open?vault=…&file=…`. A bare name resolves only when it names **one** note: this vault holds four `README.md`, and opening whichever the index listed first is worse than saying it's ambiguous. Anything that doesn't land on a real file inside the vault is a 404, which is ordinary rather than an error — the frontend links optimistically (see below) and renders the miss on the link. macOS-only, like the rest of the desktop shell |
| `GET /api/vault/search?q=`, `GET /api/vault/note?path=`, `GET /api/vault/pinned`, `GET /api/vault/recent?folder=` | notes for the plan view's ⌘K group and Vault notes view, all read off index.db. `recent` lists a folder (a literal prefix) newest-edited first; the view's "Written by the console" is `06-Generated`. Search needs every word of `q` somewhere in title, path or body text and ranks title hits over path over body, then newest edit; under two letters it returns nothing. `note` gives title, folder, edited, excerpt, frontmatter, `links_to` / `linked_from` and the `unresolved` link names. The indexer resolves links by the same rules `resolve_note` uses when a link is clicked: exact vault path, then a *unique* bare name, and a markdown `(x.md)` link also relative to its note. A name matching several notes stays unresolved, so the list and the click agree. `[[x\|alias]]` inside a table escapes its pipe, and the backslash isn't part of the name. `pinned` is Obsidian's own `.obsidian/bookmarks.json`, flattened, notes only; the file is absent until something has been bookmarked, which is the ordinary empty case |
| `GET /api/snoozes`, `POST /api/snoozes`, `POST /api/snoozes/wake` | an ask pushed out of the "needs you" list, keyed by the card hook's `ask_id`. POST takes exactly one of `preset` (`tonight` = 20:00, `tomorrow` = 08:00 on the next day key), `day` (08:00 on that day key: a deadline's eve) or `until` (explicit local time). All three resolve server-side against `db.day_key()`, and one already in the past is a 400. Expired rows are pruned on read. `wake` takes the ask id in a body because ids carry colons and slashes |
| `GET /api/usage/cleared`, `POST /api/usage/cleared` | today's "N cleared" — asks finished by acting. Stored in `usage` as kind `cleared`, but as a set per day rather than a tally: clearing twice counts once, and `undo: true` takes it back out. Kept out of the card summary, and the generic usage POST can't write the kind |
| `GET /api/providers`, `GET /api/{source}?range=&metric=` | provider registry + data slices (13 registered sources: `calendar`, `health`, `email`, `finance`, `weather`, `news`, `reddit`, `spotify`, `lastfm`, `messages`, `location`, `attachments`, `features`) |
| `GET /api/commands` | slash commands/skills from ~/.claude + vault/.claude frontmatter (name, description) |
| `GET /api/email/queue`, `POST /api/email/queue/refresh` | review queue cards (message + summary + unsubscribe capability); force sync + summarize |
| `GET /api/email/labels`, `POST /api/email/{id}/action` | bins = Gmail labels; actions: archive · delete (trash) · bin · unsubscribe · keep |
| `POST /api/calendar/events`, `PATCH`/`DELETE /api/calendar/events/{id}`, `POST /api/calendar/events/{id}/respond` | calendar writes on the backend's own Google token: create · move/edit · delete · RSVP. Events are addressable by title match against the cache instead of a retyped id; each write re-pulls the days it touched. Backs the console's `event_create`/`event_update`/`event_delete`/`event_rsvp` |
| `POST /api/email/draft` | write a reply into Gmail as a draft (`reply_to` = the message being answered; threaded via Message-ID, not just `threadId`). Creates only — no send path, so a wrong draft can never reach anyone. Backs the console's `email_draft`, on the backend's own token rather than the claude.ai connector |
| `GET /api/email/scheduled`, `POST /api/email/scheduled`, `POST /api/email/scheduled/{id}/cancel` | scheduled send (§5 tail): a Gmail draft plus a `send_at`; `drafts.send` fires at that time. Backs `email_schedule` (guarded — cards even in auto mode) |
| `GET /api/packages`, `GET /api/trips?hours=` | deliveries (§15) + travel (§16), extracted from email inside `email.refresh()`'s cadence; read-only. Feed the brief's Travel section and `TravelWidget`. `/api/packages` carries the backend `day_key` so the card can ask "arriving today?" without redoing the 4am rollover; `hours` defaults to a year for the card and is clamped to two, while the brief keeps `upcoming_trips()`'s 48h |
| `GET /api/location/summary` | the `LocationWidget` slice (§10): newest day with real coverage, its distance/places/time-at-home/entropy, the day's out-and-back clock times, and a 14-day `km_traveled` sparkline. Same shape and same reasons as `/api/screen/summary`, with two differences that come from Overland being best-effort where aw-server is always-on. **A coverage floor** (`COVERAGE_FLOOR_MIN`, `is_thin`, mirroring screen's pair): a day with one fix computes 0km/0 places, which renders identically to never leaving the house, so a thin day is a gap in the trend and never the day the card centers on. This counted *fixes* until 08-30, which measured the wrong axis — a 121-fix burst inside one drive passed while a sparser but better-spread day was refused; coverage subsumes the old count, since with gaps capped at `MAX_GAP_MIN` the floor needs at least 9 fixes anyway. `_covered_minutes` is the single definition, shown on the card by `_day_shape` and gated on by `coverage_minutes`, so the card and the matrix cannot quote different coverage. **Observed flags**: `first_leave` is the first fix that was *away*, a real departure only if a fix at home came before it, and `last_return` is the last fix seen at home, a homecoming only if the day's last fix is that one — `leave_observed`/`return_observed` carry which, and the card words the times accordingly ("Out at" vs "First seen out"). Every reader backfills its own window, safe here because computing a location day is local arithmetic over points the listener already wrote. **Silence is a signal, not a failure**: no fixes since arriving home means still home, so `home_stretch()` carries `home_since`/`home_for_min` and the card leads with "Still home · 40h"; `fix_stale` fires only when the silence ends *away* from home |
| `GET /api/location/trace?range=` | the map's slice (§24): every fix in the range grouped by day, plus the places they dwell into and the range's bounds. Hand-written because it is neither a metric series nor a summary — one range, two shapes. The places are `location`'s own (same grid cell, same `PLACE_MIN_DWELL_SEC`) so the map and the `places` metric can't disagree; the *path* is thinned to `MAP_MAX_POINTS` for drawing while dwell is always computed over every fix |
| `GET /api/tiles/{z}/{x}/{y}.png` | basemap PNGs for the map sheet (`app/tiles.py`, `tile_cache`). Fetched once from osm.org and kept forever — the frontend can't reach an image host (CDN allowlist) and shouldn't re-ask a tile server. **204, not 404**, when a tile is off the grid or upstream is down: the map degrades to the vector-only rendering rather than filling the console with red |
| `GET /api/health/night/{day}`, `GET /api/health/day/{day}` | the day drill-down (§23): one night's stage segments (keyed on the wake-up date, like every sleep metric) and one day's hourly steps + cached hourly HR. Both fill from the API on demand and cache, which is `get()`'s bargain rather than `summary()`'s — these are a click on a specific bar, not a card rendering itself. `asleep_min` and `in_bed_min` are summed from the segments, so the picture and the numbers can't disagree |
| `GET /api/leave` | time-to-leave (§14): today's remaining located timed events, each with a traffic-aware `leave_by` (`app/leave.py` — not a provider; live join, no cache table). Origin = §10's latest fix if < 30 min old else HOME; `leave_by = start − Google Routes v2 drive − LEAVE_BUFFER_MIN`. Google Geocoding resolves the location string (falls back to §6's Open-Meteo geocode); both live behind a 5-min in-memory TTL cache. Missing key or any API failure degrades to the event list with `leave_by` null. Feeds the brief's Calendar lines and a GlanceStrip chip that shows only when the next departure is < 90 min out |
| `GET /api/people`, `GET /api/people/search?q=`, `GET /api/people/{person_key}`, `GET /api/people/waiting`, `GET /api/people/signals`, `PATCH /api/people/{person_key}` | CRM-lite (§17): merged view, name lookup, full per-person history (emails/messages/meetings), stale outbound threads, birthday/watch-due signals, inline edit. `GET /api/people` takes `?day_one=true` and `?crew=<name>`, applied after the cluster merge so a person tagged on one identity still matches. Sidebar widget + slide-over dashboard (`PeopleWidget.jsx`/`PeopleExpand.jsx`) |
| `POST /api/people/waiting/remind`, `POST /api/people/waiting/draft` | §9 tail: turn a waiting thread into a task due today (deduped per message, not per person), or get candidate replies drafted from the thread's last 20 turns. Both suggest only — draft never sends |
| `GET /api/brief`, `POST /api/brief/regenerate` | daily brief: lazy per-day generation (row in `daily_brief` is cache + lock + poll target; status generating→ready/error). Kept whole for the /daily-brief skill and the plan view's "Morning brief" full view |
| `POST /api/brief/line` | the plan view's brief bar: `{asks}` in (the "needs you" rows, ages stripped client-side so they don't make every hour a new list), one line out — only how the asks bear on each other plus tomorrow's first timed event, never a restated row, `""` when there's nothing to add. Cached in memory per day and per set of asks, so the model runs only when the list changes; a failure is returned (`error`) and not cached |
| `GET /api/trends?range=&metric=`, `GET /api/trends/metrics` | internal series no provider covers: `energy` (0-100 readiness derived from sleep/RHR/HRV in the health cache, baselined on your trailing 30 days) + `habit:<name>` checkins; health series stay on /api/health. The `TrendsExpand` slide-over has three modes: `series` (sparklines), `patterns` (correlations), and `sleep` (a stacked bar of the already-synced deep/REM/light/awake minutes per night — UI-only, no endpoint) |
| `GET /api/status` | source status panel (§21): one row per provider joining auth + last refresh (`provider_health`) + cache freshness + recent `provider_events`, plus `infra` rows (vault path, app.db). States ok/stale/broken/off, worst first. `StatusChip` → `StatusPanel` |
| `POST /api/oauth/start`, `GET /api/oauth/callback`, `GET /api/oauth/addresses` | start Connect (returns consent URL); Google's loopback redirect target; the Google address per surface for `authuser=` links. Surfaced by `StatusPanel`'s connect buttons, which poll `/api/status` for the result |
| `GET /api/plaid/status`, `POST /api/plaid/setup`, `POST /api/plaid/link_token`, `POST /api/plaid/exchange`, `POST /api/plaid/sync`, `DELETE /api/plaid/items/{id}` | Plaid credentials + bank connect/reconnect lifecycle (Plaid Link); finance DATA stays on `/api/finance` |
| `GET /api/oauth/spotify/status`, `POST /api/oauth/spotify/start`, `GET /api/oauth/spotify` | Spotify PKCE connect: masked status; start (returns consent URL); the fixed, exactly-registered redirect target that does the code exchange — listening DATA stays on `/api/spotify` |
| `GET/POST /api/debts`, `POST /api/debts/{id}/settled`, `DELETE /api/debts/{id}` | personal IOUs. GET returns open debts plus `owed_to_me`/`i_owe`/`net`. `person` is free text with an optional `person_key` — the CRM only knows people who have messaged or emailed, and most of these owe from a shared trip, not a thread |
| `GET/POST /api/expenses`, `PATCH/DELETE /api/expenses/{id}`, `GET /api/expenses/marks?range=`, `POST /api/expenses/export`, `POST /api/expenses/review`, `POST /api/expenses/batches/{id}/paid`, `DELETE /api/expenses/batches/{id}`, `POST /api/expenses/batches/{id}/reveal` | expenses claimed back from Dad (`app/expenses.py`). Export writes the .xlsx into `EXPENSES_DIR` itself because the desktop webview can't download. It freezes the rows into a batch and marks every posted charge reviewed. DELETE on a batch unsends it (unpaid only). The GET carries each unpaid batch's suggested `wire`, which is never auto-applied |
| `GET /api/lists`, `POST /api/lists`, `PATCH/DELETE /api/lists/{id}` | the user's lists — shopping, packing, whatever they keep. The GET nests each list's items and its `open` count in one payload, so the card's chips switch without a fetch; `?include_done=true` to see ticked-off rows. Names are unique case-insensitively; DELETE takes the items with it |
| `POST /api/lists/{id}/items`, `POST /api/list-items/{id}/done`, `DELETE /api/list-items/{id}` | the items on one list |
| `GET/POST /api/watchlist`, `POST /api/watchlist/{id}/watched`, `DELETE /api/watchlist/{id}` | movies/shows queue with streaming availability; `?include_watched=true` |
| `POST /api/reindex`, `GET /api/healthz` | index maintenance (healthz: the health provider owns /api/health). `/api/reindex` returns `indexed_notes` (rows actually written) and `skipped` (files whose frontmatter wouldn't parse — they are invisible to the dashboard, so they're named rather than silently counted as indexed) |
| `WS /ws/console` | start {session_id?, model?, voice?} → user msgs → deltas/tool events/results; interrupt. With `voice`, also `{"type":"speech", text, wav}` per sentence |
| `GET /api/cite/{ref}` | what a console sentence was read off (Citations, below). One lookup in `cite_refs`, returning the live row behind the ref — an email's metadata and snippet, an event, a message. Three answers, all meaningful: the row; `found: false` when the ref is real but its row has left the provider cache; 404 when the ref was never handed out, which means the console invented it. Bodies are not here — an email's body stays on `/api/email/{id}/body`, fetched live and stored nowhere |
| `GET /api/voice/status`, `POST /api/voice/speak`, `WS /ws/voice` | what the machine can do (stt/wake/tts, loaded or not); one-off synthesis (WAV, or 204 when nothing in the text speaks); the mic socket — binary 16kHz frames up, `wake`/`listening`/`barge_in`/`transcript` down |

## Console

- Start protocol: client sends `{type:"start", session_id, model}`; server
  builds `ClaudeSDKClient` with `resume=` and `model=`. Session id is announced
  back and stored client-side; page refresh resumes automatically.
- Sessions live in Claude Code's own store → terminal `/resume` and the web
  list are the same history, both directions.
- Status bar: connection dot, session id, usage-window chips (§ "usage
  windows"), voice toggles, model picker (default/sonnet/opus/haiku — switching
  reconnects and resumes the same conversation on the new model), permission
  mode, resume, new. Each turn closes with its own footer line: duration and
  the tokens it read (cache reads included) and wrote.
- **The log's unit is the turn, not the tool call.** A run of consecutive tool
  calls collapses to one line — how many, and which tools (`Read×4 Grep Bash`)
  — that expands on click into every call and its full input. While the run is
  the tail of a live turn it shows the call in flight instead, and the count
  pulses; that is the only "still working" indicator on screen. Connection
  notes collapse the same way: the newest one, and `+N` for the reconnects
  behind it. One formatter (`sessions.tool_detail`) writes the one-line summary
  for both the live stream and the transcript replay, so a resumed session
  reads exactly like the session you watched.
- **Notes the console names open in Obsidian** (`ui/VaultLink.jsx`,
  `lib/obsidian.js`). The inline markdown pass linkifies three forms: a
  wikilink, `[text](path.md)`, and a bare relative path ending `.md` — plus a
  backticked path, where the code span *becomes* the link so it still reads as
  a path. Everything is one matcher, so a path inside a link's href or a fenced
  block is claimed by the outer construct and never linkified twice.
  Deliberately narrow: a slash is required (`DESIGN.md` in a sentence is repo
  talk, not a note) and a leading slash disqualifies, because the console's cwd
  *is* the vault and the alternative was hardcoding the vault's location into
  the frontend. The click never navigates — Chrome interstitials every custom
  scheme, and the .app's WKWebView allows the navigation and then silently
  fails to load a scheme it doesn't know — so it POSTs to `/api/vault/open`
  and the backend hands the URI to `open`. The `href` still carries an
  `obsidian://` URI for hover and copy-link. Because linking is optimistic, a
  404 is expected: the link marks itself and its title says why, rather than
  the reference being dropped. The generated inbox uses the same link on note
  titles — reading the note before filing it was the point of the card, and
  until then it meant finding it in Obsidian by hand.
- **Math is typeset, not printed** (`ui/math.jsx`, KaTeX from npm like the
  fonts, so the desk still renders with the network down). Math arrives as
  a photo and comes back as TeX, which the markdown pass had no
  case for: a distance-to-a-line proof was seven `\frac`s of source on screen
  (09-03). `$…$` and `\(…\)` join the *inline* matcher rather than getting a
  pass of their own, for the reason the note forms did — a formula is full of
  `_`, `*` and `\[ \]`, and claiming it whole is what keeps this pass from
  chewing on them; a backticked `$x$` still loses to the code span, which is
  leftmost. `$$…$$` and `\[…\]` alone on a line are blocks, and scroll like a
  table instead of widening the turn. Two things the delimiters have to
  survive: `$5 to $10` (hence non-space at both ends and no digit after the
  closer) and a turn mid-stream, where a display formula whose closer hasn't
  arrived shows its source rather than flashing KaTeX's red parse mark on
  every token.
- Slash commands and skills pass through untouched — it's Claude Code.
- Command discovery costs zero tokens: the SDK init message's `slash_commands`
  is forwarded once as `{"type":"commands"}` (authoritative, includes
  built-ins); `GET /api/commands` supplies names + descriptions from
  frontmatter before the first turn. Typing `/` opens a filterable popup;
  `/help` renders the merged list client-side and never reaches Claude.
  Since 10-28 the authority arrives at connect instead: `get_server_info()`
  is the CLI's initialize answer, already holding every command and skill
  *described* (built-ins, plugins, and a dev tab's repo skills, which the
  vault-side scan can't see — a dev tab skips the scan), sent as
  `session_info` with the CLI version. Still zero tokens.
- **Context meter and CLI version** (`console/CliStatus.jsx`,
  `app/claude_cli.py`). `get_context_usage()` — what `/context` shows — is
  asked on connect and after every turn and sent as `context`; the chip's
  tooltip carries the categories and the autocompact threshold. Auto-updates
  are off on this machine, so the bar is where a release shows up:
  `GET /api/console/cli` compares the symlink's version with the channel file
  the native installer reads (`downloads.claude.ai/claude-code-releases/
  <latest|stable>`, held an hour), and the button runs `claude update`. A live
  session keeps the binary it started on, so once the install moves past the
  session's `session_info` version the button becomes *reconnect*, which
  resumes the conversation on the new one. Up to date, the bar shows nothing
  (it was wrapping); the version is on the session label's tooltip.
- **Task list, effort, rewind, /mcp** (`console/Parity.jsx`, 10-28).
  *Tasks*: TodoWrite is gone from the CLI — 2.1.312 silently drops it from
  the tool list and the model's ToolSearch for it came back empty — so dev
  tabs get `TASK_TOOLS` (TaskCreate/Update/List/Get), allowed without a card.
  The panel above the composer is read off the CLI's own files
  (`console_tasks`, `~/.claude/tasks/<session>/<n>.json`) after each
  Create/Update result and on connect, not rebuilt from the calls: an update
  carries only its delta, and a resumed list was written by calls this socket
  never saw. *Effort*: `--effort`, fixed at start, so the select reconnects
  like the model; it offers the levels `session_info.models` lists for the
  model in force, per mode in localStorage. *Rewind* (dev only):
  `enable_file_checkpointing` + `replay-user-messages`; the replay of your own
  message (no tool result, no parent) carries the uuid the CLI checkpoints
  at, sent as `checkpoint`. Transcript entries carry the same uuid, and a
  rewind after a reconnect works (checked live), so resumed history can
  rewind too — a session from before checkpointing was on just fails on the
  card. Files only; the next message gets a `<rewind>` note so the model
  re-reads instead of trusting edits it remembers. Two clicks, refused
  mid-turn. */mcp*: the CLI's is a TUI screen, so `/mcp` (and the bar's
  **mcp**) open a panel over `get_mcp_status()` with reconnect and
  enable/disable; needs-auth says to sign in from a terminal, since that's a
  browser flow the SDK session can't run.
- **Rewinding the conversation, output styles** (10-28). Rewind is now
  Claude Code's menu — code + conversation / conversation / code (the code
  ones dev-only) — on your messages in every mode, so `replay-user-messages`
  is on everywhere. The conversation half rides a hidden CLI flag,
  `--resume-session-at <uuid>`, which keeps the transcript up to *and
  including* that record (checked: resumed at a prompt's own uuid, the model
  still knew the prompt), so the cut is the prompt's `parentUuid`
  (`sessions.rewind_point`, `GET /api/sessions/{id}/rewind`) — none means it
  was the first message, and "before it" is a new session. It resumes with
  `fork_session`: the original stays whole in the resume list, and the fork
  keeps the copied records' uuids, so earlier rewind points stay valid. The
  log is cut there and the message goes back in the composer. The cut is
  held in localStorage (`<session key>:at`) until a turn lands in the fork,
  because the fork has no transcript before then and a reload would
  otherwise resume the conversation you just cut; resuming another session
  or starting a new one drops it. Code + conversation runs the file rewind
  first and only cuts on its `rewound` ok. *Output styles*: no SDK option,
  so `--settings {"outputStyle": …}` at start (name-checked, so a pick can't
  smuggle other settings), offered from `session_info.output_styles`, per
  mode, reconnecting like effort. With effort and style in the bar a dev tab
  wrapped, so its speak/listen are icon-only there.
- Permission modes in the status bar, loosest first: **auto**, ask (default —
  every gated tool call renders an Allow/Deny card in the log via the SDK's
  can_use_tool callback; unanswered requests deny after 10 min; interrupt
  denies anything pending), plan (ExitPlanMode renders as a plan-approval
  card: Approve plan / Keep planning), and **never ask** last.
- **auto-edit (`acceptEdits`) was removed on 2026-09-17**, and the reason is
  the same measurement that reshaped auto: it read as the cautious middle
  option and wasn't one. Under it the CLI settles Bash writes inside the
  workspace itself, so `rm <vault>/…` ran with no card while `task_create`
  stopped for one — loose exactly where it hurts, strict where nobody wanted
  it. That policy shape was an artifact of the CLI's definition, not a line
  anyone drew; auto is the same trade made deliberately and the right way
  round. A stored `acceptEdits` migrates to auto in `readMode()`, and the
  backend still accepts the string, so a pinned client keeps working.
- **never ask (`bypassPermissions`)** is the floor: `build_options` drops
  `can_use_tool` for it, so no card, no guard list, no `is_safe_read` — a
  send or an `rm` runs the moment the model decides on it (verified live,
  both, 09-17). It is offerable only because the summary survives it: the
  ledger is built from the tool stream, not the approval path, so "no
  approvals" isn't "no idea what happened". Switching *in* confirms every
  time rather than once-and-remembered — the other modes cost a click to
  undo and this one can't be undone — and the select stays red while it is
  on, because the warning is read once and the session runs all day.
- **Auto mode is ours, not the SDK's** (`console.AUTO_MODE`): the turn runs
  without cards and the change summary at the end of it is the review. It is
  deliberately *not* `bypassPermissions`, which would drop `can_use_tool` and
  with it both the guard list and the ledger — `can_use_tool` answers yes
  itself, except for calls `console_changes.Change.guard` marks as
  unrepeatable, which still stop for a click carrying the reason ("auto mode
  paused — it would send mail, which leaves for its recipient immediately").
  The guard line is narrow on purpose: not "could this lose something" (every
  write could) but "does this leave the machine, or leave nothing to undo it
  with" — sends, trashes, `delete_event`, `rm`, `git push`, a SQL DROP.
  Everything else — vault files, tasks, drafts, db rows — runs unattended.
  **The SDK mode it rides on is `default`, and that is load-bearing.**
  `acceptEdits` looked right and silently defeated the guard: the CLI settles
  in-workspace Bash writes under it on its own, so `rm <vault>/06-Generated/
  x.md` ran with no card and no call into the backend at all, while the same
  `rm` outside the vault prompted (measured 2026-09-17, both directions).
  Under `default` every call arrives at `can_use_tool`, which is the only
  arrangement where a guard list means anything.
- **The change summary is a record, not a claim** (`{"type": "changes"}`, sent
  just before each turn's `result`, in every mode). It is assembled from the
  tool stream — a call is claimed when `ToolUseBlock` classifies as a change
  and kept only when its `ToolResultBlock` comes back without `is_error`, so
  denied, failed and interrupted calls never appear. Nothing is asked of the
  model: a summary it wrote would be a claim about its own work, and this is
  what you check that claim against. Coverage is a test, not a habit —
  `test_console_changes.py` fails if a tool on `console_tools.WRITE_TOOLS` has
  no row in `console_changes`, which is the registry loop this repo keeps
  losing. Connector tools, which are not ours to enumerate, fall back to
  reading the verb in the name, and an unknown destructive-sounding one is
  guarded rather than assumed harmless.
- **`allowed_tools` bypasses the approval card entirely** — the SDK documents
  those entries as auto-allowed *without prompting*, so can_use_tool is never
  consulted for them. `CONSOLE_ALLOWED_TOOLS` therefore holds only individually
  named read-only MCP operations, and **no `Bash(...)` patterns**. It used to
  carry prefix wildcards like `Bash(curl -s http://localhost:8788/*)`, which
  matched everything after the URL too: `… /api/tasks -X POST -d @-` shares the
  prefix, so ~25 mutating endpoints were reachable unattended. A prefix string
  can't express "and no write flags", so Bash policy lives in
  `console_policy.is_safe_read()`, which sees the whole command — it auto-allows
  provider-CLI reads and localhost GETs, and prompts for chaining/redirects,
  write flags, a second URL, `--refresh`, and anything unrecognized. This is
  the boundary that keeps injected text (from an email, a message, or an
  extracted attachment) arriving as a card the user sees rather than a command
  that already ran.
- **Context pills** carry what a widget already had on screen into the next
  message (`<attached_context>`, one `<widget name=…>` per pill). Shaping is
  split on purpose: `frontend/src/lib/pin.js` decides what's *worth* sending
  (`slim` drops ids/URLs the console can't act on, `pinSlice` byte-budgets a
  row list into a head sample carrying `omitted`, `pinFiles` names a message's
  files and says whether their text was extracted), and `console.PILL_LIMIT` is
  the floor under it, so a widget that forgets to shape still can't spend the
  window — a month of messages is 536k characters raw. Truncation is always
  announced; a silently-cut list gets confidently totalled. Pills are pure
  frontend state (no endpoint, no table), same posture as `lib/salience.js`.
- **Dragging into the console** (`frontend/src/lib/drag.js`) is the same pills, at a
  size the buttons couldn't offer. `dragPin(label, data)` spreads onto a card, a
  list *or* a row — the three nest, so which one you get is decided by where you
  grabbed: `dragstart` fires innermost-first, and stopping propagation lets the
  row beat the list and the list beat the card. It carries the exact
  `{ label, data }` that surface's pin button carries and lands through the same
  `pinContext`, so shaping stays in `pin.js` and there is no second path to keep
  in sync — a payload used by both gestures is hoisted out of the `onClick` so
  it can't fork. Two constraints show in the code: the payload rides a private
  MIME type (`application/x-vault-pin`) because `getData` is blocked until the
  drop, and only `dataTransfer.types` is readable during `dragover` — that's how
  the drop veil can name file-vs-context *before* committing; and `draggable`
  suppresses text selection inside the element, which is why sheets are
  dragged by their header and not their body. Since 11-10 that second
  constraint is lifted by a modifier: sources carry `data-pin-drag` and are
  `draggable` only while ⌘ or ⌥ is held (set on the DOM, not through React),
  so text anywhere on the desk highlights normally. Not Control, the first
  ask — Control-click is macOS's right-click and never starts a drag in
  WebKit. Dragging *out of* an expansion
  used to need one more thing — the slide-over's backdrop covered the console,
  so a drag in flight set `body.pin-dragging` to stop the backdrop taking hits,
  cleared on `dragend` and again on the window in capture because a source can
  unmount mid-drag. The dock column removed the backdrop and the whole
  mechanism with it: the deepest rows in the app (a month of transactions, one
  person's whole history) now drag onto a console that was never covered.
- **The prompt names tools by a name the model cannot look up.** The console's
  tools are `mcp__vault__<name>`; `SYSTEM_APPEND` has always written the short
  form (`debt_add`, `db_query`), and a model that takes the prose literally and
  searches for one finds nothing. Replayed over the 88 sessions on record,
  **29 of 153 ToolSearch calls came back empty and every one was for a vault
  tool** — 15/15 `select:` calls on a bare name, 14/14 keyword searches
  (`shopping item create add`, `package delete remove packages`); a qualified
  `select:mcp__vault__x` never once failed. Sessions usually recover on the
  next turn by calling the tool directly, so the standing cost is a wasted
  round trip — but an empty search is also indistinguishable from an absent
  capability, and the model is then one turn from telling the user so, which is
  the drain the writers below exist to stop. Fixed by stating the rule once
  (the tools are already loaded, the real names carry a prefix, don't go
  looking) rather than by rewriting ~40 mentions, which would cost more tokens
  on every turn than the sentence does. Same shape as `is_safe_read`'s 0% hit
  rate: **the prompt was prescribing a form the far side rejects**, and neither
  was visible without replaying what sessions actually did.
  The sweep also found the one tool the prose never named at all: `email_body`.
  The prompt sent email questions to `db_query` over `emails`, a table holding
  a snippet and no body — so the instruction quietly capped the answer at the
  summary. `test_the_prompt_names_every_tool` keeps that from recurring.
- **Which stores have a console writer is load-bearing.** For a while only
  `task_create`/`task_update` did, and the task board became the drain every
  other write fell into: six things to buy filed as six tasks, a linen order's
  pickup date as a seventh — one session saying outright that no shopping-list
  tool was exposed to it and filing the tasks anyway. A store the console can
  read but not write does not stay unwritten. `list_add`/`list_item_done`
  (`shopping_add`/`shopping_bought` before v41 made the list itself a row) and
  `package_update` close the two that hurt; `SYSTEM_APPEND` names which
  store each kind of thing belongs to, because a tool the prompt doesn't name
  is a tool the model discovers only by guessing. Debts were the prediction
  this paragraph used to end on, and they were hit on 2026-08-11: told "this
  is a debt tracker so add them to debts", a session searched the vault for a
  *file* named something like debt — the store had a table, a route and a card,
  but nothing the console could name — and wrote four people's balances into a
  task's note. `debt_add`/`debt_settle` close it. Note the second-order cost:
  the same message also said to close the task, and the session refused,
  because the note it had just written was by then the only copy of the debt.
  A write landing in the wrong store doesn't just sit there wrong; it holds
  the rest of the instruction hostage. `watchlist_add`/`watchlist_watched`
  went in beside them rather than waiting for the session that would have
  proved the point a third time — a prediction right twice does not need a
  third confirmation. "Every store behind a card now has a writer" is what
  this paragraph used to end on, and it was wrong: **habits** had a table, a
  route, a card and a `habit_checkins.source` column already reading
  `'ui'/'console'`, and no tool. Worse than merely missing, because
  `SYSTEM_APPEND` has pointed at that store since round 1 ("mood/workout
  journaling moved to habits") while leaving the task board as the only way to
  write to it. `habit_checkin`/`habit_add` (2026-08-12) close it, and the
  failure they head off is not a lost note: a habit filed as a completed task
  leaves the day reading as *missed* on the card, so the streak the user is
  keeping breaks anyway. Enumerate the cards against WRITE_TOOLS rather than
  trusting the claim — `test_every_card_backed_store_has_a_writer` now does.
  The sharper form: a writer that can only *amend* is not a writer. Deliveries
  had `package_update` and the drain simply moved one door down — an Amazon
  orders page pasted in, three arrival dates, and the console filed them as
  bought shopping items because opening a package row was the one thing it
  still couldn't do. `package_add` closes it. When adding a writer, ask
  whether the model can create as well as change; if creation belongs to some
  upstream pipeline (here, email extraction), everything that pipeline cannot
  see has no way in at all.
- Provenance: PostToolUse hook on Write stamps new vault .md files with
  `origin: generated / generated_by / generated_at / reviewed: false`.
  Edits to existing notes are NOT stamped (your note stays yours).
  System prompt directs brand-new notes into 06-Generated/.
- **Stats for nerds** (Settings, off by default; `app/console_stats.py`):
  each reply's footer adds time to first token, tok/s and model calls. TTFT
  runs from the message leaving to the first content block of the reply.
  tok/s is output tokens over time inside model calls (`message_start` to
  `message_stop`, summed), because a dev turn's wall clock is mostly tool
  runs. Subagent calls are left out. A turn the CLI starts on its own has no
  TTFT.

### Code Mode — `run_script` (`app/console_code.py`, 11-17)

One read tool that takes a short JS script; the script's functions are the
other read tools, and only its `return` reaches the context. Claude Code's
own Code Mode refuses the Agent SDK, so this is the Cloudflare shape.

- **Two sandbox layers, each sufficient alone.** Deno with no `--allow-*`,
  inside a deny-default Seatbelt profile (`sandbox-exec`) that reads only
  system libs, Homebrew kegs and the run's temp dir — no net, no writes, no
  exec but Deno. `test_seatbelt_holds_even_with_every_deno_permission_on`
  runs the escapes with Deno at `-A`. Empty env, 128MB heap, 60s, 40 calls,
  per-tool caps on calls that cost quota (Maps, Reddit, Gmail fallback).
  No Deno or no sandbox-exec: the tool refuses (fails closed).
- **The bridge runs the tool's own handler** after validating against its own
  schema, so a bridged read behaves exactly like the direct call. No SDK
  hooks fire for the inner calls — fine for reads, and the reason writes
  aren't in it.
- **Writes are absent, not gated:** every WRITE_TOOLS name (and open_tab,
  tabs) is a stub that throws "writes go through the normal tools", and the
  host refuses any non-read name regardless. Phase 2 seam: `Bridge.writes` →
  handlers + `Bridge.approve` awaiting can_use_tool.
- **What it buys, measured (evals/code_mode, Sonnet):** as first shipped
  (11-17, n=1) turns fell but input tokens were flat to worse (−4% to +34%);
  re-measured 11-18 at n=3 the script arm was 0% to +4% over direct, because
  it spent 3–9 calls before scripting (`sqlite_master`, `pragma_table_info`,
  the list it then looped over) and on two of four questions never scripted.
  The fix was words, not code: a table summary in the description
  (`console_code.SCHEMA`, held to the live schema by test_tool_eval),
  "decide before the first call", and "return the answer, not the material".
  After (n=3, medians, same-run direct arm): −52%, −30%, −44%, −5% input
  tokens, 3–5 turns against 5–14, no pre-script calls. The email question
  gains least: it still reads one body directly after the script. Re-run
  `--repeat 3` after changing either text; n=1 was misleading both times.

### Dev tabs: project scripts, a port per worktree, PR watch (`app/console_project.py`, `app/console_pr.py`)

For client-site repos reviewed through PRs (New Features #87, #88; T3 Code's `t3.json` and
PR watcher are the model).

- **`.kestrel/project.json`**, read from the tab's own folder: named
  `scripts` (icon run/test/lint/build) shown as bar buttons, `previewUrl`,
  `autoOpenPreview`, `runOnWorktreeCreate`. Scripts run in the worktree in a
  login zsh, owned by the backend process (they stop when the tab's tree is
  released and at exit), output kept for the view pane's Tasks. The HTTP route
  runs only a script the file names, by id.
- **A port per tree.** Worktrees run at once, so a framework default (3000)
  collides and one tab previews another's code. Each tree gets a port in
  41000–41999 hashed from its path (stable across restarts), stepping past one
  held by another tree or a foreign listener; it's `$PORT` in scripts and the
  start line tells the agent.
- **runOnWorktreeCreate** runs as one background "setup" run when a tab's tree
  is made (`console.open_worktree`), not in `console_worktree._setup`, which
  blocks the tab's start: an `npm install` is a minute.
- **PR watch is stateless on the backend.** The console keeps the last
  snapshot per tab (localStorage) and posts it with each poll; `console_pr.
  changes` answers the notes between the two. So a restart loses nothing, and
  a closed or settled tab (unmounted) stops polling by itself; merged/closed
  stops it too. Linked automatically after a turn that ran `gh pr create`
  (by its `--head`, else the tree's branch), or from ⋯. Notes ride the wake as
  `<pr_watch>`, sharing the handoff reports' wake path and MAX_WAKES. Your own
  comments and reviews don't wake the tab (it would wake itself). One poll in
  flight at a time: the mount poll and a refresh-bus broadcast once delivered
  the same news twice.

### Citations — what a sentence was read off

The console restates detail that came from somewhere: an address out of an
email, a room change out of an event, a number someone texted. The sentence
keeps the fact and drops the source, and checking it meant searching Gmail for
whatever you half-remembered the subject was. A citation is the handle back.

Three pieces, and the middle one is the one that decides whether the feature
exists at all.

**The ref rides the row.** `app/citations.py` mints `sha1(kind + target)`
truncated to five hex digits behind a kind letter — `e7a3f1` (email), `v2b0c4`
(event), `m9df13` (message) — and `console_tools.provider` stamps it onto each
rendered row on the way out, in both the brief and the raw-JSON reads. Six
characters a line, and the reason it is not a second lookup is that a model
which has to make another call to find out what to cite mostly will not
bother. The stamping hook is `cite()` on the provider module, beside
`brief_line()`; a provider without one, or a row it declines (an aggregate, a
day count), renders exactly as before. `email_body` stamps again, because a
body is read exactly when the answer is about to quote out of it.

**The ref is derived, not assigned.** A counter would need session state where
the row is rendered, would renumber between sessions, and would make a
replayed transcript resolve its refs to different rows than it did when it was
live. Derived, the same email is `e7a3f1` forever. Collisions lengthen rather
than steal — `remember()` never overwrites, so a ref already sitting in an old
transcript keeps pointing where it pointed.

**Resolution is the validation.** There is no separate check that the turn
really read what it cites, and there does not need to be: a ref the model
invented is five hex digits that were never handed out, so `/api/cite/{ref}`
404s and the marker renders dead — struck through, like a missing `VaultLink`,
for the same reason. A ref that *was* handed out but whose row has since left
the cache is the third state and must not look like the second: "that email is
no longer cached" and "the console made this up" are different admissions.

On the desk, `ui/Cite.jsx` renders the marker as one glyph and nothing else —
a pill carrying a subject line would be a second voice inside the paragraph,
and the paragraph already said the thing. Opened, it is a card under the line
holding the source, with the email's body on demand (`EmailBody`, live from
Gmail, stored nowhere) because reading the mail is the case the feature was
built for and it should not mean leaving for Gmail. Resolution is deferred to
that click: a long answer carries a dozen markers and almost none are opened.

Vault notes are deliberately NOT cited this way. `[[Note]]` and bare note
paths already render as Obsidian links, and a second mechanism over the same
target would be two ways to say one thing.

Three things this cost, all of them found by running it rather than reading
it:

- **A read that skips the brief line skips the ref.** The first live session
  asked for a detail out of an email, went `brief=false` for the body fields,
  got rows whose only handle was the raw Gmail id, and cited *that* —
  `[cite:1a0c5286b39d182b]` in the prose. Refs are stamped on both paths now.
  The general lesson is the same one the tool descriptions keep teaching: an
  affordance the model cannot see from where it is standing does not exist.
- **Whatever is between the brackets, the markup must not reach the reader.**
  The marker pattern was anchored to the three kind letters, so that malformed
  citation rendered as literal `[cite:…]` text — the renderer looking broken,
  in an answer that was merely wrong. It now claims any `[cite:…]` and renders
  an unref-shaped one as dead without asking the network.
- **`markdown.jsx` keys inline nodes by character offset**, and two different
  answers put a `[cite:` at the same offset often enough that React reuses the
  component. Without a reset, the card opened showing the *previous* ref's
  email — resolved, confident, and wrong, which is the one outcome the whole
  feature exists to prevent. The ref is folded into the key and `Cite` resets
  on a changed `refId`; two locks, because a caller can undo the first.

And one that is a standing trap rather than a bug: **an effect that measures
the element it transforms is its own input.** The opened card is clamped into
the viewport, and the first version read `getBoundingClientRect` on the card
itself — which converges only because a browser reports the transformed rect.
Under anything that does not, it diverges and React kills the tree with
"maximum update depth exceeded". It measures the *marker's* box and the card's
untransformed `offsetWidth` instead, so the answer is the same on every pass
by construction.

`cite_refs` is derived and droppable: every row can be rebuilt by re-reading
the provider caches it points into.

### Documents: citing a page, showing a page (`app/docs.py`)

A PDF dropped on the console (or attached from the Desktop tray) used to be
read to text and thrown away, so an answer about it could only describe a
page. Now the bytes are kept at `DATA_DIR/docs/<sha256>.pdf` (the truth; the
`docs` row is name, size and pypdf's per-page text, derived) and minted a
fourth citation kind, `d…`, over the sha. The pill carries the pages under
`[pN]` markers up to `PILL_BUDGET`; the rest are a `doc_pages(ref, pages)`
call away. The ref rides outside the untrusted fence in a `<document ref=…>`
tag, so a PDF can't mint itself one by printing it.

Two markers, one syntax: `[cite:d3f9a1#p12 "exact words"]` is a pill (`p12`
shown) whose card is the page with the words highlighted;
`[page:d3f9a1#p12]` alone on a line draws the page into the reply. Pages are
physical (1 = the file's first page), which is what pypdf and pdf.js both
count.

**A PDF on disk gets a ref too:** `doc_pages(path="~/Downloads/h9.pdf")`
runs the same read and `docs.keep` a drop does (under home, `.pdf`, under the
attachment cap), so the same bytes are one document however they arrived.
Before it (11-07) a study tab read handouts with pdftotext, and nothing in
the reply could show a page.

**The document beside the chat** (`console/DocPane.jsx`). Full screen, the
cited page has to be on screen while the reply is read, not under a pill. The
newest reply's first doc reference opens the pane there (once per reply, and
not again after the user closes it until they open one themselves); a click
on any cite, page embed or quiz slide moves it, with the quote located and
highlighted as on a card. The whole document scrolls, pages drawn lazily into
placeholders sized from each page's viewport. In the plan view it takes the
map's place (`docTarget`, a slot in the stage grid; at full sheet the grid
splits chat | document); elsewhere it shares the console, covering the log
under 760px. Reached through `DocPaneContext` (`lib/docPane.js`) so a cite
in one tab opens that tab's pane; with no provider, cites open their card
and pages their lightbox as before.

**The quote is the verification**, the way resolution is for the other
kinds. `lib/pdf.js` searches pdf.js's text layer for it on letters and digits
only, case- and ligature-folded (a line wrap or curly quote must not make a
faithful quote look invented), cited page first, then ±2, then everywhere.
Four outcomes: on the cited page (highlighted); on another page ("on p.14,
not p.12" — shown there); a scan with no text layer ("can't check", never
"not found"); nowhere (the pill is struck like a dead ref).

**Nothing on the backend parses the PDF.** The text comes from the same
sandboxed pypdf worker as before; drawing is pdf.js in the browser
(`isEvalSupported: false`), the Desktop tray's posture. pdf.js loads on the
first page drawn, like KaTeX. The worker now returns per-page text, which
outgrew the pipe buffer: `_run_guarded` drains stdout while the child runs
(capped at `_STDOUT_CAP`), where it used to read only after exit — a child
blocked on a full pipe never exits.

**Quizzes and flashcards** are ```` ```quiz ```` / ```` ```cards ```` fences
holding JSON (format in `console_prompt.py`), made interactive by
`ui/StudyBlock.jsx`. JSON because the answer key has to be in the reply and
out of sight. Choices are reshuffled client-side, seeded by the question
text: the first live quiz put all three answers at B. An item's `slide` is
the page it's worked from, drawn beside it (stacked when narrow, by
container query) and once per run of consecutive questions on the same
slide, pinned while they scroll; `cite` is where the answer is, shown once
answered. Before this, a walk-through drew its slides above the quiz and
each had scrolled away by its question. `why` precedes `answer` in the
format so the key is reasoned before it's committed — a live quiz keyed "e"
for `*(("Bye")+1)` and then corrected itself inside `why`. Embedded pages
don't page through the deck (the answer chose them; "Open" browses); a
click enlarges one in a lightbox. Results are drafted
into the composer (`draftConsole`), not sent, so the console logs the real
misses with `course_review_add`. "How sure?" is asked before the answer shows.
If skipped it stays open afterwards, at the user's request, and such a rating is
marked (`rated_after`, in the results and in `study_coverage`) because it can be
fitted to the verdict; one given first is locked. Any answered question, right
or wrong, takes a note that goes out with the results.

Not covered: a scanned PDF has no text, so it is not kept and can't be cited
(it would need OCR); attachments that arrived by email or iMessage
(`file_read`) don't get refs yet; nothing prunes `data/docs/`.


## Other models (`app/delegates/`)

Claude is the orchestrator and the only model that sees anything personal:
the vault, the Brain, texts, email, money, contacts. Gemini and ChatGPT are
there for grunt work that would only burn Claude usage, and for "make N
versions", where a mix of models is the point. They are driven through their
own CLIs (`gemini`, `codex`), the way T3 Code does it, so nothing here holds
an API key and each bills to the login the user already has.

- **The registry** (`registry.py`) is the list: Claude, Gemini, Codex. A
  provider is *ready* when its CLI is installed, signed in and switched on.
  The first two are read off the machine each time (cached 20s; Codex is
  asked with `codex login status`, Gemini by its credentials file or key),
  and only the switch is stored (`delegate_providers`; no row means on).
  Everything is built against that report, so a missing CLI is a tool error
  that names the install command, never a crash. Settings → Providers shows
  the same rows.
- **The boundary** (`boundary.py`) is the privacy rule as code, three layers,
  and `launch` is the only function that starts an outside CLI:
  1. *The brief.* `launch` takes a `Brief`, which only `scrub` can make:
     email addresses, phone numbers, long digit runs, paths under the home
     directory or the vault, and contacts' full names become placeholders,
     and the tool result tells Claude what was taken out. This catches
     identifiers, not meaning. A brief that describes someone's week in plain
     words passes it, which is what the next two layers and the prompt are
     for.
  2. *The environment* is an allowlist (`ENV_KEEP` plus the provider's own
     key variables). The backend's own holds Plaid and Google secrets.
  3. *The sandbox* is ours, a seatbelt profile through `sandbox-exec`: no
     read or write under the home directory, other volumes or the temp
     directories; then the CLI's own config and code; then, whatever those
     allowed, never the vault or the data directory; then the run's scratch
     folder, the one place it writes. Ours and not the CLIs' own because
     theirs answer a different question (Codex's read-only mode reads the
     whole disk), and seatbelt doesn't nest, so each CLI runs with its own
     sandbox off inside it. No `sandbox-exec`, no run.
  The module imports the standard library and `config` and nothing else, so
  there is no path from it to personal data. `tests/test_delegate_boundary.py`
  holds all of it: each module's imports against a list, that nothing else in
  the package starts a process, the env, the scrub, and the profile run for
  real against a fake vault and the real home directory.
- **No MCP either way.** The outside CLIs get none of the vault server's
  tools (they aren't Claude Code and are never handed a config), and their
  own users' MCP servers are turned off for the run (`--allowed-mcp-server-names`
  with a name nothing has; `--ignore-user-config`).
- **A run** (`runs.py`): every agent, Claude included, gets the scrubbed
  brief and a file name to hand back, and either writes that file or replies
  with its contents. Each variant has `<data>/delegates/<batch>/v<n>/`.
  Claude's variants go through `agent.ask` (no tools, one turn). Every run
  is a row in `delegate_runs` (provider, model, tokens where the CLI
  reports them, time, what was redacted), which is what `GET
  /api/delegates/usage` sums per provider for the usage page.
- **Three console tools**, in every mode: `delegate` (one errand to one
  outside provider), `fan_out` (N variants, the ready providers taking turns
  unless `mix` says who; optional `angles` so they differ on purpose) and
  `shortlist` (Claude's picks as a card with a framed preview of each and
  "use this one", which sends the choice as the user's next message). All
  three run without an approval card, like `open_tab`: what leaves is
  scrubbed and logged, and a card on every errand would defeat handing work
  off. They are kept out of `run_script`.
- **A variant's file is untrusted HTML** shown inside the app, so
  `/api/delegates/batches/{batch}/{n}/file` serves it with a CSP `sandbox`
  (opaque origin, no connections) as well as the frame's own attribute.
- **In the log** a delegate or fan-out call is its own row (`→ Gemini`, or
  the mix it ran), which opens to the brief that left and what came back.
  That row is the mark that another provider answered.
- **"Model changed"** (#101): every reply in the log carries the model that
  wrote it (`model` on a text entry: the socket's `model` frame live, the
  transcript's `message.model` on replay), and the mark is derived at render
  wherever two consecutive replies differ. Nothing is stored for it, so a
  resumed log shows the same marks as the live one.
- **"Retry with…"** (#104) sits on each turn's last reply. It is the
  conversation rewind with two differences: the tab reconnects on the picked
  model (as the tab's own pick, so the default for new tabs doesn't move),
  and the message is sent again by itself once the fork's `mode` frame
  arrives. The first answer stays whole under Resume. Claude models only: any
  other provider would have to be handed the conversation. A message that
  had pills can't be retried this way (the pills' data is gone from the
  page), and the menu says so.

Not built: image generation (neither CLI was installed to check what it can
do headless), letting an outside agent work inside a project checkout, and a
streamed view of an outside agent's progress.

## The Brain (`app/brain.py`)

What Kestrel knows about the user, kept rather than re-asked. Before it, the
console prompt said never to write anything down unasked, and the only memory
was Claude Code's own auto-memory under the vault's project dir: 203 sessions
had left four memories there, invisible to the user, one of them wrong ("subtract
3h from provider calendar times" — false since the timezone work).

**Shape.** Karpathy's "LLM Wiki" (IDEAS, 09-28) adapted to a firehose instead
of a curated shelf: his sources are papers someone chose; ours are texts,
chats and mail, mostly noise and constantly going out of date, so the hard
problems are *what's worth keeping* and *what stopped being true* — the two
his gist leaves open. Taken from it: an index, a log, a schema the ingest
and lint prompts share, periodic lint. Left out: a raw-sources layer (ours
already live in app.db and the transcript store; copying them would be a
second truth), and "ingest touches 10–15 pages" (a personal fact is one
targeted write). Query-by-asking is replaced by injection — the gist's other
open problem is getting the agent to consult memory at all, which is exactly
the complaint this exists to answer.

**One fact, one row.** `brain_facts` holds a claim, its page, `as_of` (the
source's day_key, never the model's), `source_kind`/`source_ref`/
`source_label`, a verbatim `quote`, and `status`: *active* (told to Kestrel
directly, or ✓'d), *pending* (read off a source — usable, labelled
unconfirmed everywhere it shows), *superseded* (`superseded_by` the newer
fact; `prior_status` is what it was, so ✗ on the replacement restores it),
*rejected* (a tombstone ingest is shown so it never relearns it). The user
chose "everything needs a ✓" (09-28); `BRAIN_INFERRED_STATUS` is that
choice, and what a user says to Kestrel counts as their ✓.

**Ways in.**
- `brain_note` / `brain_verdict` / `brain_read` in the console. The prompt
  now says to record durable facts unasked; the old "no note for what you
  did today" rule stands, because a day is not a fact about someone.
- Ingest: `gather(start, end)` collects the window's console turns (user
  2000 / Kestrel 500 chars), texts (1:1s, and groups where the user has sent
  `BRAIN_GROUP_MIN_MINE`+ messages — a group they lurk in is other people's
  lives), email (sent, plus received minus newsletters/receipts/accounts),
  dated vault notes, and the old auto-memory; `render_sources` gives each
  line a short id; one `agent.ask` per ~200k-char batch; `apply_ops` checks
  every proposal before writing — the cited id is in *this* batch, the quote
  is really in that line (case/whitespace/curly-quote insensitive), the slug
  is well-formed, the claim isn't a duplicate or a tombstone, a supersede
  names a current fact. Model output is untrusted input until then; this is
  also the memory-poisoning boundary, since injected text in an email can at
  most propose a pending fact that waits for a ✓.
- Lazy: `GET /api/brain` starts `catch_up` when a whole day has closed past
  the watermark (never before the first backfill — that is a deliberate CLI
  run), then `lint` if a week has passed, then `render`.
- Backfill is the same code over weekly windows, oldest first, each batch
  seeing the Brain as of its own week — so facts about an old home are
  learned and then superseded by the new one the way it happened. `python -m
  app.brain backfill [--since] [--weeks N] [--dry-log f.jsonl]`, resumable
  from `brain_runs`. There is no separate backfill script to retire: it is
  also the rebuild (`reset` forgets unruled ingest facts; verdicts stay).
  `python -m app.brain messages` extended the `messages` cache a year back
  from chat.db once (`messages.backfill_older`: no task capture, watermark
  untouched).

**Ways out.** `render_append` adds the `<brain>` block — core facts (≤6k
chars) and one line per page (≤4k) — at session start. A `UserPromptSubmit`
SDK hook (`console.brain_context`) matches page titles, aliases and a
person's first name in what the user typed (not their pinned context) and
attaches those pages' facts; no model call. `07-Brain/` holds `_profile`,
`_index`, `_log` and one page per slug, each fact with a `^f<id>` block ref;
only files whose content changed are rewritten, and a stale file is deleted
only if its frontmatter says `brain: true`.

**What the checks learned from real runs.** The prompt names what not to
record; code enforces the ones Sonnet broke anyway (`_claim_problem`):
identifiers (it kept the user's own email and a card's last four),
hedges ("sibling (or close family member)"), and chat metadata — the first
real backfill filled a quarter of `routines` with "is in a group chat called
'<name>' that includes …", the batch's own headings read back as facts.
The diary problem (`goals` as a log of every application submitted) is
prompt-side: record the outcome that still matters, not the steps, and
finished milestones go to `history`.

**The card** (`widgets/BrainWidget.jsx`) is a review queue: the first page
with unconfirmed facts, ✓ / ✗ per fact and "✓ all" per page, then the page
list. Its sheet walks every page with edit (a rewording is the user stating
the fact — a new active row superseding the old) and each page's history.
Unconfirmed facts make the card HAS, never NEEDS: nothing breaks while they
wait, and the console already uses them, labelled.

**Lint** batches changed pages per call: summary, aliases, merges,
same-page supersessions, and which facts are `core` (the profile, kept
under ~40). A guess never retires a fact the user confirmed.

**Known gap.** Claude Code deletes transcripts after 30 days
(`cleanupPeriodDays`), so console turns older than that are gone as a source
— the earliest on disk at build time was 08-30. Facts already learned from
them stay (the quote is kept on the row), but `reset` deletes unruled ones
like any other and nothing can relearn them — ✓ what matters before a reset,
or raise `cleanupPeriodDays`.

## Reaching the user (§21 — `alarms.py`, `reminders.py`, `notify.py`, `wake.py`)

Everything above computes things that are only useful *before* they happen — a
check-in window opening, a package arriving, a time to leave — and every channel
the app owned needed a process running and a screen open. A live notifier does
not fix that; the machine is asleep in a bag. The model is **the laptop computes
and the phone delivers**: while the lid is open the backend writes future-dated
alarms into a surface iCloud syncs, and the phone fires them hours later with
nothing of ours running. That only covers alerts with a known future time.
Reactive ones ("an email just landed") need an always-on host and are out of
scope — the at-desk channel below is the deliberate half-measure for them.

**Alarms are reconciled, notifications are emitted, and the difference is
whether the thing can be revised.** An emitter fires once and cannot take it
back, so a flight that slips leaves the phone announcing a departure that moved:
`alarms.py` therefore recomputes the whole desired set every cycle and diffs it
against what was written. Nothing in `notify.py` is a prediction — the past does
not get amended — so there is nothing to withdraw and the only bug an emitter
has is saying it twice.

- **Three states, never two.** The hazard a reconciler creates is deletion: a
  source that fails to read produces no alarms, an empty desired set withdraws
  everything, and one unreadable table silently cancels a real 6am alarm. So a
  source returns alarms, a *confident* nothing, or `unknown`, and `unknown`
  holds. A source that can answer for most of its alarms but not all names the
  exceptions (`SourceResult.hold`) — kind-level unknown was too coarse for the
  first source that needed it, since one venue that won't geocode shouldn't
  throw away two good answers.
- **An empty list is a claim.** `compute_leaves()` swallowed a failed calendar
  read into `[]`, which is exactly right for a card and a lie to a reconciler,
  which reads it as *nothing to leave for* and withdraws every leave alarm on
  one auth hiccup. Hence `strict=True`, whose only caller is the reconciler.
  Generalized in "Recurring lessons" below — it is the same rule as
  `_RETENTION_GATES` and the three-state protocol, seen from inside a helper.
- **Keys are partitioned and injective** (`app/keys.py`, shared by alarms and
  notify). The `kind:` prefix is what keeps a failing source's hold on its own
  land, so key-building must be injective or the partition leaks. `packages
  .order_key` is *literally* `merchant:order_id`, so it is escaped rather than
  stripped. Alarms **reject** a stray colon and notify **escapes** it, on
  purpose: an alarm part is always an id, where a colon means a composite got
  passed by mistake, while a notify part can be a value ("Delayed until 15:30"
  is an ordinary AeroAPI status) and rejecting it would sink the flight poll
  from inside its transaction over a punctuation mark.
- **Delivery keys on `written_at`, not `external_id`.** An amended alarm keeps
  its id, so "already delivered" would leave the phone holding the old time
  forever; an update clears `written_at` and the stale alarm is withdrawn
  before the new one is written, so the phone never holds two versions of one
  fact. Withdrawal happens inside `deliver()` and the row is dropped only after
  the backend confirms — deleting first strands an alarm that fires anyway and
  can never be retracted.
- **A measured value needs a deadband.** `leave_by` is a live traffic estimate
  and is never twice the same, and an amendment is not free (it takes the alarm
  off the phone and writes it back), so `_FIRE_DEADBAND` is per-kind and
  deliberately empty for the other two: a 9am ETA either changed or it didn't.
- **The at-desk channel** (`notify.py`) is a banner for the half that just
  became true. Dedupe is its whole discipline: one row per fact, **keyed on the
  fact**, so a status that flaps away and back is honestly two. Rows are
  persisted rather than fired inline because a row is what lets delivery
  *wait* — `send()` enqueues, `flush()` is the only thing that talks to the OS,
  the same split as reconcile/deliver. The **presence gate** (`ioreg`
  HIDIdleTime + screen lock) **fails open**, because a probe that can't answer
  swallowing everything is the exact bug this step exists to fix; and
  **staleness beats the queue** — "leave in five minutes" delivered ninety
  minutes late is worse than silence, so a row past `NOTIFY_STALE_MIN` expires
  (kept, not deleted: it's the record that the channel had something to say
  while you were out). **Notify on transition, never on discovery** — a first
  sighting already reading `delivered` can't be told apart from one that just
  happened.
- **Scheduled wake** (`wake.py`) is the assumption the first four steps never
  stated: a reconciler that only runs while the lid is open quietly stops
  holding in the eight hours before a 6am alarm. It is the one place §21 keeps
  **no table of its own** — `pmset -g sched` lists exactly what is scheduled,
  so a table could only ever drift from it, and reading the OS also reconciles
  wakes left by an older build or set by hand. (Contrast alarms, which must
  persist `external_id`: a Reminders item is invisible from here and losing the
  handle strands it forever.) The list is shared with the system, so
  `cancelall` is banned outright and every cancel names its exact
  type/date/owner — the same partition argument as the `kind:` prefix, one
  store over.
- **Failure degrades to not-waking, and that is safe twice over**: the alarm is
  already on the phone, so a missed wake costs freshness and never an alert;
  and a machine that wakes with no network fails every provider, every source
  returns `unknown`, and the three-state protocol holds. This is why the caps
  differ by intent — `ALARM_MAX_WITHDRAW_PER_CYCLE` refuses withdrawals
  *wholesale* (half a withdrawal strands alarms on a phone), `NOTIFY_MAX_PER_CYCLE`
  defers and logs (half the news still beats none of it), and the wake cap
  *truncates* the time-sorted tail (a later cycle picks it up).
- **Privilege.** `pmset schedule` needs root even for a scheduling verb — the
  `system.preferences.energysaver` rights entry is real and reads `class: user
  / group: admin`, but it governs callers that *ask* the authorization layer,
  and the `pmset` binary checks euid itself before anything reaches it. A
  sudoers drop-in (`backend/scripts/kestrel-wake.sudoers`, absolute path,
  scoped to `/usr/bin/pmset schedule`) is installed by hand once per machine.
  **The read stays unprivileged** — `-g sched` is what `status()` reads, and
  wrapping the module in sudo would make a machine without the drop-in unable
  to report its own state.

Both dry-runs (`ALARMS_DRY_RUN`, `WAKE_DRY_RUN`) default off as of 2026-08-11;
`NOTIFY_ENABLED` gates the banner. Tables: `alarms` (NOT droppable — the only
handle on an item sitting on the phone) and `notifications` (droppable; a
delivery log, not a fact).

### Scheduled prompts (`app/schedules.py`, `lib/schedules.js`)

A prompt that opens a new tab at a time, once or on repeat (Settings →
Scheduled prompts). It's an agent doing a job at a time, which lazy-over-
scheduled doesn't cover: a card can wait to be opened, but a digest you asked
for at 8pm can't. The backend only says what's due. The page opens the tab
through `handOff`, the door open_tab uses, so the working-tab cap, the queue
and the tab's model apply unchanged. **Due → claim → ran**: the claim is a
conditional UPDATE, so two windows can't both open a prompt, and a claim that
never reports back lapses after five minutes.

**The lid.** Asleep in a bag, nothing fires. `wake.py` is deliberately not
used. A dark wake keeps the display off and the page's timers frozen, and an
agent turn outlasts the wake, so the §21 argument ("a missed wake costs
freshness, never an alert") doesn't carry over. Instead, a missed time runs
**once** on the first poll after you're back, if it's within 12h of when it
was due. Past that, a repeating prompt skips to its next time. A one-off still
runs late, since you asked for it once. The brief says when it was due and
how late it is.

## Voice (`app/voice.py`, `frontend/src/lib/voice.js`)

Ears and mouth for the console. Claude supplies neither — the API has no audio
content block — so both transducers are ours, and both are local: audio never
leaves the laptop, only the transcript does (as an ordinary console message).
Nothing is persisted; there is no voice row in the source-of-truth map because
voice owns no datum.

**Three-stage gate.** The 600M-parameter ASR model must never run all day, so
nothing reaches it until two cheap stages agree: Silero VAD (~1MB ONNX, CPU) →
openWakeWord spotter (ONNX — no tflite wheel exists for Apple silicon) →
`parakeet-mlx` (`mlx-community/parakeet-tdt-0.6b-v3`, Metal) only on
the audio after the word fires. Cost of the always-on half is ~0.7ms per 30ms
frame, so the gate runs on the event loop; transcription is a task behind a lock
(the lock also covers the one-time model load). NVIDIA's Nemotron ASR is better
but had no Metal path as of 2026-08-04.

**The phrase is "hey kestrel."** openWakeWord ships six models and none of them
is ours, so that name is a model we train (`docs/wake-word.md`); until it
exists the config stays on a pretrained phrase. `VOICE_WAKE_WORD` takes either
form — a bundled name or a path to a `.onnx` — and the phrase the UI says out
loud is the file's **stem**, so the name is written down in exactly one place.
Spotting scores `max(scores.values())` and never keys on the name, so nothing
downstream cares which form is configured. `wake_ready()` is checked by the
status route rather than at socket-open, because a model that hasn't been
copied over yet would otherwise fail inside `Listener.__init__` — far from the
config line that caused it.

`voice.Listener` is the whole state machine and takes frames in, events out —
no socket — so endpointing is testable: pre-roll ring (~1s), min-speech floor
(a cough is a word to a model with a decoder), silence timeout, max utterance,
wake timeout for a false positive in an empty room.

**Where the audio lives, and why it's the browser.** Capture is
`getUserMedia({echoCancellation})` — the OS canceller (macOS Voice Processing
I/O), which removes *what the browser renders*. So synthesized speech is made
backend-side (Kokoro-82M, `say` as fallback — see below) and shipped
to the page as base64 WAV to be played through the same `AudioContext`: that is
what puts it in the AEC reference signal. Without it, on open speakers, a reply
that reads the wake word aloud summons the console. That matters because
spotting keeps running while `speaking` is true — saying the phrase again is
how you interrupt a reply. `Listener(barge_in=False)` is the fallback for a
room where cancellation loses: it makes the console deaf until it finishes
talking.

**Barge-in is the wake word, not speech.** It used to be bare VAD — 400ms of
anything speech-shaped while the console talked opened the mic, no phrase
needed. In a room with any noise in it that fired at the end of almost every
answer, because a reply carries 900ms of still-speaking grace past its last
sentence, and the transcript auto-sent. The trigger is now the phrase itself,
so a wake hit during `speaking` emits `barge_in` (stop the mouth) ahead of the
usual `wake`/`listening` pair, and there is one way into the microphone instead
of two.

**Endpointing is two thresholds, because one can't win.** The pause after a
finished sentence and the pause inside "so, um... the Ellis thread" are the
same signal to a VAD and opposite things to a person, so a single
`VOICE_SILENCE_MS` is forced to choose between cutting people off and feeling
sluggish. `Listener.silence_needed()` picks per-frame instead: below
`VOICE_SETTLED_SPEECH_MS` of accumulated speech the turn is still being
assembled and gets `VOICE_SILENCE_PATIENT_MS`; above it, a pause probably
means done and the short `VOICE_SILENCE_MS` applies. It reads *accumulated
speech*, never elapsed time — otherwise hesitating longer would end the turn
sooner, which is the exact failure being fixed. An interrupted turn is patient
on the same rule and pays nothing for it: `barge_in` already fired at the
trigger, so the console stopped talking and only the sending of your words
waits. Push-to-talk never reaches this code, and remains the tool for composing
out loud.

**Speech out is two engines, and the second one is the floor.**
`VOICE_TTS_ENGINE` selects Kokoro-82M (82M params, mlx-audio, Metal) or macOS
`say`, and `synthesize()` dispatches between them behind one signature: WAV
bytes out, which is all the playback queue, the barge-in coupling and the
`speaking` flag downstream have ever known about. Kokoro is not a marginal
upgrade — `say` is concatenative synthesis with a prosody rule set and no
emotion model in it anywhere, which is a ceiling no amount of voice-picking
raises. Measured on an M5: ~0.15s per sentence warm, 16-17x real time, *faster
than `say`*; the 2.4s first call is pipeline construction and Metal kernel
compilation, so `voice.warm()` pays it in the background when a session asks
for speech rather than on the opening sentence of a reply.

`say` stays because a synthesiser that can fail needs one that can't
underneath it. Any Kokoro failure — model absent, misaki absent, a voice name
that doesn't exist — returns None and falls through, because speech-out
degrading is annoying and speech-out taking the reply stream down is not. The
one exception is an engine named explicitly through `/api/voice/speak`, which
is for auditioning: silently answering with the other engine would make every
comparison a lie.

The voice namespaces are disjoint and deliberately not reconciled. Kokoro's
`bf_emma` and `say`'s `Serena (Premium)` name nothing in common, so the
fallback drops the override rather than forwarding it — `say -v bf_emma` does
not fail, it quietly substitutes, which is the worst of both. Kokoro's G2P
language is derived from the voice prefix (`bf_`/`bm_` British, `af_`/`am_`
American) instead of configured separately, since the failure mode of drift
there is a voice that just sounds slightly wrong forever.

**Siri voices are not reachable and this is structural.** The system voice can
be set to one — `com.apple.siri.natural.Catherine` — and it will appear in
neither `say -v '?'` nor `AVSpeechSynthesisVoice.speechVoices()`. Naming one
gets the unknown-voice fallback, byte-identical to naming a voice that does
not exist. Nor is there an Australian upgrade path: the downloadable-voice
catalogue offers one en-AU voice, Lee, who is male. This is most of why speech
out moved off `say` at all.

**Privacy is the armed flag, not a promise.** An AudioWorklet buffers 480-sample
frames but the socket only carries them when `wake || ptt || playing` — with
wake off, nothing is transmitted unless you're holding the button or the console
is mid-sentence.

**Speech out is per sentence off the `text_delta` stream**, not end of turn:
that puts the first words in the air about a second in. `SpeechPump` (in
`console.py`) buffers deltas, cuts on sentence terminators, and drops what
doesn't speak — tables, code fences, and markdown marks are on screen anyway,
and `say` reads `**` as "asterisk asterisk". A silent tool call gets an earcon.
`VOICE_APPEND` rides only when voice is on (fixed at session start, client
reconnects to change it) and asks for one plain leading sentence.

**Dropping markdown is not enough, because what's left is still written
English.** `_normalize_speech` runs at the tail of `speakable()` and is the
one place that function substitutes rather than drops: misaki has no phoneme
for a colon, so "3:30" arrives with a hole in it and "2:00" comes back "two
zero zero"; `Jul`, `Thu` and `Apr` are mispronounced rather than spelled out;
`ET` is read "ee-tee". Measured over 1094 sentences of real console replies,
17% carried at least one such defect. The rules are pronunciation fixes and
deliberately not rewrites — each replaces a token with how that same token is
read aloud, so the weaker invariant still holds: nothing is *said* that isn't
a reading of what's on screen. Anything a listener would have to be told is
`VOICE_APPEND`'s job, not this pass's.

Two things the corpus settled that guessing got wrong. Dropping the left half
of any `Capitalised/Capitalised` pair to resolve `America/Los_Angeles` also
turns the workout split `Pull/Legs` into `Legs` — the rule is anchored on the
real IANA regions instead. And `$45`, `40%`, `RSVP`, `BBQ` and the *spaced* em
dash all already speak correctly; only the tight dash between alphanumerics is
a defect, so the pass leaves the rest alone rather than churning them.

**One playback chain, and the pending flag is not the source node.** Sentences
are decoded and played one at a time, chained on `onended`. The obvious way to
ask "is anything playing?" is to check the source node, and it is wrong:
`decodeAudioData` is async, so between shifting a sentence off the queue and
`start()` there is a window where the node doesn't exist yet. A sentence
arriving in that window started a *second* chain — two of them reading the
reply over each other, and each time one of them found the queue momentarily
empty it declared speech out idle, which after `PLAY_IDLE_MS` closed the
`AudioContext` on top of the other one still talking. That is what "it stopped
halfway through" was. Kokoro is what made it constant rather than rare:
synthesizing at 16x real time means sentences arrive in bursts, and the burst
lands exactly in the window. So `decoding` covers the gap the node can't, and a
`generation` counter strands decodes that a stop or a teardown outran — the
same bug from the other side, where a sentence starts playing after the stop.

**Barge-in is a new trigger on the existing interrupt path**, not new machinery:
the leaky speech-run integrator fires `{"type":"interrupt"}` — the same message
the Stop button has always sent. Two things it taught us: a spoken sentence is
not unbroken voicing (a strict run counter reset on the comma in "stop, that's
wrong" and fired half a second late), and the mouth outlives the model's turn,
because the result lands while a dozen sentences are still being synthesized —
so the interrupt closes the pump whether or not a turn is still running, and
`flush()` waits on the worker rather than on the queue so it can be cut short.

**The upload refusal still stands.** `providers/attachments.py` refuses audio
files; a live mic is the other case, because you are in the room. The line is
held in what auto-sends: push-to-talk lands in the composer to be read first,
only summoned turns send themselves — which is now exactly the turns you said
the wake word for, interruptions included.

**Working in a browser tab does not mean working in the app.** The window is
WKWebView, and WebKit routes `getUserMedia` through a `WKUIDelegate` method
pywebview 6.2.1 doesn't implement — an unimplemented delegate method isn't a
denial, it's silence: the promise never settles, so the mic button lit up and
nothing failed anywhere. `desktop_shell.allow_media_capture()` adds the
selector before the window is built, and `scripts/make-app.sh` writes
`NSMicrophoneUsageDescription` into the bundle (the second, independent gate —
without it macOS refuses regardless of what WebKit decided). Both are outside
the frontend, so **re-run `make-app.sh` after touching either**; a stale bundle
carries a stale Info.plist. On the JS side `start()` races `getUserMedia`
against a 10s timeout, because a request nothing ever answers has to become an
error the UI can show.

## Recurring lessons

Every one of these was found twice before it was written down; each names where
it came from so the argument is recoverable. They are here because they cut
across sections — a rule that belongs to one section lives in that section.

**On checks, not prose** (11-17). A rule an agent keeps breaking becomes a
check that fails, in the tab that broke it: `.claude/hooks/lint_on_edit.py`
(PostToolUse on Edit/Write, per-repo, exit 2 hands the errors back mid-turn)
runs ruff (`backend/ruff.toml`: pyflakes, `date.today()` banned in favour of
`db.day_key()`) or ESLint (`frontend/eslint.config.js`: raw `fetch` outside
`lib/api.js`, raw `setTimeout`/`clearTimeout` outside `lib/timers.js` and
rules-of-hooks as errors, you-might-not-need-an-effect as warnings). The
best check's error message names the fix, so the tab that trips it doesn't have
to go looking: the timer ban's lists the helpers. What can't be fixed at once is a ratchet that only shrinks:
`tests/test_private_imports.py` (cross-module `_private` imports and
`module._private` uses, as of 11-17) and `frontend/lint-baseline.json` (155
useEffect calls on 11-17). `tests/test_lint.py` holds both linters in pytest. A
deliberate exception is a `noqa`/`eslint-disable` with its reason on the line.

**On delays** (11-22). A timeout is how an agent papers over an ordering bug:
wait 300ms and hope the state landed. setTimeout went from a ratchet (34 calls)
to banned in one day, and six of the seven suspects were waiting on something
they could name. Wake-after-report waited for the render that cleared `busy`,
so it moved into the effect that runs after it. The `limited` emit a tick late
waited for a subscriber, so the turn bus replays sticky state to a late one.
The map's click-suppress relied on click firing before a 0ms timer, so a
pointerdown lifts it now. Finance's 1.5s poll waited on a background sync it
couldn't see, and on a reconnect it exited early, so the POST now answers after
the sync. The orphans reload waited 4s on a socket's release(), so
`/api/console/orphans?released=` waits on that release
(`console_worktree.wait_released`). The usage chips' 4s after a turn is the one
that's real time: Anthropic's API counting the turn fires no event. What's left
is time itself, behind `lib/timers.js`: `useDebounce` (search boxes),
`useTimeout` (flash, toast, backoff, poll; cleared on unmount), `timer()`
outside React, `batch` (coalesce a burst) and `withTimeout` (a promise that may
never settle). Before you add a delay, find what it would be guessing at.

**On absence.** Nothing here can tell "no data" from "not collected" without
being told which it is.

- *Silence is a signal, not a failure* — but only if the source would have sent
  something. A phone at home produces no location fixes, so staying in is
  byte-for-byte identical to Overland having died; the tie-breaker is where the
  **last** fix was. A source that cries broken every time you spend a weekend
  at home is how a status panel gets ignored (§10, 08-05).
- *A constant about the world is a measurement with no error bar.* `HOME_LAT`/
  `HOME_LON` silently assumed the user never moves. They moved cross-country, and
  every prior day was scored against the new address thousands of km away — so
  `time_at_home_min` read a flat 0.0 for a month of days spent at home, twelve
  of which reached the correlation matrix and invented a finding there. The
  config was never wrong at any single instant, which is exactly why nothing
  caught it. If a constant describes the world rather than the code, ask what
  happens when the world changes; if the answer is "wrong numbers, no error",
  it needs a date (`app/places.py`, 09-14).
- *"I can't measure it" is not "it measured zero".* `attachments._rss_bytes`
  sampled a child's memory by forking `ps` and returned 0 on any failure —
  fail-safe by intent, fail-open in fact, because forking is what Darwin
  refuses once a child is eating the machine. The guard went blind exactly
  when it mattered and a test bomb took the laptop into swap. A watchdog whose
  sampling costs the resource it guards must treat an unmeasurable subject as
  over the limit, not under it (09-14).
- *An empty list is a claim.* A function that manufactures one from an error is
  lying to any caller that acts on absence — fine for a card, fatal for a
  reconciler (§21 step 3, 08-07). Same shape as gap-not-zero in the feature
  matrix, `_RETENTION_GATES`, and the three-state alarm protocol; the first
  time it appeared *inside* a helper rather than between stores.
- *A gate must sit where the answer changes.* Marking a day "computed" because
  *some* feature row exists hid five days of location data that arrived late,
  and a `_PASSTHROUGH` with no coverage gate let a single fix enter the matrix
  as a real day indoors — so the card and the matrix disagreed about the same
  date (08-05).
- *…and it must measure the axis the numbers rest on.* The second time, the
  gate was in the right place and counting the wrong thing: location admitted a
  day on how many fixes arrived, when every metric it guards (km, places, time
  at home) is a whole-day quantity that rests on how much of the day was
  watched. The two come apart in both directions — 121 fixes inside 2.4h of one
  drive sailed through, while a 4-fix day with twice that coverage was refused
  — so the gate wasn't loose, it ordered days wrong. Coverage subsumed the
  count rather than joining it, because with gaps capped you cannot reach the
  floor without the fixes anyway (§10/§11, 08-30).

**On reach.** A thing the system can do but cannot be *asked* to do does not
exist from inside a session.

- *A capability with no tool is indistinguishable from one nobody built.* The
  console answered "there's no write path" about a module that had shipped
  thirteen minutes earlier (contacts, 08-11), and about `search()` the same day
  its provider landed (reddit, 08-12).
- *When creation belongs to an upstream pipeline, everything that pipeline
  can't see has no way in at all.* An amender is not a writer: `package_update`
  could only touch rows the email extractor had made, so three real parcels off
  someone else's Amazon account had nowhere to land (08-04), and the same hole one
  store over hid a move flight from `trips` (08-05).
- *A store the console can read but not write does not stay unwritten* — its
  contents land in whichever store does have a writer, and then the wrong write
  holds the rest of the instruction hostage (four debt balances filed into a
  task's note, which then correctly blocked closing the task, 08-11).
- *A feature gated on a credential should gate only the part that uses it.* The
  check-in task and `_notify()` were both one line inside a loop that returned
  early when `AEROAPI_KEY` was unset, so the halves needing no external service
  never ran (08-05, 08-10).
- *The names a prompt uses must be names the model can look up.* `SYSTEM_APPEND`
  wrote `debt_add` while the tools are `mcp__vault__debt_add`; 29 of 153
  ToolSearch calls across 88 sessions returned nothing, 15/15 of them on a bare
  name (08-12). Same shape as `is_safe_read`'s 0% hit rate over 111 historical
  Bash commands (08-03) — a prompt prescribing a form the far side rejects, and
  like that one, invisible until someone replayed the sessions instead of
  reading what the prompt says.

**On verification.** What a green test measures is rarely what you think.

- *Green tests on a subprocess boundary measure the argument list, not the far
  side of it.* 43 contacts tests passed while every write path sat unexecuted
  behind a TCC grant that needs a physical click (08-11). Same trap as a
  passing vite build proving a render, and as G2P checked under a config
  production doesn't use.
- *A stub is an interface.* `def boom(args)` kept a failure-path test green
  until `_schedule` started passing `root=True`, at which point the test was
  catching its own `TypeError` and had stopped testing its subject (08-11).
- *A test that never takes the path proves nothing.* The first
  `_RETENTION_GATES` test passed against a day that was never stale enough to
  reach carry-forward at all (08-05).
- *"The reconcile is stubbed" is not a rule; "nothing in `sync_all` touches the
  real DB" is.* A fixture that stubs one caller by name lets the next one
  through — and that suite runs against a migrated copy of the real app.db
  (08-10).
- *Verify against the source, not the return value.* Alarms were confirmed by
  reading the actual iCloud list, wake by reading `pmset -g sched` back, and
  the failure path by taking a real `sudo` refusal rather than assuming one.

**On identity.**

- *A derived form is not the handle you mutate by.* Deleting a contact's phone
  number matched nothing and reported success, because the read path's
  normalized number is not what AppleScript's `whose value is` compares
  (08-11). The `tasks.md` rule, one store over.
- *A key built from user data must be injective.* Escape the delimiter, never
  strip it — and reject vs. escape is a judgement about whether the part is an
  id or a value (`app/keys.py`, 08-07/08-10).
- *A parameter the model can set but the card can't honour is worse than an
  absent one* — `cadence` on a habit whose `_streak` counts consecutive days
  would draw a broken streak for a weekly habit (08-12). Its sibling: a
  parameter accepted and ignored (`compute_leaves(day)` hardcoded "today").

## Concurrency

app.db is written by the backend AND by skills running in terminal Claude Code.
Both are safe because every connection sets `PRAGMA journal_mode=WAL` and
`PRAGMA busy_timeout=5000`. Any skill using the sqlite3 CLI must run the
busy_timeout pragma first (it's in the console system prompt as a reminder).

Entering WAL is the one thing busy_timeout can't protect. The rollback→WAL
conversion needs an exclusive lock, and SQLite deliberately skips the busy
handler on that upgrade (it would deadlock), so two connections converting
simultaneously means one fails outright with "database is locked". The mode is
persistent in the file header, so this is a fresh-DB-only race — but the test
suite creates fresh DBs constantly and hit it about 1 run in 20. `db._enable_wal`
handles it: read `journal_mode` first and return immediately if it's already wal
(every connection after the first — one read, no lock, no write), otherwise take
an in-process lock and retry the conversion until busy_timeout worth of time has
elapsed, which also covers the sqlite3 CLI converting from another process.

## Security posture

Localhost-only, this laptop only: backend binds 127.0.0.1, CORS allows the
Vite origin. The console is remote-code-execution by design — if this ever
leaves the machine, access must be network-level (Tailscale/WireGuard), not a
password form.

**One exception since 2026-09-22: the phone door, below (2026-11-23).**
Location is the only *pushed* source, so
something has to be reachable from a phone that isn't on this network — and
that something is no longer this machine. OwnTracks posts to the location
relay (`relay/`, a Cloudflare Worker + D1 queue); the poller pulls from it.
The relay holds two tokens: the phone's can only add points, the backend's
reads and deletes, so a leaked phone config can't read history back out.

History worth keeping, because each step failed the same way. Overland over the
tailnet (to 2026-08-14) needed Tailscale on the phone: three switches on at
once, and three failed independently in a week. OwnTracks → Tailscale Funnel →
a separate write-only listener process on :8790 (to 2026-09-22) dropped the
phone's VPN but kept the Mac's: Mac awake, Tailscale connected, Funnel
configured, listener up — and when Tailscale quietly stopped, location went
dark for four days with every dot reading ok. **A push chain is as reliable as
its least reliable always-on hop**; the fix was to put the always-on hop
somewhere that is actually always on, and make the Mac pull, so asleep or
offline only delays a sync. The relay also sends no reads back and exposes
nothing local, which the Funnel listener could only approximate by being a
separate process.

### The phone door: tap-to-approve from the phone (`app/phone.py`, New Features #97)

**Status: the first slice is built (2026-11-23), approved by the user as
proposed; it is off until switched on in Settings → Phone.** It is the first
exception since 2026-09-22, and was written down before any code. What
follows is the design as approved, with what was built marked where it
differs. Still nothing binds beyond 127.0.0.1. What is proven and what isn't
is at the end.

The want: when a tab stops on a card and you're away, the phone says so, a tap
opens that card, and Allow / Deny (later, a typed follow-up) works from there.

**1. Transport: Tailscale Serve, tailnet only, onto a second door.**
`tailscale serve --bg --https=8443 http://127.0.0.1:8791`. Serve terminates
TLS with the machine's ts.net certificate and proxies to loopback, so even
then nothing of Kestrel's binds beyond 127.0.0.1. Never Funnel. And never
:8788: the main app is remote code execution by design (console, terminal,
`db_query`, files), and proxying it would hand all of that to every device on
the tailnet. The phone gets its own small ASGI app, `app/phone.py` on :8791,
whose whole route list is the allowlist: the page, pairing, status, one card,
answer a card, push subscription. Reachability is then a property of which
process the proxy points at, as it was for the :8790 listener, and not of a
permission check someone has to remember on each new route. LocalGuard on
:8788 doesn't change and already refuses a ts.net Host.

**2. Who may call it: tailnet identity and a paired device, both.** Serve adds
`Tailscale-User-Login`; the door requires the owner's login. On top, a device
token: Settings shows a QR with a one-time code (10 minutes, voided after
five wrong guesses; the code rides in the link's fragment, which no server or
log sees), the phone trades
it for a 256-bit token, stored hashed in app.db (`phone_devices`), listed and
revocable in Settings. Scopes, per device:

- `status`: tab titles and states, and the open card's text (the same one-line
  detail the desk's card shows, capped).
- `approve`: allow or deny one open card by its request id. An id that is no
  longer open is refused, so a stale tap can't land on a later card. The
  answer goes onto the tab's `Link` inbox as the same `permission` frame the
  desk sends (`console_link.py` already keeps a tab's cards open with no page
  attached).
- `reply`: a follow-up message to an existing tab. **Not built**; no route
  for it exists on the door.

Not reachable with any scope: app.db, `db_query`, files, the vault,
transcripts, the terminal, settings, OAuth tokens, mode changes (a phone
can't switch a tab to never-ask), new tabs. Said plainly: `approve` plus
`reply` together are close to console access, because a reply is a prompt
and an approval lets it act. That's why both layers are required, why
`reply` is a separate switch, and why the first slice ships without it.

**3. The push.** Web Push (VAPID) from the Mac straight to Apple's push
service: outbound HTTPS only, payload encrypted to the phone's browser, and
still kept to "Kestrel: <tab title> needs an answer" plus the link, never the
card's detail. As built it doesn't hang off `notify.flush`, whose
`console-ask` rows are posted by the page: a watcher in `phone.py` reads the
open cards off the live Links every few seconds and pushes once per card
when one has waited 20 seconds and `notify.at_desk()` says nobody is there,
so it works with no page attached. `app/webpush.py` is the sender, written
against `cryptography` and held to RFC 8291's worked example byte for byte;
it will only POST to Apple's, Google's or Mozilla's push hosts, because the
endpoint is a URL a paired device supplied. iOS delivers Web Push only to a site added to
the home screen (16.4+) on a secure origin, which the ts.net name is. Not
chosen: ntfy or Pushover (a third party reads the text), iCloud Reminders
(§21's road reaches the phone, but a tap opens Reminders, not the card), and
the location relay (below).

**4. PWA, not native.** One static page from the door, a manifest and a
service worker: home-screen icon, push, a link that opens the right card. No
Apple developer account, no build chain. What this gives up is the lock-screen
view of running agents: Live Activities are ActivityKit and APNs, native only.
That third of the T3 feature is deferred.

**5. Lid closed.** A sleeping Mac runs no agent, so there's nothing to approve
that would progress, and its tailnet address doesn't answer. The push goes out
when the card appears, which is before the sleep. If the page can't reach the
Mac, the service worker shows the last state it had, says "Mac unreachable
since 14:02 (asleep or off the tailnet)", and disables the buttons. Answers
are **not** queued for later: an approval that lands hours on is the "you sent
that an hour ago" problem auto mode's guards exist for. No keep-awake: the
user chose "asleep is unreachable" over holding a sleep assertion.

**The risk this file already names.** "A push chain is as reliable as its
least reliable always-on hop", and Tailscale on the phone was that hop three
times. On 2026-11-22 this Mac's Tailscale was installed but offline, and the
phone was last seen on the tailnet 106 days earlier. So Settings → Phone
shows each hop as a step that is or isn't true (tailnet up, Serve pointed at
the door, a phone paired, notifications on) with the last phone contact and
the door's recent events (`phone_events`), because a missing hop otherwise
looks like a quiet day. A row in Sources is still owed. The failure is
silence on the phone, which is today's behaviour, and never a wrong action.

**The alternative, for the decision.** Route it through the relay instead:
the Mac posts open cards to the Worker, the phone talks to the Worker, the
Mac long-polls for answers. Nothing inbound at all and no VPN switch on the
phone. Against it: card text would sit at Cloudflare, and a public endpoint
would hold a token that can approve actions on this machine. Recommended:
Tailscale Serve. Approving is interactive, so a dead VPN shows itself at the
tap, where background location failed unseen for days; and the endpoint that
can say yes stays off the public internet.

**How it is built.** The door is a second uvicorn server in the backend's
own process and loop, because a card's answer has to reach the session
waiting on it and that is in memory (`console_link`). Its socket is bound by
`phone.sync()` and handed to uvicorn, whose own answer to a taken port is
`sys.exit`. `DoorGuard` runs before any route: Host is loopback or a ts.net
name, the tailnet login is the owner's, a write is JSON from the door's own
origin, a socket is refused. The page is four static files under a
`default-src 'self'` policy. An answer goes onto the tab's inbox as the
desk's own `permission` frame, and a `permission_answered` frame tells the
page at the desk, so the card settles there too. Plans and questions aren't
offered on the phone (a plan is pages, a question's answer is typed).

**Proven locally (11-23):** a real console turn stopped on a `task_create`
card; the door listed it; Allow from the door ran the tool and the row
landed; Deny refused it; the same id a second time was a 409; with the door
closed the page said unreachable and disabled its buttons; behind a stand-in
for Serve (`backend/scripts/phone_serve_sim.py`) the service worker kept the
shell, showed a delivered push with its link, and loaded offline.
**Not proven, because Tailscale was offline and no rig reaches Apple:**
Serve's own hop (which Host header it forwards isn't documented, so the
guard takes either), a real push subscription on iOS, and delivery through
Apple's push service, including whether it accepts the VAPID subject
(`PHONE_VAPID_SUBJECT`). Settings → Phone → Test notification is the check.

**Not built:** `reply`, the Sources row, lock-screen Live Activities.

### Backups (`app/backup.py`)

One app.db snapshot a month, into `backend/data/backups/`, taken by the poller
rather than a scheduler. Twelve kept.

The entry this replaces sat under "Deferred" for two months and read *backup
script done, cron it nightly*, which is a sentence about a script and not
about a backup. Nothing was ever cron'd. On 2026-09-19 the newest full
snapshot was dated 2026-07-22 — 59 days and 97MB of growth earlier — and from
inside the app that looked like nothing at all, which is this file's own rule
about absence arriving one layer below the data: a job that never runs and a
job that runs cleanly are the same silence.

Lazy rather than scheduled, for the reason the daily brief is: a 4:30am cron
on a laptop fires into a closed lid. The poller asks once a cycle whether this
month has a snapshot and takes one if it hasn't, so the copy happens shortly
after the dashboard is next opened in a new month — the earliest moment the
machine is demonstrably awake. The check is a glob over filenames, so asking
costs nothing and the filesystem stays the record (no table, no migration).

Two details carry the correctness:

- **The month is `db.day_key()`'s, and the filename's date is too.** A
  snapshot taken at 1am on the 1st belongs to the outgoing month, because
  day_key() says so until 4am. Naming it from the wall clock instead would
  file it under the incoming month while the due-check was still asking about
  the outgoing one, and the cycle would take a fresh copy every five minutes
  until dawn.
- **The copy is written aside and renamed.** A half-written file whose *name*
  says the month is covered is the one failure that turns a backup into the
  absence of one — the `.part` is removed on any exception, so the next cycle
  retries rather than believing it.

Retention is twelve snapshots ≈ a year ≈ 1.4GB at today's size. It was 14 when
the job was meant to be nightly and app.db was 18MB: **a retention count means
nothing without its cadence**, and this one was carried across a change of
cadence that never happened.

`scripts/backup_app_db.py` is the by-hand path (`--force` for a second copy in
the same month) and is a thin wrapper over this module, so the thing the
poller runs every five minutes and the thing you run before a migration are
the same code.

### The merge guard (`app/merge_guard.py`)

A dev tab's `worktree_merge` that adds entries to `db.MIGRATIONS` on Kestrel's
own repo is the one merge that rewrites app.db, which holds alarms and
location history that can't be rebuilt. Migrations are counted by
fingerprinting the list at both revisions (ast, `db.fingerprint`), so editing
db.py's helpers doesn't trip it.

1. **Before the fast-forward**, a snapshot goes to
   `backups/premerge-<head>-<time>.db` (`backup.copy_to`, five kept). If it
   fails, the merge is refused.
2. **A marker**, `backups/merge-check.json`, records the heads and the
   snapshot. Under the launcher (`KESTREL_MERGE_GUARD=1`) the push waits for
   step 3, so a merge that rolls back is never on origin.
3. **At the restart** (`launch_app.Backend.restart`), with the old server
   stopped, the launcher takes a fresh snapshot (so a rollback keeps writes
   made between the merge and the restart), then starts the new code. The new
   code must answer `/api/healthz?db=1`, which opens app.db and so runs the
   migration. If it fails, the guard stops it, runs `git reset --keep` on the
   main checkout to the old head, restores app.db (WAL and shm removed
   first), and starts the old code. If main has moved on since the merge,
   neither code nor app.db is touched, and the result says where the snapshot
   is. The tab's branch keeps its commits either way.

The outcome goes to `backups/merge-check-last.json` and a banner. Inside the
merging turn the server is still the old one, so read it in a later turn.
Without the launcher (terminal uvicorn) only steps 1–2 run, and the push goes
at once. The launcher is the .app's own process: a change to this half takes
effect when Kestrel is relaunched, not at a reload.

## Deferred

Reconcile sweep (stray checkboxes in notes → tasks DB).
