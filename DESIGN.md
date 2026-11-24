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

### Schema history (`db.py` MIGRATIONS)

Each migration's header in `db.py` says what it creates and what a column
means. Why a table exists, and the incident that produced it, is kept here.
Migrations replay in order on a fresh app.db, so none is ever deleted, and the
SQL of a shipped one never changes (`db.fingerprint` strips comments, so the
prose may).

- **v11–v12, task triage queue.** The migrated backlog landed with no
  defer/due/priority, so everything piled into "available" undifferentiated.
  v11 added a suggestion-plus-decision queue in email_triage's shape; v12
  removed it once that one-time cleanup was done.
- **v13, v37, v46, screen time.** Before v37, `screen.py` took `active_min`'s
  row presence to mean "fetched", so it wrote `active_min=0.0` on days
  ActivityWatch saw nothing (asleep, aw-server down, not yet installed), and
  `features` copied those false zeros into the correlation matrix; `features`
  backfills per day, not per feature, so v37 deleted them there too. v46
  dropped the provider (§7 above, 2026-09-13). Its three tables were the source
  of truth, not a cache, so the 332 days collected since 2026-01-01 were
  dumped to `backend/data/backups/screen-tables-*.sql` first.
- **v16, v35, Spotify identity.** The GDPR-export importer wrote ~266k `plays`
  rows with a synthetic `sha:` artist id, because neither export carries one.
  Grouping on `artist_id` split every artist in two at 2026-07-14 and pinned
  `novelty` near 1.0 for twelve days: a six-year favourite read as brand new
  the first time the poller saw it. v35 added `artist_key`, the casefolded
  name. `artist_genres` never held a row: the batch `GET /artists?ids=` 403s
  for this app, and Spotify then stopped returning `genres` at all, so genres
  come from Last.fm (`artist_tags`).
- **v20, location.** Overland (iOS) wrote the earliest `location_points`
  rows, which is why the SQL's column comments still name its fields; OwnTracks
  replaced it on 2026-08-14. Fixes first came to a separate listener process
  behind Tailscale Funnel, and since 2026-09-22 come through the Cloudflare
  relay (`providers/location_relay.py`).
- **v41, lists.** v39 gave `shopping_items` its own table because the vault
  had one buy-list. The next list wanted (packing for college, a party) would
  have been a second near-identical table or a `kind` column, so the list
  itself became a row.
- **v42, alarms.** The dashboard had no way to reach the user with the laptop
  shut. The answer isn't a notifier: it is writing a future-dated alarm to a
  surface the phone syncs, while the lid is open. The machine computes; the
  phone delivers.
- **v45, usage.** Nothing checked whether the desk's ranking was right or
  whether a card still earned its slot: seventeen cards rendered every
  morning, and some hadn't been worth reading since the week they shipped.
- **v47, debts remember being chased.** "Someone owes you $106" is true every
  day and actionable on none of them; the card took 44 impressions and 3 opens
  before it learned when it was your move.
- **v48, office-hours proposals.** A Piazza post saying "no office hours
  Thursday" is course data that arrives by mail and goes stale in the vault.
  An extractor confident enough to rewrite the hours on its own can send you
  across campus to a cancelled session, and you find out at a locked door, so
  proposals wait for approval. A later migration added `action='add'` after
  the CSCI 103 digest of 10-02 ("starting next week, OH Thursdays 12-2 in
  GCS") was read, judged a standing schedule, and dropped.
- **v49, sleep segments.** `health_days` held per-night totals only, while the
  `sleep` dataPoint health.py already fetched carried the full
  `sleep.stages` array, which the provider dropped (IDEAS, 09-13). The terrain
  sheet (09-05) is why timestamps keep their offset: converting stored UTC with
  the browser's current zone moved six years of evenings into the small hours
  after the desk changed coasts.
- **v53, visit labels.** At campus density a cluster is not a place: the
  busiest unnamed one spans 168m and holds Leavey Library, McCarthy Quad,
  Everybody's Kitchen, Birnkrant and the SSCI building. All three automatic
  routes were measured before the table was added: coordinates are p50 17m /
  p90 47m of error against ~15m of separation; time of day has no modes
  (visits run 08:00 to 02:00); of 71 visit/event overlaps 8 were spatially
  usable, with 48% geocoding to the campus centroid and 41% merely coinciding
  in time. So the label comes from the person.
- **v54, cite_refs.** Checking a fact the console restated meant searching
  Gmail for whatever you half-remembered the subject was.
- **email_sender_stats.** The model-only recommender agreed with 28.9% of 875
  decisions, against 78.3% for always saying "archive"; it recommended `keep`
  331 times and the mailbox owner kept 8. The prior comes from the record
  instead.
- **calendars.** With the name copied onto each event, renaming "Gym/Sports"
  to "Gym/Workouts" reached only the rows in `refresh()`'s [today-7,
  today+35] window, so `calendar_ids` (a SELECT DISTINCT over events) listed
  both names against one id, and a calendar with no cached events was
  invisible to it.
- **events.description.** The approval card for a delete could show only title,
  time and place, though the API returned `description` on every event and
  `_normalize` dropped it.
- **.docx re-extraction.** Four dietitian handouts sent on 10-16 had been
  filed `unsupported`, a cached verdict, before Word support landed.
- **`db.update_debt`** (not a migration). On 2026-10-11, merging nine split
  debt rows by settling them recorded nine payments that never happened.

Reasons removed from the migration headers in `db.py` (the headers keep the
one-line contract: derived vs real data, NULL meanings, backfills). Appended
to the list above:

- **Derived vs real data.** Most provider tables are derived caches (droppable;
  a re-sync rebuilds them). These are NOT, and a drop loses data: `tokens`
  (OAuth secrets), `plaid_items` (a real credential, never in responses or
  logs), `email_triage.user_action`/`user_label`, `plays` (Spotify returns only
  the last 50), `email_extract`/`packages`/`trips`, `people`,
  `location_points`, `net_worth_history` (fin_accounts is DELETE+INSERT each
  sync, so this is the only balance record), `alarms`, the place-suggestion
  evidence log, `brain.verdict`, `expenses`/`expense_reviewed`/batches,
  `activity`, `selftext_messages`, `scheduled_email`, `study` rows,
  `console_sessions`, `docs` bytes (the file at `DATA_DIR/docs/<sha>.pdf` is
  truth; `pages` is derived).
- **Day keys.** Behavioral sources (screen, spotify, messages, location,
  reddit, news fetch day) key by `day_key` (4am rollover: 1am texting belongs
  to yesterday); calendar/health/weather/finance use civil dates. `features`
  does not re-bucket: each source keeps its own key. News `day` is the fetch's
  day_key because Kite serves only its current edition (refreshes ~12:00 UTC);
  a pre-noon fetch is tagged under today instead of erroring.
- **v3/v5/v9/v40/v49 sync markers.** `calendar_days`, `health_sync`,
  `reddit_sync` exist so "no rows" (not worn, quiet day) differs from "never
  fetched". Reddit marks a day fetched even with zero rows so a quiet day isn't
  refetched on every read; drop `reddit_posts` and `reddit_sync` together.
- **v10, tasks v2.** New columns are nullable so v1 writers (skills, console)
  keep working; no status CHECK because skills write raw SQL; the trigger keeps
  `updated_at` right for the app and for terminal UPDATEs. `source` is who
  created the row, `source_kind/ref/excerpt` is provenance, and the excerpt is
  denormalized because the caches it could point at (emails, chat.db) are
  droppable.
- **v15 finance.** `fin_recurring.predicted_next_date` is Plaid's own
  prediction, not recomputed (Plaid already handles weekday/month-end drift).
  `fin_liabilities` normalizes per-kind upstream fields to one shape
  (statement_balance/min_payment/due_day/apr) so `bills_due` doesn't branch.
  v23/v28 (logo, location, website, counterparties) fill only on newly
  added/modified rows until a cursor reset re-pulls history; `counterparties`
  is the non-merchant parties pre-joined to a display string.
- **v17 messages.** Truly derived (chat.db keeps full history): a drop costs a
  re-sync up to `MESSAGES_BACKFILL_DAYS`. `text` is capped at 2000 chars; it is
  already on this disk, so caching changes no exposure. `messages` and
  `sync_state` have independent lifecycles: to force a re-sync clear BOTH, or
  the old watermark blocks the re-backfill.
- **v18 extractors.** `email_extract` caches raw model output per message
  (kind package|trip) so nothing is re-parsed or re-billed. `packages.status`
  only moves forward (ordered, shipped, out_for_delivery, delivered);
  `trips.status/gate` belong to the AeroAPI poller, and later confirmation
  emails update itinerary details but never downgrade them to 'confirmed'.
- **v19 people.** `person_key` is a lowercased email or the last 10 phone
  digits (same as `messages.normalize_handle`) so a person lines up across
  `emails.from_addr/to_addr` and `messages.handle` with no join table.
  `last_email_at/last_msg_at/last_event_at` are computed live in `people.py`,
  never stored. `emails.is_sent/to_addr` come from the existing day-window sync
  (Gmail after:/before: is not inbox-scoped): no second SENT pass; `to_addr` is
  the first recipient only.
- **v22 hidden.** Muting upserts a `people` row so it sticks for keys never
  CRM'd; the row is kept for reversibility; filtered from `waiting_on()`, the
  people list and brief signals.
- **v24/v25.** `chats` (group names) and `contact_clusters` are full-replace
  each sync with no watermark. Clusters come from macOS Contacts' own record
  grouping (`messages.sync_identities`): merge on a shared card or identifier,
  never a shared name.
- **v26 features.** A source with no data for a day is an absent row, never 0,
  so a real zero and "no signal" stay distinguishable.
- **v27 heart rate.** Hourly mean bpm; the API caps rollups at 14 days;
  `day`/`hour` are local so they align with `health_days`; folded into the
  `hr_var_day` feature. No sync marker (small window, recomputed each refresh).
- **v29 checkins.** `habit_checkins.source` ui|console|auto. Auto check-ins
  never overwrite a person's row (INSERT ... DO NOTHING); an un-check leaves a
  `value=0` tombstone so an auto pass can't resurrect a day the user cleared
  (the habit analog of travel's dropped-auto-task rule). value=0 reads as not
  done everywhere; streaks filter `value>0`.
- **v30 email undo.** Gmail mutations are one-way, so the action route records
  `{"unread": 0|1, "label_id": ...}` and `POST /api/email/{id}/undo` replays it
  in reverse. NULL on rows triaged earlier: best effort (back to inbox, read).
- **v31 dismissals.** Distinct from `people.hidden` (mutes a sender forever):
  dismisses ONE waiting thread. `through_ts` is the `last_at` dismissed, so any
  newer message resurfaces it with no un-dismiss. Keyed `(kind, key)` like the
  waiting feed: a person_key for messages, a `to_addr` for email.
- **v32 tapbacks.** A reaction THEY left is not them asking anything; one YOU
  left is you answering. Stored with a direction flag so the waiting feed reads
  that asymmetry, while volume metrics (sent/received/participants) exclude
  them (reactions outnumber real messages).
- **v33/v34 health and events.** `provider_health` holds only the latest cycle
  (one row per provider; a missing row means "hasn't run yet"; `fails` counts
  consecutive failures so a blip differs from hours down). `provider_events`
  is the append-only companion: state changes only (a source down all night
  would otherwise write 288 identical rows) plus sub-step failures
  `refresh()` swallowed (Plaid leg 500, chat.db schema drift, AeroAPI timeout).
  Trimmed to `EVENT_LOG_KEEP`. `provider_health.recent` is the last 24 outcomes,
  oldest first (o ok, x failed, . skipped), written by `heartbeat.record()`;
  past cycles were never kept, so it fills over two hours.
- **v35 artist_key.** Normalized in Python with `str.casefold` because SQLite
  `lower()` is ASCII-only ("Vîęzë Ãsbæk"); backfilled and kept current by
  `spotify._ensure_artist_keys()`; nullable so the DDL stays pure SQL.
- **v36 Last.fm raw reply.** The cleaned `tags` come from a blocklist, weight
  floor and cap, all judgement calls revised as noise shows (nationalities,
  "seen live", an artist tagged with its own name). Keeping the raw reply makes
  each revision a one-second offline `lastfm reclean` instead of a 9.8k-artist
  refetch. NULL = fetched before v36; skipped by reclean until refetched.
- **v38 attachments.** A "(photo)" label isn't addressable ("the packet Dad
  sent" needs a filename). `attachment_text` stays empty until something asks
  (images cost model tokens, PDFs parse untrusted bytes). `locator` is
  source-shaped: iMessage is an absolute local path (chat.db stores it
  '~'-prefixed; expanded on the way in), email is a Gmail attachmentId
  exchanged for bytes on demand. Nothing downloads during sync, so an inbox of
  hostile PDFs never lands on disk by itself. Text is capped at extraction;
  `status` separates "extracted, nothing there" (empty) from "never tried" (no
  row) and the ways parsing refuses (`providers/attachments.py`).
- **v39 lists.** debts/shopping/watchlist (and the day-one tag) left vault
  markdown for the reason tasks did: both a human and agents edited it, so
  neither copy was truth; old files are never read back. `debts.person` is free
  text with optional `person_key` (most owe from a beach week, not a thread).
  One row per debt (not a running total) so "$24 + $25.95" keeps two reasons
  and either half settles alone. `people.crew` keeps where a day-one is known
  from (Kramden / NCAAT / Triangle Insights Group), a different axis from close.
- **v40 reddit.** `day` is the day_key of `created_utc`, so a day's window is
  [04:00, 04:00) local, which is what `_sync` asks Arctic Shift for in epoch
  seconds. `score`/`num_comments` are NULLABLE on purpose: .rss-fallback posts
  have no score and the archive omits counts on removed posts; storing 0 for
  "unknown" is how screen got its false zeros (v37). NULL sorts last in `top`.
- **v41 lists.** `shopping_items` rows are carried over ids-and-all into the
  seeded 'Shopping' list because the console addresses items by id
  (`list_item_done`), and renumbering would strand ids in open sessions; the
  table is then dropped (nothing reads it). `lists.name` is UNIQUE NOCASE so
  "College"/"college" can't become two chips; items carry no such constraint
  (two "milk"s is legitimate).
- **v42 alarms.** Not derived: `external_id` is the only handle to what's in
  Reminders; lose it and alarms on the phone are unreachable and will still
  fire. Back up with app.db; never DELETE a row without withdrawing the alarm.
  `key` is `kind:...` (built in `alarms._alarm_key`); the reconciler partitions
  by kind so a source that failed to read only holds its own alarms.
  `fire_at` is offset-aware ISO because an alarm promises an instant and the
  row can outlive a move across TIMELINE; a bare '09:00' would change meaning.
- **v43 notification queue.** Alarms are for known future times (the phone is
  handed them hours ahead); this is for a fact that just became true and
  matters within minutes (a flight slipped, a package out for delivery).
  Persisted because dedupe must survive restart (`--reload` would re-announce an
  in-memory set) and because a banner posted while nobody is at the desk looks
  seen; the queue holds until presence says otherwise. `key` (`app/keys.py`)
  identifies the fact, not the moment: a status re-observed every poll is one
  row; one that flaps is two facts. `sent_at`/`expired_at` are two nullable
  timestamps instead of a state string, so no state value exists that no code
  writes; queued = both NULL.
- **v44 failures.** `provider_health/events` cover only the poller; three
  other layers failed into a /tmp log: on-demand reads in `providers/api.py`, a
  widget throwing in render, a console turn returning is_error. One row per
  fingerprint (not per occurrence): a widget that throws re-throws every
  re-render, so an event stream would bloat the DB. `first_at/last_at/count`
  bracket it, and "what breaks most" is an ORDER BY. Derived, trimmed to
  `FAILURE_KEEP`, nothing reads it back.
- **v45 usage.** Counted per day, not as a running total: "still used?" can't
  be told from fifty opens last March vs five a week since. Distinct days is
  the honest unit; raw `count` is the weaker number. `band` is the tie-breaker
  because a card read at a glance leaves no click: unopened in `quiet` every
  day for a month is dead weight, unopened in `needs`/`has` is read as
  designed. kinds: seen (rendered, band set) | open (unfolded a card the desk
  had folded, i.e. the ranking being told it was wrong) | sheet | command.
- **v46 screen drop.** `IF EXISTS` on all three tables: a fresh app.db built
  after v46 never created them and a plain DROP would fail first boot. v13
  stays so replay creates what v46 removes. v13 keyed on `day_key`, per
  (day, host, app) in `screen_apps` so `apps`/DOOMSCROLL_APPS matching didn't
  re-walk raw events.
- **v47 debts.** An open IOU is two facts: the money, and whether you've said
  anything lately; without the second the card could only sit in HAS.
  `reminded_at` NULL = never asked (act now), recent = their turn, older than
  `DEBT_NUDGE_DAYS` = unanswered, yours again; stored as a timestamp, read via
  `day_key`. `reminded_count` isn't used by the nudge but can't be rebuilt from
  one overwritten timestamp, and decides whether to keep asking or write off.
- **v48 proposals.** `quote` is required: the proposal is only checkable next
  to the verbatim source sentence. It is email body text, which the extract
  pipeline otherwise never stores (`email_triage.body_excerpt` fetches
  transiently); one sentence per proposal, deleted with the row. Rejections
  are kept so the same message isn't re-proposed next cycle (`email_extract`
  can't carry that: one message can propose amendments for several dates).
  Approving calls `set_exception`; the vault stays truth for the hours.
- **v49 sleep.** Keyed on the wake-up civil date like health_days' sleep_*
  metrics so a night joins its own totals; naps land on the same day, so
  `seq` orders segments across the whole day (the sheet draws a day, not a
  session). Timestamps are local ISO8601 with offset because the hypnogram is
  read against the clock you slept by. The two `health_sync` marks (segments
  ride the daily sleep fetch; hourly steps are their own rollUp call) let an
  empty read mean "asked, nothing there": drill-downs fill on demand, and
  without a mark an unworn night re-hits the API on every open.
- **v50/v51 tiles and geocode.** The frontend can't fetch tiles (image hosts
  are outside the CDN allowlist, and it shouldn't hammer someone's tile
  server), so the backend fetches each once. Street geometry doesn't move, so
  no TTL; `fetched_at` is informational. `map_tiles` is the only blob table
  (~15KB/tile, a few hundred for a city you pan around). v51 keys reverse
  geocodes by lat/lon rounded to 4dp (~11m: finer than the fixes, coarser than
  centroid jitter between recomputes); answers you chose live in the vault as
  `type: place` notes.
- **v52 evidence log.** The place-name suggester's per-source weights can't be
  fixed (whether calendar beats OpenStreetMap depends on whose calendar and
  streets), so weights become the accept rate per evidence kind. One row per
  candidate SHOWN (one taken, others passed over), because the ranking needs
  "given a candidate of this kind, how often is it the one you take". Not
  derived: rejections exist nowhere else; a drop falls back to priors.
- **v53 visit labels.** In app.db, not the vault, because it is one assertion
  per visit (hundreds, keyed by an instant): the shape of a checkin. Stay
  boundaries come from append-only `location_points`, so a visit's start is
  stable; a label matches any stay whose span contains `started_at`, so a stay
  split by a range boundary keeps its label.
- **v54 cite_refs.** `ref` = truncated sha1(kind + target), not a counter: a
  counter needs session state at render time, renumbers across sessions, and
  makes a replayed transcript resolve to different rows. A derived ref is
  stable (the same email is `e7a3f1` forever) and doubles as validation: a ref
  the model invented doesn't resolve and the pill renders dead instead of
  linking confidently to the wrong thing. A collision would silently link
  wrong, so `target` is stored and the insert is OR IGNORE: a second target
  never overwrites the first, and `resolve()` returns the row actually handed
  out.
- **email_sender_stats `n` vs `obs`.** `obs` (decisions ever made about the
  sender) is evidence and doesn't decay; `n` (decayed) says which action is
  current. Collapsing them made a sender decided twice a day apart sum to
  1.992 and never clear a floor of two, and twice a quarter apart sum to 1.5.
  The backtest gated on a raw tally and used weights only to pick the winner.
  The table is an aggregate of `email_triage.user_action`, rebuilt by
  `rebuild()`; it exists because it is read on every queue render.
- **tasks.due_time.** `due` stays a day key so every `day_key()` comparison
  (`due == today`, `COALESCE(due,'9999')` ordering) keeps its meaning;
  widening it to a datetime would have broken them silently. The hour is a
  separate optional refinement; NULL = due that day, nothing overdue until it
  rolls.
- **location_computed.** `_ensure_days` treated "has location_days rows" as
  "computed", so a gap day (rightly no rows) re-ran a DELETE write transaction
  on every GET/summary/trace. One row per computed day; `points` is the fix
  count it was computed from, because OwnTracks flushes offline fixes late and
  a changed count triggers recompute. Backfill marks days location_days holds
  at current counts; gap days are marked on the next covering read.
- **Review labels and email_fts.** Review collapses to archive / unsubscribe /
  keep and files every message under a label from `email_triage.TAXONOMY`
  (afterward a message's only use is being found). `label`/`terms` are model
  output (order numbers, amounts, people, dates; regenerable); `user_label` is
  real data and what the label sender-prior reads. `email_fts` holds no body
  text; `email_search.ensure_index()` rebuilds it when `email_fts_state`'s
  signature stops matching. The porter stemmer was dropped because every
  search word is a prefix query and porter indexes "Dining" as "dine", which
  "dini*" can't reach; prefix matching covers "receipt" vs "receipts".
- **Expenses.** Real data: an expense is what you asked to be paid back (part
  of a grocery run, or cash that never touched a card), not a view of
  `fin_transactions`, so each row snapshots day, merchant and amount with
  `transaction_id` a soft link; the Plaid cache stays droppable. `batch_id`
  NULL = waiting to send; a sent row is frozen to match the file Dad has.
  `expense_reviewed` is per transaction, not a date watermark, because Plaid
  dates a posted charge by authorisation, so a swipe posting after an export
  lands below the date line unreviewed. Pending claims: Plaid posts a pending
  charge as a new transaction (new id, maybe new amount: tips, fuel holds,
  hotel deposits) naming the pending one it replaces;
  `expenses.follow_posted` moves the claim on that sync. `claimed_pending`=1
  until then (still 1 with the charge gone = a hold that never posted).
  `posted_amount` is set only on an already-sent claim that settled at a
  different number.
- **Snoozes.** `ask_id` is the card hook's stable key (`task:<id>`,
  `email:review`, `people`, `debts:<id>`): a snooze outlives a reload, not the
  ask; expired rows are pruned on the next read. `until` is a local ISO
  datetime.
- **Brain.** One row per fact, rendered into `BRAIN_DIR` as a read-only view
  (the tasks.md arrangement); facts cite `source_ref` with a verbatim `quote`
  so pages rebuild from sources. Only the user's ruling can't be rebuilt:
  `verdict` (confirmed|rejected) is NOT droppable; a rebuild deletes only
  unruled ingest rows; a rejected fact stays as a tombstone ingest is shown.
  status: active (told directly, or confirmed) | pending (read off a source,
  usable but labelled unconfirmed) | superseded (`superseded_by`) | rejected;
  `prior_status` is what a supersede overwrote so rejecting the replacement
  restores the old fact. `brain_runs` is the ingest/lint ledger and the
  watermark lazy ingest/backfill resume from. `source_label` ("texts with
  Jamie") is fixed at ingest so a page needn't join a droppable cache.
- **Office hours `add`.** A standing schedule ("starting next week, OH
  Thursdays 12-2") isn't a dated cancel/change. `when_str` = weekly rule
  ("Thu 12:00-14:00"), `day` = first date it applies, `role` (TA vs
  instructor, which `courses.set_hours` needs), `prior` = JSON of the entry an
  approved add replaced (set_hours amends a person's same-weekday block in
  place, so undo restores rather than deletes).
- **Owed beers.** A favor, not money: as a $5 debt it would inflate the
  owed-to-you total and be nudged like rent. One row per beer; the card groups
  "Mo ×2" and a check settles the oldest. Kept out of `debt_summary`.
- **Athletics, Brightspace.** Athletics: Sidearm iCal per USC sport, refetched
  past the feed's 2h TTL; `day` is the Pacific civil date; `start` NULL when
  the kickoff is TBA (DTSTART;VALUE=DATE). Brightspace: the D2L feed URL
  (`BRIGHTSPACE_ICS_URL`, a per-user token, in .env); `kind` due | available
  (release dates) | event (deadlines arriving without the " - Due" suffix); no
  submission status (D2L doesn't publish it).
- **Docs.** PDFs dropped on the console were read to text and thrown away, so
  answers could describe a page but never show it. Bytes are kept; `pages` is
  pypdf text per page (JSON array, index 0 = page 1); the citation ref (`d…`)
  lives in `cite_refs`, minted over `sha`.
- **Food.** `foods` key `(source, source_id)`: sr28 = the SR28 id,
  usc_estimate = normalized menu-item name (estimated once, shared by every
  hall/day), custom = "venue|normalized name". Every source sets `source_id`,
  so the one unique index dedupes all. Nutrients are a JSON object per
  serving; unknown is absent, never 0. `portion_conf`: NULL = amount stated,
  0.6 = guessed (photo, vague line), 0.9 = the one portion question answered
  (portion is where most calorie error comes from, so a range uses both it and
  `foods.confidence`); `ask` = the pending portion question (JSON). Backfill:
  entries whose console-written note says guessed/estimated. `weigh_ins.at`:
  entry time so non-morning weigh-ins show; not backfilled.
- **Study.** One row per answered item, posted by `StudyBlock.jsx` "Send
  results"; truth for "which lectures have I drilled" (the vault review log
  holds what was wrong, this holds what was attempted). `block` = hash of the
  fence source so a resend replaces. `doc_sha`/`page` come from the item's
  `slide` (else `cite`) ref. `confidence` dont_know|guess|unsure|sure, NULL =
  unsaid and for cards (right/wrong alone can't tell a slip from a
  misconception; wrong-and-sure is the misconception). `confidence_late`=1:
  rated after the verdict was on screen, kept apart because it can be fitted
  to the verdict.
- **Read receipts.** chat.db stamps `date_read/date_delivered` in place AFTER
  arrival (ROWID doesn't move), so the watermark sync never sees them;
  `messages._sync_receipts` refreshes a recent window and backfills once.
  Outbound = when they read yours (NULL forever without iMessage receipts);
  inbound = when you read theirs. `is_read/is_delivered` are kept because
  chat.db sets the flag without a date on a few percent of rows.
- **Scheduled email.** Gmail has no scheduled send: the draft in Gmail is the
  content (edit it before `send_at` and the edit goes; delete it and the send
  404s and records `cancelled`); `email_schedule.send_due()` calls
  `drafts.send`. A lost row is a mail that silently never goes.
- **Activity log.** `activity` is every unprompted backend write; `ref` is
  the JSON undo handle (task id, calendar event id, food_log ids); `origin`
  groups rows from one self-text burst so redo finds its source. A burst is
  texts to yourself sent close together ("photo", then "for the party
  friday"), routed as a unit; `selftext_messages` records which burst each
  went into and is the already-routed mark.
- **Project folders.** Picker grouping only: a project stays where it is on
  disk (hardcoded paths and `~/.claude/projects` history key on the path). A
  folder exists while something is in it; no row = no folder; rows for gone
  directories are ignored.
- **.docx** (continued). `attachment_text` is derived, so the migration just
  deletes the cached `unsupported` rows; `extract()` never retries a row.
- **console_sessions.** The transcript knows model, tokens and cwd but not
  whether a vault session was a general or study tab, nor the tab title, and
  `console_work` rows go when the work does.
- **Fingerprints.** Migration identity used to be list position, which
  stopped being unique the first time two branches each added a 40th
  migration: `schema_version` said 40 on both and each skipped its own 40th.
  Identity is now the SQL (comments and whitespace stripped), so prose edits
  don't re-run a migration. `_adopt` hands an old counted DB over by recording
  the first `v` fingerprints; when the DB is counted past this checkout's list
  (another branch migrated it) it returns None, because claiming the missing
  scripts makes their branch re-run them into "table already exists" and
  ignoring them reuses their slot. Counting continues for that process;
  opening the DB once from the longer branch completes the hand-off.
  `schema_version` is now display-only and never goes backwards.
- **Migration lock and WAL skip.** `connect()` runs `_migrate()` on every
  connection and the poller opens many in parallel threads, so a process-wide
  lock serializes the first migration (this process is the only migrator;
  skills only query via the sqlite3 CLI). A path-keyed set of already
  WAL-and-migrated DBs skips four statements per open on a call made thousands
  of times a day (keyed by path so test fixture DBs still migrate).
  `busy_timeout` is set BEFORE `journal_mode=WAL`, since WAL needs a write lock
  and fails at once without it.

Tables added by the model/accounts work, formerly commented inline: `claude_accounts` (uuid = the CLI's accountUuid; `profile` = the CLI's oauthAccount JSON written back to ~/.claude.json on a switch; caps are percents, NULL = no ceiling; `active_since` / `claimed_idle` = when Kestrel took it and whether its 5h window was idle then, for the idle-only rule), `claude_account_state` (mode auto|hold, desired uuid, login_at), `claude_account_events` (switch log, newest 200). `model_catalog` and `claude_model_access` are derived and droppable (live lists; per-family one-token probe or refused-turn answers per saved account); `model_defaults` is the user's, keyed `mode:dev`, `tier:grunt`, `provider:codex`. `delegate_accounts` (one row per sign-in; `home` under `~/.kestrel/agents/accounts/<id>`; `allowed_for` comma list; `monthly_credits` = plan allowance for the meter; `credit_cap` = stop once this many are used in the month; `window_cap` = stop when a rate window reads at or over this percent; `delegate_runs.account` is NULL before accounts or on an API key). `model_prices` (derived; one row per (source, model id), $/M tokens, `long_*` = the tier a prompt over `long_above` tokens bills at) and `model_price_fetches` (each source's last try at/ok/note and last good `good_at`, whose rows stay when a fetch fails); `delegate_runs.tokens_cached` = the part of `tokens_in` the CLI said was cache reads (NULL when it doesn't say).

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

- `foods.source` is one of: `sr28` (USDA reference, per 100 g), `usc_estimate` (a dining-hall item, `food_estimate.py`), `custom` (a venue item or home food, typed or from the model's knowledge), `off` (a barcode via Open Food Facts; added after the migration's comment was written). Tables: `foods`, `food_log` (one row per thing eaten), `food_profile`/`food_targets` (inputs and hand-set targets), `food_days` (a day closed by the user), `weigh_ins`; USC menus/venues are the usc_dining provider's derived cache; SR28 is a read-only checked-in file.
- A training day is the Exercise habit checked (Fitbit's AZM auto-checks it), or, before that, a gym-shaped calendar event; a surplus is only built with when there is training to build with.
- **Ranges, not false precision.** A guessed number shows as a range; half-width = (1 - confidence) * 0.7 of the value (0.8 -> ±14%, 0.6 -> ±28%, 0.4 -> ±42%), capped at ±45%; a published or corrected number (>= 0.95) has none. An entry's spread combines the food's (is the recipe right?) and the portion's (was it that much?) in quadrature since the errors are independent; a day's combines its entries likewise.
- **The scale as ground truth.** Logged intake minus the energy the weight trend says went into (or out of) the body is what you actually burn, in the units you log in. The gap vs the formula's burn is either logging running low/high or a wrong formula; the two can't be told apart and it doesn't matter for targets, since maintenance from your own log is right in your log's units. The gap is shown ("logging runs ~300 kcal/day low, or you burn more than the formula thinks") and the observed number replaces the formula, phased in as data firms up (weight = min(1, span/CAL_WINDOW) * min(1, days/14); a short slope on few readings is mostly water). Water weight swings a reading by a kilo or more, so the trend is a least-squares slope over up to four weeks of weigh-ins. Only complete days count (closed, or >= 2 meals and 1000 kcal): a half-logged day would read as eating less, the very bias being measured. Once calibrated, the weekly ±100 nudge stands down. The "7-day average" weight is given only while the last reading is recent.
- **Suggestions, extra detail.** Plates are pruned to the 22 best singles before combining (22C3 ~ 1.5k). A hall plate is up to 3 items, a café order 1-2. Identical item sets (same Positive Foods bowl at three cafés) collapse to one plate, and a hall serving the meal keeps a slot (three café bowls hid every hall on the first run). Overshoot of the kcal budget costs; a quarter under is free so a plate isn't padded with a cookie. Plain-first: the first version judged against the day's need and put 117 g protein on one lunch.
- **Corrections** (`food_fix`) keep the last 10 before/after records in `detail.corrections` so estimator bias can be measured later. A corrected or better-sourced row is never overwritten by a later estimate under the same name (a re-texted photo, the model re-creating it); equal confidence still updates (retyping is an edit); the user's typed numbers (confidence 1) always land.
- `match_known` matches only exact normalized name (at the venue if given): a photo of the usual bowl logs the usual bowl, a near miss is a new food, not a guess. A food logged without an amount returns at its last logged amount (after any fix): the usual portion, learned. A portion question is asked only for amorphous dishes (`_AMORPHOUS`), never an obvious single.
- **Seeded café menus (`app/food_seed.py`).** USC publishes no café menus or nutrition, so rows load from `food_data/`, each labeled with origin: `coffee_bean.json` (Coffee Bean & Tea Leaf's nutrition PDF via `scripts/build_coffee_bean.py`; exact, confidence 1); `panda_nutrition.txt` (Panda's page: macros, fiber, sugar, sodium exact; no vitamins/minerals, so `fill_micros()` estimates only those from an SR28 decomposition and says so in `detail.micros`; fiber/sugar/sodium stay official since every US label has them); `usc_dietitian.json` (label numbers USC Hospitality's dietitian emailed: Taco Taco, C&G smoothies and açaí, Katora curry bowls; confidence 1); `campus_menus.json` "published" (numbers with a stated origin) and "estimate" (real item names from USC's Balanced Dining guide with no numbers, estimated by `food_estimate.py` once per distinct name; "packaged" items are grab-and-go with a label, and a barcode or label photo makes them exact). Rows are `custom` foods keyed `venue|name`, the same key `food_create` produces. The seed writes only rows it wrote (`detail.seed`) and the user hasn't corrected; seeded rows whose item left the data (renamed/removed) are dropped unless corrected or ever logged. Diet marks from the guide are passed as tags (without them the vegan Beet Poke box came back with 32 g protein). Seed estimate failures share `food_estimates` retry rules under a `seed|` key so a name the model can't do isn't re-asked each poll. A usage limit during `ensure()` is reported as held, not as a failed refresh. Estimates are one per name copied to every venue selling it (Positive Foods bowls are at three cafés). Run: `python -m app.food_seed [--no-estimate]`.
- **Estimator details (`app/food_estimate.py`).** Once per distinct item name (menus repeat; "Scrambled Eggs" is on every breakfast), never per day. Steps: decompose (model gives a standard portion and 1-5 components with grams, constrained by the item's allergen/diet tags so a "vegan" item can't come back as chicken) -> match each component in SR28 (top candidates shown back) -> pick (model chooses a candidate id per component, or none). Nutrients = sum of picks scaled to grams, stored as a `usc_estimate` row (`source_id` = normalized name). Confidence 0.6; 0.5 when some component had no database match and used the model's own macros; 0.4 when the sum disagrees with the model's own calorie guess by more than half. The decomposition is kept in `detail.components` so "why does it think the burrito is 900 kcal" is answerable. An unmatched component contributes the model's macros so it isn't silently zero (the Beyond Burger once summed to 3 kcal from its lettuce); its micros stay unknown, so totals under-report those micros rather than invent them. Failures are recorded in `food_estimates`, retried at most `MAX_ATTEMPTS` times and no sooner than `RETRY_AFTER`; a name the model says isn't a dish ("Condiment Bar") is recorded with attempts=MAX_ATTEMPTS and left alone. `pending()` orders most recent menus first. `run_pending` is non-blocking (a lock; the poller and a menus read can race, the loser is a no-op); a blocked/rate-limited `AgentError` propagates so the poller reports it. `estimate_dishes` is shared by menus and the café seed.
- **USC dining provider.** Menu endpoint is the one the hospitality site's own menu page calls, `/wp-json/hsp-api/v1/get-res-dining-menus/<hall>?y=&m=&d=`: one hall-day per call -> meals -> stations -> items with allergen and diet tags; no calories/macros/portions. Venues: the location listing pages (UPC + HSC), each tile a slug, a type and today's open shifts as epoch-ms pairs. Dining Dollars are accepted at "any Residential Dining Hall or on-campus retail cafe" except USC Hospitality venues that serve alcohol; the listing's `restaurants` type (University Club, McKay's, The Lab, MoretonFig) is that set, so `dining_dollars = type != restaurants` (an inference, not a published field; set on insert only so a hand correction in app.db survives). Menus are published ahead so future ranges work; a read fetches any hall-day in range not fetched within `MENU_TTL` (days answered with nothing are recorded so they aren't re-asked); the poller refreshes today and tomorrow, then estimates what's new, then `food_seed.ensure()`. A usage limit holds the estimates and doesn't fail the menus that landed. Open Food Facts (`providers/openfoodfacts.py`) isn't in REGISTRY (nothing to poll); one call per barcode, result becomes a `source='off'` foods row so a second scan never hits the network; OFF reports nutrients per 100 g in grams, scaled to `food.NUTRIENTS` units.

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

Additions (details removed from `providers/ranges.py`, `__init__.py`, `metric_cache.py`, `__main__.py`):
- Range aliases: the past side of the vocabulary uses words people say ("today", "yesterday") and the future side did not. Asked for "tomorrow", a console found no token, computed a date off the wall clock and landed two day-keys out (at 2:30am the day-key is still the previous date, the point of the 4am rollover and what hand-arithmetic misses). `ALIASES = {"tomorrow": "next-day"}`, normalized away in `parse_range` so `Range.token` and cache keys stay canonical; `VOCAB` is what to advertise to tool schemas and CLI help. `MAX_SPAN_DAYS` (366): a typo'd year must not become a giant API backfill. Custom spans use `..` so the token is one shell-safe word; the future gate is by date (`end > today`), same rule `next-*` uses; `next-*` parse everywhere and `ALLOW_FUTURE` gates in `providers.get()`. `days_between` is the one day expander: location, screen and reddit each grew a private copy, which is how a span rule drifts four ways unnoticed.
- `REGISTRY` order is the poller's refresh order: `lastfm` after `spotify` (a just-synced artist is tagged the same cycle it first appears), `attachments` after `messages`/`email` (reads their attachment rows), `features` last (a denormalization of every source above; worst case one cycle stale).
- `providers.get()`: an offline read answers from cache and stamps `stale: true` + `offline` (the numbers are real, just not new) instead of raising. `query` filters string fields after the cache read and recounts `count`/`unread`.
- `metric_cache.upsert_days` exists because the upsert statement was copy-pasted in five places and forgetting `refreshed_at=excluded...` is an easy silent bug; each provider still owns its table, `_fetch`/`_sync` and missing-day logic in `get()` (marker table vs row presence; future allowed vs not; day_key vs civil date genuinely differ). `missing_days`/`oldest_refresh` are the read half shared by the sync-marker providers (calendar, email, health, reddit); `upto` is health's "never ask Google about tomorrow". `oldest_refresh` is the honest number for a "refreshed Nm ago" label.
- CLI (`__main__.py`): importing `vault_views` registers db's task-write hook so a `--refresh` that creates tasks (finance, email_extract, messages) keeps `tasks.md` in step as in-app. A `RuntimeError` from a source (a timed-out archive search, an upstream 4xx) is an answer about the world, so it prints one line, not a traceback. `--extract` is never implicit (model tokens per image, untrusted bytes per document); `--body` is live and never stored.

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

Additions from `providers/calendar.py` and `providers/ics.py` comments:

- Which date an event falls on is a zone question: answering with the machine clock files a 1am Eastern event under the wrong date for someone living in Pacific, so the row renders under a day whose label disagrees. `days_covered` seeds the zone lookup with the event's own start date (no zone here is over three hours off; the seed picks the wrong side of a TIMELINE entry only for an event on the transition date itself, i.e. on the plane). `_local_tz()` (the machine's zone) is for the API fetch window only.
- `_fetch` returns the calendar list alongside events because it was already fetched to walk it; `_sync` needs the (id, name) pairs and a second calendarList call would repeat the round trip. The window is padded a day each side because the user's zone can be 3h off the machine's and an unpadded window would miss events that fall in range only after the shift; `_sync` keeps only days inside the range.
- `_save_calendars`: the name lives in `calendars`, and the copy on every cached event row is updated too (including rows far outside the pulled window), otherwise `calendar_ids` would list old and new names against one id until the cold rows aged out, which for an event six months out is never. Calendars that are gone are pruned only when the whole list was pulled: with `GOOGLE_CALENDAR_IDS` pinned to a subset the rest is absent from `calendars`, not unsubscribed, and pruning would empty the table to the pinned few every sync.
- `refresh()` window [today-7 .. today+35] covers the widget's today, this/next week and most of this/next month. `sync_days` knows exactly which days a write changed (refresh's six-week window is too much work and, for an event three months out, the wrong window).
- Citations key on `day/event_id`: a recurring event carries one id across instances, so an id-only ref would make "your 9am Tuesday" and "your 9am next Tuesday" one citation.
- `_dedupe`: the cache PK (day, event_id) collapses re-syncs of one event, but Google issues two distinct ids for the same event when an invite arrives by two routes (subscribed calendar plus a copy on your own); the widget showed one class registration twice. Identical day + summary + start + end is never worth rendering twice; first in display order wins (primary calendar ahead of subscribed); the survivor keeps its own `calendar` label and `duplicates` records what folded in.
- Spans: a timed event over a day long (retreat, trip, hold, or a mis-dated end such as a 1h appointment filed as ending a week later) frames its days rather than filling them. `get()` returns it all-day with `span`, so free slots, brief, Load chart and cards show a banner instead of 24h busy per day (one such appointment otherwise read as 696h booked for the week). SQL readers use `SPAN_SQL`.
- `get()` re-expresses start/end in the user's zone on that date and names it in `tz`: Google returns each event in the zone it was filed in, so one slice could carry three frames (a 9:00 AM Pacific block next to an 8:30 PM Eastern one rendered as the same clock). `tz` is None for all-day rows (a bare "ET" on an all-day card was wrong). `continued` (event started before this day locally, e.g. 10pm-1am on day two) lets a renderer say "runs until 1am"; it uses the day's zone, since the machine's would flag a row as spill-over on the day it starts. `refreshed_at` is the stalest synced day in the slice.
- Free window is 09:00-21:00, wide enough for a student's real day, narrow enough that nothing proposes 3am. `free_slots` reads through `get()` to inherit backfill, per-day zone conversion and dedupe; all-day banners (birthday, "Move-in week") are listed beside the slots because blocking whole days is never a useful answer.
- `ics.py` (shared by athletics and coursework feeds): unfold continuation lines, VALUE=DATE means "no time set", never midnight; no RRULE because Sidearm and D2L publish each occurrence as its own VEVENT. `refresh_table` replaces the table (rolling-window feeds: a refetch is the whole truth) and raises on an empty feed (no rows from a feed that had them an hour ago is a broken fetch).

Additions from `routes/calendar.py` comments:

- The claude.ai MCP connector can't stand in for these writes: it goes needs-auth in every headless session, so the console could read the calendar all week and not move one event. The write path uses the backend's own Google token (same as the provider's reads), so it works wherever the backend does.
- Reading takes `calendar.readonly`; a vault authorized before writes shipped holds a token that can't write. Every write path checks the stored scopes first (either `calendar.events` or the older catch-all `calendar` grants write) and returns a RECONNECT hint, since Google's 403 "insufficientPermissions" body looks like a bug in the request.
- An event is addressed by id or by a `match` against the cache, never an id the caller retyped: a calendar id that lost its `@group.calendar.google.com` suffix once cost 18 consecutive 404s. `find_event` resolves titles from the cache so ids stay out of model-typed arguments; ambiguity is an error listing candidates (the wrong one of two "Standup"s moves the wrong meeting). `event_candidates` is the single query shared by `find_event` and the console's approval card (`console_preview`) so they can't disagree. `%`/`_` in `match` are literal.
- Default length for a start with no end is 60 minutes (the Calendar UI's own). `_reschedule` sends start and end as a pair because half a change turns 10-11am into 23 hours; a bare "15:00" `start` is a time on the event's own date (a series' first occurrence can be months back and invisible to the caller). A timed<->all-day change needs both ends: a PATCH merges into stored start/end, so an event would hold both `date` and `dateTime` (Google: "Invalid start time"; the console used to work around it by delete+recreate), and a null clears a field. A new end given alone is wall time in the user's zone, replaced onto the offset-aware old start (comparing naive and aware is a TypeError); the start goes back exactly as Google gave it so its stored zone isn't rewritten. A series keeps the zone it was created in (re-anchoring to today's zone would shift every occurrence, including past ones).
- Recurrence: `REPEATS` gives "every other week" its own names (biweekly, fortnightly) rather than an `interval` argument; `weekdays`/`weekends` are day sets for a weekly repeat; "none/never/off/once/single" mean stop repeating. One-letter weekdays are refused ("T", "S" each name two days). `BYDAY` keeps the order said. A full RRULE (e.g. the first Monday of each month, `FREQ=MONTHLY;BYDAY=1MO`) passes through alone and must carry its own ending (merging two stop rules gives a series that stops at neither). `MAX_OCCURRENCES` 500 caps enumeration (an open-ended rule has no last instance; 500 = a daily event for a year, a weekly one for a decade). `open_ended` is read off the rule (no UNTIL/COUNT), not the instance count, so a failed `_instances` can't report the one series worth warning about as bounded. Changing how an event repeats always addresses the master (Google refuses `recurrence` on an expanded instance). `_instances`/`_recache` are best-effort: the write already happened, so failing to count or re-pull must not report a real change as a failure.
- Cache refresh order: vacated days of a series are read before the PATCH/DELETE (afterwards they're not derivable from anything); `_series_days` keeps only days already in `calendar_days`.
- Delete reads the event first so the answer can name what went ("deleted" with only an id is unverifiable). Delete is not recoverable from here (Google's event trash is the UI's undo). RSVP sends the whole guest list back because `attendees` is replaced wholesale (one entry would uninvite everyone else). `notify` is off unless asked: an approval card for "move the meeting" isn't consent to mail six people.

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

Additions from `providers/health.py` comments (the module docstring points here):

- Days are the API's own civil dates (user timezone), plain dates like calendar, not day_key rollover. Sleep filters accept `sleep.interval.civil_end_time`, but the response interval carries only UTC instants + offsets, so the wake-up date is computed as endTime + endUtcOffset. Naps count toward that day's totals and share the wake-up day (segment sequence runs across the day, not within a session). Wall-clock sleep/wake times are stored with the recorded offset, never re-derived with today's zone (the terrain sheet hit that bug at six-year scale).
- Derived cache tables: `health_days`, `health_sync`, `health_hours`, `sleep_segments`; all droppable and re-pullable. Nightly SpO2 %, breaths/min and skin temp are sleep-derived Daily-record list types keyed on the wake-up date; skin temp arrives in °C and is stored as °F.
- `refresh()` keeps [today-7 .. today] warm (sleep and resting HR land within a day; steps backfill when the watch syncs late); colder days are fetched on demand by `get()` and backfill the cache.
- Intraday steps share the `health_hours` shelf with HR buckets (same rollUp call, different dataType): the curve behind "14,000 steps". Steps are not capped at 14 days by the API, so cold days fill on demand in `day_detail()` instead of widening the poller window. `_write_hours` deletes scoped by metric as well as day, since the two series use different windows and one sync must not evict the other's rows. A fetched day with no buckets is still marked fetched (stops `day_detail()` re-asking about a day the watch sat on the nightstand); only the steps pull sets `steps_at`. 0 steps in an hour is a real value; only a missing `countSum` is a gap. A series that fails doesn't drop its sibling: write each series that came back, then raise the first error.
- Hourly bucketing: rollUp takes a physical-time UTC `range` and `windowSize`, returns one bucket per hour stamped with a UTC `startTime`. The span is requested wide (a day each side, still inside the type's cap) and each point is bucketed by its start instant in the local tz, so DST/offset shifts land in the right hour. The zone is `tz.zone_on(day)`, never the machine's: a cold July day spent on another coast framed with the machine clock lost the day's first hours and folded the next morning in (10,610 hourly steps vs a stored daily 8,765). `_local_hour` resolves zone-of-day and day-of-zone by seeding with the UTC date and re-reading once; zones either side of a move differ by hours, not days.
- `_fetch_all` runs the series concurrently: serial, a refresh made 11 calls for ~12.6s (0.6-1s each except the two heart-rate rollUps at 2-4s, the only ones whose cost grows with the window); concurrent costs about the slowest call. Workers only fetch; the caller writes on its own connection. Every call is a read even when POST (rollUp/dailyRollUp take the range in a body), so http.py retry is safe.
- SERIES table: each entry owns its `health_days` metrics, which is what lets a failed series leave its own cached rows while others are replaced. `optional` wellness types may be unavailable for a given watch/account: an API refusal (RuntimeError) is "no data", a network failure still counts. The sleep dataPoint carries stage segments and totals in one response, so nights ride along for free.
- On a partial failure the window is NOT marked synced (a mark claims every metric was fetched; a cold day backfilled without heart rate must read as missing so the next `get()` asks again). The raised error prefers a non-network one so `offline.best_effort()` can't swallow a dead token behind a timeout. `_write_segments` leaves the transaction to the caller so totals and shape land together (a night's totals without its shape would draw a hypnogram disagreeing with the bar above).
- Desk-card window is 28 days: four of each weekday, so a body's weekday/weekend shape averages out instead of making every Saturday "unusual" against a 7-day baseline. The card's lead metrics are deliberately not `METRICS`: stage breakdown and the wellness trio are sheet-only, otherwise the card would open with "REM 84 min", the number-reporting the card form exists to stop.
- Auto check-in helper: fills only untouched days (manual check/un-check wins); reads the `health_days` cache, where each metric already sits on its honest civil day; an optional `gate` `(metric, threshold)` must also clear (e.g. exercise peak-HR gate in config); a gated day with no gate metric cached does not check in (a requirement, not a tiebreak).

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

`email_learn.py` numbers and rules:
- Backtest: time-ordered over 831 decisions, predicting each from strictly earlier ones (scratchpad/verify_real.py; rerun after changing a threshold): model 30.4% overall, caught 52/178 non-archive (29%), precision 52/610 (9%); always-archive 78.6%, 0/178, never fires; `email_learn` 82.7%, 78/178 (44%), precision 78/137 (57%). Precision is the column that matters: always-archive scores well and is useless since the 178 non-archive decisions are the only ones worth a recommendation.
- Sender memory: 91% precise over 349 calls (97% over the 289 saying "archive"; `delete` 60%). Domain 77%, only where the exact address is unknown, needs `DOMAIN_MIN_OBSERVATIONS`=3 (one newsletter says nothing about a colleague at the same company). Dwell (retired with `delete`/`bin`) was 52%.
- `MIN_OBSERVATIONS`=2 because the third decision matches 97% of the time when the first two agree; `AGREEMENT`=0.75, below which the sender is genuinely mixed and nothing is said. The floor compares the raw count (`obs`), never the decayed weight.
- `HALF_LIFE_DAYS`=90: by backtest every half-life from 30 d to 365 d scores within 0.2 points (the data spans ten weeks, so decay has little to bite on); 90 d beats no decay by 3 points of precision and is the shortest that doesn't thrash on a sender seen twice a quarter.
- Decay is applied on write (each key's counts aged to now before the new decision lands, so `n` is current as of its own `last_at` and a read is a plain SELECT). All actions for a key age by the factor from the key's most recent decision of any action; otherwise an action unseen for months would decay twice. `unrecord` subtracts without undoing the decay applied on the way in; the residue is bounded by one action's decay and disappears at the next `rebuild()` (exactness would need every decision stored individually, which `email_triage` already is). `record` is called after the mailbox write succeeds: a recommendation learned from an errored action would be learned from something that never happened.
- `rebuild()` gives the learner its history from the start (875 decisions then) rather than a cold "archive". `ensure_warm()` makes "droppable" true: a dropped or never-backfilled table is recreated empty by the migration and every recommendation would silently fall back to bare "archive" forever, looking like a learner that hasn't learned yet. Lazy on first read; the guard is a LIMIT 1 probe per queue render.
- `_parse` flattens tz: `emails.received_at` carries an offset (Gmail header) and `email_triage.user_action_at` does not; subtracting raises, which once silently measured no dwell (every difference landed in an `except` as zero).
- `_verdict`: the action is chosen by decayed weight (a changed habit reports the new habit) but the share quoted is the raw one, so "you archived 4 of the last 5" means five actual messages. Reasons quote the count, not a hedge ("most of the last 43" is vaguer than the domain line under it).
- `predict_label` uses recency (the last two filings agree) rather than a majority because it is the correction path: relabel a sender twice and the next card already says so. It reads `email_triage.user_label` directly, one indexed scan, nothing to decay or rebuild.
- `FOLD` maps retired `delete`/`bin` to `archive` at read time (`_counts`); `ACTIONS` keeps every action ever recorded so the record replays whole; only the first three are still offered.

Additions from `providers/email_search.py`:

- Local tier covers "the Lyft ride in September" and "order 124057035" without a body ever being stored (the `terms` column holds order numbers, amounts, people and dates the summarizer extracted). Gmail's own search (which does read bodies) is the second tier; its hits are fetched as metadata and written into `emails`, so the backfill summarizes them and the next search finds them locally.
- `ensure_index` rebuilds `email_fts` whole whenever a signature over the tables it reads (`emails` count/max refreshed_at; `email_triage` count/max summarized_at/max user_action_at/count user_label) stops matching. A rebuild of ~1,200 cached messages takes tens of milliseconds and runs only when a search does, replacing a hook in five writers (summarizer, both sync paths, the action route, undo) that each could be forgotten. In the indexed row the filed label (`user_label`) wins over the model's pick since that's what the message is under in Gmail. bm25 weights favour subject and terms over the summary (a subject or extracted-term hit is a far stronger signal than a word the summary happened to use). `fts_query` quotes each word so `c++`, `#1240` or `AND` can't parse as syntax, and makes each a prefix, so half-typed words match and "receipt" finds "receipts" with no stemmer (porter would index "Dining" as "dine").

Additions from `app/email_schedule.py` (Scheduled send paragraph) and `app/reply_draft.py` (already covered by the People section):

- Gmail's UI "Schedule send" isn't exposed by the API, so the content stays in Gmail as a plain draft. Consequences: editing the draft in Gmail before `send_at` changes what goes; deleting it cancels (`drafts.send` 404s, row recorded `cancelled`, not retried). It is the one place the dashboard sends mail without a human pressing Send at that moment (routes/email.py's draft-only line, §17); the approval card (recipient, subject, time) stands in, `email_schedule` guarded in console_changes so it cards even in auto mode.
- Exactly-once, not at-least-once: the `scheduled` -> `sending` claim is one UPDATE before the Gmail call so the send loop and a manual /api/sync can't both send; a row still `sending` after `STUCK_MIN` (10 min) means a crash mid-call, whether Gmail sent is unknowable, so it becomes `failed` with a "check Sent" note.
- Transient Gmail/network failures back off 1, 2, 4 ... 64 min (about two hours, enough to ride out a wifi drop; `MAX_ATTEMPTS` 8) then fail loudly into the failures log (a promised mail that didn't go must not live only in a table nobody opens). The backoff clock is in-process; a restart retries at once. With no usable token the rows stay scheduled with the error recorded (a dead token must not look like a quiet queue).
- `parse_when`: naive datetimes are local; the past (beyond `LATE_GRACE_S` 120s: "send at 3:00" asked at 3:00:30 isn't an error) and over 366 days out are refused as more likely a wrong date than an intent. One schedule per draft: rescheduling moves the existing row (a duplicate row's second send would 404 and read as a cancellation that never happened). Cancel keeps the draft (cancelling the send shouldn't throw away the writing). Time is validated before Gmail is touched so a bad date leaves no orphan draft. `recent()` is pending first then latest outcomes, so an overnight failure shows.
- `reply_draft.email_thread` uses snippets only (bodies are never stored; a snippet suffices for a short reply). `drafts_for` returns `turns` so the caller can say "drafted from 12 messages" without implying the model knew more; an empty thread returns no drafts rather than inviting the model to invent a conversation.

Additions from `routes/email.py` comments:

- Review files the message under a `Filed/` Gmail label (all three actions; keep files too, since a message left in the inbox is still one to find later). One `messages.modify` does the filing: label on and, except for keep, INBOX/UNREAD off. That POST is idempotent (setting the same labels twice changes nothing) and sent with `idempotent=True` so it gets a read's retry: Gmail dropping the socket must not fail a card whose unsubscribe may already have gone out. A modify failure clears `_label_ids` (label ids never change once made; the cache goes stale only when a label is deleted in Gmail). `_clean_label` takes a bare name as a new label under `Filed/`: review never files outside its own tree. Labels are real Gmail labels, so the filing shows as one tree and Gmail search can use it.
- Unsubscribe ladder: RFC 8058 one-click POST -> `mailto:` via the Gmail send API -> hand the https link back for the browser (card stays). A refused or dropped one-click POST reached nobody, so the mailto rung is still safe when the header offers both. The one-click request uses a custom User-Agent: urllib's default is on Cloudflare's banned-signature list (loops.so and anything fronted that way answers 403 "error code: 1010"). The unsubscribe request can't be recalled, only the archive after it; undo says so (`unsubscribe_sent`).
- Queue recommendation is the learner's, not the model's (see the card paragraphs above); default is "archive" because the model said "keep" 331 times and 8 were kept. `label` is the sender's record (last two filings agree) before the model's; relabelling a sender twice is the whole correction loop. `unsubscribable` (model flagged 28 of 30 actual unsubscribes: 93% recall, 28% precision) is an offer only where Gmail gives a way to act. Cards carry attachments (`attachments.decorate`) because an attached PDF is often the point of the mail.
- `_record` writes the triage row and folds the decision into `email_learn` in one transaction so the aggregate can't count a decision the record doesn't hold. Undo snapshots whether the message was unread (Gmail can't say afterwards) and calls `email_learn.unrecord`: an undo retracts the decision, otherwise undo would teach the learner the opposite of its meaning and a sender gets more confidently wrong each time a call is corrected. Undo also untrashes for a legacy `delete`.
- `archive(msg_id)` (console `email_archive`): a cached message goes through `post_action` so queue, learner and card undo stay right; an uncached one (found by search, older than the sync window) only leaves the Gmail inbox.
- Body read (`GET /{msg_id}/body`): read-through, never cached; `body` is always text; `?html=true` adds the defanged HTML part for the reader modal's script-less sandboxed iframe (CSP blocks remote loads, so a tracking pixel fires only on "Load images"). Reading doesn't mark the message read; only an action does. Gmail's 404 maps to 404 (message deleted or moved), not 502, which had filed itself in the failure log as a broken upstream.
- Drafts: Gmail threads on In-Reply-To/References, not threadId alone (a draft with only threadId opens a fresh thread on the recipient's side), so the parent is fetched for its Message-ID; `References` is the full chain oldest first. `attach` ids are all loaded before Gmail is touched so a missing file refuses the draft instead of leaving one without it. The 25MB `DRAFT_MAX_BYTES` is Gmail's send ceiling (a larger draft saves but never delivers). `replaces` uses `drafts.update` so a revision leaves one draft, not two (a second `email_draft` for a revised email once left the first, placeholder and all, beside it); a 404 (sent or deleted meanwhile) falls back to a new draft.

Additions from `providers/email.py` comments:

- Day mapping is the plain local date of `internalDate` (as calendar: a 1am email belongs to its own date). The sync window therefore ends on the civil date, not the day key: between midnight and 4am a day_key end bound stops at a midnight that has already passed, so small-hours mail (a pass forwarded at 12:41am) fell outside the window and was invisible until dawn. `_sync_days` never marks a future day synced (its empty result isn't an answer and a marked day is never backfilled; a this-year read would otherwise mark the months ahead and a day the recent-days refresh missed while the app was closed stayed empty for good). `refresh()` keeps [today-7 .. today] synced; colder days are fetched on demand.
- `message_memo()`: one refresh reads the same message whole up to three times (attachment metadata, triage excerpt, package/trip extraction excerpt); inside the block each `format=full` answer is kept so a message costs one Gmail call per refresh. Module-level with a lock, not a contextvar, because the extractor fetches from a thread pool a contextvar doesn't reach. Outside a refresh every read is live (a `body()` landing mid-refresh may get that refresh's seconds-old copy, which is fine: a sent message doesn't change). Only successes are memoized; a failed fetch is retried by the next caller.
- `fetch_new` is network-only and must never run inside a write transaction (N sequential Gmail round-trips would hold the app.db write lock past every other connection's busy_timeout: "database is locked" 500s elsewhere); `_sync_attachments` follows the same network-first, transaction-after rule.
- Attachments: `format=metadata` omits the parts tree, so learning an email's attachments costs a second `format=full` call; `has:attachment` first and the already-recorded check limit that to once per such message. Only metadata lands (filename, mime, size, opaque attachmentId; inline images included, a pasted screenshot is still a file that arrived). `fetch_attachment` is on demand only, never from a sync path: hostile PDFs stay on Google's servers until something asks, bytes stay in memory, nothing is written to disk.
- `refresh()` model half: mail has synced by then, so a spent usage window (`agent.AgentError`) must not paint the whole source failed; the work it owes is keyed on what's unprocessed and catches up next cycle. Anything that isn't the model saying no still fails the refresh.
- `body()` failures are the answer (an empty body would read as "this email says nothing"): NotAuthorized -> ValueError (400/one-line CLI error), HTTP refusal -> RuntimeError (502), lost network -> RuntimeError too, not a bare 500. `html` is never capped by `limit` (a text budget).

Additions from `providers/email_triage.py` comments:

- Summarizer: `claude_agent_sdk.query()` on the subscription login (never the raw API; `ANTHROPIC_API_KEY` stays unset), no tools, one turn, `EMAIL_SUMMARY_MODEL` (haiku). Vault-blind: messages, the label set and this term's course codes only. `terms` (handles pulled from the body for search) is as much of a body as app.db keeps. `TAXONOMY` was chosen from the ~1,200 cached messages at the time: recurring senders (Brightspace, Piazza, Handshake, Basecamp, Lyft, BofA) and the things mail gets searched for later (an order number, a flight, a code, what a professor said). Free-form labels (the old "bin") forked into `USC Orientation` vs `USC Orientation/Weekly Updates` and `Spam Digest` vs `USC Spam Digest` within weeks: a label you can't predict is one you can't search by. `_parse_reply` drops (never creates) labels outside the set, matches case-insensitively and tolerates a missing `Filed/` prefix (how a small model most often gets a right answer wrong). All review labels live under one Gmail parent `Filed` so the filing system is one collapsible tree apart from hand-made labels. The model's action guess feeds only `model_action` and `unsubscribable`; the learner's recommendation is right three times as often.
- `pending()`: every inbox message, then up to `EMAIL_BACKFILL_PER_REFRESH` older ones per refresh. Mail summarized before labels/terms existed, and mail archived straight from Gmail that was never summarized, is what search runs over, so it gets a pass too, a slice per refresh instead of one sweep that would hold the poller for an hour. `terms IS NULL` is the "still owed" marker, so `_terms` always returns a string ('' when nothing found). A backfill upsert never touches `user_action`/`user_label` (it refreshes the model's words, not the owner's decisions). `agent.raise_if_blocked()` runs before the body fetches (a spent window would refuse anyway; don't spend six Gmail requests per batch learning it); body excerpts are fetched on a 6-worker pool (the memo in `email.full_message` is lock-guarded for this). Ids the model invents are ignored.
- `attachment_context` reads the extracted-text cache only: triage must never trigger extraction (model tokens per image would turn a routine sync into an unbounded bill); un-extracted attachments are simply absent. Fenced as `<untrusted_attachment>` because it is the one prompt input an outside party authors (a PDF's text is whatever the sender chose); the system prompt tells the model to treat it as data.
- HTML to text (`_strip_html`): most mail that matters is HTML-only, and dropping tags alone collapsed whitespace into one run of words, left entities (`&#39;`, `&nbsp;`) and repeated the hidden preheader. The passes restore block shape and entities without ever producing markup. Proofpoint (every usc.edu message) injects an "External Sender" banner between `ZjQcmQRYFpfptBannerStart/End` markers, dropped. Hidden `display:none` blocks are dropped with a non-greedy match (nesting stops early and leaves junk in: the harmless direction). Anchors keep their words always and the address only when under `LINK_MAX_CHARS` (120; tracking links are hundreds of opaque characters), in parens because the later tag-drop pass would eat `<address>`. Proofpoint v3 rewrites are unwrapped (`__url__;`), but any containing `*` (encoded characters) stay wrapped: an ugly link beats a broken one.
- `fetch_html` (reader's formatted view) is defanged in layers: the real containment is the script-less sandboxed iframe under a CSP blocking remote loads until images are requested; stripping `<script|iframe|object|embed|base|link>` and meta http-equiv is the second layer for what a script-less document can still do (meta refresh navigates the frame, `<base>` retargets links, an embedded frame escapes this document's CSP). Over `EMAIL_HTML_MAX_CHARS` (600k; newsletters run 30-150KB, beyond that mostly inlined images) it returns None.
- `fetch_body` picks the HTML-derived text over text/plain when plain is empty, CSS soup (Handshake builds its plain part by dumping the `<style>` block; 3+ rule blocks in the first 20k chars is not prose) or diverges: Mailchimp-style senders duplicate last term's campaign and edit only the HTML so plain can be a different, stale email (one carried a plain part two years old); Gmail shows the HTML so that's what was sent. Measured on 69 two-part messages: real alternates share >=0.75 of their words, stale/raw-markup ones <=0.48; threshold `_PLAIN_MIN_OVERLAP` 0.6, below `_PLAIN_MIN_WORDS` 8 words it's too little to judge. `fetch_body` raises on failure (an empty string would be indistinguishable from an empty email); `body_excerpt` is the best-effort wrapper (a failed fetch is "" so one unreadable message can't sink a batch; the office-hours extractor passes a larger `limit` for whole Piazza digests). `fetch_body` is public because email_extract and `email.body()` depend on it.
- `strip_quotes`: used only when a body exceeds the limit; trimming the quoted chain beats a blind cut, which loses the link at the bottom of a forward. Patterns cover Gmail/Apple "On ... wrote:", Outlook dividers and "Original Message", `>` runs and From/Sent headers; `\r` tolerated for raw text.

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

Additions (details removed from `providers/finance.py` comments):
- `_sync_item`: `ITEM_LOGIN_REQUIRED` parks the item quietly (status `login_required`); the widget's Reconnect (Link update mode) plus the next sync heals it, and `refresh()` skips parked items rather than failing every tick. `refresh()` is a quiet no-op without creds or items (the poller runs from first boot).
- `PRODUCTS_NOT_SUPPORTED` (institution lacks the product) and `ADDITIONAL_CONSENT_REQUIRED` (item never consented) both surface as `needs_consent`, not a hard error: no reconnect fixes the former, but the item otherwise works and must not look broken.
- Transfers (`TRANSFER_IN`, `TRANSFER_OUT`, `LOAN_PAYMENTS`) are excluded from `spend` and `cashflow` so a card payment does not count as purchase plus payment.
- `networth`: assets = depository/investment/brokerage; liabilities = credit/loan (`current` is the positive amount owed); `other` is ambiguous and excluded. One `fin_balance_history` upsert per (day, env) after all items sync, so repeated syncs overwrite. It only exists from the first snapshot; earlier ranges are empty (overwritten balances can't be backfilled).
- Leg markers: an unknown/dropped `sync_state` key means due (one extra fetch, never a miss); marking a failed leg would park it until tomorrow.
- A posted charge names the pending one it replaces: `expenses.follow_posted` moves expense claims over in the same write, before the pending row is deleted (local import: expenses imports the providers package).
- `bills_due` requires a non-empty `merchant` on recurring outflows: Plaid flags anonymous peer transfers (monthly Zelle) as mature outflow streams with `merchant` stored as '' (not NULL); they are real but not bills, so no mute list is needed.
- `spend` includes pending and zero-fills days up to today (sparklines); `cashflow` buckets by ISO week (Monday), zero-filling empty weeks; `location` is only populated by Plaid for in-store purchases; `counterparties` drops the merchant and `financial_institution` rails.
- `brief_line` branches on shape; bills_due and transactions both carry `amount`, so bills_due is tested first.

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

Additions (details removed from `providers/weather.py` comments):
- `past_days`/`forecast_days` are offsets into Open-Meteo's own calendar, anchored on the real civil date, not `db.day_key()`: between midnight and the 4am rollover day_key still reads yesterday, so using it asks for a window a day in the future and the requested day never lands. Stored `day` keys still come from the API's dates. Caps: `forecast_days` <= 16 (counts today), `past_days` <= 92; `_horizon()` is that window.
- Units are forced to °F / mph (API default is °C, km/h); `weather_days` is derived, so wipe it after changing units or adding a METRIC (rows mix scales / cached days don't backfill). `current()` is °F end to end (live reading and today's high/low from `weather_days`): never re-convert (the glance strip once read 150°+).
- A day is "fetched" if it has any row (`_fetched_days`), not per (day, metric): `_fetch` skips null metrics (gap, never 0), so a per-metric check would make every read of such a day a live call. Only days inside the horizon count as missing, else long ranges (this-year, next-month) call live forever without being able to fill the gap. Core fields (tmax/tmin/code/sunrise/sunset) are always present inside the horizon.
- `_origin()` uses today's home (`places.home_now`), not a constant: the owner has moved, and forecasting the old city is a silent wrong answer.
- `current()` cache is in memory on purpose: a right-now reading decays in minutes, and a restarted process should ask rather than open with yesterday's weather. `_CURRENT_STALE_S` (3600) is how long a cached reading may still show; older, the chip goes blank (a two-hour-old temperature as current is worse than blank). Open-Meteo 502'd on four separate days in one week (the longest-running `failures` row). Retry: two attempts 0.8s apart, under a second because it sits in a page load. The stale fallback returns a copy with `stale: True` so the cached payload keeps its honest `as_of`. `fresh_s` > 0 (route passes `CURRENT_FRESH_S` = 600) serves a same-origin cached reading without asking: the call is ~0.8s of pure upstream latency and `current` moves in 15-minute steps; default 0 always asks (`refresh()` depends on that).
- `forecast_at()` is uncached (weather_days is the single-origin series); `geocode()` caches name->coords.

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

Additions (details removed from `providers/news.py`): Kite content is community-curated and clustered from 25+ outlets so single-source bias washes out; CC BY-NC, personal dashboard use only. Kite's index lives behind a redirect (kite.kagi.com -> news.kagi.com) and category files resolve against the index fetch's final URL. `refresh()` is once per day_key because Kite republishes about once a day (~12:00 UTC), so re-fetching within the day re-downloads identical content. `top_headlines` round-robins categories so one cannot crowd the rest and never raises (an empty list degrades cleanly; brief._gather also guards).

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

Additions (details removed from `providers/spotify.py` comments):
- `plays` is NOT a purely derived cache (v16 migration): recently-played only ever returns the last 50 tracks, so the table is the only durable record between polls; the GDPR-export importer (`scripts/import_spotify_history.py`) backfills the same table (~266k plays back to 2020 from the Extended Streaming History), reconciled by covered-span with live rows.
- Auth is its own small flow, not `oauth.py`'s Google-flavored one: `SPOTIFY_REDIRECT_URI` must be registered exactly and is the 127.0.0.1 IP literal ('localhost' banned Nov 2025). `exchange_code` is called by the callback route in `routes/spotify.py`; token storage/refresh in the provider is what both the route and the poller call. A refresh grant may omit `refresh_token` (not always rotated), so `_save` keeps the old one (`COALESCE`). An `invalid_grant` refresh drops access+refresh token so `status()` says needs-auth instead of retrying a dead token every poll.
- `LISTEN_GAP_MIN` = 30: music runs unattended, so the gap between plays is how "put an album on" differs from "put it on, left, came back after dinner"; longer than any single track (an album never splits), shorter than an errand. `SHAPE_MIN_PLAYS` = 5: `repeat_ratio` over two plays can only be 0.5 or 1.0 and a lone play is a session of length zero; gap, not zero (the `GENRE_MIN_PLAYS`/`HR_MIN_HOURS` precedent). `repeat_ratio`: 0.6 is one song on loop, 0.05 a day on shuffle; eight scattered stretches and one long one can carry identical `listen_min`. A one-play session has span 0.0 ("no sustained block happened"). A test pins `listen_shape_by_day` and the features.py caller together.
- `artist_key` casefolds in Python: SQLite `lower()` is ASCII-only, so it can't be a SQL expression. `ensure_artist_keys` is the v35 backfill and self-heals rows a future writer inserts without a key (the WHERE hits the `plays_artist_key` index; SQLite indexes NULLs).
- `summary()`: `{"connected": False}` when unlinked so the widget shows a Connect hint; zero-fill of the week and baseline (`DESK_BASELINE_DAYS`) is right here, where it would be wrong on health: a day with no plays is a day not listened, not a sensor that missed it. Genres come via a lazy `lastfm.day_mix` import (cycle).

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

Additions (details removed from `providers/lastfm.py` comments):
- Tag identity: `_tag_id` is the *stored* form, not just a de-dupe key. Last.fm tags are free text and two artists can return different preferred spellings ("hip-hop" vs "hip hop"); per-artist de-duping would not merge them in the chart. Separators collapse to a space, costing prettiness ("lo-fi" -> "lo fi") and buying one row per genre.
- `_clean_tags` drops a tag equal to the artist's own name (Drake tagged "drake" would chart as its own genre). Only exact matches go; stylised spellings ("travis scott" on Travi$ Scott) survive.
- `TAG_BLOCKLIST` matches casefolded and exactly ("favourites" goes, "favourite metal" stays). A second block of bare nationalities/countries ("usa", "united states" both appeared in the first 79 artists): where an artist is from is not what they sound like. Compounds ("uk garage", "french house", "j-pop") are real genres and don't match; cities are left alone for the same reason (Chicago house, Detroit techno) and are too rare to distort the chart.
- `_fetch_artist` uses `autocorrect=1` and stores the name Last.fm settled on. `NotFound` (error code 6) is recorded as `missing=1`, else the artist is re-requested every cycle and logs a `degraded` event each time. Last.fm signals failure in a 200 body as often as in the status line. Pacing: `ENRICH_PAUSE_SEC` 0.25 (Last.fm asks <=5 req/s averaged). `RECENT_DAYS` = 7 defines "recently played" in `_untagged`; it compares against `db.day_key()`, because `plays.day` uses the 4am rollover.
- Every `artist_key` reader calls `spotify.ensure_artist_keys(con)` first: without it a DB whose backfill hasn't run matches no rows, reads as "no plays", and `health()` would say "off", the one state that renders as fine. `health()` reports coverage rather than freshness; incomplete is normal for a while (only an unstarted backfill is `stale`).
- `reclean` run after changing blocklist/weight floor/cap; pre-v36 rows without `raw` are skipped.

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
and writes only rows whose state changed (`_sync_receipts`). Tapbacks are kept but flagged
`is_reaction` (their synthesized pseudo-text, "Loved 'yes'", would otherwise
inflate `sent`/`received`/`contacts` and could self-capture a spurious task);
un-reactions and stickers are dropped (see "Tapbacks are kept" below). Contact names
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

Additions from `providers/messages.py` comments (the existing text above is still right except where noted):
- **Tapbacks are kept, not dropped.** `_sync` filters `message` rows: `item_type<>0` (group renames/joins/leaves, no text, would land as a blank message from whoever triggered it) is dropped; `associated_message_type` 2000-2999 (tapbacks "Loved/Liked ... to <quote>") are kept and flagged `is_reaction`, because a reaction you left is still you answering and the waiting feed must see it; consumers counting volume filter them back out; 3000-3999 (un-reacting) and 1000 (stickers) are dropped (removing a heart isn't a reply, a sticker has no text). Reaction asymmetry for "who owes whom": a reaction you left answers the thread; one they left asks nothing of you, so an incoming tapback must not start the clock or hide the genuinely unanswered message under it.
- **Watermark rule.** The ROWID watermark is persisted only if the run saw rows or the watermark was already non-None. Otherwise an empty first 90-day backfill would write 0 and the next sync would take the `ROWID > 0` branch, syncing all of chat.db history instead of retrying the bounded backfill.
- **Clean re-sync** = drop `messages` AND `DELETE FROM sync_state WHERE key='messages_last_rowid'`. Dropping only `messages` leaves the old high-water ROWID, so the table stays empty instead of re-backfilling.
- Both source DBs (chat.db, AddressBook) are opened read-only (`file:...?mode=ro`); the provider never writes to Messages or Contacts. `message.date` is Apple epoch **nanoseconds** (/1e9 + APPLE_EPOCH), not seconds. `_extract_text` short-circuits on non-null `text`; the fallback fails soft (may return a wrong earlier `NSString`), an accepted risk for a single-user cache.
- **Separate watermarks.** `ATTACHMENT_SYNC_KEY` (`attachments_last_rowid`) is separate from `SYNC_KEY`: the attachments table arrived on a DB whose messages were already synced, so it backfills on its own schedule. `_sync_attachments` windows itself (first run walks MESSAGES_BACKFILL_DAYS, later runs go incremental off the highest message ROWID already recorded in app.db). The watermark must come from app.db, not chat.db: a subquery for "newest message with an attachment" evaluates over the same rows being selected, so it always includes the newest and the window is empty forever. It advances past every message scanned, not only those with files, or the window re-walks the same tail forever. It runs above the no-new-messages early return in `refresh()` so the back catalogue is recorded. Metadata only: nothing opens a file; extraction is on demand in `providers/attachments.py` because parsing untrusted bytes carries the cost and risk. `_attachment_path`: chat.db stores `~`-prefixed paths (made absolute); NULL/empty means the file isn't on this Mac (iCloud-offloaded or never downloaded): keep the row (transfer_name says what it was), nothing to extract.
- **Attachment-only messages.** U+FFFC (OBJECT REPLACEMENT CHARACTER) is the placeholder where an attachment renders; U+FFFD appears beside it when the attributedBody decode hit an unmappable byte. `_attachment_label` gives "(photo)" etc. so an uncaptioned photo stays in the thread rather than being dropped as empty; mixed sets fall back to a generic word; `_ATTACHMENT_KINDS` is ordered most- to least-specific ("image/gif" before "image/"), first match per type. A caption wins over the label. A placeholder with no `attachment` row (expired/deleted media) is still named "(attachment)" rather than shown as mojibake. In `get()` each message's files are also listed by name because `text` only falls back to "(photo)" when nothing was typed.
- **Receipts.** chat.db writes 0, not NULL, for "never" (no receipt yet, or none coming: SMS, receipts off). Receipts land on rows in place after sync, so each cycle re-reads a 7-day window by `date` (indexed) and writes only changed rows; a one-time backfill (`RECEIPT_BACKFILL_KEY`) covers the whole cache for rows synced before receipts were stored. Returns rows updated. It runs after `_sync` (so a message that arrived and was read since the last cycle has its row) and is not gated on new messages (someone reading your text writes no new row). `brief_line` marks receipts on outbound only; "delivered" alone on iMessage means receipts are off or still unread; SMS carries neither so gets no mark. Attachments are named in the brief line, not counted, with a mark for whether their text is searchable yet.
- **`backfill_older(days)`** extends `messages` below the oldest cached row (the Brain's first ingest reads `config.BRAIN_BACKFILL_DAYS`, a year). It leaves the ROWID watermark alone; `selftexts.py` routes only texts newer than its own start mark, so a year-old note-to-self is not a task. Idempotent: INSERT OR IGNORE on guid.
- **`_sync_chats`**: caches each chat's custom group name + group flag (chat.db `style` 43 = group, 45 = 1:1) so People conversations label a group by name. Full get-and-replace each run (a few hundred mutable rows; self-heals if dropped); runs BEFORE the watermark early-return because a group can be renamed with no new messages.
- **`sync_identities`**: caches identity clusters so a person's fragments (a CRM row by email, texts under a phone) resolve to one human. Union-find over AddressBook record grouping (`addressbook_records()`, one group per Contacts card by ZOWNER): identifiers on one card merge, and cards sharing an identifier (iCloud + local duplicate) merge. A shared NAME never merges. Full-replace each run, but an empty AddressBook leaves the droppable cache untouched so a transient FDA/read failure doesn't un-merge everyone. Canonical key = an email if the person has one, else a phone (matches import_birthdays' email-first keying, so cluster_key usually equals the `people` row's person_key). `addressbook_records` yields `card_id` (ZUNIQUEID `UUID:ABPerson`, the id Contacts.app AppleScript returns, which lets `app/contacts.py` address a card for editing); identity clustering ignores it (ids are per-source). Unreadable/schema-drifted source files are skipped, never fatal. `_load_contacts` (handle -> name) is best-effort: an empty dict means no contact_name, never a failure. `normalize_handle` is public because `people.person_key` and `scripts/import_birthdays.py` depend on it.
- **`health()`**: probes whether chat.db is readable by *this* process. refresh() degrades to a quiet no-op when it isn't, identical to a week of not texting; launched from the .app bundle, Full Disk Access doesn't carry over and sync silently writes nothing forever. Cheap (open + one row) so /api/status probes live.
- **`cite`**: aggregates (`sent`/`received`/`contacts`/`reply_latency`) are day counts with no single message, so no citation; `unanswered` rows are a person, so none either.
- **`_is_correspondent`**: "unanswered" nudges a reply so it is only for humans: sender is in Contacts (name resolved) or you have written back at least once. A nameless never-answered number is a robot/wrong number/spam (the list was ~65 entries, mostly shortcodes). Shortcodes (SMS 3-8 digits: banks, 2FA, delivery) are rejected outright even after a stray reply, since they can't be dialed back.
- **`_unanswered`** is range-independent (a thread quiet for months should surface; candidates are every conversation's true last message). It (1) excludes group chats (`chats.is_group`; an unknown chat_guid is assumed 1:1 so a dropped chats cache degrades to the old behaviour); (2) collapses to one row per person by identity cluster (iMessage + SMS threads of the same human: replying in one answers both; unanswered only if their newest message anywhere went unreplied); (3) only correspondents. Outgoing rows mostly carry no handle (`handle_id` unset), so a chat's counterpart is read off its inbound rows and applied to the whole thread. Each row also carries the text owed a reply (a "6d - name" says nothing about effort) and when you opened it (None = never read, a different kind of owing than read-and-ignored).
- **`reply_latency_by_day`** lookback: reaches back a full gap window before the range, since a Sunday-night message answered Monday morning is a Monday sample whose inbound half lies outside the range. Thresholds: `REPLY_MAX_GAP_MIN` 48h (keeps genuinely slow replies, drops "texted them again next week"; a 90-hour latency would pollute a 4-minute median); `REPLY_MIN_SAMPLES` 3 (measured: days under it were single-sample, e.g. "2556 minutes" off one answer; costs 12 of 97 days; same gap-not-zero precedent as HR_MIN_HOURS / GENRE_MIN_PLAYS). Math lives only here; features.py calls it (Spotify novelty drifted from a second copy). A day with only never-answered inbound contributes nothing (not zero/infinity) so one ignored text can't swamp fast replies. `get()` needs no on-demand backfill (chat.db keeps full history).

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

Office-hours extractor (`email_extract.OFFICE_HOURS_SYSTEM`, `_upsert_oh_proposal`, `decide_oh_proposal`):
- Nothing is applied on the model's word. Output is a *proposal* you approve, so the prompt's job is to make it checkable: `quote` is required and verbatim, the one field a human verifies at a glance; every other field is the extractor's reading of it. An empty quote drops the proposal.
- `course_codes` is injected per batch, not asked for freely: a model guessing codes invents "CS 103" for "CSCI 103" and the proposal points at a note that doesn't exist; constrained to the vault's codes it becomes `relevant: false`. `current_hours` (each note's weekly hours) is injected beside it so a post restating the note can be told from news; `_upsert_oh_proposal` re-checks against the note itself.
- `add` exists because "starting next week, I will hold office hours 12-2 Thursdays" is a new weekly block and a prompt that calls a standing schedule never an amendment drops it. An `add` also needs a name, a weekly rule the card can place (`courses.is_weekly`), and not already on the note (rooms compared on letters/digits only; the same post read twice came back "GCS, Lower Level 2" and "GCS Lower Level 2"). A `change` with no new time is dropped.
- Validation, not trust: unparseable date, unknown course, missing quote drops the proposal rather than surfacing a card you can't act on. `_snap_to_quoted_weekday` fixes the model's off-by-one on "Thursday next week" (it can come back Friday): when the quote names exactly one weekday and the date disagrees, the nearest date on that weekday within 3 days wins; several or none, the model's date stands.
- `sent` (the message's `received_at`) is given only to this extractor: relative dates ("this Thursday") are unresolvable without it; the other two have none. Digest mail (subject contains "digest") gets `DIGEST_EXCERPT_CHARS`=8000 of body instead of the 1500-char default, which holds roughly the first Piazza post of several.
- Candidates exclude `is_sent` mail; the LEFT JOIN on `email_extract` is what stops a rejected message coming back. `oh_proposals` rows are the record of your decisions; one message can carry amendments for several dates.
- `pending_oh_proposals` drops past dates (approving "cancel last Tuesday" writes an override for a day already gone), except `add`, whose day is when the block *starts*.
- `decide_oh_proposal` writes through `courses.set_exception` / `set_hours`, so the vault stays the only answer to what the hours are. A `CourseError` (note moved, entry unmatched, code gone) leaves the row pending. Already-decided returns the row (idempotent). Approving a change keeps the room: "office hours on lower level 2 today" changes where, not when. For an `add`, the block it replaces is read first (`standing_block`) and stored in `prior` for undo.
- `undecide_oh_proposal` removes the override (or restores `prior` / removes the new block, addressed by `slot=` the rule just written) and the row is pending again. It drops whatever that date's exception now says, wrong only if something else rewrote the same date within the toast's undo window.
- `reextract_office_hours(since)` forgets only the empty (`relevant: false`) extractions for mail on/after `since`: a false carries nothing to replay and the old prompt said exactly that about every new weekly block. Messages that produced proposals keep their row, so nothing approved or dismissed comes back; each re-read is one cheap-model pass. Run after changing `OFFICE_HOURS_SYSTEM`: `python -m app.providers.email_extract --reextract-office-hours YYYY-MM-DD`.
- The office-hours pass runs last of the three and guarded on its own (it reads the vault; an unindexed vault costs only this extractor). A refused model call (`agent.AgentError`: usage limit, overload) defers the three extractors, never the flight poll and check-in tasks, which use no model; nothing is lost, since candidates are whatever has no `email_extract` row, with no age cutoff.

Extraction runner: bodies are fetched in a 6-thread pool (the refresh already sits on its own worker thread; threads, not asyncio, overlap blocking Gmail calls; `body_excerpt` swallows fetch errors and returns ""). All three prompts ask for one object per segment/amendment under the same id, so replies are grouped by message and every object kept (a round trip's return leg or the second post in a digest was once parsed and discarded). One object caches as a bare object (the shape of every older row), several as a list (`_cached_items`). Messages the model dropped or marked irrelevant still get an `email_extract` row so a false-positive candidate isn't retried forever.

Package upsert details: `merchant:order_id` is the canonical key because it is on nearly every email of an order's life, unlike `tracking_no`; the fold-in of a stray tracking-only row keeps one order from forking (a "shipped" email with only tracking plus a later "delivered" with only order id). `last_msg_id` COALESCEs because `create_package` passes `msg_id=None`. `_upsert_package` returns the key because `create_package` needs it to read the row it opened; re-deriving the rule at the call site would drift. A match by `_match_open_package` also keeps the good merchant/item_desc: keeping the follow-up's versions turned "Campus Ready" into "campusready.com" and an itemized pack into "Linen order". The at-desk banner (`_notify_package_move`) fires on a transition, never on discovery (a first sighting can't be told from "it just happened"; this also keeps `--backfill-packages`, which re-extracts, silent), and not for 'ordered' (the confirmation is the transition); the alarm channel owns arrival day (`PACKAGE_ALARM_HOUR`). The label derives from `PACKAGE_STATUSES` (no second lookup table). The banner body is item_desc, else tracking number, never `order_key` ("Vuori:27901251" is a DB row). The notify call is unconditional because the forward-only clamp already makes a backwards move read as no move.
`backfill_packages` stamps each row with its email's `received_at` truncated to 19 chars: every other writer stores a naive local isoformat and the card's "delivered in the last 3 days" test is a string comparison.
`_manual_suffix` / `_manual_pnr` stand-ins (`manual-<n>`, `MANUAL-<n>`) can't collide with a real id (merchant numbering; a PNR is six alphanumerics, never hyphenated) and read as placeholders in `db_query`. `create_trip` splits "AA2299" into carrier + number because the model reaches for the joined form and the card renders `{carrier}{flight_no}`; the row would read right while keying wrong. `delete_package` removes only the `packages` row, not the `email_extract` cache (keyed by msg_id while a package is keyed by order; `last_msg_id` names one of several messages), so `--backfill-packages` or a new email re-opens it; deleting cleans the card, it does not blacklist. Marking a wrong row delivered is the wrong verb (delivered rows stay on the card 3 days and reach the brief's return window). `delete_trip` clears only what a trip stores that derives from it, the open T-24h task, set to `dropped` (kept so `task_exists_for_source` refuses to re-open it); the 48h warning and card read `trips` live and the phone alarm is withdrawn by the next `alarms.reconcile()`.

Flights: a trip's UPDATE SET never touches status/gate, so a later confirmation email can't downgrade a live status to 'confirmed'. `_apply_flight_status` shares one transaction between the row UPDATE and the notify write: if the row doesn't move there is nothing to announce, and if the announcement can't be recorded the row doesn't move and the next poll retries both; the failure is deliberately not swallowed. A gate learned for the first time is not a gate *change* (`row["gate"]` is NULL until a poll fills it, else every flight buzzes once at T-48h); the status arm has the same first-poll guard via `is_delay`. The notify key includes status and gate: Delayed -> On Time -> Delayed told you three things, the same delay re-read each cycle told you one. AeroAPI `start` is capped at 2 days out (measured; `end` accepts +3d), and the poll's 48h horizon keeps departures inside that with the window starting a day early. 400 is the request being wrong and does not mark the source degraded; 401/403 must show. The notify channel (`app/notify.py`) replaced a local `_notify` that had no second caller and never fired (its only path was below the `AEROAPI_KEY` early return). Check-in tasks run after the poll, so a row opened or amended this cycle gets its task on current values, and outside it so an unset key doesn't take them down.

`_match_open_package`: a follow-up in an order's thread often carries only the merchant and the changed fact (a reply that gave only a pickup date was dropped, so the card showed no ETA while the date sat parsed in `email_extract`). It attaches to the merchant's one open order and abstains on none or several: guessing which of two orders a date belongs to is worse than leaving it blank. `_merchant_key` normalizes across case, spacing and a sending domain because the extractor names one seller differently per email ("Campus Ready" vs "campusready.com"). Only the SELECT for an office-hours candidate carries `received_at` (the other two have no relative dates).
`_notify_package_move` is silent for a new row even one arriving already 'delivered': a first sighting reads as "just learned", not "just happened"; 'ordered' is skipped because the confirmation is the transition. The banner body is item_desc, else tracking number, never `order_key`.

`_match_flight`: AeroAPI keys `/flights/{ident}` on a UTC window but a trip row stores `dep_local`, so a one-UTC-day window built from a local date misses any departure whose UTC instant falls outside it. Westbound evening departures are the ordinary case (21:00 at LAX is 04:00Z the next day): the call came back empty and the poller recorded "checked, nothing" for a flight it never looked at. Hence the window is a day either side; the wider window returns the ident's other daily operations newest first, so `flights[0]` would be the most recent run, not the one asked about; `_match_flight` picks by date. It matches on the origin's timezone as the response reports it, deliberately not `tz.zone_on()`: that answers "which zone is the user in that day", and on a moving day they disagree (an RDU->LAX move flight departs Eastern on a date TIMELINE files under Pacific). Rows with no schedule ("result unknown") are skipped.
`ensure_checkin_tasks` was once a call inside `poll_flight_status`'s loop, which left the one piece of travel needing no external service behind the AEROAPI_KEY early return, so no check-in task was ever created. The "departing soon" window lives in its SQL so there is one definition, not two.

Hand-written rows: every path in `refresh()` starts at a message in this mailbox, so an order on someone else's login (a backpack and shoe spray on a parent's Amazon account ended up on the shopping list marked bought, arrival dates only as prose), a store pickup, or a number read over the phone could never reach the deliveries card; `create_package` is that way in. Keys on `tracking_no` when given (same rule as `_upsert_package`, so a later carrier email lands on the row) else the synthetic `manual-<n>` order_id, which reuses `_upsert_package` whole (key choice, stray fold, forward-only status, COALESCE) rather than a second INSERT that would drift. `create_trip` is the same hole one store over (an RDU->LAX move flight was on the calendar and in no store, so the card read "2 trips booked" and meant Thanksgiving; the booking predates mail history). It goes through `_upsert_trip` so COALESCE rules, derived dep_day and the status/gate protection stay written once; a real `pnr` lets a later schedule-change email land on the row (the job `tracking_no` does for packages). A bare date for `dep_local` is refused (lands at midnight: wrong T-24h day, "12:00 AM" card).
`update_package` is how a date learned by phone/desk/unkeyable reply gets in (console `package_update`, the card's mark-delivered). Unlike the email path it may move status backward: forward-only exists so out-of-order emails can't undo a delivery, not to overrule the person looking at the package.
`delete_package` is for rows that shouldn't be on the card at all (promo read as an order, one parcel forked into two, a mistyped hand-add); marking such a row delivered is the wrong verb. `delete_trip` is for legs replaced by a rebooking or exchanged ticket (the airline's change email opens the new leg and never says the old is gone) or duplicates; `AA2308` is accepted for `flight_no` and split like `create_trip`. The `email_extract` cache stays for both, so only a backfill replay or a new email about it brings it back.
`backfill_packages`: `email_extract` is keyed by msg_id and the upsert runs only at extraction time, so a matching-rule fix never reaches already-parsed mail (a pickup date sat parsed in the cache while the card showed no ETA). Replay is safe (upsert idempotent, status forward-only); each row is written with its email's `received_at` so a long-delivered package isn't shoved back into the 3-day window. Run after changing identity rules: `python -m app.providers.email_extract --backfill-packages`.
`_manual_pnr` / `_manual_suffix`: `MANUAL-<n>` / `manual-<n>` read as placeholders in `db_query` rather than a booking you could look up. `_apply_flight_status` previously used a local `_notify` that sat in this file for months with no second caller and never fired (its only path was below the AEROAPI_KEY early return); it now uses `app/notify.py`.

Behaviours the tests (`test_email_extract.py`) pin whose reasoning isn't elsewhere here:
- `delete_package` clears the card; it is not a blacklist. The `email_extract` cache is keyed by msg_id and a package by order, so there is no honest set of cache rows to drop with it: a genuinely new email about that order builds the row again. Pinned as the surprising half of the contract, not as desirable. Marking delivered is not removing: the row keeps its three-day return window, which is why delete exists separately.
- At-desk banners fire on a transition, never on first sighting (a first sighting already reading 'delivered' can't be told from one that just happened). Re-observing the same status queues nothing (the poll re-reads every open order); a backwards status changes nothing so announces nothing; an 'ordered' confirmation never announces. The queue write shares the caller's transaction on purpose: a fact that didn't land must not be announced. The channel (`notify`) is shared; flight gate/delay banners go through it too (it once lived in this module below `AEROAPI_KEY`'s early return and had never fired).
- Flight rows open as `status='confirmed'` with gate NULL, so the first poll changes both fields: neither the status arm (`is_delay` guards it) nor the gate arm may trigger on change alone, else every flight buzzed once at T-48h to announce a gate nobody had been told. A gate learned for the first time is silent; a change from a known gate banners.
- A poll whose lookup finds nothing still stamps `last_checked_at` (so the row isn't retried every cycle); it is also why the one-UTC-day-window bug was invisible, since a flight never read still reads as checked.
- `_banners()` in tests orders by rowid: `queued_at` is second-resolution and ties.
- `create_trip` splits a carrier-prefixed flight number ("AA2299" -> AA + 2299): the card renders `{carrier}{flight_no}`, so unsplit it would print right with an empty carrier while keying the row wrong. `last_msg_id` COALESCEs for trips as for packages.

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

Additions from `app/people.py` comments:
- `people` is NOT droppable (it holds the user's CRM facts; see the v19 migration in `db.py`); nothing about contact activity is cached in it. The PATCH upsert is the same as the manual SQL `INSERT INTO people (person_key, name, watch_cadence_days, created_at, updated_at) VALUES (...) ON CONFLICT(person_key) DO UPDATE SET watch_cadence_days=excluded.watch_cadence_days, updated_at=excluded.updated_at`. Birthdays are imported by `scripts/import_birthdays.py` (rerunnable), not synced live; the dashboard can edit an imported birthday but doesn't re-run the import. PATCH coalesces never-set `hidden`/`day_one` on a stub row to 0 (both NOT NULL).
- Constants: `HISTORY_LIMIT` 200 per source in person detail (plenty for a lifetime of one contact); `DRAFT_CONTEXT_MESSAGES` 20 (enough to see what is being answered without shipping a year of texts to a model); `UNANSWERED_DAYS` 3, `WAITING_MAX_DAYS` 30.
- **Waiting feed (`waiting_on`)**: outbound email threads + iMessage chats with no reply; the console drafts `waiting` tasks from it, nothing auto-creates; brief signals are likewise suggestions only (decided, ROUND34 plan). Three separate ways a row leaves: **muted** (`people.hidden`; a bot or someone uncared-for; permanent for everything they send; keyed by the identity cluster so muting mutes every thread), **dismissed** (`waiting_dismissed`; this dangling thread needs no reply; only the current last message is written off, so a newer `last_at` un-dismisses it without action), **aged out** (`WAITING_MAX_DAYS`; past it "you owe a reply" becomes "that conversation ended", and stale rows sat at the top because the feed sorts by longest silence; nothing to undo, a new message restarts the clock). Mute/dismiss keys are normalized like person rows so email/phone matches land. iMessage rows carry the text they said (what is owed a reply); email rows don't (those are threads you sent last). `_waiting_email` scans whatever is cached (email.py backfills cold days on demand, no rolling window), so a thread cold outside the sync range won't surface: best effort.
- `Reminder.last_at` is the message being replied to (the `source_ref`, see the Reminder paragraph). The reminder note carries who + what they said so the task still means something in tasks.md away from the panel. `get_dismissed`/`get_hidden` resolve names (cluster/Contacts fallback, like the detail view) because the tables store only the key, which for a phone is a bare number, useless for deciding to undo.
- **Names/merging**: `_derived_name` falls back to the Contacts name Messages/Mail resolved (AddressBook first: authoritative, keyed by identity cluster, so it resolves when the key is one identifier but messages arrived on another). `_last_event_at`/`_meetings_for` are a substring match of the person's name on event summary/location (the events table has no attendee list): best effort, no RSVP join. `_merge_batch` does one GROUP BY scan each over emails/messages instead of 2 queries per person; `_last_event_at` can't be grouped (LIKE) so stays per person; activity is aggregated over the cluster's identifiers (`_ids`), falling back to the bare key. `_merge_people_rows` collapses several `people` rows for one human: canonical = most CRM signal (name/birthday/notes/cadence), newest-updated as tiebreak, its person_key stays the clickable identity, gaps filled from the others so no user-entered datum is lost. `_cluster_merge` is shared by `_all()` and search so both show unified people; rows with no cluster stay their own person. `_identity_index` reads the `contact_identities` cache (messages.sync_identities); missing/empty cache means callers fall back to the bare key, never an error. `_identifiers_for` is driven only by the AddressBook cluster, never name, so same-named different people never merge; one union list serves both message and email lookups (a phone never matches an email column). `get_person` falls back to the Contacts name for the header (also sharpens the meeting match), unifies identifiers (search opens people by CRM key, often not the phone the messages live under), and tags each message with its conversation (1:1 vs named group: a group message is theirs but said in front of others).
- **Open in Messages**: `_imessage_handle` picks the identifier to hand `imessage:`: prefer a phone (what 1:1 threads are keyed by), re-expanded to E.164 (+1...), since `messages` stores last-10-digits which Messages won't resolve; fall back to an Apple-ID email; ordering prefers identifiers seen in message history over ones only on the card. `post_open_messages` passes the handle as an argv element (list form, no shell): it is DB-derived but reaches an OS launcher. `get_person` returns the handle (or null) so the UI hides the button instead of offering one that 404s.
- **Conversations axis** (every chat_guid as its own row, so group chats show as one row instead of dissolving into members; rolled up from the `messages` cache plus the `chats` metadata cache from `messages._sync_chats`, i.e. real custom group names, with participant-list labels like "Alice, Bob +2" as fallback): `_is_group_guid`: group if the guid has the `;+;` segment (1:1 uses `;-;`) or more than one distinct sender (the guid marker catches quiet groups; the sender count covers non-conventional guids); the chat.db `style` flag wins when the chat is cached. `_conversation_label` dedupes participants by display name in order (one person on a phone + email is one participant). `conversations()`: participants = distinct non-me senders, busiest first; last_* describe the newest message so it reads like an inbox; `guids` scopes the scan and lifts `CONVERSATION_LIMIT` because a person's history can link to a chat older than the newest 100 and capping handed the UI messages with no header. A 1:1 row carries the person key so the UI drills into person detail. `_labels_for_chats` applies the same rules to just the given guids so a person's old group chats resolve.

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

Worker (`_extract_worker.py`) limits and what they stop:
- `RLIMIT_CPU` (20 s): parser infinite loops, quadratic blowup, most decompression bombs (inflating gigabytes burns CPU, so this fires before the machine notices).
- `RLIMIT_FSIZE=0`: no file can receive a byte (EFBIG at flush, verified; creating an empty file still succeeds, so it bounds content, not existence), so a parser bug that becomes an arbitrary write has nowhere to put a payload.
- `RLIMIT_NPROC=0` no fan-out; `RLIMIT_CORE=0` no core dumps of file contents.
- Each limit is applied independently (`_limit`): one unsettable limit must not skip the rest, which a single try-block around all of them would cause. FSIZE and NPROC go first so the child loses write and fork even if later ones fail.
- Memory is not capped in the child: Darwin accepts RLIMIT_AS/DATA/RSS in the API and refuses every attempt to lower them ("current limit exceeds maximum limit", even with an unlimited hard limit; `ulimit -v` is a no-op too). The ceiling is the parent's RSS watchdog (`_run_guarded`). `RLIMIT_AS` is still set for Linux.
- Not stopped: a memory-safety exploit in a C extension that reaches code execution within the limits; the worker is a normal-privilege macOS process. The mitigation is upstream, in what is parsed at all: only pypdf (pure Python), plain text, the JSON inside a .pkpass, and the XML inside a .docx (expat, DTD refused before parse) run there. Image decoding (the libjpeg/libwebp/libtiff CVE family) never happens locally, so a pass gives up its text but not its artwork; a Word file's pictures are handed back undecoded, signature-checked only, for the parent to show a vision model.
- stdout is one JSON object and nothing else, so a parser that scribbles on stdout can't forge a result; unparseable output is a failure. The worker imports nothing from `app` so it runs under `python -I` (which drops cwd from sys.path), hence `PKPASS_MIMES` / `DOCX_MIME` duplicated by hand from `attachments.py`; the child's env is `PATH` + `HOME=/nonexistent` only.
- Caps: `MEM_LIMIT_BYTES` 1 GB (a real 50 MB scanned PDF's page tree, still under a second for a bomb); `TEXT_CAP` 20k; per-page `PAGES_MAX` 1000 / `PAGES_CAP` 1.5M / `PAGE_CAP` 20k (a 300-page textbook chapter is ~600k chars; these bound the JSON the parent reads); `PKPASS_JSON_CAP` 1 MB (pass.json is a few dozen short fields); `BARCODE_CAP` 500 (the message is the ticket, not worth 20 KB); `DOCX_XML_CAP` 5 MB (a real 50-page handout is 54 KB); docx images: max 12 (bounds vision calls billed per file), 3.5 MB each (matches the parent's `ATTACHMENT_VISION_MAX_BYTES`, re-checked there), 8 MB total (bounds stdout JSON; the parent's `_STDOUT_CAP` is 16 MB and base64 adds a third).
- Zip members are read with a bounded `fh.read(cap + 1)`, never checked against `ZipInfo.file_size`: the declared size is a number the sender wrote, and believing it is how a 4 KB file becomes 4 GB of memory.
- PDFs: an empty-password decrypt covers "encrypted but not really"; a real password means no text, not an error. A page that fails to extract keeps its slot as "" because dropping it renumbers every later page and a citation's page number must be right (`pages` feeds `app/docs.py`).
- pkpass: the signature is not checked ("did the issuer write this" is not "what does it say"; a forged pass is not a way into the process). Field `value` can be a string, number, date or a list since nothing validates it; containers are dropped, not `str()`'d. Label/value pairing is reproduced as authored even when odd ("August 20" under "12:15 pm"), because guessing which half is the label quietly rewrites a ticket; `attributedValue` is the fallback only (one real pass fills it with "hello"). `authenticationToken` and `webServiceURL` are skipped deliberately: they are the issuer's update-endpoint credential and extracted text ends up in prompts. `barcodes` is the current key and `barcode` the pre-iOS 9 one; issuers emit either. An empty `{}` pass returns "" so it files as `empty`, not as a pass whose content is the word "pass". backFields repeat the front verbatim, so lines are de-duplicated.
- docx: a table row is one line, cells joined by " | " (a nutrition table's row is the unit that means something); each picture is an "[image N]" marker; a handout that is all labels (most of what a dietitian sends) is otherwise five item names. External (linked) pictures have no bytes. DOCTYPE/ENTITY in OOXML is damage or an entity-expansion attempt, refused.

Parent (`attachments.py`):
- `VISION_MIMES` is verified against a real HEIC out of chat.db: the API answers "I cannot process this image" as ordinary *text*, not an error, so widening the set without transcoding would file a refusal as the photo's contents with status ok. `INLINE_MIMES` is a list of the safe because the sender chooses the mime and SVG/HTML carry script. `_sniff` is a signature comparison (no decoder): it stops a HEIC relabelled PNG billing a vision call to fail and keeps the transmitted media_type matching the bytes.
- `_mime_for`: passes arrive as octet-stream (Apple Mail; Gmail passes the sender's header through), so without suffix routing half the .pkpass files would be `unsupported` on a technicality. The suffix only *upgrades* a type that says nothing, so it can't talk a JPEG into the local parser; a .pkpass that isn't a zip costs one worker exit and an `unsupported` row. `extract_pending`'s SQL mirrors `_mime_for` and `_is_transcodable` (source check included), else the queue offers files `extract()` would refuse or hides passes only reachable by id.
- HEIC: 96 files here, 28% of everything, and the API accepts only JPEG/PNG/GIF/WebP, so without conversion they are permanently unreadable. Converted by `sips` only from iMessage: macOS ImageIO already decoded those exact bytes to draw them in Messages, so it adds no exposure; Gmail HEIC would be the first decode of a stranger's bytes and is not converted. `read_upload` (dropped files) skips the source check because the bytes are the user's own, and it also routes AVIF and oversized PNG/JPEG through `_heic_to_jpeg`; `extract()` still sends it HEIC from Messages alone. Size is checked after the convert (`-Z` shrinks a 7 MB phone photo well under the cap). `TRANSCODE_LONG_EDGE` 1568 is the standard-tier long edge: the API downscales larger anyway, so capping costs no fidelity.
- `VISION_PROMPT` gives a photo a description even when something in it has text; otherwise a lunch photo comes back as only a bottle's label and the meal never reaches the food router (`selftexts.py`).
- `fetch_bytes`: Gmail attachments are not on disk and syncing never puts them there; bytes stay in memory. Local parsing spools to a temp file for one subprocess (the child must read from somewhere); `_extract_local` removes it unconditionally. The worker is run by path, not `-m`, because `python -I` drops cwd and the worker is standalone.
- `_run_guarded` / `_rss_bytes`: `_rss_bytes` returns 0 for a gone process and None when measurement failed (see Lessons: "I can't measure it"); `ps` has a 1 s timeout because under pressure `ps` goes slow before it goes wrong. `_RSS_POLL_S` = 0.03: the cap is only as tight as the poll gap; a page-touching alloc loop commits ~0.84 GB RSS in 150 ms, so a 0.15 s gap made a 1 GB cap behave like ~1.8 GB (and config.py's "killed long before the machine swaps" was off by ~84%); at 30 ms worst-case overshoot is ~0.17 GB. Cost is one `ps` per 30 ms for at most `ATTACHMENT_EXTRACT_TIMEOUT_S`, only while parsing. `_RSS_BLIND_LIMIT` = 3 consecutive failed samples (~90 ms) before killing unmeasured: `ps` can miss for boring reasons (EINTR, full process table) and one hiccup shouldn't kill a good parse, but three bounds what a runaway commits in the dark. SIGKILL, not SIGTERM: a wedged parser doesn't get to ignore a handler. A killed child reports a negative code like any signal death. Stdout is drained by a thread while the child runs (a PDF's per-page text can pass the 64 KB pipe buffer, and a child blocked on a full pipe never exits, so a finished parse sat until the wall clock killed it); the drain is capped at `_STDOUT_CAP` 16 MB (a real result is a few MB at most) and past it the child is killed like any bomb.
- `_read_docx_images`: four vision calls at a time (each is a CLI process; a label handout runs to eight); an unread picture keeps its marker with "not read" so a gap is never silent; method gains `+vision`. `_ask_vision` takes an async iterable prompt, the only form that carries an image block through the SDK.
- `read_upload` returns an `image` ({media, data}) rather than text for a dropped image: the session looks at it itself (a tool-free description reduced a CAD sketch to "10.00 5.00 10.00 5.00"). The session history keeps that image, so one the API refuses would fail every later turn: it must open under sips (`_long_edge`) and is held to the long edge the API would scale to anyway. A sendable format that is merely too big (a 13.5 MB photo) is shrunk through the same guarded sips call rather than refused. Finder drops of some types have no type at all, so the suffix is the hint (`mimetypes` has never heard of .pkpass, so `_mime_for` runs after). The note on the pill says when a HEIC/AVIF became JPEG. `pages` is for the caller that keeps the file (`console.py` -> `app/docs.py`) and is taken off before replying.
- `for_source` excludes stickers and `dyn.*` (Apple's opaque UTI for app/rich-link payloads, e.g. the blob behind a shared Apple Music card); the sync records them but never lists them. One query per batch because callers render a log.
- No `refresh()` on purpose: the poller calls it on every module that defines one, which would make extraction automatic, a bill growing with the inbox spent on files nobody asked about. `--extract N` is the only way in.
- Extracted text is attacker-authored; `fenced()` makes the boundary explicit (it doesn't make injection impossible), and the extractor itself runs with no tools.

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

Additions from `providers/features.py` comments (existing text above covers gap-not-zero, `_stale_days`, `_STALE_SOURCES`, retention gates):
- **Absent vs 0.** A source with no data for a day leaves (day, feature) absent. Within a present source a 0 is real and kept (a day with texts but none sent: `msgs_sent` 0); an entirely missing source (Plaid down, no location fix) is a gap. An empty day (no source had data) stays absent and is simply recomputed on the next read (same tradeoff as location.py). `features(day, feature, value REAL, refreshed_at, PK(day,feature))` is derived/droppable. `get()` also recomputes days whose sources were written since (`_stale_days`): "has any feature row" is not the same question as "has every source that now has data for it" (a day computed with health + spotify is never revisited, and a later-landing source, e.g. provider backfill, history re-import or new source, would read as "source had no data that day"; location hit this: `_ensure_days()` recovered five `location_days` that nightly refresh had already computed features for, and all five stayed location-less).
- **Two notions of a day.** Behavioral sources (spotify, messages, location) and tasks use day_key (4am rollover); health/weather/calendar/finance use civil date. Each source's `day` is passed through, not re-bucketed, so a feature always matches that provider's own endpoint. The cost: a pair drawn from both notions joins across a boundary off by up to four hours (matters for late nights). `FEATURE_DAY_BASIS` (derived from `DOMAIN_DAY_BASIS` x `FEATURE_DOMAIN`, so there is one registry to update per feature; `test_every_domain_has_a_day_basis` catches a new *domain* with no basis) records which; `correlations()` marks such pairs `cross_basis: True` (absent otherwise, so the brief pays no tokens in the common case). It is a caveat on r, not a reason to drop the pair.
- **Correlations**: Pearson r over days where *both* features are present (n is true overlap, not window length). Defaults `CORR_WINDOW_DAYS` 365, `CORR_MIN_N` 20 (a handful of coincident days can't manufacture an r), `CORR_MAX_P` 0.05 two-tailed. p is the textbook t-test: regularized incomplete beta I_x(df/2, 1/2) at x = df/(df+t^2), computed in stdlib (Numerical Recipes continued fraction + lgamma) rather than pulling in scipy for one function. `FEATURE_DOMAIN` lets `cross_domain` drop mechanically-obvious within-source pairs (msgs_sent ~ contacts, steps ~ zone_min). `genre_variety` is domain "spotify", not "lastfm", though tags come from Last.fm: it derives from the same `plays` rows, so pairing it with listen_min/novelty would count plays twice and read as a finding. `focus` keeps pairs touching a feature.
- **Coverage gates (`_COVERAGE_GATES`)**: for passthrough sources whose silence isn't a zero, a table maps to a function returning the days whose numbers rest on enough data; the rest stay gaps (the HR_MIN_HOURS / GENRE_MIN_PLAYS rule, one source over). Sensors that ride on hardware being awake need one; server-side pollers (health, weather) run whether or not anything happens, so a 0 from them is an observation. Only location is gated: OwnTracks reports *movement*, so a one-fix day computes 0 places and 0 minutes at home, byte-for-byte a day indoors and the opposite of the truth. `location.summary()` already refuses such a day, so without the gate the card and matrix disagree about one date. Rule: a sensor's silence is only a zero if the sensor was watching, and "is it on?" can't be answered from the numbers it wrote (a hand-launched app can have died weeks ago). `_location_covered_days` uses `location.coverage_minutes`/`is_thin` (absent coverage = no fixes at all; is_thin owns the rule for both callers).
- **Retention gates (`_RETENTION_GATES`)**: asks whether a source can still speak for a past day at all, distinct from coverage gates. Only calendar: `calendar.get()` calls `_sync(missing[0], missing[-1])`, which DELETEs the whole span and re-inserts what the API returned, so a past day's rows can vanish; `calendar_days` is the marker separating "no events that day" from "never fetched". Every other cache is append-only or rewritten per-day in place (plays and messages keep full history; health/location/weather rewrite the day they refresh; Plaid's window is far wider than the matrix reads). `features` is the only surviving record of a dropped day's load, so `_carried_forward` rows are re-stamped with `now` (what refreshed_at means: when last confirmed as the matrix's best knowledge) so the day doesn't read stale forever and recompute on every read. `_DOMAIN_FEATURES` derives from `FEATURE_DOMAIN` so a new feature can't be forgotten.
- **Extractor notes**: passthrough tables are pure renames (`health_days`, `location_days`, `weather_days`); raw-event tables are aggregated to match each provider's get() exactly. Novelty groups on `artist_key`, not `artist_id`: the importer's synthetic ids never match the poller's real ones, so artist_id makes every artist look new again at the import seam (`spotify.artist_key`, v35). `_listen_shape_rows` (repeat_ratio = one song on loop vs shuffle, listen_sessions = scattered vs sustained, longest_listen_min) is its own extractor so a failure can't cost the day its listen_min, and is delegated to `spotify.listen_shape_by_day` (one implementation, a test pins them together; same lesson as novelty drifting for twelve days). `genre_variety`: normalized Shannon entropy of the day's genre mix (0 = one sound, 1 = evenly spread); it is what's left of "genre as mood": the tag vocabulary is genre words not mood words (bare mellow/upbeat/romantic appear once each across ~2k artists, sad/happy never), so a tag-to-mood lexicon would fire on one or two artists and call it a mood. Gates (gap not zero): tagged share of plays under `GENRE_MIN_COVERAGE` 0.6 (spread would reflect which artists are tagged) or under `GENRE_MIN_PLAYS` 5 plays; a single genre after clearing the gates is a real 0.0. `_messages_rows` reply_latency measures how fast you answered, not how much you said (volume can't tell chatty from responsive); the `days` set filter exists because `days` needn't be contiguous and the DELETE only covers what was asked for. `_finance_rows`: no zero-fill, a $0 day is indistinguishable from Plaid being down. `_events_rows`: timed events only, each clamped to its civil-day window (overnight events don't over-count); all-day events (birthdays/holidays) and timed events over a day long (a retreat, a mis-dated hold; `calendar.SPAN_SQL`, the provider's own all-day rule) are excluded. `_tasks_rows`: `completed_at` is local wall-clock, so it is day_key'd here, not sliced on civil date. `hr_var_day`: population stdev (bpm) of hourly mean HR; needs `HR_MIN_HOURS` 12 hourly buckets or it stays a gap (partial wear makes the spread unreliable). Extractor order is cosmetic (features are partitioned by source, no (day, feature) collides); each is guarded independently in `_compute_days`, so one failing (Plaid unconfigured) drops only its features to gaps.
- **Stale detection details**: `calendar_days` re-stamps the whole trailing window each poll whether or not an event changed, so the window goes stale once per poll and converges on the next sweep (the health_days behaviour). Tasks: `updated_at` is written by the `tasks_touch` trigger with a space separator, so it is normalized to ISO-T before comparing as a string against `features.refreshed_at`; a task inserted already-done never fired the trigger, hence the `created_at` fallback; the day is `completed_at`'s day_key, the same 4am fold `_tasks_rows` does in python (`DAY_ROLLOVER_HOUR` interpolated so the rollover is defined once; `test_late_task_completion_folds_on_day_key_not_civil_date` pins the SQL against `db.day_key`). Last.fm tags: `artist_tags` is keyed by artist so the day comes from the plays that reference it; tags are fetched lazily one artist at a time, so a first read of a long history computed nearly every day's genre_variety before its tags existed (both the entropy and the coverage gate move). `_stale_days` issues one statement per source with `day BETWEEN` pushed into each arm: a single UNION against a features CTE made the planner walk every source's day index end to end (78-166ms on the real DB vs a few ms with index range searches). Tables absent from the DB are skipped (the per-extractor guard one layer up).

Test-pinned facts from `tests/test_features.py`: on the real DB, tags fetched lazily one artist at a time meant 284 of 365 days had `genre_variety` computed before the tags existed (both the entropy and the coverage gate move when tags land); before the retention guard a calendar rebuild cost 34 of 45 calendar days. A burst of fixes (many fixes minutes apart) is refused by the location gate on coverage minutes, not fix count; this matters more than for other gates because location clears `CORR_MIN_N`, so those days would land in pairs Patterns shows. Health/weather zeros are observations (their pollers run server-side whether or not anything happens) and are never gated on location fixes. A 2am task completion must mark the previous day (day_key) stale, not the civil date; `DAY_ROLLOVER_HOUR` is interpolated into that SQL and a test pins it against `db.day_key`.

`reply_latency` details pinned by `tests/test_reply_latency.py`: keyed on the day of *your reply* (a Monday answer to a Sunday text is Monday's), so a finished day is never revised; measured from the *first* of several unanswered inbound texts; group chats excluded (nobody owes a group a reply, same reasoning as `unanswered`); a reply a week after their last message is you starting a thread, not a 10,000-minute latency; a pair whose latest unreplied text is ancient retries against their most recent message before being dropped (measured: 61 of 111 dropped pairs were fast answers behind one ancient unreplied text); below `REPLY_MIN_SAMPLES` the day is absent (the `HR_MIN_HOURS` precedent), never 0 or a wild median; never-answered texts stay `unanswered`'s job (folding them in as a huge number would swamp fast replies); rows from the extractor's gap-window lookback outside the requested days are not kept because the caller's DELETE only covers requested days.

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

Additions (details removed from `providers/oauth.py` comments):
- Security model: tokens and client secrets live only in app.db on this machine; the app is localhost-only (backend binds 127.0.0.1), and that is the whole model. Never return token values from a route or print them; `status()` masks everything.
- Desktop-app clients allow loopback redirects to `http://127.0.0.1:<any-port>` implicitly, no redirect URI registration; Web-app clients die on the loopback flow with `redirect_uri_mismatch`.
- Access tokens refresh `EXPIRY_MARGIN` (60s) before expiry rather than risk a mid-call 401. A refresh grant may omit `refresh_token`, so `_save` keeps the stored one (`COALESCE`). On `invalid_grant` (Testing-mode ~7-day expiry, or revoked) the dead access and refresh tokens are cleared but the client creds are kept, so the Connect button can re-auth without the client JSON.
- `as_value_error()` re-raises `NotAuthorized` as `ValueError` (the providers' "bad request": `providers.get()` callers turn it into a 400, the CLI into a one-line error), used inside `offline.best_effort()`, which re-raises anything that isn't the network.

Additions (details removed from `routes/oauth.py`, `routes/spotify.py`, `routes/plaid.py`):
- `/api/oauth/start` hands back a Google consent URL bound to our own callback and, with `open_browser`, opens it in the system browser itself (the desktop shell can't open a consent tab from the frontend, see browser.py; the CLI opens its own, so it is off by default). The pending `state` row is recorded *before* the browser opens: the callback can land the moment the browser has the URL. Pending attempts are in-memory with `PENDING_TTL` 600s (single user; a dead entry just means clicking Connect again); the frontend polls `/api/status` until the account flips to authorized. `/api/oauth/addresses` resolves each linked surface's Google address (Gmail profile, calendar primary id), cached per process (it only changes on re-authorize), so outbound links can carry `authuser=<address>`: without it every link opens under whichever account is first in the browser session, usually the wrong one. Falls back to plain URLs when unauthorized or offline.
- Spotify's callback is its own router (`/api/oauth/spotify`: `/start`, `/status`, callback) because Spotify requires the registered redirect exactly (127.0.0.1, not 'localhost'), so the redirect always lands on the running backend rather than an ephemeral CLI loopback catcher; the `authorize` CLI is a thin client of `/start` (client_id first time only) and `/status`. Listening data's range/metric slice is registry-generated; `/api/spotify/summary` is the MusicWidget's one-call summary.
- Plaid: the widget POSTs `/link_token` (with `item_id` for Link update mode after `ITEM_LOGIN_REQUIRED`), opens Link, and on success POSTs `/exchange` (new item) or `/sync` (reconnect: update mode keeps the same access_token, so a fresh sync heals the status). Both run the sync before answering so the widget's next `/api/finance` read has it; the earlier design (kick off in the background, widget polls `/status` until every `last_synced_at` is set) failed because a reconnected item had already synced and passed at once. `_sync_now` answers rather than raises: the item is stored and a failed sync wrote its error to the item's row. `POST /api/plaid/setup`: the secret transits 127.0.0.1 once, is stored backend-side and never echoed. `POST /api/plaid/sync` resets parked `login_required`/`needs_consent` items first, since `refresh()` deliberately skips/short-circuits them; a still-broken item just parks itself again.
- `providers/plaid.py`: `api()` never echoes the request body (it holds secrets). `setup` reuses `tokens` (`token_uri` = API base, `scopes` = env, oauth token columns NULL). Items remember their env; sync/read only touch the configured env's, so sandbox->production orphans sandbox items rather than mixing data. `create_link_token` always sends `additional_consented_products: ["liabilities"]` (optional, never blocks Link for institutions without liabilities, grants it up front where supported). `exchange` starts the cursor NULL (full history on first sync). Sandbox: `ins_109508` First Platypus Bank is the one institution Plaid documents for scripted test items.

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

- The status read is cheap enough to poll: the only live probes are the ones a cache can't answer (chat.db readable, OwnTracks fixes arriving), both localhost. `off` exists so never-configured sources (Plaid without creds, a second AW host not up yet) don't render red: a panel that cries wolf is a panel you stop reading. Rows are ranked worst-first. Auth rows reuse the masked status() helpers the connect buttons use, errors are the repr the poller already logs: no secrets.
- Google accounts are shared (calendar and email are both 'school'), so one dead token shows broken on every source using it: that is the truth. A Google account never bootstrapped is `off`, not broken (it needs the CLI once for the client_secret json the button can't supply). Plaid with no item is `off`: the first-ever link needs the Finance card's own flow (institution metadata, setup form), not duplicated here; a broken item's action opens Link in update mode so it re-auths that item instead of adding a duplicate.
- Events slice per response: the newest `EVENTS_SCANNED` (120) rows scanned once and bucketed by provider, `EVENTS_PER_SOURCE` (5) each: covers "what happened overnight" without shipping the 500-row table.
- Partial trouble is reported inside a provider's own info dict rather than raising. Only `unreachable` (a host didn't answer, so the day synced short) implies a state (stale): partial data is stale data. `skipped` stays a note: news' "already fetched today" is a normal no-op and there's no generic way to tell a benign skip from a damning one; sources where a skip is the failure say so through their own `health()` probe (messages checks chat.db directly). A note sets the state but stays out of the one-line detail, because the health() probe words it better and two phrasings of one problem read as two problems. `_names` renders dict/list/str "which things" as names since a dict of urllib errors is unreadable in one line.
- No heartbeat row means no completed cycle since boot: not an error (the first cycle takes a moment) and the cache check still speaks. A probe that raises is reported broken, never a 500. An ok source with no details gets "synced Xm ago" only when the poller drives it; no detail beats a detail that repeats the state column (attachments has no poller).

Additions (details removed from `providers/heartbeat.py` and `providers/poller.py` comments):
- Red/green alone lies constantly, hence four states. `stale` catches a refresh that writes 0 rows (chat.db unreadable without Full Disk Access reports success forever otherwise); `off` must not render as a failure or the panel becomes noise.
- `FRESHNESS` thresholds are "when a gap becomes suspicious", not an SLA (you can go three days without texting, so `messages` is 3); they mark where to look, so err generous: a false amber costs more attention than a late one. Per-source reasons: health 2 (Fitbit's own sync lags a day), spotify 3, messages 3, finance 4 (no spend over a long weekend). `reddit` uses the `reddit_sync` marker table, not `reddit_posts`: a quiet subreddit day writes no posts. What a cache can't show (reachability, file permissions) goes in the provider's own `health()` hook.
- `last_ok_at` survives a failure ("worked 6h ago" is the useful reading, not NULL). `recent` outcome characters: `x` failed, `.` reported `skipped` (news fetched once a day, reddit inside its interval), `o` otherwise. `heartbeat.broken` = last two failed (`xx`): one bad cycle in a row of good ones is weather and flagging it teaches you to ignore the flag.
- `log_event` is for places a provider swallows a failure and carries on (refresh() returned success, so `provider_health` says ok=1; previously a print into a /tmp log macOS deletes). Never raises: logging is not load-bearing.
- `_record_freshness` logs `stale`/`freshened` events only after a successful refresh (while failing, `broke` already says so). It exists because the `stale` state is computed live by `routes/status.py` and evaporates when data catches up: location ingest once stopped for four days, then OwnTracks' store-and-forward flushed the backlog, and `provider_events` for location was empty, so a real, self-healing outage was unrecoverable from the record. Previous staleness is read back from the event log (no new column, no migration; the log is its own state).
- `_staleness` returns None (not False) for three non-answers, each a different lie as a boolean: no FRESHNESS rule (the provider's `health()` owns the verdict), an empty cache (never synced, not gone stale), an unparseable day (a cache bug, not an outage). Same three-way discipline as `alarms.py`'s `unknown`. `newest_day` returns `(None, 0)` for a dropped table (derived, so legal). `age_days` clamps future days to 0 (forecast, next week's calendar: ahead isn't stale).

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

Additions (module notes for `failures.py`):

- The `poller` origin covers the post-provider steps (alarms, notify, wake, backup); providers themselves use heartbeat. Each row stores a capped text (`failures.py` cap): a React component stack or Python traceback is a few KB, a minified stack occasionally far more, and an error built from a response body has no ceiling.
- `middleware` and `install` live in `failures.py`, not `main.py`, so the test exercises the real function instead of a reimplemented copy (a stub stops testing its subject once the two drift). `main.py` and the tests both call `install`.
- The middleware labels a route by its template (`GET /api/email/{msg_id}/body`), not the raw path; by raw path one Gmail outage was one row per message id. The router writes the matched route into the shared scope, so it is available when `call_next` returns or raises; an unmatched path (a mount) keeps the raw path.
- `http_exception_handler` is FastAPI's own handler plus a note: the exception is gone by the time the middleware sees the response, and the body is a stream it would have to drain. For a 5xx it leaves the detail and traceback (for `raise HTTPException(502, ...) from e`, the upstream error itself) on `request.state` (scope-backed, so the middleware's Request sees it). Without it a row read "HTTP 502" and the cause had to be dug out of the stderr log. Recording stays in one place.
- `fingerprint` of free text masks numbers and hex ids: an HTTPException detail is usually `f"...: {e}"` and carries counters, ports, message ids and timeouts that change every time; raw text would make one flaky upstream a row per request. The words saying what failed still split rows.
- A recurrence keeps the newest trace (what you want when you go to read one); fields otherwise identical by construction unless a `key` was given.
- `recent()` returns newest first with `ago` precomputed, as `/api/status` does, so the panel does no clock arithmetic.
- Desk traces are resolved through sourcemaps when the report arrives: the built desk is one minified bundle (`gk@http://127.0.0.1:8788/assets/index-D2c-yNSi.js:55:48155`), `vite.config.js` builds `sourcemap: "hidden"` (a `.map` beside each bundle, not referenced from it), and builds prune assets after a week, so the report moment is the only time the matching map is certain to exist. A frame whose map is gone stays as it came. Resolved paths are reported from the repo root (maps are relative to `dist/assets`). The parsed-map cache key includes mtime so a hand-copied dist doesn't serve a stale parse.
- Sourcemap decoding: a segment is base64 VLQ (5 data bits per digit, low bit of the assembled value is the sign). Every field but the generated column is a running delta across the whole `mappings` string, so the map is walked once at load keeping the state at each line start (a minified bundle has ~100 lines); a lookup then decodes one line.
- The desk report model never reads `origin` from the body: anything arriving there is from the browser by definition.

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

Additions: a card's `seen` rows and `sheet` rows differ: sheets with no card behind them (status, review, usage itself) log a `sheet` row and nothing else, so `summary` skips labels never `seen` (scoring them would put a permanent "0/0" row in a table read as "days active out of days on the desk"). `sheet` is engagement that works for every band, since an unfoldable card is only ever one the desk folded; `open` is the ranking being told it got a folded card wrong. `idle` is the one automatic claim and is deliberately narrow: quiet on every day seen (>= 7 days) and never touched; anything with a day in needs/has is excluded because that card was being read. The list is sorted least-touched first because it exists to surface the bottom (burying it under used cards is how a panel goes unread). `MAX_BATCH` (200) bounds one POST since the desk buffers ~20 impressions on load and a stuck client must not turn a bug into unbounded writes.
The "N cleared today" count in the plan view's list header (asks finished by acting, not snoozing) is stored in the same table as kind `cleared`, but as a *set* per day, not a tally: acting on one ask twice (tick a habit, untick, tick) is one ask cleared, and an undo must be able to take it back out. So `set_cleared` writes the row outright (INSERT OR REPLACE / DELETE) instead of `record()`'s count-adding upsert, and the generic POST can't send that kind at all.

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
- **Cost is list price, not a bill, and the prices keep themselves current**
  (`app/model_prices.py`). No vendor has a pricing API, so a model's price
  is looked up in order: Claude Code's own cached list
  (`additionalModelCostsCache` in ~/.claude.json: input, output, both cache
  write TTLs, cache read, the long-prompt tier; Claude only), then LiteLLM's
  model_prices_and_context_window.json (the vendors' own list prices for
  OpenAI, Gemini and Anthropic, with `_above_<N>k_tokens` tiers), then
  OpenRouter's /api/v1/models (fills gaps such as GPT-OSS; its prices can sit
  a few percent over the vendor's, so it comes after LiteLLM), then the hand
  table `BUILTIN_PRICES` (Claude only, the last resort). Each list is cached
  in `model_prices` (derived, droppable), fetched again past a day (the CLI's
  hourly), lazily on page load: in line the first time, in the background
  after. A failed fetch keeps the last good rows (`model_price_fetches.good_at`)
  and records why. The two fetches are bare GETs: no headers, no identity.
  Matching is exact id, else the longest listed id followed by a date or a
  word (agy's `-high`, `-thinking`), never a digit, so `claude-opus-5` can't
  price `claude-opus-5-5`. A model nothing lists is **unpriced**, shown as
  such, never $0. A prompt over a model's long threshold bills at the long
  tier, except a delegate run, whose tokens are summed over many calls.
  "Caching saved" is the same calls with every prompt token at input price,
  minus actual. The "one model" line reprices every priced call, errands
  included, on the newest Opus, Fable and Sonnet in the live list and each
  outside provider's first-listed model; unpriced calls are on neither side.
  It ignores tokenizer differences, and says so. The page lists each model's
  price, which list it came from and how fresh it is.
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
  that sent it, so a tab's row includes its errands. Each run records the
  tokens its CLI reported: `tokens_in` (cached included), `tokens_out`, and
  `tokens_cached` where the CLI says (Codex's `cached_input_tokens`, Gemini
  CLI's `cached`; Gemini's `thoughts` count as output). agy reports no cached
  count, so its input is priced as fresh, and the page says so. agy's stream
  never names its model; its log does ("selected model override … label="),
  so each run logs to `.agy.log` in its scratch dir (the resolved path: through
  a worktree's symlinked data/ the sandbox refuses the file) and the label maps
  to an id through the stored model list. Runs from before that were backfilled
  from agy's per-run logs in its home. Codex's `--json` names no model
  either, so an account's run passes the model codex would pick there (the
  lowest-`priority` listed one in its home's `models_cache.json`) and records
  it. If the log can't be read, the page loses those rows and nothing else.

Additions from the comment pass (everything else is already in this section):
- A transcript record from before the TTL split carries only `cache_creation_input_tokens`; it counts as 5-minute writes (the CLI's default TTL then).
- A delegate run refused before any model call (bad `--model`, signed out) has no tokens and is skipped; as a call it would add a model row named after the typo. A run with no model recorded shows as "default model" (the CLI's unnamed default); its `tokens_in` includes the cached part, which is split out as cache reads.
- Account grouping: outside runs group by the account they ran on; Claude transcripts don't say which login wrote them, so Claude is one row ("all accounts"); outside runs without an account read "API key, or before accounts".
- The "one model did it all" counterfactuals are the newest of the opus / fable / sonnet families in the live list (`counterfactuals`), the fixed `COUNTERFACTUALS` ids when no list was fetched or it can't be read, and each outside provider's first-listed model.
- `model_prices.py`: LiteLLM lists every Codex and Antigravity model in use except GPT-OSS (hence OpenRouter as a gap-filler). A list missing a cache rate means the vendor charges no premium (OpenAI and Google cache automatically), so it falls back to input price, and the 1h write to the 5m write. The CLI list's fast-mode rates are ignored because a transcript doesn't say a call was fast. LiteLLM batch, flex and priority rates are ignored (other ways to buy). OpenRouter variants (`:batch`, `:free`) are dropped and dotted Anthropic versions renamed to the API's dashed form. BUILTIN_PRICES are prefix-matched ("claude-sonnet-4" covers 4.5 and 4.6) and writes are 1.25x (5m) / 2x (1h) input. `ensure_fresh` runs per page load: the local CLI list in line; a web list in line only when never fetched (or never worked), else in the background. Failed fetches retry after an hour (`RETRY_S`).

### Claude accounts (`app/claude_accounts.py`, `widgets/ClaudeAccounts.jsx`)

Several Claude subscriptions, one CLI login slot, and rules for which one
Kestrel spends. The CLI keeps its login in the Keychain item
"Claude Code-credentials"; ~/.claude stays shared, so accounts are not
separate `CLAUDE_CONFIG_DIR`s (those would split sessions, skills and memory).

- **Where a login lives.** Each saved account's OAuth tokens sit in a Keychain
  item of ours, "Kestrel Claude account", keyed by the account's uuid. app.db
  (`claude_accounts`) holds name, email, plan, priority and rules, never a token.
  Every 30s tick copies the slot into its account's item, so the item always
  has the newest rotated tokens. An unknown uuid in the slot becomes a new saved
  account, so adding one is signing in (Settings → Add account runs `claude auth
  login`, after stashing the current one).
- **A switch** writes the target's tokens into the slot (keeping the item's
  other keys), points ~/.claude.json's `oauthAccount` at it, and trades
  `claude_quota`'s live cache, so the chips show the new account at once. A
  running CLI keeps the token it holds, so a turn in flight finishes where it
  began; the bar's account chip reconnects idle tabs. If an old process
  refreshes and writes the previous account back, the next tick stashes those
  tokens and puts the chosen one back.
- **Usage for all of them.** The active account through `claude_quota` as
  before; parked ones with their own token, each with its own 60s throttle
  and 429 backoff. A parked token near expiry is refreshed here with the CLI's
  own client id (which also keeps a parked login alive). The slot's is never
  refreshed here: two refreshers on one refresh token log each other out.
- **Rules** (`check`, `pick`, pure): per account a 5h and a weekly ceiling,
  "only when its 5h window is idle" (idle, or a window Kestrel opened: it took
  the account while idle and has held it since before the window began), on /
  off, and a priority order. Auto uses the first account inside its rules and
  logs every switch (`claude_account_events`). When none is, `refusal()` is
  set: `agent.over_budget` (the #9 guard) pauses background calls and the
  console holds a new message back. An unread account in use stays put;
  a manual pick ("hold") turns the rules off until "Back to auto".

Additions: the CLI's OAuth client id and token endpoint are the CLI's own bundled config, so a parked refresh is the same request the CLI makes. `security -i` truncates long lines, so a secret longer than `STDIN_LINE_MAX` (4000) goes on argv, as the CLI itself does. A 5h window that began within `IDLE_SLACK` (120s) of Kestrel taking the account counts as Kestrel's own (its first call opened it). A stale usage reading errs low (usage only climbs within a window), the same bargain `agent.over_budget` makes. `~/.claude.json`'s `oauthAccount` is pointed at the slot's account best effort: the CLI rewrites that file itself. The slot's capture is safe to call at any time, since the slot holds the newest tokens for its account.

Additions (console / quota): console `start_model` swaps an unrunnable model for a runnable one and says so on the start line rather than failing the first turn; a session records the account its CLI started on so the bar reconnects idle tabs when the active account changes; a message sent while every saved account is past its rules is held back ("Not sent: …") like the #9 guard, and picking an account by hand overrides it. `claude_quota` keeps one `_LiveUsage` per saved account (own throttle and 429 backoff); `_live` is the slot account's and `set_active` trades it with the parked one so the chips switch at once. `console.note_refusal` answers the CLI's `<synthetic>` error turn naming a refused model with `model_refused` and the page retries on the fallback as a fork. `agent.ask` swaps an unrunnable model (logged) and retries once on the fallback for a refusal the catalog didn't know. open_tab's model/effort are checked against the session's CLI list, else the live catalog; fan_out runs Claude on a model the account may run and each outside provider on its Settings default (a stale one falls back to the CLI's).

### Models (`app/model_catalog.py`, `widgets/ModelDefaults.jsx`, `lib/models.js`)

Every place Kestrel chooses a model asks one module, so a new model shows up,
a retired one drops out and a model the account can't run is swapped, with
no code change.

- **Live lists** (`model_catalog`, derived), refreshed every 6h and on an
  account switch: Claude from Anthropic's `/v1/models` asked with the
  account's OAuth login (family = its `line`, newest = release date,
  `deprecated` left out); Antigravity from `agy models`; Codex from `codex
  debug models` (`visibility: list` only).
- **Access** (`claude_model_access`, derived): neither `/v1/models` nor the
  CLI's bootstrap differs between Pro and Max, so each saved account asks the
  newest model of each family for one token (about 35 tokens a probe), redone
  weekly and on a switch. A Pro account asking for Fable gets a 429 with
  `error_code: credits_required`; a plain rate limit says nothing either way.
  A turn the CLI refuses ("Fable 5.2 requires usage credits", a `<synthetic>`
  message) records the same fact.
- **Names.** A tier (`plan` / `decide` / `grunt`, Fable / Opus / Haiku out of
  the box), a family alias (its newest), or a pinned id (a retired one runs as
  its family's newest). `pick` then walks a refused family down the ladder
  (fable → opus → sonnet → haiku; a family it doesn't know ranks first) and
  returns a note saying so. Callers: `console.start_model` (the note goes on
  the start line), `agent.ask` (logs; a refusal it didn't know about is
  learnt and the call retried once on the fallback, never tripping the usage
  breaker), open_tab (refuses with the models that work), schedules
  (name checked at save), fan_out / delegate (`outside_pick` against the
  provider's list), the computer-use job. The background `*_MODEL` settings
  in `config.py` are the grunt tier, or a family alias.
- **Defaults** (`model_defaults`, the user's): per mode, per tier, per outside
  provider, in Settings → Models, checked against the live list on save. A
  default that's gone or refused shows what runs instead under its row.
- **Pickers** (bar chip, retry with…) take the CLI's own list as before and
  show a refused family disabled, "not on this account". A console tab told
  `model_refused` answers the same message again on the fallback, as a fork.

Additions (all other `token_usage.py` reasoning is already in this section): 
- Two trap notes beyond those listed: the transcripts cover every Claude Code session on the machine (Kestrel tabs, `entrypoint: sdk-py`, and the terminal's alike); there is no API cost in a transcript and on a subscription there is no bill, so "cost" is a yardstick for comparing, not money spent. Cache writes are priced off input (`WRITE_5M` / `WRITE_1H`); older transcript records carry only the total, and the CLI's default cache TTL then was 5 minutes. `PRICES` are matched by longest prefix of the model id so dated ids ("claude-haiku-4-5-20251001") resolve; they come from the claude-api skill's table, and a model not listed is counted in tokens and shown unpriced rather than guessed. Kestrel's own one-shots (`agent.ask`) run with the backend as cwd, which is how they sort into "background". `mode` is one of general, study, dev, terminal, background, night, other. Tiering is about Claude's tiers, so only Claude's calls are repriced and compared with Claude's own cost. The 5h / weekly range start comes from the live window's `resets_at` (`_window_start`), else a rolling span.

Additions: `LADDER` (fable, opus, sonnet, haiku) is the fallback order; a family the list adds that isn't named there ranks above all of it. The access probe must send a Claude Code system prompt ("You are Claude Code, Anthropic's official CLI for Claude."), because the subscription login only answers a Claude Code request. A failed list fetch keeps the last list and only records the attempt. `outside_pick`: an empty name takes the provider's Settings default, else its CLI's own; a listed name runs as is; a retired id runs as its family's newest with a note; a model neither listed nor of a listed family is refused with why. Claude's list is the same list the CLI's own `console.session_models` shows.

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

- `NO_TOKENS` matches the message `oauth.py` writes for a terminal (`no tokens for <provider>/<account>`); `WidgetError` answers it with a connect button and `errorSummary` folds the same way. A failed fetch is `NEEDS` whatever the card would report; a broken credential and a broken widget must not look identical.

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

- **Why two gates (statistical AND practical):** resting HR sits in a 68-76 band, so a purely statistical test calls +3 bpm a two-sigma event and the card cries wolf; steps swing 2.7k-22k, so a purely practical test with a floor low enough to catch a real collapse fires on an ordinary Sunday. Together: "unusual for this metric AND big enough to say out loud".
- **Baseline excludes today.** `score()` accepts `{day, value}` series (reading excluded by `day` key) or a bare number array (last point assumed to be today, the sparkline convention). Leaving today in drags the median toward it and hides its own deviation.
- **`MIN_BASELINE` (8):** with a handful of points the median is one of the points and MAD is often 0; a card making claims on day three of a new sensor is how screen time and location got caught calling dead-source gaps a behaviour change.
- **Rarity is an empirical p-value** (same direction only, so 2k-step days don't make a 21k day ordinary); z stays as a ranking key, not the arbiter. `lead()` ranks by |z| because units are not comparable and a fixed-order favourite metric is the old desk one level down.
- **`tone`** is whether to render a concern (`worse` direction), null where neither direction is bad (distance, minutes listened). `asFar` is surfaced so the card can say "first time in 28 days".
- **`restLine`** is also what `Slot` shows when the card folds: a folded and an open quiet card say the same thing, from live data, never an empty state. **`deltaLine`** names the day count ("vs your 17-day median") because n varies with sensor wear and "typical" can't be audited.
- A null in `series` is a gap, never 0: treating a missed day as 0 poisons every later comparison.

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

PlanApp is also the window's tab orchestrator. Facts moved out of its comments:
- **Console states** `read` (console beside the map), `peek` (folded to its bar), `full` (takes the window). ⌘J toggles read/peek, a click on the bar too; ⌘F toggles full (it shadows the browser's find on purpose; find still works from the menu). The prompt stays on screen in all three. The hybrid layout opens peeked. A question card raises the sheet (it can only be answered in the conversation); a permission can be answered from the bar. Going to a tab raises a peeked sheet.
- **Heights are measured, not guessed.** `--top-h` (the floating top bar: one line, two with the focus line, more when wrapped) and `--dock-h` (the folded console's bar plus the composer, which grows with queued messages, the task strip and a long draft) are set on `.plan` by ResizeObservers hung off ref callbacks (an external system, so no effect). The rooms desk lays its pages and the open console under `--top-h` and leaves `--dock-h` at a room's foot so the dock never covers the last card.
- **Unread marks.** A reply doesn't raise the sheet. A turn that ends where you can't see it (sheet peeked, or a tab that isn't showing) marks that tab unread (tab, bar, Kestrel) until its conversation is opened; kept per tab because a background tab finishing is the case worth a mark. The mark is cleared in render so it's gone in the paint that shows the conversation. Turn ends arrive over Console's socket as `trace.tabs` changing, which is why this is an effect.
- **Notifications.** A tab finishing while Kestrel isn't in front notifies, but only for the user's own tabs (#79): a spawned tab reports to its parent through the handoff unless the user has typed in it (`engaged`). Asks notify from any tab, once per request id. `notify.at_desk()` gates the rest (a locked screen collects none). A child stopped on a question tells its parent once per question.
- **Closing/resting a tab** is refused mid-turn or while waiting (unmounting would cut the turn off). The socket's close sweeps the repo (`console_worktree.release`); what it leaves is the left-behind list's, asked for with the closed tab's tree after the sweep. Resting keeps the worktree: the tab is coming back. Ticker once a minute, hidden or not: wake due snoozes, settle tabs (`lib/tabRest.js`).
- **Handoff admission** (`open_tab` → `handOff`) lives here because only the window knows the caps: a free tab slot (`MAX_TABS`), fewer than `MAX_WORKING` tabs mid-turn (all draw on one 5-hour window). Past MAX_WORKING the brief queues (greyed under its parent, started in render by `nextQueued`, an own-state update so a freed slot is taken before paint); past MAX_TABS it's refused, and the model can see the tabs and close finished ones (`manageTabs`, `console_tools.tabs`, #57). The id is made in `handOff` so the model hears it (`message_tab` names it later); `model`/`effort` ride on the tab so a queued tab still has them. State is read through `capsRef` so callbacks stay stable and memoised panes don't re-render per turn event; a second `open_tab` in the same turn reads the ref because state hasn't rendered. `manageTabs` refuses closing a tab that is working, waiting, on screen or the asker (the strip's × rule plus "not what you're looking at"); closed sessions stay resumable from the sessions panel. `backgroundTab` (⌘Enter, #93) is the user's tab, not a child: no parent, nothing reported.
- **Reports.** A `held` report isn't one (the brief's turn ended short: error, stop): the tab is marked waiting and the parent hears nothing until a turn finishes it. A finished report is where a child closes itself (`lib/children.js landing`): work landed (no commits beyond base, nothing uncommitted, no `Needs you:` line) → done, closed into the parent's group; else it stays open marked "needs you" and the report says why. Never for a tab the user has typed in.
- **Waiting cards** (from every tab, so a background tab's card is on the list) head the list as `blocking` and answer into their own tab. One modal at a time (two would fight over keys): the showing tab's, else the first asking.
- **One cause, one row.** When Messages can't be read the People row carries the fix and sorts as the broken source; two rows would read as two problems. Usage-limited tabs minus any a quit also cut off get one row (the quit row's Resume already covers them); with auto-resume (`lib/autoResume.js`) each tab's banner resumes a minute or two after the reset. The status bar carries Resume for the tab on screen because a folded console hides the banner. Interrupted tabs: one Resume row plus a banner each; nothing resumes without the click; settled ones leave at once (here and on disk).
- **Usage logging.** Each card's band (needs/has/quiet) is logged via `seen()` only once the card has reported: before that it has no band, and a guess would make `days_active` never zero. usage.js dedupes per band.
- **Snooze options** (max four so keys 1–4 cover them): a clock gets minutes, a deadline its eve, anything else the end of the next event (only while the calendar card shows today), tonight, tomorrow. The map's badges count unsnoozed asks; a card whose asks are all snoozed draws as `today`, dashed.
- **Esc** unwinds the top: full console (unless a card is waiting: Esc is its "no"), newest full view, then card/Sources; the palette owns it while up. Inside the console Esc is the console's, except leaving full screen. A new picture mid-turn brings Claude's view up unless it was closed during that turn (a browser session sends one a step); a computer-use job asking for an answer opens even a closed pane; a new preview ask from the tab on screen opens it (switching tabs isn't one). ⌥T/⌥W/⌥1–9 use `e.code` because ⌥ remaps the key itself on a Mac, and not ⌘ because Chrome keeps ⌘T/⌘W/⌘1.
- Day arithmetic (`dayBefore`) runs at local noon and reads local parts: DST never tips the date, and `toISOString` is a day off east of UTC.

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

- Snoozes (`app/snoozes.py`): the list has one way out per row: act, snooze, or open. A snooze makes a permanent-amber card livable (a habit that needs you can be pushed to tonight instead of glowing all day), so the list stays strict about what it shows without nagging about what you've already decided to leave. Times are local wall-clock ISO datetimes; "tonight", "tomorrow", "the day before a deadline" resolve in the backend against `db.day_key()` and its 4am rollover, never the browser clock (between midnight and 4am the browser's "tomorrow" is a day ahead of the app's). Presets: tonight = 20:00 (when the evening nudge starts, HabitsWidget's `evening`); tomorrow and a deadline's eve = 08:00 (a working morning, not the 4am the key begins at). Wake takes a body rather than a path segment because ask ids carry colons and may carry slashes (note paths).

Settle rule: acting holds the list's order (the finished row stays, marked done) until the pointer leaves the list, or `SETTLE_MS` (6s) passes for a keyboard-only run; only an action that does something finishes a row (opening a card, sheet or draft doesn't). Keys j/k move, ↵ the row's first action, s then 1–4 snoozes, o opens, Esc backs out; the keys listener is capture-phase so a handled Esc doesn't reach the stack's Esc (which would close the side column); a focused control's own Enter wins and a waiting approval card keeps Enter and Esc; the key hint shows only once you're on the keys. The quiet cards fold away (the map shows them), remembered per browser (`needs_quiet_open`). A dragged row sends the ask minus its button functions (they don't survive JSON). Row hover lights the card's map node and the other way round (`hot`, via PlanApp). `BriefLine` strips the trailing age from reasons ("waiting 27 h", "in 12 min") and leaves out `console:` rows: left in, every hour is a new set of asks and a new model call, and the console rows (seconds-long, on screen already) were about half the background calls (#53). The rooms desk: `Today`'s order is approvals, cut-off tabs, unmerged branches, then each room's asks in sidebar order, a card no room holds last rather than missing; `FocusLine` follows the same order, a stepped-past ask goes to the back not away; a card's pill reads "snoozed" when every ask is, so it can't contradict its room; the rooms desk only adds siblings in `.plan-main` and moves panes with CSS (`plan/desk.css`) because re-parenting the console would remount it and drop live sessions.

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

(Bullet under the Console chapter; append these facts to it.) Two dev tabs sharing one checkout sweep up each other's edits with `git stash` and leave the tree unbuildable; a tab's own worktree means a stash, reset or checkout in one can't reach another's files. JetBrains Air does the same (`.air/worktree.json`); this copies its model as `.kestrel/worktree.json`. With no config file, `.claude/settings.local.json` is the one linked path (the allow-rules a dev tab already reads). The directory is in the repo's `.git/info/exclude`, so the main checkout's status and rg never see it; link paths are excluded individually because `.venv/` in .gitignore matches a directory and a symlink to one is a file to git (`git add -A` would commit it). Setup never refuses: a tab whose venv didn't link can still edit, so problems return as notes for the start line. Links are removed before `git worktree remove` so nothing can follow one into the main checkout's data, and restored if the removal fails. A tab's tree can sit on another branch (a site repo's tab trees sit on `try/*`), so status/merge use the tree's current branch, and the made-on branch is deleted too only if merged. `review` reads only (the tab may be mid-turn); `preview_conflicts` is a preview, since `merge` rebases commit by commit and can stop on a change a later commit undid: its answer counts. Conflict-region parsing matches markers with their labels (a line that merely starts with `<<<<<<<` isn't one), strips `\r`, and returns nothing for non-line conflicts (modify/delete, binary). A resumed session gets its tree back at the same path (Claude Code files a transcript under its cwd; `for_session`). A branch is "held" when a socket here uses it or its git lock names a live pid (Claude Code locks a `--worktree` tree while its session runs); a lock naming no pid was set by hand and holds. Discard forces twice: past uncommitted files and past a dead holder's lock. `release()` notifies waiters under `_lock` so `wait_released` never sees the gap between the count dropping and the removal. The module is stdlib-only and py3.9-importable because the SessionStart hook loads it outside the venv (hence lazy `console_diff` imports).

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

Extra constraints moved out of comments: node labels are monospace so a label's width is its length (`LABEL_CH`), used to keep a label inside the pane near an edge. `spread` treats each node as a box (its size plus its name) and pushes overlapping pairs apart along their shallower side, so a roomy pane keeps the plan exactly. Hub dots orbit at `HUB_ORBIT` (26) clear of the halo and the hub's words slide aside while dots exist (`.map-hub`). The ring is right-aligned: the newest sync is always the last segment. The ground's hill height is the need (asks add height, so a queue outgrows a single row; machine nodes raise nothing; Kestrel always stands) and the warm tint was cut to half strength (docs/ui-overhaul.md): the tint hints where to look, the node's ring and count say what. `trace.live` is a new object each render, so the map keys on node ids. Drag listeners read `cards/sal/onPin` through refs so they subscribe once; the click a drop release fires is swallowed (the next press lifts the flag). A dragged node carries the summary and payload only, not the hook's functions. `dotMotion.js`: one loop owns the motion because a CSS flight and an SVG orbit couldn't see each other; it writes `transform` straight to the element once a frame and never goes through React (a turn changes state many times a second); it runs only while a dot exists; `sync()` is called from a layout effect after each map render so a new dot is placed before first paint; home runs back down the same line (`bend`, shared with PlanMap's lines), not its mirror; in flight the orbit circle closes so the dot rides the line and reopens on arrival; dwell/settle: only the way home waits, a new call is immediate. `MapTerrain` colours are read off the canvas (not the root) so a ground's own tokens in `looks/grounds.css` win and a theme flip lands within a second; `mapGrounds.js` painters get `{field, nx, ny, step, width, height, hills, colors, t, working, mem}`, `mem` living as long as the ground, `fps` the rest rate; the tint is the nearest tinted hill's reach so the hue stays a hint.

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

MoneySheet details (`widgets/MoneySheet.jsx`):
- Six reads per range in parallel (`VIEWS`); `balances` and `bills_due` ignore the range (snapshots of now) and are asked anyway to keep one code path. All are SQLite reads against app.db: `finance.get()` makes no API calls (the cursor model keeps the cache complete to `last_synced_at`). Default range is a month: rent, statements and paychecks land on it.
- Plaid stores money leaving as positive; the app shows outflows negative, so callers flip the sign before `fmtMoney` (lib/units, shared with the card). Chart labels use a true minus sign (−).
- `NOT_SPENDING` mirrors `SPEND_EXCLUDED_CATEGORIES` and `ASSET_TYPES` mirrors `NET_WORTH_ASSET_TYPES` in `providers/finance.py` (`current` on a credit/loan account is the amount owed). The category breakdown is grouped in the browser from transaction rows (no per-category endpoint), past eight categories the tail is summed into one bar. A lone first net-worth snapshot renders as a hero stat, not a one-point line. Charts: daily spend bars up to 31 days (zero-filled to today, dotted average), weekly net cashflow beyond (a quarter of daily bars is a comb); cashflow polarity is carried by sign and label, not colour alone.
- **Claims and the reviewed line.** Claims/reviewed marks are their own read on the "expenses" topic so a claim from the sheet or the Expenses card redraws both; failure costs only the claim buttons. Only a *waiting* claim can be taken back here: a sent one must keep matching the spreadsheet already sent. A pending charge can be claimed (the sync moves the claim to the posted charge, `expenses.follow_posted`); money in cannot. The reviewed line sits above the newest charge you've been through (on screen at the last export, or when you said nothing to claim). An unreviewed row below it posted late (Plaid dates a charge by swipe, not post) and gets a dot; within a day unreviewed rows sort first so the line splits days cleanly.
- **Hovered row is tracked in JS, not CSS `:hover`.** WKWebView leaves `:hover` stuck: WebKit doesn't recompute it when the list scrolls under a still cursor and a native window can miss the pointer leaving. So `useHoveredRow` re-asks `elementFromPoint` (rAF-throttled, one per frame) on scroll and clears on pointer-leave or window blur.

Music sheet (`widgets/MusicSheet.jsx`) and the other desk cards' notes:
- **Music.** Spotify's six metrics answer two questions, how much and what; the generic panel made you pick one at a time and read `top_repeat` as a word (a day of listening looked like a schema). The sheet puts day totals on top, an hour histogram for when you listen, and artists/tracks ranked against each other. `bracket` (first/last play of the day) is deliberately unread: the hour histogram says it in more detail and a bracket past midnight can't sit on a midnight-anchored axis without lying about its day. `played_at` is UTC with a Z and has no zone (unlike calendar events, which the backend re-expresses), so the browser's zone buckets the hours; a play abroad lands in the hour it would have been here, the honest read of "when do I listen". The histogram always has 24 buckets (dropping empty ones would put 2am beside 9am). Top-N is keyed on the Spotify id so same-titled tracks don't merge. `minutes` is the provider's `listen_min`, never re-summed (two derivations of one number can print differently). Novelty over a range is the plays-weighted mean of daily shares (a two-song day must not weigh as much as a two-hour one). Genre shares come from Last.fm tags (a crowd's word for an artist) and the note says how much of the range is tagged.
- **TokenUsageSheet.** Leads with two questions: did model tiering save anything (the lede reprices the same tokens as if one model did it all) and where did the 5h/weekly window go (range chips start on the live window when the usage chips know its reset). "Cost" is list price for the tokens, not a bill (nothing is charged per token on a subscription); the note under the table says so every time. Unpriced calls are said, never shown as $0 (a gap in the number, not a free call); price provenance and freshness show so a number can be checked.
- **BrainWidget.** Facts read off a text/email/note wait as "unconfirmed" until ✓ but are already used, hedged, so to-confirm is the "today" band, never "needs you". ✗ makes a tombstone ingest never relearns; it is held for the undo toast because rejecting can also revive what the fact superseded, so it is sent when the toast goes, not reversed after. Review goes a page at a time (the backfill brings hundreds: "✓ the rest of Jamie" keeps it an evening, not a chore); the page's facts refetch when the page changes since useLoad reloads only on mount and the bus.
- **FoodWidget.** Writes go on the `food` topic, and closing a day also on `habits` (that check-in is the Log food habit). A day with nothing logged has no totals: `NaN%` is a width the browser drops and the fill would span the track. B12 (2.4 µg) and omega-3 (1.6 g) targets need decimals or they read "1/2".

Health sheet and the small task/habit/list/debt/activity cards:
- **Health sheet (`widgets/HealthSheet.jsx`).** Fitbit exposes fourteen metrics and the generic panel showed them one chip at a time (`sleep_deep_min` on screen as a word). They are sleep, movement, and the two numbers for how the body handles both; the sheet reads nine at once as those three, latest day on top ("how am I doing" is asked far more than "what was Tuesday"). Nothing computes a score. The latest reading is compared to the *other* days: including today in its own baseline drags the baseline toward today and shrinks every delta, most in a short range. "Better" direction is null wherever it depends (a resting HR that fell 8% may be fitness or a bad reading, and green would claim knowledge the app lacks). The size of a move is stated only when big enough ("1% vs 73 bpm" is noise). A night with no stages is dropped, not drawn as a zero-height night.
- **Lists.** The card opens on the seeded buy list (first chip); user-made lists (packing, a party) get their own chip rather than categories on someone else's list. All lists arrive in one `/api/lists` read (a few dozen rows), so switching is free and the card height doesn't grow. Selection is by id (a list deleted elsewhere must not shift it) and per-mount (first chip is the right landing). HAS on any open item: a shopping list is never "needs you" (as a folded HAS on the old rail desk it drew zero opens; revisit if the usage sheet shows that again). A rejected name (duplicates refused server-side) shows next to its box. Deleting a list cascades its items, so the count goes in the undo toast ("deleted Party" reads harmless when it's eleven things); held via `holdDelete`.
- **Debts.** Personal IOUs, deliberately not `fin_liabilities` (Plaid's institutional debt); owed beers are their own table and section, never in dollar totals or the chase list, and never raise the card past HAS. NEEDS is about your move, not the money: a standing balance is true daily and actionable on none (the card took 44 impressions and 3 opens); a debt never chased or chased a week ago is due, and once all are chased recently it drops to HAS until the week is up (`to_chase` is the same test). Day arithmetic (`nudge`, `reminded_days_ago`) is the server's: the browser's midnight disagrees with day_key for four hours nightly. A nudge goes out under your name so it's drafted to read and send yourself; "ask" records it. Quick-add: trailing number is the amount ("Rowan 24 pizza"); beers ("Theo 2 beers ski trip") are checked first so "2 beers" isn't $2.
- **Habits.** Writes broadcast on `habits` so the glance strip's "habits left" moves. One call feeds toggles and strip (history's last day is today). Unchecked habits are "today", never "needs you" (routine, not deadline); `evening` (8pm to 4am) changes only the summary framing. `created_day` is the server's day_key: `created_at`'s date runs a day ahead before the 4am rollover and rendered the habit's first day blank. The 11px mark sits in a 24px target so a past-day edit needs no aim (toggle offers undo). Delete's confirm names the streak ("delete Exercise" reads harmless as a 40-day run) and offers archive, the non-destructive option; the add form sits outside the empty/non-empty branches because empty is when adding matters most.
- **Tasks.** Writes broadcast on `tasks` (glance open count answers to it). NEEDS only for dated work (overdue is a clock; due today is a clock only with a time, a bare date has all day); the capped "available" pick is HAS. A deadline passed today is hours late, not "0 d". The quick-add day key comes from the payload (4am rollover is the backend's, `tasks_today`), and the card echoes what `@` was read as (an unreadable `@` is said, or the phrase sticks in the title). The "N more" are open but not on today's board.
- **Activity.** Things the backend did on its own (`app/activity.py`) are applied first and reviewed here; "needs you" only while unseen (they already happened, so seen ones are history). The summary's lead-in ("Saved a note:") is dropped since the tag says it; links show host plus 28 chars of path; "(photo)" placeholders are hidden beside a thumbnail; at most three thumbnails (a dozen photos pushed the rest off the card); HEIC arrives as JPEG (`routes/attachments.py`).

Cards that lead with a conclusion (health, trends, location) and desk plumbing:
- **Health card.** Four metrics in a grid every day are real numbers on a useless card (nothing says whether 71 bpm is your ordinary 71), so one `/api/health/summary` call brings today plus 28 baseline days, `lib/notable.js` decides whether any of it is unusual, and the card leads with that or says nothing; ranges, sleep stages and the rest stay in the sheet. HAS is earned: "any metric non-null" is true every synced day and would park the card in "today" 365 days a year; a body doing its usual is QUIET and the fold line still carries the numbers. `floor` per metric is the smallest change worth a sentence and lives in the card because it knows 3 bpm is a lot and 3 steps is not; resting HR is the tightest band (68-76 over a month) and the most diagnostic (a few bpm over your median is the early-illness tell), so its floor is low and `worse: "up"`. Sleep is attributed to the wake-up date, so "today" is last night. The pin carries all four metrics and 28 days though the card shows one reading.
- **Trends card.** Narrowed to energy (0-100 readiness from sleep, resting HR, HRV): sleep and steps are the health card's headline metrics and drawing them here would announce the same short night twice in two voices (the don't-answer-twice rule), so they stay only as the muted inputs line. Energy is 0-100 so a flat floor of 12 (about a night's sleep debt) is meaningful across the range. A calendar month is the only anchored window long enough, but on the 3rd it is three days: ask this month and, if it can't reach `MIN_BASELINE`, prepend last month (second call only in the first week). Spans resolve server-side from `db.day_key()`; the frontend must never compute a day key off the wall clock (a day out between midnight and 4am). The last valued point is today; later days are gaps, not zeros. `TrendsExpand` isn't a `useSheetData` sheet (it multi-selects across `/api/trends` for energy+habits and `/api/health` for health series) but keeps the newest-request-writes guard because `/api/health` syncs first and a slow earlier answer can land last.
- **Location card.** OwnTracks reports movement, so every number is a lower bound and silence is not a failure: a phone sitting home sends no fixes, which looks exactly like OwnTracks dying; the last fix's location breaks the tie. On the lede form distance/places/at-home/pattern are a muted line unless one broke pattern; out-and-back clock times, the entropy label and the 28-day trend live in the sheet. Two things print unconditionally because neither is a measurement: the coverage line (the honesty guard stopping a lower bound reading as fact) and "still home" (live state). An ongoing stay at home wins the fold line over a past day's mileage. Durations that run into days don't use `fmtDur` ("40h 0m"). **Location sheet:** never draws a zero it didn't observe (`finite` drops nulls), words the away window as the first fix elsewhere and last fix home (not leaving/arriving, a few minutes either side of anything recorded), and gives every day with any fix a row even with no away window (a blank track is "the phone never left"; dropping it would shorten the week). `ENTROPY_BANDS`/`_entropy_label()` in `app/providers/location.py` hold the cuts (~1.5 an errand and a bed, ~3 a day with a shape); minutes are counted from 4am (the day-key start, see `SpanBars`).
- **People card.** The card leads with the freshest waiting thread (`/waiting` is longest-silence-first, the full view's "who have I neglected" order, the wrong one for what you can still act on today). Dismiss is daily so it lives on the card and is optimistic (low-stakes, high-churn; undo is the expansion's "show N dismissed"); mute is permanent and stays in the expansion. Only messages carry a preview; email rows are threads you sent last with nothing owed back.
- **Brief card.** Generated lazily on the first open of each day_key and cached in app.db; `GET /api/brief` kicks generation off and the card polls (`POLL_MS`) while a model call writes it (tens of seconds). DB-only, never touches the vault. SYSTEM constrains output to `##` headings, `-` bullets and `**bold**`, so a tiny renderer covers it; parsed into a lede plus per-heading sections (not a flat node list) so the card can use the two-column grid and clamp by whole section, never mid-bullet, which the console's flat `renderMarkdown` can't do. The inline renderer is the console's: the model writes links despite "no links" and a literal `[Kagi](url)` was the tell. It is the only model-written prose on the desk, so the badge says so and names its sources. Shown whole (bounded: 250 words, six sections); sections drag separately so "expand on this" doesn't carry the rest. Section `text` is kept beside the rendered heading because the fold label must be a string.
- **Usage sheet (desk ranking).** A card read at a glance leaves no click, so "least clicked" would rank the best cards as cuts; each row shows band history beside touches, and `idle` (the only automatic verdict) needs both that the desk judged the card had nothing to say every day seen and that it was never opened. `days_seen` (time on the desk; a card added last week has no month) and `days_active` (days it had something to say) are two denominators on purpose; a low active ratio is a card usually correctly folded. Nothing deletes or hides a card.
- **`views.jsx` / `cards.js`.** `SHEET_TITLE` keys are what `openSheet()` takes (card expand buttons, ⌘K); for provider-backed sheets the key is the registry name, which isn't a title ("spotify" is the database talking). Day drill-down is keyed `day:YYYY-MM-DD` and a note `note:<vault path>` so the stack's rules hold unchanged (two days are two sheets, re-opening reuses one). No generic fallback: a new sheet gets a case in `SheetBody` and a title. Sheets that write (`SHEET_TOPIC`) must refresh their card on close. Every widget exports `useXCard()` (fetch, refresh subscription, level, fold line) and the component is a view over it, so the desk can rank, summarise and list asks without mounting a card and opening never refetches; the registry object is constant so hooks run in one order. Card hooks run in the app's render outside any ErrorBoundary, so an unreadable payload would blank the page (console included): each hook makes all its React calls first and derives after, and `cardHook` catches a throw so the card renders as an error. It is `cardHook`, not `use`, because rules-of-hooks reads a bare `use()` as React's. A failed fetch outranks what the card would say. A card mid-fetch keeps its last reported `{level, summary}` (the calendar clears its payload on a day switch) so it doesn't hop bands; the write is idempotent so safe during render. The brief isn't in `DESK_ORDER`: it summarises everything and leads whatever the day holds. A card in the needs-you list gets no rows at other levels; one needing you with none built (or broken) gets a single row that opens it.

Remaining sheets and settings panels (`frontend/src/widgets/`):
- **Email sheet vs review queue.** The review queue acts (archive, bin, unsubscribe) one card at a time and holds only the inbox; the Email sheet answers how much mail a week was, who sent it and where a message went, over any range, with no buttons that change anything. Triage wants one message at a time, search all of them, so keeping them apart means neither compromises. Search ranks over every cached message (subject, sender, summary, label, body terms), not the range; Gmail's own search is one click further and runs itself when nothing local matches. Gmail hrefs carry `authuser=`, which arrives after first render, so the list re-renders. A day's bar splits read/unread within its height (the unread part is still yours). The Email card is one row for the whole queue (seventeen rows would bury the list), its reason names the oldest, and it follows the `email` topic the queue broadcasts after every action (`/api/email/queue` is an app.db read, no Gmail round-trip).
- **Sleep stages / Patterns / Weather / Sports.** Sleep stages are UI only over `/api/health` series (deep/rem/light/awake, `providers/health.py`): depth is a single-hue blue ramp (deep darkest), awake neutral gray because it isn't sleep, legend + labels so colour is never the only cue; a 1.5px surface-coloured stroke gaps segments without distorting heights; nights align by wake-up day and a night with no stage data is a gap, never a zero-height bar. Patterns shows significant Pearson pairs with sample size and a "tentative" flag per r so a 29-day coincidence never reads as a law. Weather draws each day as a low-to-high span on one shared scale (a cold snap is visibly left; two lines make you subtract); rain under 10% stays blank. Sports shows the whole year with no range picker (a season doesn't line up with a week or month) and keeps exhibitions on the schedule but out of a record.
- **Music card.** HAS or QUIET only (listening is something you did, never a to-do). The top track is the one line a person reads; genres, last-played and the week are the sheet's. One baseline series of daily minutes (the backend keeps per-day totals, not play counts), so `plays` is scored against minutes scaled by today's minutes-per-track: approximate, and it exists so a day of forty half-finished skips can still lead.
- **Settings.** Choices made once: no config you could only break, nothing a card does better (Sources keeps connections, the console bar per-conversation controls); "never ask" is only on the console bar, where its warning is. Preferences stay in per-browser localStorage; the Claude login belongs to the CLI and is only asked about (`app/claude_account.py`) and stays in the Keychain. Providers: each row says whether a CLI is installed, signed in and on, and what gets it there; Codex and Antigravity sign in per account (agy opens Google's page itself, the callback lands on localhost, device code is fallback only). Models: a default that's gone or that the account in use can't run says what runs instead rather than breaking a tab; a pick the browser remembered from a tab's model chip stops overriding a changed mode default. Phone: the door is off until switched on; three rows (Tailscale up, Serve points at the door, a phone paired) and the Serve command is the user's to run, since this app never changes Tailscale's configuration.
- **Vault notes / NoteTab / Generated inbox.** The vault's way in from the desk (Vault node, ⌘K): search, pinned notes (Obsidian bookmarks), then `06-Generated/` until filed; all reads `index.db` (derived). A note tab edits nothing: "Open in Obsidian" goes through `openNote()` → `/api/vault/open`, never a plain `obsidian://` link (Chrome prompts every click, the desktop app ignores the scheme). Comments (`<!-- -->`, `%% %%`) are hidden as in Obsidian's reading view (tasks.md's banner and task ids rendered as text). Only the header drags (on the whole article, selecting text picked the note up). A clear Generated inbox stays a labelled line so "nothing to review" differs from "not rendering"; notes to file are "today", not "needs you".
- **Watchlist** never asks for anything: a reference list outside the day's ranking, reporting what it holds in one line; `services` rides every row (what can I put on right now).

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

- The rollover check asks the server for `day` from `/api/healthz` because the 4am rollover is a backend rule the client must not compute; it also runs on a 5-minute interval as a backstop for a window focused straight through 4am; offline fetch failures are ignored until the next wake.
- `useTopic` keys its subscription on the joined topic names so an inline array literal doesn't resubscribe each render; `useLoad` reads the loader through a ref so the subscription stays stable. `useWidgetData(source, topic)`: `source` is a URL or a promise-returning function (read through the ref, so a state-dependent URL is picked up on the next reload); a successful load clears `error`; `setData` is for optimistic edits, `setError` for failed mutations.
- `onTopic` is for code outside React (lib/look.js), installed once, never removed.

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

- Look lab frame: a round of looks ships as CSS scoped to `html[data-look=...]` (frontend/src/looks/); you live with them and flip between two; each verdict lands in `look_votes` with a note. The next round's dev tab reads them (`GET /api/looks` or the table) to drop losers, keep winners, try the next variable. Which looks exist is the frontend's registry, not data. A theme is a look like any other: `data-look="theme-<slug>"`, a chip in the lab's Colour row, votes under that id; which theme is on (`active`) is held in the backend, not per browser, so the one Kestrel set is the one the window shows.
- Free CSS is refused anywhere on the path because guessed CSS is what failed in the tool this was learned from, and its agent said "fixed" anyway. The tool result is a WebKit picture (`specimen_shot`) so "done" can't be said unseen. Contrast pairs follow the baseline's design (docs/ui-overhaul.md); a pair the theme didn't touch is the baseline's and skipped. `CHECKS` thresholds: 4.5 text, 3 incidental text/boundaries/graphics, 7 where the baseline holds body text; the baseline clears every row in both schemes. `SCHEMES` is dark first (where the app is used). `THEMES_MAX` is 24 because the lab's Colour row is one line of chips, not an archive; the same name again replaces a theme. Theme hex values are stored uppercased.

- webshot: the theme tool hands its result to the model as a picture rather than "applied", because the lesson it was built on is an agent that changed colours, never looked, and said it was fixed. WebKit because the .app is a WKWebView, so it is the engine the user sees. It is a subprocess of its own (`shoot()` is the way in) since AppKit wants the main thread and a run loop, which uvicorn's threads lack. Loopback only: it loads a URL in a real browser engine with the user's network access. The subprocess has no Dock icon/menu bar and is never frontmost (must not take focus from the window being typed in); non-persistent data store (no cookies/localStorage shared); prefers-color-scheme follows the view's appearance, not the system's; a view outside any window snapshots blank, so it sits in an off-screen never-shown window; one pixel per point (the Retina default doubles bytes for a picture a model reads no better); after the ready flag it waits one more beat (0.3s) so the frame that set it is painted. `SHOT_SECONDS` 25 covers the whole subprocess including page load.

`looks/index.js` registry: four axes, each an attribute on `<html>` so they combine freely: LOOKS (`data-look`, rounds 1–3), LAYOUTS (`data-layout`, rounds 4–5), GROUNDS (`data-map`, round 4), AGENT_STYLES (`data-agents`, round 5). Votes are stored under the variant's id, prefixed for layout/ground (`layout-rooms`, `ground-dots`), so `look_votes` reads as one history. Round 1's winner Cupertino became the baseline palette in round 2 with the token system in `docs/ui-overhaul.md`; retired round-1 looks: `git show 914683d:frontend/src/looks/`. Round 3 opened the accent (accents.css). Round 4 moved the layout (four answers to clutter: Today, Rooms, Tiles, Canvas, since retired, each keeping every card, sheet, ask and the console reachable) and five alternatives to the contour ground. Round 5 combined the winners into the hybrid layout (Rooms' sidebar as structure, Today's sentence glance as header, Canvas's centred ask bar and floating glass bar, plus three takes on Needs you and an agent visualizer); the hybrid is the desk now (Needs you inline per room, the most urgent on the top bar); the round-4 concepts are in `git show c1423ce:frontend/src/plan/Concepts.jsx`; Classic stays as fallback; the agent visualizer is a setting, Orbs default (it draws only while something works). Each layout says what it cuts and where that went: every function must stay reachable. The lab: pins two layouts to flip (⌥⇧L), steps the map ground (⌥⇧M), opens from ⌘K, takes focus on open so Esc works without a click, and shows a chip only on the Classic desk (on the rooms desk it covered the composer's send button). `AgentLayer` (the visualizer): each working tab and live subagent is an agent; its current call's map node (`lib/turn.js nodeFor`) resolves to a place on screen, best first: the card `[data-card]` (room page, tile or map node), an ask row `[data-ask-card]`, its room's tile on Today `[data-room-door]` else the sidebar `[data-room]`, the vault → Notes `[data-room=vault]`, else the console's ask bar; a write also rings the element. Fixed elements positioned from the DOM on a slow loop (no React render per frame), the glide a CSS transition so the loop only moves the target; a replay (a past session's calls from the lab) plays through the same path one call a second and lives in a module store (the lab starts it, plain-interval clock). A query against app.db names its table, most of which are a card's, so "db" sharpens to what was read. Labels flip left near the right edge (the open console's bar is there).

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

- Which writes: a bill's due date opens a task (`providers/finance.py`), a flight opens a check-in task (`providers/email_extract.py`), a text to yourself becomes an event, food entry or list item (`selftexts.py`). Each used to write and say nothing, so the only way to find out was to notice the result; `record()` is the obligation, not a convenience. Undo is per kind.
- `seen_through` is an id, not a time: a row written in the same second as the "looks fine" click would otherwise be marked seen without ever having been on screen. The `through` the card posts is the newest id it was showing, not "everything", so a row that landed between the fetch and the click stays unseen. Undo of an event already deleted in Google Calendar (404/410) counts as undone.
- Self-texts, before the change a text to your own number became a task verbatim every time and half were dropped unread: a flyer photo is an event, a lunch photo a food entry, "There will be blood add to watchlist" a watchlist row, "(photo)" as a task title means nothing. Kinds: task (something to do), event (something with a date you might go to; your own calendar, never attendees), food (what you ate, estimated like a dining-hall dish), watch (a film or show), list_item (something to buy or pack, on one of your lists), note (worth keeping, not an action; a 06-Generated note), keep (unclear: nothing written, text stays on the card). "Redo as..." re-files the same texts as a chosen kind. A burst closes once `BURST_GAP` passes with nothing new, or `QUIET` since its last text (the poller only looks every five minutes anyway); the poller step runs after the messages provider has synced; the newest group is left alone until QUIET so a caption lands with its photo. Only texts from the first run onward (`selftext_since`) are routed: earlier history was already captured as tasks by the old rule and routing it would duplicate every one. Untrusted input, the attachments way: the user's own texts, but a pasted announcement or flyer is someone else's, so attachment text is fenced, the model has no tools, and its answer can only name one of the kinds.
- Routing details: `extract()` caches "error" forever (usually the vision call dropping with the API unreachable), so a failed read is retried (`RETRY_STATUSES`) or one bad minute would make a photo unreadable for good. A photo that didn't read is usually the whole point of the text, so filing the caption alone (or "(photo)" as keep) is a wrong answer, not a partial one: wait for the next poll, and route with what there is on the last attempt (`MAX_ATTEMPTS`). A usage-limit `AgentError` leaves the burst pending untouched. The burst is marked done before anything is written: a crash halfway must not route it again and write the first half twice. Food items take the best source first: a barcode or readable label is exact; a food already known by that name (corrected, published, eaten before) is what it was last time (no stated amount = usual amount; `food.log` carries over a guessed portion); only the rest are estimated, and for a known food the estimate only sizes the portion (numbers scaled by grams when both sides have them). A failed write is logged as a keep saying why, so the texts stay on the card for a Redo.

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

Reasoning moved out of `app/console.py` comments (items already in this chapter, such as the auto-mode `default` rationale, pill shaping and bypassPermissions, are not repeated):

- **Stream buffer.** `MAX_BUFFER_SIZE` is 32 MB because the SDK's 1 MB default is smaller than one stream-json line carrying a Read of a large PDF or a screenshot. Crossing it kills the SDK reader silently (seven turns in the log died this way, UI left spinning). 32 MB is room, not a target.
- **Pills.** A month of messages serializes to ~538 KB, more than the window, from one click; the console's cap is the floor under `pin.js`'s per-source caps. `_pill_json` is unindented because `indent=2` was 18-55% whitespace on real widgets (the health card's 9.0k chars held 4.6k of data), paid every turn the pill stays in context. A cut is announced, because a silently cut array gets a confident wrong count.
- **Restart holds (`/api/console/busy`).** The launcher's reloader holds a restart while any socket has a turn in flight. "Working" means the CLI spoke within a window longer than one foreground command's 10 minutes; never put a clock on the turn itself (it kills long dev turns mid-work). A terminal tab has no turn of ours to watch and can miss the turn's end, so one whose transcript has been quiet `TERMINAL_QUIET_S` (120 s) stops holding it; it resumes either way.
- **WebSocket Origin.** Browsers don't apply CORS to WebSockets, so without the check any page open in any tab could drive a socket that runs tools. A missing Origin isn't a browser. The rule is `localguard.origin_ok`.
- **Warning filter.** Every session start logged a ~1.5k-char SDK warning that `allowed_tools` shadows `can_use_tool` for the read tools. That is the design (reads don't card); the risk, a writer in that list, is held by `test_only_reads_are_auto_approved`. The warning is silenced so real errors show.
- **Auto-memory off.** The Brain replaces Claude Code's own auto-memory in console sessions: 203 console sessions left four memories in a directory nobody opened, one wrong; two memories that can disagree are worse than one. The four were read into the Brain as dated sources first.
- **AskUserQuestion.** It never reaches `can_use_tool`: the CLI answers from its own TUI, so under sdk-py it auto-declines in about five seconds and the model falls back to prose. A PreToolUse hook is the one place to intercept it. The model receives the answer as a *denial reason* (the hook's only channel back into the turn); a bare "the user picked blue" makes it call the result malformed and say so, so the wording imitates the CLI's real tool result (verified on a live turn). `_shape_questions` trims field by field rather than through `_safe()`, which turns an oversized list into a `{"truncated": …}` dict: the card then draws nothing and the turn hangs 10 minutes on an answer with nowhere to be given. The shaped list is also what answers are matched against, and options are capped at eight so the free-text row stays reachable by digit (number keys stop at 9).
- **"Don't ask again" (`_always_rule`).** Tool-wide except Bash, where one `git status` approval would otherwise also cover `rm -rf`; Bash narrows to the program that will actually run. Reading the first word is wrong (`cd backend && npm test` starts with `cd`, and a `cd` rule would grant `cd / && rm -rf ~`), so the command is parsed like `is_safe_read`: `strip_cd_prefix`, then `has_breakout` refuses anything that could start a second command. A leading `VAR=value` is an assignment, not the program (rsplit once named the rule "abs" for `VAULT_PATH=/abs uvicorn …`). Unparseable earns nothing; it once fell through to the bare "Bash" key, so one unbalanced quote covered every command for the session. ExitPlanMode earns nothing: it approves one plan. Decided here to keep it in one place.
- **Card text.** `_danger` finishes "this will …": the change table's guard sentence where there is one, else verb and scope; only the two irreversible verbs warn, or every card would. A card's reason is the tail of what the model last said and is emptied when a card takes it, so a stale justification never sits under a different call; leftover narration from the previous turn is cleared at turn start.
- **Day correction.** The system prompt's day-key is fixed at session start and a tab stays open for days, so after a rollover every "what's on today?" was answered against the boot day. The first message of each new day carries a correction (nothing on the common path). A rollover can also be a timezone move (a flight day), so the zone is corrected too.
- **Prompt trust.** A dropped file is untrusted (a mailed PDF, a photo), so it goes in the same fence as extracted attachments, not a `<widget>` (our own data). A kept PDF's ref sits outside the fence: a document can't mint itself a ref by printing one. Session digests (`app/session_digest.py`) get their own tag since they may quote mail or pages. Handoff reports read as a colleague's note; PR-watch comment bodies are other people's words. After a file rewind the prompt says so, or the model assumes its remembered edits still exist.
- **Interrupt and queue.** `cancel_queued` exists because without it Stop doesn't stop: the CLI runs whatever is queued as a fresh turn the moment the interrupt lands. `cancel_async_message` has no SDK wrapper (private call); an error answer counts as "already read", and the replay then settles it. `interrupted` is set when Stop lands and cleared before each query: the CLI ends an interrupted turn with the same `is_error`/`error_during_execution` shape as a broken one, so without the flag every Stop was filed in the failure log as an error. Held messages (`console_queue`) send only after a turn that ended neither stopped nor in error.
- **One reader (`read_session`).** Reading per query (`receive_response()` from send to result) loses every turn the CLI starts itself: a Bash call that outlives its timeout goes to the background, the model ends its turn, and on completion the CLI runs a second turn nobody reads. That reply sat in the SDK queue and the next question's read stopped at the stale result, so every answer showed one turn late. A continuous reader gives such turns the same path; content with no query of ours behind it is announced first (`turn_start`), or the UI renders a reply to nothing with the input unlocked. Reader-task exceptions kill the SDK reader, so the session is declared dead and the turn closed so the UI unlocks (they once ended as an unretrieved-task log line and an endless spinner).
- **Turn ledger.** A change is claimed when the model asks and kept when its result returns clean; counting denials or failures would be wrong in the reassuring direction. Subagent messages ride the same stream tagged `parent`: their tool calls show under the Agent call, their prose doesn't (the parent's reply says what came of it), and their model doesn't count for the bar.
- **Speech.** `SpeechPump` synthesizes off the delta stream so the first words are about a second in; the client's earcon covers tool-call dead air. A single serial worker keeps sentence order. `finish` waits on the worker, not the queue, since the model is done long before the room is (a dozen sentences may still be queued) and an interrupt in that window must stop the mouth; `close()` cancels mid-backlog and the unheard sentences are never sent, so the model is told only what was said.
- **Session info.** The init message's `slash_commands` is names only and arrives with the first turn; the initialize answer already has every command and skill described, including a dev tab's repo skills, which the vault-side scan can't see. `/context` is asked of the CLI because it knows categories and the autocompact buffer, which summed turn usage can't separate. The model list is the CLI's, cached per profile; no list is kept in the backend. `<synthetic>` from CLI error turns is not a model.
- **Quota stops.** `_fault_key` collapses "· resets 6pm" / "· resets 1am" into one fingerprint (seven rows of one answer to "how often am I cut off"). The window names are the CLI's to change ("session limit" became "5-hour limit"), so matching is by family and `success` is never a label.
- **Calendar and mail hooks.** Connector writes bypass the derived cache, so a PostToolUse hook re-pulls Google Calendar immediately (writes from phone or gcal web heal only on the poller tick). The vault's `event_create` etc. are deliberately unmatched: `routes/calendar.py` re-pulls the days it touched, narrower than `refresh()`'s six-week window and right for events outside it. The mail hook fires only on calls that move mail in or out of the inbox (the queue reads the emails table); sends and drafts leave it as it was.
- **`!` shell.** A user-typed command runs without a card in any mode, as the CLI's shell mode does; Claude then sees and answers the output. It runs on a pty so prompts (y/n, password, vim) can be answered from a terminal at `/ws/console/shell`; Claude gets the same bytes with escape codes stripped. Ctrl+B or outliving the limit backgrounds it (a held turn locks the tab) unless a terminal is open on it; Claude hears the outcome with the next message. The pty fd is cleared before close so a keystroke can't land in a reused fd (`shell_ws` checks); writes are non-blocking since `shell_ws` writes from the event loop, and a full buffer drops the keystroke. A pager would only wait for "q". Dropped images are held in memory, bounded; a restart loses them and the pill's marker says so rather than letting the model guess.
- **Dev tabs.** A dev tab is the stock CLI on its repo plus the handoff tools (no vault prompt, Brain or vault-write hooks); settings sources include "local" because `settings.local.json` holds the repo's allow-rules. Task tools are auto-allowed (a card per step would be the whole log). File rewind relies on the CLI's per-prompt-uuid snapshots (replay-user-messages). Editing this backend repo from a dev tab on a server `launch_app` didn't start restarts the server on every save, mid-turn (`reload_note`).
- **Chrome and the permission callback.** `can_use_tool` is registered in every mode (a CLI started without it denies every call after leaving bypass). Claude in Chrome never runs without its gate: the approval sits in a PreToolUse hook because `can_use_tool` is gone in never-ask, with the card's timeout so the CLI doesn't give up before the click. Image frames sent to the bar are raster only (they land in an `<img src>`) and capped on the base64 so one frame can't stall the socket. `_with_suggestions` uses the SDK's private `_query` because its parser drops prompt suggestions; if that moves, only suggestions are lost.

(Modes bullet addendum, `app/console_profiles.py`)
- `DEFAULT_MODE` = general auto, study auto, dev `bypassPermissions` (the Modes bullet's "dev asks" is out of date). The browser remembers the user's pick per profile; this is only the start. Dev never asks: a Claude Code session on a repo opened on purpose doesn't stop for clicks and the turn's diff is the review; that includes a tab another tab opened (`open_tab`), which sat on a card nobody was watching while it was held to ask. General and study stay on auto because they hold the mail and calendar tools, and auto still stops for what can't be undone.
- Dev profile tools: none of the vault tools but `open_tab` and `project_info`; other projects are readable from it (Read/Grep/Glob without a card, `is_project_path`, which also covers a project's auto-memory dir) but never added as working directories, so writing there still asks. A tab moves work elsewhere with `open_tab`, which opens a new tab rather than reshaping this one.
- `resolve_project` accepts only an immediate child of a configured root: no `..`, no symlink out, no other directory. Project folders (`with_folders`, `file_project`) are picker metadata only, kept out of `list_projects` so they can't change resolution or readability; one level, no `/`, 64 chars.
- `create_project` runs `git init` so the diff pane works from the first turn; nothing else is scaffolded. Names match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. `list_projects` sorts by `.git/index` mtime (moves on every stage/checkout), else the dir's.
- `FILE_READ_ONLY` (`tasks.md`) is generated from app.db/index.db; an edit would be overwritten or read as truth. `describe` follows `@file` imports in CLAUDE.md (inside the repo, max 4) because a CLAUDE.md that is mostly `@AGENTS.md` imports has its instructions in the imported files. `_blurb` skips `@` lines (imports, not prose).

(Stats for nerds addendum) The footer already says wall-clock time and tokens; neither is a speed, since a dev turn is a dozen model calls with tool runs between, so tokens over wall-clock measures the tools as much as the model. A thinking block counts as the first content block (the model is answering even when the thinking text isn't shown). `message_start` arrives with the response's first bytes, so tok/s is generation rate and the wait before it is TTFT's. Subagent calls are excluded (console.py only passes top-level events): they run on other models, often in parallel. Computed always (a few clock reads per event); only displayed behind the Settings toggle. `finish()` returns None when nothing was timed (partial messages off, or an error before any stream event).
(Enter mid-turn addendum, `console_queue.py`) steer = straight to the CLI (read at the next tool boundary; in the turn, can be taken back unread, can't be edited or reordered); queue = held here until the turn ends, then sent as a turn of its own, oldest first; editable and reorderable, and "steer now" hands one to the CLI mid-turn after all. Held in app.db rather than the page or Link because a backend reload ends both; the page reconnects under the same tab id and gets the list back (`held` is in `console_link.STICKY`). A held message's pills come back whole except images (memory only in `console._held_images`), which go as "the image is gone, ask for it again". The drain is skipped after a Stop (hands the list back to the prompt), an error or a usage cut, where the next message would only hit the same wall. `MAX_HELD`=20 (a turn's queue, not a backlog: past it the page is told no).

(Console.jsx, top region; acceptEdits removal is already documented above.)
- Mode order in the picker is by how much each asks; "never ask" is last because it is the only mode in which a send or delete happens without a click, despite resembling "auto" in name.
- Per-profile mode defaults mirror the backend `console_profiles.DEFAULT_MODE` (general/study auto, dev never ask); a spawned tab sends no mode until the user picks one. A stored per-profile "auto" from when dev defaulted to auto would pin every new dev tab to it, so it is cleared once (`console_mode:dev:bypass-default` flag).
- Rewind: the `:at` key (record to fork-resume at) persists across reload because the fork has no transcript until a turn lands; dropping it would resume the whole conversation the user just cut.
- Tool runs: consecutive calls are one activity row (count + tools); while running it shows the call in flight, and the count pulses as the only "still working" sign (a lone call keeps the count as "1" but also its detail, since the tally would drop which file). Expanded it is the per-call list with full input on hover.
- Working line shows socket `phase` and elapsed seconds ("thinking · 9s"); a bare "working…" for ten seconds reads as stuck.
- `otherTabs` (for `@`) reads the stored tab list rather than props so memoised panes do not re-render on every tab change.
- Every tab stays mounted (socket, turn and log live while another is showing); `active` gates only single-instance things: composer, speakers, push-to-talk key, ⌘K focus. `onTitle` reports the first message, which names the tab.
- Doc pane: `auto` (a new reply may open the document) turns off when the user closes it, on again when they next open it themselves; closing a file (not a cited document) leaves it alone. A second document splits the pane (homework under handout); each half picks and closes on its own, and closing the top moves the lower up.
- Streaming text: reply deltas collect in a buffer and land once a frame (every 100ms when the window is hidden and frames stop); each delta used to re-render the whole log.
- `attachRef`: the next socket may attach to the tab's live backend session (console_link) after a reload or dropped socket, not after a reconnect the user asked for (New chat, another session, model change). While an attach rebuilds the log, live messages are held so nothing lands ahead of the replay.
- Model/effort at start: the tab's own pick when open_tab gave one (a queued child keeps it until it starts), else the profile's remembered pick, re-read at each connect so Settings reaches a reconnecting tab; an effort the model does not take is dropped, not sent. Each reply is tagged with the live model id (the "model changed" mark and the retry menu read it; resumed logs carry the tag via `sessions.session_transcript`).
- Diff-pane line comments are composer chips sent with the next message, stored per folder in localStorage; read per folder because the worktree arrives after mount. They are cleared in the send handler, not a state updater, since a send that unmounts the pane would leave a sent comment to return.
- The bus holds `limited` emitted on mount and replays it to `useTurnTrace` because child effects run before the plan view subscribes.
- Drag veil names "file" (read and summarised) vs "context" (already-shaped rows) before the drop commits.
- Only `voiceOut` reaches the session (a prompt rule fixed at start), so toggling it reconnects like model/mode.
- Handoff: a handed-off tab sends its brief once (`onBriefSent` clears it so a reload resumes the session instead of asking again); a parent's follow-up (`message_tab`) arrives as a new brief and `briefRef` remembers the last one sent so a different one goes too; its turn reports back like the first; a busy tab takes it at turn end. The report state runs sent → running → reported for a brief's turn only. A report is held while the work is unfinished (`lib/handoff.js` verdict: stopped, errored, cut off, queued message or background subagent pending) and goes with the turn that ends the work, whichever later turn that is. A reload mid-wait re-arms the wait, or the report would never come. Resuming after a quit (lib/interrupted.js) waits like a brief.
- Held reports/PR notes wake the tab through the held-command effect once idle. `fallbackDue`: a model to re-answer with after the turn when the account refused the one asked for (`model_refused`).
- Opened collapsed runs are keyed by the first entry's flat index; entries only append, so the key is stable.
- The "Resumed session" line is suppressed for reconnects nobody asked for (backend restart after a dev edit, window reopening on its tabs).
- Tab title is the first user message (typed, resumed transcript or socket replay, whichever lands first), as the resume list is named.
- Reconnect: an unasked socket close is usually a backend restart (launch_app.py holds restarts until no turn runs, but a terminal `--reload` does not); the tab reconnects and resumes on its own (six tries, `RETRY_DELAYS_MS`, ~37s) instead of sitting on "Connecting…" until ⌘R. The retry budget resets only after the socket has stayed up 15s (resetting on open would let a socket that dies on arrival retry forever). Close code 4001 means another window took the tab. A new socket is a new CLI: agents clear, and the unread queued messages are lost with the old CLI.
- The session id is remembered only once a turn has produced output (text_delta/phase, or result) or it is already the stored one: the CLI names the session at init but writes no transcript until a turn starts, and resuming a session without one fails ("No conversation found", CLI exits 1, socket dies). Output (not result) triggers it so a long first turn survives a restart instead of coming back blank.
- `idle` frame (Stop with no backend turn): the page's "live" turn died with an earlier socket; it is settled locally, otherwise Stop is the only control and does nothing.
- The worktree is also reported on the tab (`onWorktree`) so the window knows the tree is held before the socket is back after a reload (PlanApp's left-behind list).
- Connect: `session_info` (what the session loaded, a dev tab's repo skills/plugins included) is authoritative over the vault-side scan and init names, except the local /help and /mcp. A (re)connect refreshes every picker's model list since the account may have changed. `model_refused`: pickers learn it and the message is re-answered on the fallback once the result is in.
- `open_tab` is decided by the window (it knows how many tabs are working) and answered as the tool result; `tabs` closes are said in the log so vanishing tabs do not read as a bug. `rate_limit` is stamped on arrival; the CLI sends one only on a status change, so chips weigh it against the polled reading by age.
- Speech: a `speech` frame is audio for words already arriving as text_delta and never joins the log; only the active tab speaks (one mouth).
- Turn started by nobody (a background command finished) locks the input and opens the block with the waking row; the user's own message that opened a turn an attach is catching up on is never echoed by the stream, so the backend kept it.
- Held list (`held` frame) is sent whole each time; a Stop hands held items back to the prompt ahead of typed text; a steered one becomes the CLI's (read at the next step). A queued message's turn opens with `queued_in`, a task-woken one with its `tasknote` row.
- An earcon plays on tool_use when voice output is on (a slow provider call is silent otherwise). Turn events (`lib/turn.js`) are emitted outside the entries state updater, which must stay pure.
- Turn end: dispatches TURN_END_EVENT (usage chips re-read; subscription numbers moved) and reloads an open diff pane (the turn may have committed or answered its comments). The rewind `:at` key is removed once a turn lands (the stored id is now the fork). A Stop returns is_error but is not a failure. A usage-limit cut-off is remembered past a reload so the tab can offer to resume; success clears it. Mid-turn reports/PR notes are picked up after setBusy(false) renders (`wakeDue`); a Stop makes them wait for the user.
- AskUserQuestion tool_use is not logged as a chip (the question card is its display). A subagent's tool call is marked `↳` so the tally reads "Agent ↳Read×12". Permission cards are built from `preview` (what the ids name), `danger` (what yes costs) and Claude's lead-in sentence; `inputs` stays for the raw view. A `permission_answered` frame (the phone door, app/phone.py) settles the card as if clicked. The `changes` frame arrives just before the result so it lands inside that turn's block. `shell_done` with no code and not backgrounded was refused before running, so no turn follows to unlock the input.
- `fileCtx`: reply paths resolve against the worktree, then its repo (the backend falls back to the vault); clicked files open in the tab's document slot.
- Detach on cleanup: switching model/mode/session bumps `epoch`, closing this socket and opening the next; the old close handshake can land after the new socket's onopen and undo its `setConnected(true)`, leaving the bar "disconnected" and send() refusing with a healthy socket under it (only a reload escapes). The old socket's handlers are detached before closing.
- Re-attach: a session that kept working while the page was away shows transcript up to the current turn's start, then that turn's events, then live ones, held in order so nothing live lands ahead of the replay. A turn the backend cannot place in the transcript makes the replay the whole log, and the mount-time transcript load must not land on it (`transcriptGenRef`).
- The status line for an unasked restart's "Resumed session" follows every dev turn that edits the backend and would read as if the tab restarted itself; it is hidden unless it carries a ⚠.
- Scroll following: the log follows only while the user is at the bottom; scrolling up unsticks (the jump pill returns), sending re-sticks. Only the user moves the view up: own pins, the smooth jump and content growth never lower scrollTop, so "not at the bottom" alone must not unstick (a pin whose scroll event lands after the next chunk grew the log read as the user leaving: pill flicker, lost follow). Zero clientHeight is a hidden tab (display: none), neither "at the bottom" nor scrolling up; the browser drops the position, so the reading place is kept as top entry + offset, not scrollTop (a reply landing shifts scrollTop; hidden WebKit tabs lose it). Wheel/key up lets go before its scroll event, or a chunk landing in between pins the view and swallows the gesture; a tool output box scrolling inside the log is not the log moving. A new first entry (new session/resume) re-sticks in a layout effect (before paint and before the observer reads `stuckRef`). A ResizeObserver/MutationObserver pins on any DOM growth while stuck (phase line, agent strip, images, expanding blocks), and the zero-height log (sheet shut / tab hidden) is re-pinned or restored to the reading place on resize, before paint.
- Composer height is measured (not `field-sizing: content`, which the .app's WebKit may lack); styles.css caps it. Width-only resize observer, or fitting height would loop.
- Only the last `TURN_WINDOW` (30) turns are drawn: a long chat drew every turn (markdown, math, slides) as DOM laid out per streamed token. "Show earlier" keeps the place (the log grows upward under it).
- Dropped files are read and attached as pills (a pasted local path is useless: the console's cwd is the vault). The tray has the backend open a Desktop file by name; a just-taken screenshot chip is checked on window focus (screenshot, then back to the app = focus event, no polling). Clipboard images (⌃⇧⌘4) all arrive named "image.png", so a paste is named by when it happened. A dragged widget lands as the pill its pin button would make (same label, payload, replace-by-label). Sending while a pill is still "reading…" is blocked (the model would see it as an unreadable attachment).
- Exactly one card holds the keyboard: the SDK has one question in flight, but an interrupt can leave an unresolved card in the log. It opens as a modal in the window's picked tab (the showing tab if asking, else the first that is).
- Typing while an approval card waits declines it with the text as the reason (the terminal's "no, and tell Claude what to do differently"; #99): it reaches the model as the call's answer, so the turn turns on it instead of the message sitting behind an unapproved call. Line comments stay for the next message.
- Mid-turn send: setting "queue" holds for its own turn (server-side, #92), "steer" sends in after the current step; ⌥Enter does the other. Commands and `!` lines can't be taken mid-step and wait locally for turn end. `sendQueued` uses a uuid its replay hands back, so the message joins the log between the steps it came between; `withPills` false for diff-pane asks, `shown` for log text when more than typed is sent.
- Formula field: inserts `$…$` at the caret (the notation the console reads and the transcript draws); a drawn formula opens again on click (Enter replaces in place, emptying removes it, Esc leaves it). The span is re-found by text since typing moves it.
- `/btw`: a side question to its own one-turn model with what is on screen; works mid-turn and the session never hears it.
- Ctrl+Enter: stop and send now (the Stop keeps the queue, so the CLI's next turn is this prompt). ⌘Enter: prompt and pills become the first message of a new tab (same mode/project) opened behind it; the composer clears (#93); the window refuses past MAX_TABS. A Stop returns waiting messages to the prompt (backend `unqueued`; no socket means no CLI holds them).
- The held-command/wake effect runs after the render that cleared `busy`, not in the socket handler (whose `wake` still sees busy).
- `sendText(text, withPills, typed)`: split from send() because a summoned turn has no composer to clear. `withPills` false for a handed-off brief (must not carry off the shared prompt's pills); an array means exactly those pills. The auto-resume banner (lib/autoResume.js) sends with typed=false so it does not make the tab the user's or unlock handoff (`console.unlock_handoff`). `typed` also tells the window not to close the tab for room (`onTyped`). `!` is the CLI's shell mode: no card (the user typed it), Claude answers the output (backend run_shell).
- A child's report wakes the parent with a turn nobody typed, carrying the held reports (as a finished background task wakes Claude Code); mid-turn it waits for the result handler. `MAX_WAKES` in a row without the user is the cap: parent and child passing work back and forth stop for a person.
- `answers` null means the card was dismissed with esc; the backend turns it into the CLI's own "did not answer"; a dismissal settles the card as false.
- Stop: stops speech first (sentences already in the browser would keep playing); with no open socket no `result`/`idle` will come to unlock the composer, but Stop must still work. The backend denies every waiting card, so they are settled locally (no option should be pickable that goes nowhere). `cutTurn` does the same when the socket is gone, using refs and setters only because socket handlers hold the copy from the render that opened them; `flushText` likewise, and runs before anything else touches the log so a chip or card never lands ahead of earlier text.
- Voice: barge-in (wake word during speech) is a new trigger on the Stop path, not new machinery; a `wake` event follows and overwrites its note. It is not conditional on `busy`: the mouth outlives the turn by the synthesis backlog, which is when a long answer gets talked over; the backend closes its pump either way. Push-to-talk lands in the composer to be read before sending (the upload-refusal line, held: a live mic is fine because the user is there to catch a bad transcript); wake-word speech sent mid-interrupt is held until the turn stops, else lost. Hold-to-talk is Ctrl+Shift+Space (Cmd+Space is Spotlight; plain Ctrl+Space switches input sources).
- Model/style/voice switch reconnects and resumes (mode is live, `changeMode`); a session nothing was said in has no transcript and resuming it kills the socket ("No conversation found", CLI exits 1), so with nothing to keep the switch starts fresh (a new tab whose mode was set first hit this).
- `reconnect` marks `setConnected(false)` itself because the old onclose is detached and send() on the next, still CONNECTING, socket throws.
- Rewind: "code" restores the repo's files (dev only, on this socket), "conversation" drops the message and everything after it and restores its text to the composer, "both" is files first. The cut is resolved off the stored transcript (`sessions.rewind_point`) and the tab resumes at it as a fork, so the original stays whole in "resume". With `retry` (a model name) the cut is the same but the message is re-sent by itself (`retryTextRef`, on the fork's `session` frame) instead of returning to the composer. "Retry with…" (RetryMenu) forks from just before the answered message on the picked model; the pick is the tab's own (like one open_tab gave) so a retry does not change what every new tab starts on. If the cut fails the tab stays as it was. `model_refused` re-sends on the fallback the same way, from the post-turn effect.
- Model and `--effort` are fixed at session start, so a pick is one reconnect; a tab with open_tab's own pick keeps the change its own.
- Switching into "never ask" is confirmed every time (not once and remembered): other modes cost a click to undo, this one cannot be undone, and the moment to notice is before the next turn. `changeMode` returns false when declined so the caller resets the `<select>` by hand (state never changed, React will not re-render it). Mode changes are live, not a reconnect: the backend switches the running CLI so the turn in flight and its cards survive (a reconnect killed them on every press while cycling); with no socket the next start reads the stored mode.
- "Empty log" means nothing was said: the session-init line is a `status` entry, so `entries.length === 0` is false once connected (the invitation used to flash and vanish half a second after open). In an empty pane the init line keeps only what the mode does without asking (console.py `mode_note`, needed before the first message); a warning (`reload_note`) stays whole; once there is a conversation the line stays as the record.
- `@` list: tabs first (few, and what you are juggling), then files, then sessions from the chat_search search once there are 2 letters; a tab or session lands as a digest pill (lib/pills.js), not text.
- Merge from the bar or diff pane = the tab's `worktree_merge` tool. A conflict never touches the main checkout; it comes back as a line (from the bar also a drafted message asking the tab to resolve it, which the user may send or not). The pane shows the conflict itself, so it passes `draft: false` and gets the answer back.
- Diff pane opens with a fresh key each time so it opens where asked, not where it last was. The doc pane auto-opens on the newest reply's first doc reference, once per reply (streaming re-renders on every token; the key keeps it to one); an empty log is a new chat whose documents are not ours.
- Log marks: `changes` flags a reply whose model differs from the previous ("model changed"); `retries` is each turn's last reply with the message it answered (where "retry with…" sits; a `!` turn has none).
- Dev bar (scripts + PR chip, ProjectBar.jsx) waits for the socket's worktree so the first load reads the tab's own tree, not the main checkout's. The bar is one row: mid-session controls (model, mode, diff, a live voice) stay out, set-and-leave controls and session actions (docs, resume, new) live in ⋯; under the plan view it is portalled into the status bar (`barTarget`) so the console has one row of chrome. What the status bar folded for room (lib/barHost.js) goes first in ⋯ (it was one click away on the bar).
- Drag: `getData` is blocked until the drop, so the kind comes off `dataTransfer.types` (the veil can say which is about to happen); `relatedTarget` null means the pointer left the window, not a child border.
- Log render: grouping is presentational, one block per exchange (user → tools → reply → result); perm/help cards stand alone between blocks; items carry their flat index (what runs key on). Windowing is by user turn so a turn is never cut in half. Group keys are `g${gi}` because a bare group index collides with a lone card's entry-index key (the card at entry 3, the turn after it at group 3), leaving stale card copies behind, one more per message.
- The working line shows only when no call is in flight (its own line already pulses; two "still going" signals are one too many) or the model moved on to thinking/writing the next call. A quit-interrupted tab resumes on a click (its banner); while that banner is up the usage-limit banner and its auto-resume stand aside: one tab, one resume (Resume sends a turn, which clears `limited`; Dismiss leaves it for a click).
- The plan view draws the prompt under the stage, outside the sheet, so it stays on screen at every sheet height; a portal keeps its state in Console and React events still bubble to the pane (dropping onto the prompt is a drop here). The plan view gives the document the map's place (`docTarget`) so the conversation keeps its width; elsewhere it shares the console; only the showing tab draws into the shared slot.
- Card modal: the waiting card is centred over everything because in the log it could sit under a peeked sheet or in a tab you were not in; a background tab's card names its tab; "In the conversation" returns it to the log to read what led up to it. A hidden tab's card is not live (the keys are the window's).
- `coalesce`: an Agent call is its own row (AgentCall) because its subagent's work never rides this stream and folded into a run it read as one more chip; a delegate-provider call is its own row (DelegateCall), the log's mark that someone other than Claude answered. Connection notes: every model/mode switch reconnects and says the same kind of thing; only the last is true, so it shows with a count standing in for the rest.
- Per-entry error boundary: an entry that cannot render costs that entry (plain text where it has any), never the transcript, which has no other copy on screen. `Entry` must be a component: drawn inline, markdown would run during Console's own render, above the boundary.
- The "changes" entry is the receipt auto mode trades approval cards for. It is deliberately not a summary anyone wrote: each row is one tool call that ran and returned, so the list is countable rather than agreeable; rows past the eighth fold because a long unattended run is when the count matters more than the detail.
- Held list UI (#92): click edits, ↑↓ reorder, "steer now" hands the item in mid-turn (when idle "send now" makes it the next turn), × drops; the server owns the list.
- Composer keys: Shift+Tab cycles the permission mode only on an empty composer; with text (or ⌥⇧Tab) it is WebKit's move-focus-back, which every other control gets. The cycle skips "never ask" (its confirm is no place for a stray key) but works mid-turn (live switch). Up from an empty prompt takes back the newest held item first (last in line), then queued ones (a sent one returns only if the CLI has not read it, per `unqueued`; a held command is ours), then history; only from the top line so it still moves through a long prompt.
- Composer preview draws only while the prompt holds a formula; the textarea keeps the `$…$` source. The plan view shows only the composer, so it carries its own "new": hidden while nothing was said (already a new chat) and not mid-turn (reconnecting drops the reply being written). An unreadable file's note is what the model sees of it, so it sits on the chip, not in a tooltip.
- `ConsolePane = memo(Console)`: the app re-renders whenever any card's fetch starts or lands (widgets/cards.js), which does not concern the console; props are stable callbacks plus `pills`, so memo keeps a long transcript from re-rendering on each.

Tab strip (`plan/ConsoleTabs.jsx`): a tab another tab opened belongs to its opener's group. The bar holds the parent and a badge (a dot per child, "3 running · 1 needs you"); the badge drops the group open under the parent (a row per child, `lib/children.js`, done last). The strip is a few hundred pixels wide, so children aren't inline, but the one on screen is drawn beside its parent. A strip of one tab draws only the "+". Right-click menu (#102, #91): pin, move, Later, settle, retitle; top-level tabs drag to reorder, pinned first; snoozed and settled tabs live behind the rest button. The open group and menus are `fixed`, placed from the badge's rect because the strip scrolls sideways and clips anything absolute; the active tab is kept in view by `scrollLeft` (`scrollIntoView` would scroll page ancestors) and again when the strip narrows because the bar's fold runs after. Urgent-first sort is stable so ties keep open order. `StatusBar`: folds by priority (`lib/barHost.js CUT_ORDER`) into ⋯, before paint on resize or any row change, coalesced to a frame; a cut-off tab says so there because a folded console hides its banner. `ViewPane` tabs: seen (images tools handed the model this turn, newest unless picked), preview (a dev tab's app; another origin so back/forward walk addresses through the bar; opens at the ask, else this tree's `previewUrl` from `.kestrel/project.json` (own port so worktrees don't show each other's server), else last address used here, else the project's first listener; same-origin is this very app and would reconnect saved sessions from a second place, so it's refused; one chip per address, `:3000` = `:3000/`), terminal (a computer-use job; forward while it wants an answer, back to screenshots after), files, plan, tasks. xterm loads only for such a job and its load must not reject.
Console cards: `Prompt.jsx` copies the terminal's prompt (↑/↓, digit jumps, enter, esc) because a keyboard answer beats hunting a scrolling transcript; the keys listener is capture-phase (the composer's onKeyDown would eat digits/enter) but yields if the user clicked into the composer; in multi-select, enter with nothing picked commits the row under the cursor outright (`pick()` only toggles in multi mode, so a second enter looked like the first did nothing); "Other" still uses `pick()`. A plan has no "don't ask again" (a standing yes to unread plans), the backend withholds the label so the row goes. Raw arguments toggle on a letter, not a digit, so it can't collide with answer rows; the raw arguments stay one click away because a preview can be wrong (unfamiliar tool, id resolved to the wrong row). The backend folds a turn's parallel calls to one tool into one card (`console.py bursts`) and the answer applies to all. The danger line shows in every mode (a delete and a rename must not look alike); the guard line only in auto mode. A command folds past `CMD_FOLD_LINES` (8; a 60-line heredoc pushed the answer rows off screen). An unresolved id says so. `UsageChips`: the cached percentages come from the poll (30s; the backend asks the API at most once a minute; skipped while hidden and caught up on return) and merge with socket `rate_limit` events, newer wins (#52): the CLI sends an event only when a window's status changes, so an event winning outright kept a stale percent or a passed `resets_at` (blanking the chip to "—" over a fresh reading). Past its reset a window's percentage is void, not merely old (the 5h chip sat at 100% for hours after the limit lifted; the week survives staleness); checked at render so it flips on the clock. Only interactive Claude Code refreshes the cache, so its age is shown. A turn ending triggers a refresh a few seconds late (backend throttle 15s) so the API has counted it. The rotating chip is sized to its widest window with/without countdown, or a 4s width change would fold/unfold the status bar. `DiffView`: scopes are a turn, uncommitted, and for a worktree tab the branch against its base (the review: `console_worktree.review`; uncommitted files shown apart since a merge carries only commits); lines are read off git so Bash edits appear; comments (shift-click widens) belong to Console per tab folder, ride as composer chips with the next message (`lib/review.js`) because the tab that wrote the line can act on it; a left-behind branch's review has no tab so takes no comments; tokens use the whole file's line when it still reads the same, else the hunk's lines (wrong only if the hunk opens inside a string/comment from above); branch sides are merge-base vs HEAD, uncommitted HEAD vs working tree, a turn has no revision. `DocPane`: opens on the newest reply's first doc reference unless closed, always on a clicked cite/page embed/quiz slide; the quote is found as a citation card does (`lib/pdf.js locate`), so an off-by-one page still lands; pages draw lazily into placeholders pre-sized from each viewport, at the pane's width in steps; under 760px wide it covers the log. `FilePane`: `/api/files/raw` serves only image/video/audio/markdown/text, text as text/plain under a sandbox CSP (`app/files.py`), so `.html`/`.svg` read as source or picture. `LimitBanner`: resume waits for the window (a closed one just cuts it off again); auto-resume runs for off-screen tabs too since every banner stays mounted. `AccountChip`: a switch reaches only a CLI that starts after it, so an idle tab reconnects and resumes on the new account while a busy one finishes its turn first. `AgentCall`/`AgentStrip`: subagents run in the background and their calls never reach the tab's stream, so the backend reads the subagent's transcript (`sessions.subagent_run`, by call id; works on resumed sessions); polling stops when finished or one read after the strip drops it (a subagent that died stays "unfinished" on disk forever). `CommandPalette`: Escape is on the window, not the field's onKeyDown, so it works without input focus; selection returns to the top on typing (else Enter runs the old index); no query = grouped menu, typing = one flat ranked list; notes (`searchNotes`) come from the second letter, after commands, never a better match. `SessionsPanel`: stale in-flight search responses are dropped; mounted only while open so closing resets the search. `ModePicker`: folders list the one with the most recently touched project first so a bare ↵ is your last work; selection follows the pointer only when it moved (↑↓ scrolling fires mouse events on a still pointer). `Parity.jsx` rewind: two clicks because the second throws work away and the viewer has no confirm(). `RetryMenu`: Claude's models only because another provider would be handed the personal conversation (`app/delegates/boundary.py`). `ShellBlock`: the output box follows the newest line until you scroll up; becomes a live terminal after `TERM_AFTER_MS`. `ProjectBar`: runs alive at first load predate the mount, so their server coming up isn't a preview ask; polling is fast while a server comes up, slower once serving.

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

Additional detail moved from the module docstring and comments:
- **Seatbelt profile**: `sandbox-exec` (the mechanism Claude Code's own sandbox uses), enforced by the kernel whatever Deno does. Deny by default; read only system libraries, the Homebrew kegs Deno links against and the run's own temp dir; no network, no writes, no exec but the Deno binary. Verified with Deno's permissions fully open (`-A`) inside it: /etc/passwd, ~, app.db (even a stat), fetch, 127.0.0.1:8788, spawning /bin/ls and writing /tmp all fail. The profile allows only the specific mach services Deno touches at startup (logging, directory lookups, trust evaluation), narrower than a blanket mach-lookup, and tested. The path matched is the real Deno binary (symlinks resolved), because Seatbelt matches the path the kernel execs.
- **Deno flags**: `--no-prompt` (a denial is an exception, not a question on a tty nobody reads), `--no-remote`, `--no-npm`, `--cached-only` so an `import()` can't fetch. The process also starts with an empty environment, a V8 heap cap, a wall clock, a cap on what it may print, and call caps overall and per costly tool.
- **Why Deno, not Node**: Node's permission model is documented as "not a security boundary against malicious code" and leaves `process.env` and `process.binding` reachable; Deno is deny-all by design.
- **Bridge protocol**: the script's only way out is stdout/stdin, newline-delimited JSON. A call is `{"t":"call","id","name","args"}`; the host validates args against the tool's own JSON Schema, runs the same handler the direct call runs (so the tool's rules, caching and context var apply), and answers `{"id","value"}` or `{"id","error"}`. Results are what the tool would have said, parsed when JSON (db_query rows, `provider` with `brief=false`), else the text. `provider` defaults to rows because the brief line is for a model.
- **Signatures** in the tool description are shorthand (untyped = string), with no per-function prose; every session carries the text, so it's held to ~1.2k chars (test_tool_eval budget). A parameter whose description is only "One of: a, b" renders as a union; provider's `range` also takes dates, so it stays a string.
- **Schema summary** (`SCHEMA_TABLES`): without it a script spends 2-3 db_query calls on `sqlite_master`/`pragma_table_info` first and loses the savings. Columns are the ones answers use; `tests/test_console_code.py` holds them to the live schema.
- **Phase 2 seam** detail: today `Bridge.writes` holds names only (for the rejection message); phase 2 makes it name → handler and has `_dispatch` await `approve(name, args)` (the console's can_use_tool: a card or the auto-mode verdict) before running one, and the harness's `_reject` stubs become `__call` for those. Nothing else has to change.

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

- `project.json` shape: `{"scripts": [{"id","name","icon","command"}], "previewUrl": "http://localhost:$PORT", "autoOpenPreview": true, "runOnWorktreeCreate": ["npm install"]}`; read from the tab's own folder, so a branch can change it. `previewUrl` fills `$PORT`, `${PORT}` or `{port}`; `autoOpenPreview` opens the preview pane once a script's server starts listening. `runOnWorktreeCreate` entries are commands or script ids; they run in order as one "setup" run in the background (the tab opens at once; shown in Tasks like any run). A second click on a running script stops it.
- The port is in every script's env as `PORT` and `KESTREL_PORT`, and the tab's start line tells the agent so a server it starts itself lands there too. After a backend restart, a port already served from this tree counts as this tree's.
- `on_created` never raises: a tab whose install didn't start can still edit.

(Per-turn diff, `app/console_diff.py`, lives in the Console section's bullet and already says it; nothing to add there beyond:) the change ledger (console_changes) lists tool calls that wrote; the diff is the other half a review needs, the lines. `snapshot()` returns None for a non-repo or a repo with no commit; untracked files over `MAX_KEEP_BYTES` (256KB) aren't kept, so a change to one shows as "changed" without lines; caps are 600 lines per file, 4000 per turn, 400 untracked files. `commit_diff` (a tab's branch review) takes no working tree or untracked files, only what a merge would carry; `REV` only admits an object id or HEAD (never an option, range or reflog walk); `blob()` gives the whole file at a rev so the pane can colour a hunk (a docstring's middle line is a string only to a reader who saw where it opened), refusing binary, >512KB, missing, or paths with `..`/leading `/` or `-`. Every entry point never raises: an unreadable diff costs the pane, not the turn.

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

- Kind letters (`e` email, `v` event, `m` message, `d` doc) are not decoration: they make `[cite:e7a3f1]` readable in a raw transcript and split the hash space by kind, so a collision has to be within one kind. `REF_RE` is anchored to those letters so `[cite:x1234]` is a non-match rather than an unresolvable ref; the distinction decides whether to blame the model or the DB. A per-session counter looked cheaper until a transcript is replayed: its `e3` means whatever the replaying session numbered third.
- `CITE_RE` allows `#pN "quote"` after a doc ref; the ref stays group 1 and the page/quote are the frontend's (ui/DocPage.jsx), not something to look up.
- `remember()` must be called on the way out (as the row is rendered into the model's context), never lazily when the citation comes back: `resolve()` is a lookup on `cite_refs`, so a never-recorded ref is indistinguishable from an invented one. It is INSERT OR IGNORE; on a held ref of another target it lengthens the hash (5 up to 10 digits). Ten digits deep and still colliding doesn't happen; the long form is returned unrecorded rather than raising inside a brief-line render.
- Targets are ids the provider caches already hold; nothing stores an email body or message text. `resolve()` reads the live row, so a re-synced subject shows its new self and a purged cache resolves to nothing. Handed-out-but-gone returns `found: False` with the minted label (None only for never handed out).
- Event refs: a recurring event keeps its id across days and a cache re-sync can drop the exact day a ref was minted on, so resolution falls back to the nearest surviving instance of that event_id rather than a dead pill.

- `/api/cite/{ref}`: the 404 for a never-handed-out ref is load-bearing, not an error condition: it is almost certainly one the model invented, and the frontend renders it dead; logging it as a server fault would bury a real signal ("the console is citing things it did not read") in noise. `found: false` is the third answer (ref real, row gone: provider cache re-synced past it, message deleted); the pill still says what it was. Bodies never come through here: an email citation resolves to metadata plus snippet; the body is `/api/email/{id}/body`, fetched live from Gmail, stored nowhere, and only asked for when the pill is opened.
- `/api/cite/docs/recent` is declared before `/{ref}` (which it would otherwise match); it feeds the console's document pane so any kept document can be opened, not just what the newest reply cited. `/api/cite/{ref}/file` serves by ref only, never by path or hash (the ref is the one handle the console was given, so a request can only reach files someone dropped); bytes are untrusted and decoded by the browser, never the backend; immutable per ref (file is named by its sha256), so cached for a year.

`ui/Cite.jsx` card placement: the opened card hangs off a marker that can sit anywhere in a line (most land near a sentence's end), so left-aligned it overflows the right edge. The first version was a CSS `:nth-last-child` flip, wrong because that selector counts element children (a mid-paragraph marker with no elements after it reads as "last"). Order of the JS clamp: pull left by the overflow but not past the left margin; then the left edge wins outright, which decides the one case where both can't hold (a card wider than the window) and keeps its first words readable. Click-away and Escape close the card, but not a click inside it (selecting the address you came to check must not dismiss it); the email reader is portaled to `<body>` yet counts as the card's child. A not-ref-shaped marker is dead on sight with no network call. A document citation inside a console opens the page beside the chat (`DocPane.jsx`) because reading along wants the page to stay put while the reply scrolls; "p12" shows on the collapsed pill because it's where to look. A quote absent from the document strikes the pill.
Quiz/cards (`ui/StudyBlock.jsx`) extras: free response = no `choices`, `answer` is text, optional `accept` lists short exact answers that grade themselves (a number, a term, what a line prints), anything else is revealed and self-graded with the typed text sent back for regrading; `long` = textarea; self-graded "right"s go back too (the console judges whether "it's the derivative" covered the answer). "Don't know" is its own button: the honest alternative to a guess, which would otherwise score as one in four; it exists only while the question is open. A crossed-out choice (✕ or right-click) isn't ruled out and can still be picked. Results persist to `study_attempts` keyed by a hash of the block's source, so sending twice replaces. Results go back like a debt nudge (drafted, not sent) so the user can add "I guessed on 3". A streaming fence is a placeholder, not a parse error. The model sometimes double-escapes newlines in the JSON string, even mixing literal `\n` with real breaks (an unclosed fence drawing an empty code box); literal ones become breaks except inside math (`$\nabla f$`, `\neq`, `\nu` are TeX) and well-formed code (`printf("\n")`). Slides are grouped only when consecutive: a quiz returning to slide 5 after 6 is asking again and draws it where asked. Deck keys work only while the deck has focus so typing in the composer never flips a card.

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

- Budgets: `PILL_BUDGET` 18k sits under `console.PILL_LIMIT` (20k) to leave room for page markers and the closing note, so the cut lands on a page boundary in docs.py rather than mid-sentence in the console. `READ_BUDGET` 40k per `doc_pages` read: a run of dense textbook pages, and "read the whole thing" takes a few calls rather than one that fills the window. A page is never cut in half (a quote must come off one page; half a page makes it a guess).
- `tidy`: a Google Docs export comes back one word per line ("large\n \nimpacts"), which doubled a 5-page essay's pill to 10k characters of mostly newlines. Newline-space-newline is a word gap there, never a paragraph break (those are a bare newline), so it becomes a space. Nothing else moves; the quote check runs on pdf.js's own text layer, not the tidied text.
- `keep` is idempotent by content (same bytes keep first filename and ref; only `opened_at` moves, pages refreshed). The file is written through a temp name so a crash never leaves a truncated file under the name that says it's complete.
- `_pdf_path` root is the home folder: the session can already read anything under home, so it is not a secret being kept; it keeps a stray "/" or symlink out of the docs store and makes the refusal say why.
- `open_path` exists so "go through handouts 9-16 in my Downloads" can point at pages; read with pdftotext from the shell the handouts had no ref.


## Other models (`app/delegates/`)

Claude is the orchestrator and the only model that sees anything personal:
the vault, the Brain, texts, email, money, contacts. Gemini and ChatGPT are
there for grunt work that would only burn Claude usage, and for "make N
versions", where a mix of models is the point. They are driven through their
own CLIs (`agy` for Antigravity, `codex`), the way T3 Code does
it, so each bills to the login the user already has. An API key is the
fallback, not the default: `GEMINI_API_KEY` or `CODEX_API_KEY` in
backend/.env, passed only to the CLI that reads it.

- **The registry** (`registry.py`) is the list: Claude, Antigravity (the
  Google provider) and Codex. (Gemini CLI was a third until 11-23; Google is
  retiring it for Antigravity, and it was never installed here.) A
  provider is *ready* when its CLI is installed, signed in and switched on.
  The first two are read off the machine each time (cached 20s; Codex is
  asked with `codex login status`, Antigravity by its own keychain), and only the switch is stored
  (`delegate_providers`; no row means on). The probes run under the
  boundary's env allowlist, so even `--version` never sees the backend's
  secrets. Everything is built against that report, so a missing CLI is a
  tool error that names the install command, never a crash. Settings →
  Providers shows the same rows, and a signed-out one lists every way to
  sign in (`login` is a tuple of ways).
- **Antigravity's sign-in lives in a home of its own.** `agy` keeps its
  Google token in the keychain through go-keyring, which runs
  `/usr/bin/security`. Opening `~/Library/Keychains` to the sandbox was
  tried: it works, and it also lets anything inside read every item that
  trusts `/usr/bin/security` (gh's token, Cloudflare's, Claude Code's own
  login), silently. So `agy` runs with `HOME=~/.kestrel/agents/antigravity`
  (`registry.AGY_HOME`), which holds its own `.gemini` and an
  empty-password keychain with agy's token and nothing else.
  `security` honours `$HOME`, and the login keychain stays walled even for
  a process that points HOME back. `scripts/antigravity_home.py` makes the
  home and copies an existing sign-in through `security -i` on stdin (or
  starts `agy` there to sign in). What an outside agent can still find is
  its own token, and so the Google account it runs as.
  agy's token store is composite: keychain first, and when a keychain write
  fails it moves the token to `.gemini/antigravity-cli/antigravity-oauth-token`
  (`registry.AGY_TOKEN_FILE`) for good. Two runs refreshing an expired token
  in the same second do exactly that (`security` exits 45, item exists), and
  the keychain item can end up gone. So signed in means the item *or* the
  file; reading only the keychain once reported a working agy as signed out.
- **Codex in a workspace with the CLI off** (`codex_cli_workspace_disabled`
  in the browser) leaves nothing on disk to read, so a signed-out Codex row
  always offers both ways round: an account added in Settings, or
  `CODEX_API_KEY`, which `codex exec` reads (OPENAI_API_KEY only seeds its
  interactive login, so it is never passed).
- **Accounts** (`accounts.py`, #110), the shape of Claude accounts. Each
  sign-in is a home of its own under `~/.kestrel/agents/accounts/<id>`
  (siblings, never nested, so one account's sandbox can't reach another's
  token), and a run points the CLI at it (`Provider.home_env`): agy through
  HOME, with an empty-password keychain made there (agy also falls back to a
  token file, `.gemini/antigravity-cli/antigravity-oauth-token`, once a
  keychain write fails, so either counts as signed in); codex through
  CODEX_HOME and HOME with `cli_auth_credentials_store="file"`, because its
  keyring is the login keychain. The sandbox opens only that home. The first
  agy home is adopted as an account as is, with the runs it already made.
  *Adding one is signing in*, a browser click and nothing typed: "Add
  account" runs the CLI's own sign-in in a new home through
  `boundary.start_login`, whose `login_profile` is a run's walls with the home
  as config dir, plus what that sign-in may listen on and whether it may open
  the browser (`registry.LoginWay`).
  - *Codex*: plain `codex login` (device code is off in USC's workspace: "contact
    your workspace admin to enable device code authentication"). Its callback
    server listens on 127.0.0.1:1455, and when that's taken, after asking the
    stale server to stop, on 1457, its other registered redirect port; the
    sandbox lets it bind those two and nothing else, and shuts LaunchServices
    so it can't open a tab itself. Kestrel reads the port and the page off its
    output and opens the page (`boundary.open_sign_in`, auth.openai.com only)
    in the default browser, where the user is already signed in; "Open it
    again" reopens it while codex still waits. Device code stays as a
    fallback for workspaces that allow it, and known refusals are said in plain
    words (`accounts.PLAIN_ERRORS`).
  - *Antigravity*: agy has no login command, and print mode's link redirects to
    antigravity.google, a page that shows a code to paste. Its ACP server
    (`agy acp`, what T3 Code drives too) advertises `oauth-personal`, and
    `authenticate` with it redirects to `localhost:<random>/auth/callback`
    instead: agy opens Google's page itself (`/usr/bin/open`) and catches the
    redirect. So that sign-in may open the browser and bind any loopback port.
    Kestrel speaks two JSON-RPC calls (initialize, authenticate) and waits for
    the reply. The token lands in the home's own keychain (HOME points there),
    as with the CLI.
  A sign-in waits 10 minutes (a device code 16); one that fails or is
  cancelled takes its home with it. *Usage*: Codex's 5h/week windows and credit balance come from
  `chatgpt.com/backend-api/wham/usage` (what its /status reads), with the
  account's token from its auth.json, backend-side; credits used = the monthly
  allowance (typed in Settings) less the balance. agy reports nothing, so its
  accounts show the log's runs and tokens. *Rules*: on/off, signed in,
  `allowed_for` (personal / learning / commercial) against the errand's
  `work_for`, a window ceiling and a credit ceiling; `pick` takes the first in
  order that fits, falls back to the API key if one is set, and otherwise
  refuses with each account's reason. A school login (an .edu email, or
  ChatGPT's edu plan) starts as personal and learning only: USC's Codex is
  for public and internal-use data and the account is USC's.
- **The gate** (`boundary.gate`, run inside `scrub` whenever a purpose is
  given, and `launch` takes no brief without one) decides whether the work
  may leave at all; the scrub only takes identifiers out. Outside models get
  generic grunt work or what they're plainly better at, so the tools require
  a `purpose` from seven kinds (boilerplate, refactor, tests, format,
  public-research, variants, image): a project's own ideas can't be read off
  the text, so anything that isn't one of these stays with Claude. It also
  refuses, with a reason Claude can act on: coursework (a course code, from a
  fixed list of subjects plus the vault's own course notes' names; school
  words like essay, homework, problem set, rubric, Brightspace, due date; a
  path into `02-Areas/usc`), anything from a study tab, and a brief that
  still carries personal details after the scrub (a contact's name, a vault
  link or folder, a street address, an SSN). A Claude-only fan-out isn't
  gated. `fan_out` and `delegate` run it once on the whole task before
  anything starts, and each outside variant's brief passes it again.
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
     sandbox off inside it. No `sandbox-exec`, no run. The parents of the
     scratch and config folders get `file-read-metadata` (literals, not
     subpaths): SQLite lstats every component of a database path, and
     without them agy's own stores failed with "operation not permitted",
     which took a 150-word brief from under a minute to almost six.
  4. *The caps.* `launch` reads stdout line by line: `timeout` (10 minutes)
     of wall clock, and `max_turns` lines that the provider's `ends_turn`
     says close a turn (agy: 30 `agent_response` steps on `stream-json`;
     Codex: 80 `item.completed` steps). At either the process group is
     killed and the run keeps what it had: the file if written, else the
     parse of the partial stream. The row stays `ok` with the cut in
     `error`, and the tool line says CUT OFF. A CLI that printed only at
     the end would be capped by the clock alone.
  The module imports the standard library and `config` and nothing else, so
  there is no path from it to personal data. `tests/test_delegate_boundary.py`
  holds all of it: each module's imports against a list, that nothing else in
  the package starts a process, the env, the scrub, and the profile run for
  real against a fake vault and the real home directory.
- **No MCP either way.** The outside CLIs get none of the vault server's
  tools (they aren't Claude Code and are never handed a config), and their
  own users' MCP servers are turned off for the run (`--allowed-mcp-server-names`
  with a name nothing has; `--ignore-user-config`; agy's home has none
  configured). agy does carry its own built-in tools (a browser, web
  search, `generate_image`, subagents); the sandbox bounds what they reach
  on disk, not the network.
- **What a real run showed** (agy 1.5.15, 11-23): a 150-word explainer in
  48s, about 14k tokens in and 5k out (mostly thinking, and `output_tokens`
  already includes it); a 512×512 PNG from `generate_image` in 63s, headless,
  written as the artifact. The stream names no model, so the row's model
  is empty and means agy's default (Gemini 3.8 Flash High). `agy models`
  also lists Claude and GPT-OSS models served by Google; they'd be outside
  agents like any other.
  A design task is slow: left alone it spent ~50 steps and 5 minutes on a
  400×240 business card (test scripts, SVG previews, screenshots in headless
  Chrome inside the sandbox). A capped agent's brief now ends with its
  limits (`runs.LIMITS`: N steps, 10 minutes, write the file early, no
  tooling or screenshots), so a cut still leaves the file. It still runs
  3–5 minutes a card on the default; `--effort low` halved it (135s, half
  the tokens) and is the lever if that's too slow. Text errands take 25–65s.
- **Failures name the fix** (`Provider.explain`): agy prints the model list
  after a bad `--model`, so the last line of stderr read as a model name.
  The result event's error is read instead: no such model (with the list
  and `agy models`), or not signed in (with the setup command).
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
  That row is the mark that another provider answered. The brief shown is
  the one Claude wrote; when the scrub changed it, the label says what was
  swapped for placeholders before it left, since the agent never saw the
  original. A run stopped at a cap that still handed something back reads
  "cut off" (one that handed nothing back is "failed").
- **On the usage page** a run that failed before any model call (a bad
  model, signed out) adds nothing; one cut off or failed after spending
  tokens counts. No model name reads "default model".
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

- Registry: command lines were written against `gemini --help` 0.70.0 and `codex exec --help` 0.171.0. What each prints when it finishes (`parse`) has not been checked against a signed-in CLI (neither was installed when written), so parsing falls back to the raw text when the shape isn't the one expected. Gemini runs `--approval-mode yolo` (it may write in its scratch dir without asking; the seatbelt is what bounds it) with an MCP server name no server has, so none of the user's configured servers load. Codex runs with its own sandbox off (`--dangerously-bypass-approvals-and-sandbox`: seatbelt doesn't nest and ours is already around it; the flag says it is "intended solely for … externally sandboxed" environments, this case) and `--ignore-user-config` (no MCP servers from `~/.codex/config.toml`); the brief goes in on stdin.
- Runs: every agent gets the brief and a file name to hand back (`artifact`), and either writes that file in its scratch directory or replies with its contents. Each variant has its own directory `<data>/delegates/<batch>/v<n>/`, also the only place an outside agent can write (`boundary.profile`). Claude's variants go through `agent.ask` (no tools, one turn); others through `boundary.launch`; all get the scrubbed brief, so a batch is the same task N times and the comparison is fair.
- `app/delegates/__init__.py` module map: boundary.py (what an outside agent can be given and reach; the only place an outside CLI starts; stdlib and config only), registry.py (providers and whether each is installed, signed in, on), store.py (the switches table and the run log), runs.py (one errand or a batch of variants), api.py (/api/delegates/*: providers page, a batch and its files).

Additions from the comment pass:
- Provider CLIs were checked against real signed-in runs of `agy` 1.5.15 and `codex` 0.171.0; parsing falls back to raw text when the shape differs. Probes (version, sign-in) run under `boundary.clean_env`.
- **agy command line.** Permissions are skipped (`--dangerously-skip-permissions`; print mode has nobody to answer, and the seatbelt bounds it; agy's own `--sandbox` is seatbelt too, which doesn't nest). `--disable-slash-commands` so a brief starting with "/" is text. Output is `stream-json`, one event a line, so turns are counted live (turn cap) and a cut-off run still parses. The stream never names the model, so the log goes in the scratch dir (`--log-file`), by its resolved path (through a symlinked `data/` in a worktree the sandbox refuses the file and agy logs to stderr). `output_tokens` already includes `thinking_tokens` (1.5.15: total = input + output; a one-word reply reads 70 out, 69 thinking). A bad `--model` prints the model list after the error, so `_agy_explain` reads the whole error, not the last stderr line. agy keeps its token in the keychain until a write fails (two runs refreshing in the same second: `security` exits 45), then moves it to `.gemini/antigravity-cli/antigravity-oauth-token` in the home for good, so signed-in checks both. `agy models` runs in the first enabled account's home.
- **codex command line.** Its own sandbox is off (`--dangerously-bypass-approvals-and-sandbox`, "intended solely for externally sandboxed" environments; seatbelt doesn't nest). `--ignore-user-config` drops `~/.codex/config.toml` MCP servers, and because it also skips the home's config the file credential store is passed again with `-c` (login = the file in CODEX_HOME, never the keyring). Brief on stdin. `codex exec` reads `CODEX_API_KEY` (`OPENAI_API_KEY` only seeds its interactive login). Steps (`item.completed`) are counted, not model calls, so its cap is higher. `codex debug models` lists the catalog the CLI picks from (only `visibility: list`). `codex exec --json` names no model; `_codex_home_model` is the lowest-`priority` listed model in the account's `models_cache.json` (checked on the USC account: `gpt-6.1-sol`, priority 1) and Kestrel passes it explicitly so the run records it.
- Sign-in ports: codex's browser sign-in uses 1455, else 1457 (0.171 logs "falling back to the registered fallback port"); device code is off in some workspaces (USC's) so it is only the fallback. agy has no login command, and print mode's link lands on a page showing a code to paste, so sign-in uses ACP `authenticate`. `LoginWay.ports`: None = any loopback port, () = none.
- `ACCOUNT_HOMES` homes are siblings, never nested, so one account's sandbox can't read another's token. The first agy home (`scripts/antigravity_home.py`) is adopted as an account as is, and every earlier agy run is claimed by it. A school account (.edu email or ChatGPT "edu"/"education" plan) starts personal + learning only. Some plans (Edu) report no credit balance, so a credit ceiling can't apply there (`credit_cap_inert`).

- **Gate scope.** Scrub removes identifiers; the gate decides whether work leaves at all: grunt work that would only burn Claude's limits, and what they're plainly better at (images). Never coursework (academic integrity, a school account's data rules), never anything personal, never a project's own ideas; the last can't be read off the text, so `purpose` is an enum and anything outside stays with Claude. `WORK_FOR` (personal | learning | commercial; commercial = a business's or client's) is matched against an account's `allowed_for`. Course-code subject prefixes are a fixed list plus the vault's own course notes (`02-Areas/usc/<term>/<SUBJ>-<num>.md`, names only), so a new term needs nothing. The gate runs on the raw brief, before `scrub` hides the paths it looks for.
- `Brief.purpose` is set only when the brief also passed `gate`; `launch` refuses one without. `scrub` catches identifiers, not meaning: a brief describing someone's week in plain words passes, and the sandbox is what holds then.
- **Seatbelt profile order** (later rules win): allow all; deny the home directory, other volumes and temp dirs (other apps' caches, this session's transcripts); allow the CLI's config and code (`tool_dirs`: nvm node or an npm prefix under ~; Homebrew installs need nothing); deny the vault and app data whatever those said; allow the scratch dir, which lives inside the data dir and is the one place it writes. Resolving a path under a walled directory stats its parents, so their metadata (not contents) is allowed: SQLite lstats every component of a database path, and agy couldn't open its stores under `~/.kestrel/` without it.
- **Turn cap.** A turn's tool calls run after the line that closes it (agy marks a response DONE, then runs what it asked for), so a cap cuts at the next turn's line; killing at the last one dropped that turn's file writes.
- **Account keychain.** `make_keychain` gives each home an empty-password default keychain so agy's go-keyring (via `/usr/bin/security`, HOME pointed at the home) never writes the login keychain; no lock timeout, since a locked one would stop a headless run on a dialog. A sign-in's seatbelt shuts LaunchServices (`launchservicesd`, `lsd.*`) unless `browser` is set, so a sign-in Kestrel opens itself (codex) can't open a second tab. Seatbelt refuses about 1 in 8 literal-port binds even with a literal allow (scattered, the same ports every run, a different set per filter form; `localhost:*` has no holes); 1455 and 1457 pass, and `test_a_sign_in_listens_only_where_it_may` checks it, so changing `CODEX_LOGIN_PORTS` needs that test green.
- **Runs.** The `LIMITS` text appended to an agent brief tells it its step and minute caps: left alone, agy spent 50 steps and five minutes on a business card (test scripts, headless-Chrome screenshots); told the limits it plans, and a capped run still leaves the file. codex `--json` reports failure on stdout (`turn.failed` with `error.message`, or `error` with `message`). A run cut off mid-way often has its first files written, so the error names what it left. A capped run that did write the artifact is `ok` with the cap in the error column.

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

Additions (module-note facts removed from `brain.py` comments):
- Rebuild rule: the vault's `BRAIN_DIR` is a generated read-only view (the `tasks.md` arrangement). Every fact cites its source with a verbatim quote so pages can be rebuilt from sources at any time; the one thing that can't be rebuilt is the user's ruling (`verdict`), so a rebuild deletes only unruled ingest rows. `render` rewrites only changed files because the watcher reindexes on every write, and removes a stale page file only if its frontmatter says `brain: true`.
- Model calls go through `agent.ask` (subscription CLI, no tools), so injected text in a message or email reaches a model with nothing to act with; the worst it does is propose a fact, which must survive `apply_ops` and waits for a ✓ unless the user said it to Kestrel.
- Batch size: ~200k chars of sources is ~50k tokens: room for the brain context and the answer, and a week of heavy texting splits into a few calls instead of one that loses the middle. Ingest runs without thinking (`NO_THINKING`): with the CLI default, the first dry run spent all 32k output tokens thinking about one week and wrote no JSON.
- Aliases: learned aliases only accumulate (a nickname learned once is still a nickname) but never one another page already answers to (a shared "Noni" on Nora's page filed Noelle's texts there, and removing it didn't stick because the next lint could learn it back). `edit_page` is the user's path and can remove an alias; adding one another page answers to is refused rather than dropped so the caller moves it off that page. `move_fact` keeps status, verdict and source (re-noting with `replaces` would make an unconfirmed copy and leave the original as history).
- `set_verdict` returns `restored`: rejecting a stale fact can revive an even staler one (one review session revived ~50 unseen), so it is said out loud.
- Timestamps: messages and email carry the offset of the zone the user was in; sources use naive wall-clock where it happened (a text at 11pm in LA is that evening, not next morning in NC). Windows run 04:00 on `start` to 04:00 the day after `end` (the day rollover; a 1am text belongs to the day before).
- Group chats count as a source only when the user sent `BRAIN_GROUP_MIN_MINE`+ messages since the window start: a group they only lurk in is other people's lives.
- Claim checks: identifier/hedge regexes exist because the prompt forbids them and the second dry run recorded them anyway (a rule the model breaks one time in ten is a rule for code); the chat-metadata check exists because the first backfill filled "routines" with the batch's own headings read back as facts, a quarter of the page.
- Quote check: the quote is the evidence and the id is bookkeeping; `_quoted_in` accepts the cited item, else the single other item in the batch containing the quote (a true quote under a neighbouring id was the second most common drop on the first dry run). Found in several places or nowhere proves nothing and is refused. The line is compared as the model saw it, speaker included ("Rowan: Goooo"). Comparison is case/whitespace/curly-quote insensitive.
- `_ingest_batch`: a malformed reply is retried once, then the batch is halved (shorter source list, shorter answer; the second dry run's one crash was a long answer with a missing comma); a single item still unanswerable is skipped and counted so a year-long backfill doesn't stop on one bad reply. `dry_log` appends each batch's raw ops and drops, for reading what a prompt change does before trusting it. `catch_up` ingests through yesterday (a day is ingested once it is over) and is resumable: a crash leaves the watermark at the last finished week.
- Lazy path: `maybe_start` runs only after the first backfill (the first year is a deliberate CLI run, not a page load) and skips when `agent.blocked()` (a run in a spent usage window would only log a failed row). A `AgentError` is logged as its message only (meant to be shown as-is; the traceback was ~60 lines of SDK frames per hit).
- `rebuild_reset` forgets unruled ingest/backfill/lint facts so the next catch_up relearns them; chat-told and user facts and every verdict stay.

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

**Alarm sources (`alarms.py`).**
- Flight: `dep_local` is local to the *departure airport*, so it is read via `tz.airport_zone()`, not `tz.zone_on()`; on a cross-country moving day they disagree by three hours. The body carries the zone label because a bare departure time is unreadable when the user isn't in that zone. A check-in window already open gets no alarm (past can't fire; `ensure_checkin_tasks()` puts it on the task board).
- Package: an `eta_day` has no clock, so the hour comes from config (`PACKAGE_ALARM_HOUR`) and the zone from `tz.zone_on()`, the user's zone that day (contrast flights: a departure time belongs to the airport, an arrival day to wherever the user reads it). `status` only moves forward, so 'delivered' is terminal and dropping it withdraws the alarm. The carrier shows only as a hint for errands (counter pickup), not couriers.
- Leave: reads no table; calls `leave.compute_leaves(strict=True)` (strict exists only for this caller). Calendar failure = kind-level unknown; a single event with null `leave_by` (won't geocode/route) = per-key hold, since an alarm already written is the best answer until next cycle. `leave_by` already includes LEAVE_BUFFER_MIN, so the alarm fires at it. `horizon` is unused (a leave is today; ALARM_HORIZON_DAYS is days). `fire_at` seconds are truncated (not rounded): the writer zeroes seconds, so keeping them would store a time that never matches the delivered alarm, and truncation errs early. The body has no drive time: it is recomputed from live traffic each cycle and a changing body would defeat the deadband. Event id falls back to `event@start` if the provider drops `event_id`.
- A naive `fire_at` is rejected: it reads as a different instant either side of a timezone move and the row outlives one. A source may hold only its own keys; a violation raises in `SourceResult`, and `desired()` turns it into `unknown`.
- A kind with no builder in `SOURCES` stays absent, never stubbed with `()` (a confident nothing withdraws). A stored row whose kind isn't in `KINDS` is held (the code is older than the row, e.g. after a rollback).
- `_apply` only records what should be true, so a reconcile with no (or a failing) writer still leaves a correct queue. An update clears `written_at` and keeps `external_id`; blanking the id would strand the phone's copy beyond withdrawal. Withdraw rows are deleted only by `deliver()` after the writer confirms. `deliver()` leaves failures for the next cycle (self-healing beats a partial state nothing revisits). The `ALARM_MAX_WITHDRAW_PER_CYCLE` refusal means a source lied about being `known`: a stale alarm is a wrong notification, a wrongly withdrawn one is silence. `reconcile()` runs after the providers land, since alarms derive from the stores and mid-cycle would diff against half-updated trips.

**At-desk channel details (`notify.py`).**
- It is for facts that just became true and matter in the next few minutes (a flight that slipped, a package out for delivery); there is no future time to date them to. `display notification` succeeds whether or not a human is in front of the screen, so there is no delivery proof at any layer, hence the presence gate and staleness rule above. An alarm carries its own fire time and is worth retrying all day; a notification means "now". Senders enqueue and nothing more: an extraction loop must never block on a subprocess, and there is exactly one delivery path to test. Kinds are enqueued by whatever noticed the fact, where it noticed it ("the row moved" is knowledge the writer has and a later re-derivation doesn't), so there is no builder registry as in alarms.
- `send()` `parts` name the fact, not the moment: the value that changed is passed as a part, so the same status re-observed each poll collapses to one row, and a status that flaps away and back notifies twice. `target` (what a click opens: a console tab id for console kinds) is not part of the key. `ON CONFLICT DO NOTHING`, not an upsert: the standing row may already be sent, and rewriting it would say the same thing twice. Passing the caller's `con` puts the row in the caller's transaction (if the write that discovered the fact rolls back, so does the promise to announce it); `commit()` runs only when `send()` owns the connection, because sqlite3's connection context manager doesn't nest and an inner `with con:` would commit the caller's half-finished transaction.
- Presence: `at_desk()` is false on a locked screen (`CGSSessionScreenIsLocked`, absent entirely when unlocked, so missing = unlocked) or input idle > `NOTIFY_IDLE_MAX_SEC` (`HIDIdleTime`, ns, the same input-idle clock aw-watcher-afk samples; screen.py's afk buckets derive from it). Fails open because `_notify` once sat in email_extract.py for months and never fired; a banner nobody sees costs one line in Notification Center, a quietly always-off channel costs the feature. Display power state is deliberately not probed: `pmset -g powerstate IODisplayWrangler` returns "Internal failure" here and IODisplayWrangler isn't in the IORegistry; idle already covers a shut lid, and a suspended laptop isn't running this code. The lock check exists because typing a password is input.
- `_post` goes through Kestrel.app's window pipe (`config.NOTIFY_FD`) when it started this server, so the banner is Kestrel's (name, icon, Notification settings entry) and a click focuses Kestrel on `target`; `display notification` posts as Script Editor, so clicks opened Script Editor. Only a FIFO fd is ever written; writes are under a lock because the poller thread and a route can flush at once and a pipe write is atomic only to PIPE_BUF (512 bytes on macOS). A gone pipe falls back to osascript. The osascript fallback passes values via `on run argv`, never interpolation: the old `_notify` pasted `json.dumps(body)` into the script, and AppleScript doesn't understand `\uXXXX` (emitted for every non-ASCII char), so osascript refused to compile it (-2741) and "Nespresso Café" or an en-dash would have posted nothing under `check=False`.
- `flush()` goes oldest first; the cycle cap defers and logs a count (a silent cap reads like "nothing else to tell you"); a failed post stays queued and retries (self-healing beats a half-state nothing revisits). Held while away is neither failure nor drop. Expired rows are kept as the record of what you missed.
**Scheduled wake details (`wake.py`).**
- Why: a reconciler only revises on a poll cycle, and cycles only run while awake; the hours before a 6am alarm are exactly when the machine sleeps, so the phone would fire a 5:40am check-in for a flight that moved to noon. The wake runs one cycle a little before each alarm (sources re-read, alarm amended or withdrawn), then back to sleep. It is also a reconciler: `pmset schedule` is append-only global state that outlives the process, so appending each cycle would leave hundreds of duplicates; the desired set is diffed against what's scheduled. No migration, nothing to back up. A wake left by an older build or cancelled by hand is reconciled, not double-counted.
- Privilege: `pmset schedule` checks euid and refuses ("This operation must be run as root") even for an admin; `system.preferences.energysaver` (class user, group admin) governs the Energy Saver pane through the Admin framework and is never consulted by the binary. The two write verbs use `sudo -n` per call (not a blanket wrapper, so the read path works on a machine without the drop-in); `-n` never prompts, so with no rule the writes fail fast ("a password is required") and apply() logs and retries. `_PMSET` is the absolute path because a NOPASSWD rule matching a bare word off `$PATH` is the classic way to over-grant.
- Wake type is `wake`, never `wakeorpoweron`: a machine shut down was shut down on purpose and booting it from a bag is much ruder. `cancelall` would take the calendar travel engine's and Do Not Disturb's wakes with it, so cancels name exact type/date/owner; `scheduled()` filters to `WAKE_OWNER` and everything downstream (including every cancel) reads from it. Past events are dropped (pmset reaps them; a wake whose time slid while asleep is spent); an unparseable date is skipped, not cancelled.
- pmset is asymmetric about years: it accepts `MM/dd/yy HH:mm:ss` and prints `MM/dd/yyyy`; writing the 4-digit form risks being read as year 2 0 2 6. It prints a zoneless wall clock meaning local time; `naive.astimezone()` attaches the offset in force on that date (stamping `now`'s offset would be an hour out across a DST boundary), making it comparable to an alarm's `fire_at`. Repeating events print without a `[N]` index and so can't match the schedule regex.
- Desired wakes: `WAKE_LEAD_MIN` before each future alarm within `WAKE_HORIZON_HOURS`; which alarm a wake belongs to isn't recorded (a cycle refreshes all). A wake already in the past is dropped, not clamped: the alarm is inside the lead window and the cycle that would check it is the one running now. `_coalesce` keeps the *earliest* of a cluster (it lands before every alarm in it; the latest would wake after the first alarm fired). Scheduled emails (`email_schedule.wake_times`) need the machine awake *at* `send_at`, so they join after coalescing. Diff matching is nearest-first within `WAKE_TOLERANCE_MIN`, not positional, so one new early alarm doesn't shift every pairing. `apply` cancels before adding: a failed cancel is noise, a failed add leaves an alarm unrefreshed. `reconcile()` runs after `alarms.reconcile()`, which populates the table it reads.
**Reminders delivery (`reminders.py`).**
- Reminders is the target because iCloud syncs it to the phone, so an alarm fires on a device the user has, hours after the machine slept; it is worse than a notification otherwise (no urgency control, no actions), which doesn't matter next to firing at all. A dedicated calendar was the other candidate (easier to inspect and wipe); Reminders won on alert semantics (a real alarm with a snooze). Kept behind `write()`/`withdraw()` so the backend is swappable.
- AppleScript traps: titles carry merchant names and flight numbers (user-shaped text with quotes), so values go through `on run argv` and subprocess's argument list, never parsed as script. Set `day` to 1 before setting `month`: AppleScript dates mutate in place, so month=2 while day=31 rolls into March (only appears on 3 days of the month). A date built from components lands in the machine's zone, which is correct: `fire_at` is an instant, Python converts to local wall clock, Reminders stores an absolute date, so after moving to Pacific the written alarm still fires at the same moment.
- Retry: a windowless Reminders launched by osascript is auto-quit by macOS a few seconds after the script ends; connecting while it shuts down gives -609 ("Connection is invalid") or -600 ("isn't running") (273 failed deliveries in the log, 75 of 88 update cycles, reproduced 1 in 4 from a shell). The retry relaunches it. Retrying is safe for every script: `_DELETE` answers NOTFOUND for what an earlier attempt removed, and `_CREATE` adopts an identical open reminder rather than making a second (this also covers a -609 on `id of r` after the reminder was made but before its id came back, which used to orphan one on the phone every cycle). If every retry hits -609 it's a wedged instance (can answer -609 for several cycles running); waiting doesn't clear it, killing does (data is in iCloud). A delivery failure never sinks a reconcile: the row stays pending. `withdraw` returns True for NOTFOUND because a hand-deleted alarm is the same decision and a permanent failure would block the row from ever being cleaned up.

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

- Nothing in the backend runs a model; it only says what's due. The page asks `/due` once a minute, `claim`s a prompt (conditional UPDATE), opens the tab, then reports `ran`, which advances `next_at`. A claim that never reports back (window closed mid-open) lapses after `CLAIM_S` and the prompt is due again.
- The lid: a missed time runs once on the first poll after the lid opens and Kestrel is up, however many times it was missed, if within `GRACE` (12h) of when it was due; a repeating prompt past that skips to its next time (`skipped`); a one-off has no next time and you asked for it once, so it still runs late. The time it was due goes in the brief so a late digest says so. wake.py isn't used because a dark wake keeps the display off and the page's timers frozen, and an agent turn takes minutes, longer than the wake lasts.
- Times are local wall-clock (the machine's zone), like everything a person sets here; "Sunday 8pm" is a clock time, not a `day_key` day. Resuming a paused prompt starts from the next time after now so a paused week isn't run on the spot.

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

Additions (details moved out of voice.py and config.py comments):

- **Why local, and why attachments still refuse audio.** Claude content blocks are text, image, document, thinking and the tool pair: no audio in, none out. `providers/attachments.py` answers a dropped audio file with "the model cannot listen to it" and that stays right, since nobody is present to catch a hallucinated transcript of a file. A live microphone is the other case: the user is in the room and a push-to-talk transcript lands in the composer to be read before it sends. Frames are transcribed and dropped; synthesized WAVs end with the response that carried them; app.db gains no table. Always listening is true of ~1MB of ONNX; nothing becomes text until the console is summoned.
- **Wire protocol** (`WS /ws/voice`, documented in the module docstring): client -> server binary 16kHz mono int16 frames of 480 samples (30ms), plus JSON `mode` (off|wake), `ptt` (down), `speaking` (on). Server -> client `ready` (the phrase), `wake`, `listening`, `barge_in`, `transcript` (`send` true for wake, false for ptt), `status`. A ptt transcript goes to the composer; a wake one is sent; `barge_in` triggers the interrupt the console already has.
- **Wake model config.** `VOICE_WAKE_WORD` is one of openWakeWord's six pretrained names (alexa, hey_jarvis, hey_mycroft, hey_rhasspy, timer, weather) or a path to a trained `.onnx`; `Model(wakeword_models=[...])` takes either. The trained model goes at `WAKE_MODEL` (`data/wake/hey_kestrel.onnx`), is picked up on next restart, and the phrase shown is the file's stem. While the file is missing the config falls back to `hey_jarvis` so a missing file cannot take the mic socket down; `voice.wake_label()` reads the spec in use, so every surface says "hey jarvis" and the mic answers to it. Knobs: `VOICE_WAKE_THRESHOLD` trades false summons against misses and also applies while the console is talking (the wake word is the interrupt); on speakers loud enough to defeat echo cancellation it is the knob to raise. `VOICE_VAD_THRESHOLD` decides what counts as speech for endpointing. ONNX, not tflite: no tflite-runtime wheel exists for Apple silicon, and the ONNX models ship in the same download.
- **Endpointing limits.** A turn ends after the silence threshold, or at `VOICE_MAX_UTTERANCE_S` regardless (stops a stuck-open mic handing the model a ten-minute recording of the room). Under `VOICE_MIN_SPEECH_MS` of actual speech the utterance is dropped, not transcribed: parakeet has a decoder and will invent a word for a cough. Raise `VOICE_SILENCE_PATIENT_MS` if it still cuts you off; raise `VOICE_SETTLED_SPEECH_MS` to stay patient longer into the turn. `VOICE_WAKE_TIMEOUT_S` (12): wake fired but nobody spoke; this is the pause when you are deciding what to ask, so it errs long (giving up costs a re-summon, waiting costs nothing, and the min-speech floor drops the recording either way). Without it, a false wake in an empty room records until the length cap. Barge-in has no threshold of its own; `VOICE_WAKE_THRESHOLD` covers it.
- **Wake spotting during a reply.** An earlier build listened for speech during a reply so you could talk over it; it opened the mic at the end of every answer (a reply carries 900ms of still-speaking grace over a rarely silent room). Requiring the phrase costs a word and makes the trigger unambiguous. The hazard is the synthesizer reading the wake word aloud and summoning itself, hence browser echo cancellation (`lib/voice.js`). A wake hit during `speaking` emits `barge_in` before `wake`/`listening`: the same event the Stop button raises, so the SDK bookkeeping for a turn cut off mid-sentence is unchanged. With `barge_in=False` the console is deaf while it talks.
- **Pre-roll.** Push-to-talk starts after the trigger (the button is pressed as you begin talking), so a ~1s ring buffer (`PREROLL_FRAMES`=33) is prepended to the utterance; leading room noise costs parakeet nothing. Wake capture skips it: the audio before the trigger is the wake word itself, which nobody wants in the transcript. After a capture ends the wake model's buffer is reset so the same phrase does not re-trigger on the next frame.
- **STT details.** One `_mlx_lock` covers every MLX graph evaluation, transcription and synthesis alike (two models on one GPU; MLX evaluation must not run concurrently with itself, and it stops a reply being synthesized into the middle of transcribing the turn that interrupted it); it also covers the one-time model load. The ASR model (~2.5GB on disk, seconds to load) loads on first use, never at import. The pcm must be float32: `get_logmel` views the FFT's complex output back through the input's dtype, so bfloat16 silently doubles the bin count and the filterbank matmul fails. Kokoro synthesis takes the same lock per chunk, so a barge-in waiting on it waits ~0.25s, not a whole reply. `SPEAK_MAX_CHARS`=600: a paragraph handed over whole cannot be interrupted for its duration.
- **Speakable text.** Fence markers are not the problem; their contents are (dropping only the ``` lines left `uvicorn app.main:app` read out as a sentence), so everything between fences is dropped. Table rows and `---` rules are eyes-only (a table read aloud is numbers with no structure). Citation/page embeds `[cite:d3f9a1#p12 "..."]`/`[page:...]` are dropped (read aloud they were a string of hex). `speakable()` is subtractive except `_normalize_speech`. `take_sentences` hands over a sentence only once its terminator and the following whitespace exist, and an unclosed fence holds everything from the fence on; `flush` (end of turn) gives up both.
- **Normalization rule details.** Order matters: the range dash is resolved while both sides are still digits ("5:30-9:00" afterwards has words either side), the clock rule runs before the timezone one so it can consume the meridiem between them, and `~` becomes "about" before the dashes ("9am-~2" only looks like a tight dash once the tilde is a word). Misaki phonemizes `Jul` as "ʤˈʌl" and `Apr` as "ˈapɹə"; `May` needs no entry; abbreviations match only capitalised (`Sun`/`Sat`/`Mar` lowercase are ordinary words). Units: `tsp` reads "T S P" (`tbsp` is already fine); the leading digit group is captured so "24hrs" becomes "24 hours" and plural is told from "1 hr". `w/` is its own rule (the slash leaves no letter boundary to anchor on; unqualified it reads "double-u"). `_CLOCK` keeps the whitespace inside the optional meridiem group, otherwise "5:30 to 9" comes out "five thirtyto". A range dash becomes "to" only when tight; other tight dashes between alphanumerics are spaced rather than interpreted (misaki emits nothing for them, but with room around it the same character is a clean pause, and "PHYS—120" need not be guessed). IANA ids are anchored on the real regions (America, Europe, Asia, Africa, Australia, Pacific, Atlantic, Indian, Antarctica): a generic Capitalised/Capitalised rule turns "Pull/Legs" into "Legs" and "Brown/White" into "White". Verified-good and left alone: `$45`, `40%`, `&`, `@`, `1st`, `RSVP`, spaced em dash.
- **`say` voice selection.** `say -v '?'` lines are name, locale, `#`, sample. Names hold spaces and brackets ("Sandy (English (UK))"), so the locale is the anchor; locales are not always `xx_XX` (Majed is `ar_001`, Fiona `en-scotland`), so the pattern matches loosely. A bare first name is not unique across locales (novelty voices ship in a dozen: "Sandy" matches `Sandy (German (Germany))`), so English wins outright, then Premium over Enhanced over compact; a non-English voice can still be pinned by name. `say -v` exits 0 on an absent voice, silently substituting (`say -v Catherine` and `say -v Zzzznotreal` render byte-identical output, both different from bare `say`), so the voice is verified against the installed list; `/api/voice/status` reports what it resolved to. The result is cached because it shells out; a voice downloaded mid-session needs a restart. `say` reads its text from a file, not argv, because a reply starting with a dash would parse as a flag. `kokoro_ready()` needs both mlx-audio (runner) and misaki (G2P): the first without the second imports cleanly and fails on every sentence. `warm()` is fire-and-forget so it cannot delay session start; `_synthesize_kokoro` swallows every failure. The status route reports only the live engine's voice, because Kokoro and `say` voice names share no namespace.
- **Config for `say`.** `VOICE_TTS_VOICE=auto` walks `VOICE_TTS_VOICE_PREFS` (Serena, Kate, Karen, Moira, Tessa) and takes the first installed; naming one pins it; empty is the system default. Every entry is a female en_GB/en_AU voice because the default is what people turn the speaker off to escape. macOS ships every voice compact and keeps the good build behind a download (System Settings -> Accessibility -> Spoken Content -> System Voice -> Manage Voices); compact Serena and Serena (Premium) differ, so `tts_voice()` prefers Premium/Enhanced. Not in the list, verified on macOS 25.5: Siri voices (appear in neither `say -v '?'` nor `AVSpeechSynthesisVoice.speechVoices()`, so naming one resolves to nothing), and a premium Australian female (Matilda and Catherine are Siri voices; the downloadable catalogue at `/System/Library/AssetsV2/com_apple_MobileAsset_VoiceServices_Combined-VocalizerVoices` offers one en-AU voice, Lee, male; Karen compact is the whole Australian option). `VOICE_TTS_RATE` is words/min, 0 = default. `VOICE_TTS_ENGINE` is `kokoro` or `say`; `say` is the floor, so no engine is allowed to be load-bearing. `VOICE_TTS_KOKORO_LANG` is derived from the voice prefix, not configured. Kokoro measured on this M5 after warm-up: 16-17x real time, ~0.25s per sentence, cheaper per reply than the network hop that produced the text.

Test-side reasons (test_voice.py), beyond what is above:
- The bugs live in the state machine (turn start/end, what is discarded) and the markdown-to-speech pass, not the models; the tests run without loading the 2.5GB parakeet model.
- Tables are on screen in full, so speech skips them (read aloud they are numbers with the structure gone); a table-only turn produces silence. Mid-fence lines look like prose until the closing fence arrives, so the sentence cutter must hold them or a shell command is read aloud. Deltas arrive mid-word: never speak a half sentence.
- Dropping the right half of any Capitalised/Capitalised pair silently deletes a word the screen still shows. Normalizer rules must not re-fire on their own output: the tilde becomes a word before the dash rules run.
- openWakeWord ships six phrases; the own-name phrase is a trained model configured as a path, and the file stem is the only place the phrase is written (the UI must say it aloud). A wake model configured before training finishes fails inside Listener's constructor (when the socket opens), so the status route checks first.
- `say -v '?'` parsing: match the locale loosely (`ar_001`, `en-scotland` are real, not `xx_XX`) or a voice silently becomes unselectable. macOS lists compact and Premium spellings of one voice that sound different; picking the bare name hands back the voice the user downloaded Premium to escape. `say -v Serena` with no Premium installed exits 0 using the system default, so the failure symptom is the wrong voice, silently. Novelty voices ship in a dozen languages under one name and `say` lists German Sandy first; prefer English.
- TTS degrade: a missing model, missing misaki or bad voice name falls back to `say` rather than taking the reply stream down. A Kokoro voice name (`bf_emma`) passed to `say` would be silently substituted, so the override is dropped on the fallback path; an explicit engine override (auditioning) never silently answers with the other engine. Kokoro `bf_` is British, `af_` American; the G2P must match or the voice sounds slightly off.
- Gate: with the mic idle, room speech yields no capture and never reaches ASR. A pause that would end a settled utterance must not end an unsettled one (hesitation early in a sentence); the patient threshold is longer, not absent; hesitation frames do not count toward settling. A capture after wake starts empty, not from the pre-roll (the pre-roll holds the wake phrase). Parakeet decodes a noise burst into a word, so a min-speech floor (~90ms burst is under it) discards it. Wake with no speech stops recording rather than waiting for the length cap; a stuck-open mic must not hand ASR a recording of the room. Holding the push-to-talk button, a thinking pause is not an end.
- Barge-in is the phrase again; bare speech must not trigger it (the mic used to reopen at the end of every answer: a reply carries 900ms of still-speaking grace and a room is rarely quiet for it). Nothing is armed by the console going quiet. With interrupts off, not even the wake word is spotted until the reply ends.
- SpeechPump speaks off the delta stream, not the finished turn (first words in the air about a second in). On interrupt the queued sentences are never sent to the model, so it believes it said only what was heard. The wait for the backlog must be interruptible (barge-in once worked only on short replies). Concise-reply mode ("lead with one plain sentence") is a real behaviour change, so it is fixed at session start with the voice flag, not always on.

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

- The snapshot name has one-second resolution (`app-YYYYMMDD-HHMMSS.db`); on a collision `run` adds a numeric suffix, so a forced second copy in the same second never replaces the automatic one.
- `backup.copy_to(dest, src_path=None)` is shared: the merge guard's pre-merge snapshot uses it. It goes through SQLite's backup API so a live WAL database copies whole.
- `backups_dir()` is `db.APP_DB.parent / "backups"`, so a test that redirects `db.APP_DB` redirects the backups too instead of writing into the real data dir.
- `existing(month)` returns the newest match when a forced run made two. `run` raises on a failed copy: the poller wrapper decides a failure costs the cycle nothing, and swallowing it in `run` would make the CLI report success over an unwritten file.
- Snapshots contain OAuth tokens: same rule as app.db, this machine only, never synced.

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

- Why a snapshot at all: the monthly snapshot (backup.py) can be weeks old, and a migration is the one kind of merge that rewrites app.db (alarms are the only handle on an item sitting on the phone; location history; tasks).
- `migration_prints` returns None when db.py or its `MIGRATIONS` list can't be read (not a plain list of string literals); `new_migrations` then returns -1 and the guard assumes a migration changed. 0 means db.py didn't change or no new fingerprint.
- Step 1 runs under the merge lock (`before_ff`, called by `console_worktree.merge`) and raises OSError/sqlite3.Error if the snapshot fails (the merge is then refused: an unbacked migration is what the guard exists to stop). Only on Kestrel's own repo (`APP_REPO`).
- Step 3's health wait is `HEALTH_S` (90s). If the new code fails and the code can't be rolled back (main moved on, or `reset --keep` refused because of uncommitted edits), app.db is left as is, since restoring it under the new code would only re-run the migration that just failed. The fresh snapshot replaces the merge-time one (which is deleted); if the fresh one fails, the merge-time one stands. If the old code also doesn't come up, the result says so.
- `restore` copies the snapshot aside (`.restore`) and `os.replace`s it; the `-wal`/`-shm` files go first because they belong to the database being replaced and SQLite would otherwise replay the failed migration's pages over the restored file.
- A server run without the launcher gets steps 1–2 only and pushes at once: nobody to run step 3. Stdlib at import: the launcher imports this and it is the one process that must not fail to.

## Module notes

Per-module reasoning moved out of docstrings: the code keeps one line of why and points here by heading.

### `app/agent.py`: one-shot model calls, breaker and budget gate

- One place for "run the subscription CLI with no tools for a single turn and hand back the text" (the brief, email triage, email extraction, reply drafts, attachment vision each carried their own copy of the loop, and of its one sharp edge). Nothing here talks to the API directly: no tools, one turn, so untrusted text in a prompt reaches a model with nothing to act with.
- The edge: on a failed turn the CLI emits a result message and *then* exits non-zero on purpose (for shell consumers). The SDK replaces the bare "exit code 1" with the result's error text, which for an API failure is the result's `subtype`: the literal word "success" (the turn ran; the API call inside it didn't). Every caller rendered "Claude Code returned an error result: success", which says nothing and blames the wrong thing. The result message is already in hand: `is_error` with `api_error_status` 429 means the window is spent and no retry inside it helps; `ask()` translates and raises it (`_why`). CLI-worded cut-offs ("You've hit your session limit · resets 2:10pm", "Failed to authenticate: OAuth session expired") arrive with no status and subtype "success", the reason only in the result text; `_why` uses that text (it once dropped it for "the model call failed (success)"), and `_is_limit` matches the wording. The non-zero exit trailing an error result is expected and swallowed when a result is in hand; any other exception is a real crash for the caller.
- Breaker: a 429 trips it. Every caller is background-ish and retries on its own clock (triage every poller cycle, the brain's lazy catch-up, a brief someone keeps re-asking), and each retry inside a spent window spawned a whole CLI process just to be told 429 again (104 in one log). After a 429, `ask()` refuses up front with the same message until the window's reset, or `BREAKER_FALLBACK_SECONDS` (15 min; short, only absorbing a retry storm) when no usage source can date it. The reset time is trusted only as far as `_reset_at` trusts it (best-effort: every source behind `console_usage` is a cache possibly hours old and a confidently wrong reset is worse than none; only a "rejected" window whose reset is still ahead speaks, since a past one is a stale reading of a rolled-over window) and capped at `BREAKER_MAX_SECONDS` (1h; the weekly reset can be days out, caches can be wrong, extra usage can be bought): a wrong reading costs one probe per cap, never a day of silence. Any success clears it.
- Budget gate: the breaker waits for the wall; the gate stops short of it. Automatic callers (triage, extraction, brain; none urgent, they catch up next cycle) stand down once a window passes `AUTO_PAUSE_PCT`, leaving the rest for the console and buttons. A path a person asked for (reply draft, dropped file, Regenerate) runs inside `interactive()` (a ContextVar so it follows into `asyncio.to_thread`/`asyncio.run`, which copy context), which the gate ignores; the breaker still applies ("a 429 is a 429"). The gate fails open (no reading = no pause; the breaker still catches a real 429); a window past its `resets_at` is void, not full; a stale reading can only be low since usage climbs until reset, so trusting it never pauses anything wrongly. A reading is cached `BUDGET_TTL_SECONDS` (60s): the poller asks per batch and the CLI cache is a multi-MB JSON file. On the event loop `blocked()` reads cached windows only (`fetch=False`); off it (poller threads, `ask()`) it may refresh; `ask()` checks via `asyncio.to_thread` so the refresh doesn't stall the loop. `blocked()` is for callers skipping expensive set-up before a doomed call.
- `ask(thinking=...)`: a long extraction wants `{"type": "disabled"}`; on the Brain's first dry run the CLI default spent all 32k output tokens thinking over a week of texts and never reached the JSON. `tools=[]` strips the built-in tool schemas (~12k tokens). The SDK is imported lazily so a backend that never asks pays nothing. `AgentError` messages are written to be shown as-is (`daily_brief.error`, the desk).

- Before the breaker, each caller hit a spent window on its own clock: email triage every poller cycle (82 "email refresh FAILED" lines in one log, status panel red while mail synced fine), the brain's lazy catch-up, the brief seven times in a row. A refused model call in the email refresh is deferred work reported in the info dict, not a failed source, and stays pending (a refused batch writes no `email_extract` row, so the candidate returns next cycle); only the model refusing is deferrable, a non-model summary failure still fails the refresh. While the breaker is open callers skip expensive set-up (body fetches, the brief's source gathering, a brain run). The model-free flight poll and check-in tasks still run in a spent window.

### `app/brief.py`: daily brief

- One brief per day_key, generated lazily on the first GET of the day and cached in app.db (`daily_brief`); DB-only, never written to the vault. The row is also the generation lock: an atomic upsert flips status to 'generating', which works across backend restarts and terminal-skill callers (WAL + busy_timeout). Without force only a missing row or a stale 'generating' row (backend died mid-run) is claimed; ready/error rows return as-is, so a failed brief never retries on its own. `/regenerate` forces, and runs inside `agent.interactive()` past the budget gate the first-open brief stops at (the task copies the context, flag included). The /daily-brief skill is a thin wrapper over these routes.
- Not a provider (no range/metric axis, no external source, nothing for the poller to refresh). Model call mirrors providers/email_triage.py: the Claude Code CLI on the subscription login, never the raw API. `_generate` is a sync worker only called through `_spawn` (asyncio.run inside would blow up on the event-loop thread); routes are `async def` on purpose because `asyncio.create_task` needs the loop thread and they only do fast reads. When `agent.blocked()` (usage window spent) generation skips gathering ~two dozen sources and writes the same error row a refused call would. Each write opens its own connection rather than holding one across the multi-second model call.
- The system prompt makes sections earned: giving every section every day with a filler line made the brief a wall to skim past; a quiet day should produce a short brief.
- `_event_weather` uses leave.is_virtual() (reused, not recopied): geocoders resolve junk locations confidently, so a "TBD" event quoted the forecast at (2.77, -77.67), open Pacific off Colombia where the day is always a 100% thunderstorm, and a Zoom URL quoted the campus centroid, which reads plausible enough never to be questioned.
- The plan view's brief bar (`/line`, LINE_SYSTEM) is one line over a "needs you" list the reader already sees, so it may only say what the list can't (how asks bear on each other + tomorrow's first thing); the morning brief stays whole for the skill and the "Morning brief" view. Lines are cached in memory per (day, signature of asks); a restart costs one regeneration. Ask fields are clipped, not rejected: a 422 hid the whole bar (8 times in the log), and a row's `why` grows on its own (the Messages-broken suffix).

### `app/browser.py`: open a URL in the system browser

- The dashboard runs in a pywebview window (launch_app.py) as often as in a browser tab; WKWebView answers a JS `window.open()` with null (pywebview's Cocoa delegate forwards only link activations to the browser), so a tab the frontend opens itself is dropped and assigning to the null is a TypeError. The OAuth consent page couldn't live in the webview anyway (Google refuses sign-in in an embedded one). So the hand-off is backend-side, the same call people.py's Messages hand-off makes: one path for both shells. The URLs are ours (built by OAuth providers) but reach an OS launcher, so open_url keeps the URL an argv element.

### `app/claude_cli.py`, `app/claude_account.py`, `app/session_digest.py`: console CLI, login, session digests

- `claude_cli.py`: the console runs `CONSOLE_CLI_PATH`, a native install (a symlink into `~/.local/share/claude/versions/<version>`); this machine has auto-updates off, so nothing says a release landed short of opening a terminal. `GET /api/console/cli`: the installed version is the symlink target's name (asking costs no process); latest is the channel file the native installer reads (`downloads.claude.ai/claude-code-releases/<channel>`, a bare version string), fetched lazily and held an hour (`LATEST_TTL`; a failed check retries after 5 min) because the bar polls. `POST /api/console/cli/update` runs `claude update` (the CLI's own updater, repoints the symlink; one at a time since two would race on it; `UPDATE_TIMEOUT` 300s is a slow download, not a hang). A running session keeps the binary it started with; the next connect (new tab, model/mode switch) gets the new one, and the page reconnects its tab for that.
- `claude_account.py`: one login, the CLI's, shared by the console, `agent.ask` and the terminal, so switching here switches everywhere (intended). Per-account `CLAUDE_CONFIG_DIR`s would keep several signed in at once but split the transcripts, skills, hooks and memory under `~/.claude`, most of what makes a tab worth resuming. Kestrel never sees a credential: it runs `claude auth status` and `claude auth login` (opens the browser and waits on a localhost callback; seen in a throwaway config dir), reading back named fields only. One login at a time; gives up after `LOGIN_LIMIT_S` (5 min). Not established: what a cancelled sign-in leaves behind when one was already signed in (only a real switch can show). The sign-in URL the CLI prints is stripped from failure text. The email goes on an argv as `--email`, so the regex rejects anything that could read as a flag.
- `session_digest.py`: the digest reads the same replay as the console's back-scroll, so what's summarised is what you'd see scrolling back. It is extractive on purpose (what you asked in order, files changed, last reply, cut to a budget): a model summary would be a claim about the session, cost a call per attach and be one more thing to be wrong; the tab reading it can open the session by id for the rest. The first asks frame the session and the newest are where it ended; past `ASKS_KEPT` the middle goes and the digest says how many. The title is the one place a model is asked (#102): a first message like "ok so" names nothing; one Haiku turn over the digest, on click, never in bulk.

### `app/claude_quota.py`: subscription usage windows

- Why: the `rate_limit_event` the CLI streams carries a status and reset time but usually no utilization, and only for the currently binding window, so the console bar said "5h -" forever and never mentioned the week. The interactive `/usage` percentages come from `GET /api/oauth/usage` (needs the OAuth token); the CLI parks the answer in `~/.claude.json` under `cachedUsageUtilization`. Reading that file gives real numbers without touching the token.
- Age is part of the response: only the interactive UI refreshes that cache (throttled to 5 minutes); print/SDK runs, this console included, never write it, so a number can be an hour behind. A percentage stamped with its age beats a dash. Past `USAGE_STALE_SECONDS` (15 min) it renders dimmed but stays; a quarter hour into a 5-hour window it shouldn't look authoritative.
- Age is not the whole story: once a window's `resets_at` has passed, the percentage is void (the quota rolled over; the number describes a window that no longer exists). That is why the 5h chip was wrong far more often than the 7d chip (an unrefreshed cache usually describes a dead 5h window; the week's number is merely a little behind). The console drops the percentage for a window whose reset has passed (`mergeUsage`), which is why `resets_at` travels with every window even with no percent.
- `limits[].kind` maps to the stream's `rate_limit_type` so a live event and a cached percentage land on one chip. Overage (credits) uses its event's key so a live "rejected" and the cached percentage share a chip; credits are in minor units (cents). One parser (`_windows_from_utilization`) serves both the CLI cache and a live fetch since the CLI stores the API's object verbatim; an older flat-key shape (no severity/scope) is still parsed. Unknown kinds render under their own name rather than vanishing; scoped weeks (Opus, Sonnet) name their model.
- The CLI rewrites `~/.claude.json` wholesale, so a read can land mid-write; it's a cache and the next poll gets it.
- Asking the API ourselves: the CLI's cache goes quiet exactly when it's wanted (when you stop using the interactive UI); a local proxy poller that once gave a fresher second reading is gone, and the cache alone is routinely hours or days stale, so this fetch is the only live number.
- Credential decision (overturned deliberately): it was "the environment and nowhere else", on the principle that a dashboard has no business lifting the CLI's login from the keystore; that cost the feature outright. A `claude setup-token` token never worked here: it lacks the `user:profile` scope the endpoint demands (`403 OAuth token does not meet scope requirement user:profile`, `required_scopes: ["user:profile"]`); setup-token has no flag for more, there is no `claude usage` subcommand and `claude auth status` reports no usage. The CLI's `~/.claude.json` cache is not a fallback: it freezes and doesn't resume, not from `-p`, `--bg`, or an interactive TUI on a pty (all tried). So the login keychain (`Claude Code-credentials`) is the only option. Defensible because we read, never write: the CLI owns and rotates the credential (~3h lifetime, own refresh token); re-reading on every fetch picks up rotation; we never refresh it (that would rewrite a credential the CLI owns). `CLAUDE_CODE_OAUTH_TOKEN` still wins as an escape hatch for a token minted elsewhere with the right scopes; never point it at setup-token output (reinstates the 403).
- Throttles: `USAGE_FETCH_INTERVAL` 60s (was 5 min copying the CLI, so with the console polling each minute chips ran up to 6 minutes behind a turn; 1 request/min however many tabs poll; the slot is claimed under the lock so two tabs can't both fetch). `USAGE_FRESH_INTERVAL` 15s: a turn just ended, which is when the number moved (`fresh=1`), unless backing off. `USAGE_AUTH_BACKOFF` 3600s on 401/403/404 (a token we can't use won't fix itself; 404 is an account with no subscription windows). 429: `Retry-After`, else `USAGE_RATE_BACKOFF` 300s (the CLI's cadence, never told off for it).
- `_LiveUsage` is process-local on purpose: a cache of a cache, worthless across restart, and app.db is for data the vault owns. A previous answer stays on offer after a failed refetch; `usage_snapshot` picks the freshest snapshot (no fetch time = unknown age, any dated one wins). `fetch=False` exists for callers on the event loop where a 10s urlopen would freeze the backend. Empty windows is a normal answer: nothing has successfully asked the API under this login.

### `app/commands.py`: slash-command discovery

- Scans the same places the console's setting_sources = ["user", "project"] load from (~/.claude and VAULT_PATH/.claude) for commands/*.md and skills/*/SKILL.md, reading only frontmatter (name + description), so the frontend can offer autocomplete and render /help without the message reaching Claude. The SDK init message (forwarded over the WS as {"type": "commands"}) remains the authority on what a live session loaded, including built-ins like /compact this scan can't see. Lowercase skill.md is tolerated (old-vault convention); descriptions are capped at `DESCRIPTION_CAP` 300 because skills carry essay-length ones; project shadows user.

### `app/config.py`: why the constants are what they are

Plain constants are self-describing; the non-obvious ones, by group.

- **Env and secrets.** `.env` (gitignored; `.env.example` is committed) loads first, and a real env var beats it (dotenv default), so `VAULT_PATH=... uvicorn ...` still overrides per invocation. Secrets (`LOCATION_RELAY_TOKEN`, `AEROAPI_KEY`) belong in `.env`, never in this file. `BACKEND_PORT` must match the uvicorn `--port`: it is baked into the OAuth loopback redirect URI.
- **launch_app handshake.** `KESTREL_HELD_RELOAD` is set by launch_app on the server it starts, whose reloader waits for console turns to finish; absent means some other server holds the port, one that restarts the instant backend/app changes, mid-turn. `KESTREL_NOTIFY_FD` is the write end of a pipe to the window process, which posts notifications as Kestrel (`desktop_shell.Notifier`); absent means no window, and `notify.py` falls back to osascript. `NOTIFY_FD` is popped from the environment at startup, not just read: every process the server starts (each tab's CLI, commands, scripts importing `app.notify`) would otherwise inherit the name without the pipe and write banners into whatever that fd number is there.
- **`DEBT_NUDGE_DAYS`=7.** A chased debt stays quiet this long. A week doesn't nag someone who said "next payday", and an ignored ask doesn't vanish: the failure prevented is the debt mentioned once in July and never thought of again.
- **`EXPENSES_DIR`.** The backend writes the export; the page does not download it, because the desktop shell's webview has downloads switched off.
- **Calendar.** `GOOGLE_CALENDAR_IDS=None` means every calendar on the account's calendarList (subscribed ones too); a list of ids narrows it.
- **Model calls.** Email summarizer, reply drafter, brief writer, brain, extractors, self-texts and tab titles all run through the Claude Code CLI on the subscription login (`agent.ask`), never the raw API. Reply drafting is on demand from the People panel only, never from the poller, and never sends. `EMAIL_BODY_EXCERPT_CHARS`=1500 is sent per message and never stored; `EMAIL_BODY_FULL_CHARS`=20000 (`--body <msg_id>`) is for a person or the console chasing something a summary dropped (a call link, an address), bigger because nothing is fed to a model, and still never stored. `EMAIL_BACKFILL_PER_REFRESH`=60: mail outside the inbox still owed a label and search terms is worked through this many per refresh so the backfill trickles behind the poller. `BRIEF_STALE_GENERATING_SECONDS`=300: backend died mid-run, so the row is reclaimable. `FOOD_ESTIMATE_MODEL`: dining-hall dishes -> ingredients + grams, then a pick among SR28 matches (`food_estimate.py`), once per distinct item name, so a cold start is a few batches and a normal day single digits.
- **`AUTO_PAUSE_PCT`.** Automatic model calls (poller triage/extraction/self-texts/food estimates, brain catch-up, the lazy brief) stand down once a subscription window is this full (five_hour 80, seven_day 85, percent, keyed like `claude_quota`'s windows) so the rest is left for the console and clicks. Paths a person asked for run inside `agent.interactive()` and ignore these.
- **Brain.** `BRAIN_INFERRED_STATUS="pending"` is usable by the console but labelled unconfirmed until the user confirms: everything read off a source needs a check. Group chats count as a source only with at least `BRAIN_GROUP_MIN_MINE`=5 of the user's own messages in the backfill window; 1:1s always count. `BRAIN_BACKFILL_DAYS`=365 is how far the first ingest reaches and how far messages are pulled from chat.db (groups and 1:1s alike).
- **Auto check-ins** (health refresh; `source='auto'`; never overwrite a manual check/un-check; a threshold of 0, or renaming the habit away, disables). Exercise: Active Zone Minutes >= `EXERCISE_AZM_THRESHOLD` (30; ordinary active days are under ~26 AZM, real workouts 60+; Fitbit's default daily goal is 22 if you want it looser) AND peak HR >= `EXERCISE_HR_MAX_THRESHOLD` (180). The HR gate exists because AZM alone is not a workout signal: anything holding the heart rate in the cardio zone earns 2 AZM/min, so a long evening of elevated resting HR (alcohol, illness, stress) can out-score a session: observed 179-315 AZM on days that never broke 177 bpm, against 111-152 AZM on workouts that all hit 180+. Volume can't tell them apart; peak effort can. Tune to your own peaks. Sleep: `sleep_hours` (lands on the wake-up day) >= `SLEEP_HOURS_THRESHOLD` (9) checks `SLEEP_HABIT_NAME`.
- **Plaid / weather / leave.** Plaid's client_id/secret live in app.db (`python -m app.providers.plaid setup`); `PLAID_DAYS_REQUESTED`=730 is Plaid's max. Weather: Open-Meteo, no key; `get()` prefers the latest location fix under 6h old over `HOME_LAT/LON`, which default to the centre of the contiguous US as an "unset" placeholder. Leave: Google Maps Routes v2 + Geocoding; solo use stays in the free tier. `leave_by` = event start - drive - `LEAVE_BUFFER_MIN`; origin prefers a fix younger than `LEAVE_FIX_MAX_MIN`, else HOME. An empty key or any API failure degrades: `/api/leave` still lists events with `leave_by` null, never a hard failure. `LEAVE_GEOCODE_CONTEXT` is appended to a location that geocodes to nothing, then retried once (a university calendar writes half its rooms bare, "KDC 240", which Google resolves only with the campus attached); empty = no retry.
- **Spotify.** Authorization-code + PKCE, public client (no secret; the dev dashboard only needs the redirect URI registered exactly); the token row is `provider='spotify'`, `account=SPOTIFY_ACCOUNT`, `client_secret=''`. Spotify banned `localhost` and non-loopback HTTP redirect URIs in Nov 2025, so the URI must stay the `127.0.0.1` literal matching `BACKEND_PORT`. The callback is a real FastAPI route (`routes/spotify.py`), not a CLI loopback server, since the URI can't be a throwaway ephemeral port.
- **Last.fm.** Genre/mood tags per artist, because Spotify stopped returning `genres`. Free key, no OAuth; unset = provider reports "off" and never calls out. Enrichment trickles at `LASTFM_ENRICH_PER_CYCLE`=25 artists per poll, most-played first, so ~9.8k artists fill in over about a day without hammering a free API (raise it, or `python -m app.providers.lastfm backfill --limit N`, to hurry). `LASTFM_MIN_TAG_WEIGHT`=10 on a 0-100 scale relative to the artist's own top tag.
- **Messages.** chat.db is truth; app.db `messages` is a genuine cache (chat.db keeps full local history, so dropping it costs a re-sync), unlike Spotify's `plays`. No OAuth: reading chat.db needs Full Disk Access on whichever process runs uvicorn (test with a manual sqlite3 read first).
- **Self-texts.** `MESSAGES_SELF_HANDLES` are the user's own numbers/iCloud emails (comma-separated in .env), normalized as `messages.handle` is before comparing (phone: last 10 digits; email: lowercase); a message sent to oneself is routed to a task, event, food entry, list item or note. Empty: routing is a silent no-op.
- **Email extractors** (deliveries, travel). Shared pipeline in `providers/email_extract.py`; the model only sees candidate messages matched by the hint lists, never the whole inbox; LIKE matching is case-insensitive. Office-hours hints match sender OR subject; a *subject* hint carries most weight for course mail (a TA emailing from a personal address about "OH moved to Friday" has nothing recognisable in the from line; course platforms are recognisable there and say nothing in the subject). Deliberately loose: a false candidate costs one cheap extraction answering `relevant: false` and is never retried; a miss means hours quietly go stale.
- **Attachments** (`providers/attachments.py`). Never runs on its own (no poller hook): every image costs model tokens and every PDF parses bytes an outside party chose. `ATTACHMENT_MAX_BYTES` (25MB) only stops slurping a 400MB video; it is not containment. `ATTACHMENT_WORKER_MEM_BYTES` (1GB) is the real bomb defense, enforced by the parent polling the child's RSS, not an rlimit: macOS accepts RLIMIT_AS/DATA/RSS and then rejects every attempt to lower them, so an in-child cap silently does nothing. Generous enough for a legitimate 25MB scanned PDF's page tree, small enough that a file inflating toward gigabytes dies before the machine swaps. The real ceiling is the cap plus whatever the child allocates in one poll gap (`attachments._RSS_POLL_S`): a 0.15s gap measured ~0.84 GB past the cap; at 30ms the stated and effective caps are within ~17%. The vision cap is the API's: base64 inflates by 4/3 against a 5MB request limit, so ~3.5MB raw. The timeout is wall-clock and belongs to the parent; RLIMIT_CPU can't cover a process blocked in a syscall, which burns no CPU.
- **Flights** (AeroAPI, Personal tier; optional, trip extraction from email works without a key; only `poll_flight_status()` needs it). Cost control: only flights departing within `FLIGHT_POLL_HORIZON_HOURS`, re-checked at most every `FLIGHT_POLL_INTERVAL_HOURS` until `FLIGHT_POLL_CLOSE_HOURS` out, then every poller cycle.
- **Alarms.** `ALARMS_DRY_RUN` defaults off: a channel that never fires is indistinguishable from one that works, so a default dry run tests nothing. `ALARM_HORIZON_DAYS`=14 keeps the Reminders list readable and stops a far-future flight collecting rewrites for a season. `ALARM_REMINDERS_LIST` is a dedicated list, never an existing one: the reconciler writes and deletes everything in it, so a list with real reminders would be in reach of a withdrawal; the account must be the syncing one (iCloud), since a local-only list never reaches the phone. `ALARM_MAX_WITHDRAW_PER_CYCLE`=5 is a runaway guard: the three-state source protocol is what should stop a mass withdrawal, so tripping this means that failed; a plan pulling that many at once is likelier a bug than a day's changes; withdrawals are skipped wholesale for the cycle, not partly applied. `PACKAGE_ALARM_HOUR`: packages carry `eta_day` and no clock, so the alarm gets an hour in the user's zone (`tz.zone_on`), unlike a flight whose departure clock belongs to the airport. `LEAVE_ALARM_TOLERANCE_MIN`=3: `leave_by` is recomputed from live traffic each cycle and jitters a minute or two; without a deadband every cycle is an amendment, and an amendment withdraws the alarm off the phone and rewrites it.
- **Notify** (`app/notify.py`). `NOTIFY_ENABLED` defaults on: a local banner has no blast radius worth a dry run. `NOTIFY_IDLE_MAX_SEC`=180 is aw-watcher-afk's number for "still at the desk". `NOTIFY_STALE_MIN`=45: a notification means now; held past this it is a lie ("leave in 5 minutes" an hour late), so it expires instead of firing. This is the one deliberate data loss in the channel, the opposite call from the alarm writer, which retries all day because an alarm carries its own fire time and stays true. `NOTIFY_MAX_PER_CYCLE`=5 defers rather than refuses (unlike the withdraw cap): half a withdrawal strands alarms, but half the news beats none; the rest keep their turn next cycle.
- **Wake** (`app/wake.py`). The alarm writer hands the phone an alarm hours ahead but cannot keep it true across a night, because the reconciler only runs while the lid is open; an OS wake shortly before each alarm buys one more cycle. Scheduling reaches root through a sudoers drop-in scoped to `/usr/bin/pmset schedule`; until installed the write verbs fail on `sudo -n` and `apply()` logs and retries, a soft failure (a missed wake costs freshness, never the alert). `WAKE_DRY_RUN` defaults off. `WAKE_OWNER` is stamped on every event scheduled and is the only owner ever cancelled: `pmset -g sched` is shared with the system (com.apple.alarm events for calendar travel and Do Not Disturb), so `cancelall` is never used and each cancel names exact type/date/owner (same partition argument as the alarm `kind:` prefix). `WAKE_LEAD_MIN`=20 must cover a whole cycle (every provider, the reconcile, the Reminders write) plus iCloud's sync to the phone, which is not ours and not instant. `WAKE_HORIZON_HOURS`=36 is much shorter than `ALARM_HORIZON_DAYS` and not the same kind of number: a Reminders row costs nothing sitting there, a wake event is a machine spinning up, and a Thanksgiving flight needs none (the laptop will be open many times before). 36h covers tonight and tomorrow. `WAKE_COALESCE_MIN`=30: alarms cluster (check-in and leave_by for one trip) and one wake refreshes every alarm in its cycle, so wakes within the window collapse to the earliest. `WAKE_TOLERANCE_MIN`=5 is a deadband for every kind (as `_FIRE_DEADBAND` is for `leave`): coalescing makes the wake time the minimum over a cluster, so any member jittering a minute would drag it, and every move is a cancel plus an add against global state. `WAKE_MAX_EVENTS`=6 truncates rather than refuses: the list is time-sorted, so what falls off is least urgent and a later cycle schedules it.
- **Location.** OwnTracks (iOS, HTTP mode) posts to the relay (`relay/`, a Cloudflare Worker), never to this machine; `providers/location_relay.py` pulls each cycle. URL is the Worker's workers.dev origin; `LOCATION_RELAY_TOKEN` is the Worker's `PULL_TOKEN` (read + delete; the phone holds a different write-only one). Both unset = no ingest and health reads "off". `HOME_RADIUS_M`=250, not 150: HOME is a dorm building whose own fix cluster spreads ~127m around its centroid, so 150m put the far side within 20m of the boundary and `at_home` flickered across one building.
- **News / athletics / coursework.** News: Kagi Kite (`kite.kagi.com/kite.json`), no key; slugs must match a "file" entry in the index minus `.json` (CLI `--list` on the index if unsure); `refresh()` fetches once per `day_key`. Athletics: Sidearm's public iCal of every USC sport (no key), so kickoff times come from here, not web searches. `BRIGHTSPACE_ICS_URL` carries a per-user token so lives in `.env` only; unset = the provider answers with nothing rather than failing.
- **Reddit.** Arctic Shift (Pushshift's successor) is the only anonymous route left: reddit's own `.json` answers 403 to any script regardless of User-Agent, old.reddit redirects to a login, `.rss` feeds all answer 404. `REDDIT_SELFTEXT_CHARS`=600: selftext is stored whole but trimmed on the way out so a range read stays token-cheap; `--query` filters the payload so it only sees these first N chars (the untrimmed column is for `--search` and direct table reads). `REDDIT_SCORE_MATURE_DAYS`=3: Arctic Shift ingests a post at creation and revisits it later, so score is worth ~nothing for the first day (measured: every post from the previous day read score=1, day-2 and older carried real values: 52, 465, 214); days inside the window are re-synced every refresh and reported `provisional: true`, never charted as final. `REDDIT_REFRESH_MIN`=60: re-fetching three days per subreddit every 5-minute poll would be rude to a free archive for data that moves hourly. `REDDIT_MAX_PAGES`=20 (pages of 100) covers a very busy sub's day; the cap only stops a pagination bug walking the whole archive.
- **Google OAuth scopes.** One token row per (provider, account); scope presets per account so `python -m app.providers.oauth authorize --account school` needs no flags. `school` (Workspace): `calendar.readonly` lists the calendars themselves; `calendar.events` is read/write on contents and is what `routes/calendar.py` needs to create, move, cancel and RSVP; neither can create or delete a whole calendar (that takes unscoped `auth/calendar`). `personal`: Google Health / Fitbit Air, restricted-tier scopes, so expect scary consent screens on your own External/Testing app.
- **Console permission mode.** `PERMISSION_MODE` defaults to `auto` (`console.AUTO_MODE`), not the SDK's `acceptEdits`, which the picker no longer offers: acceptEdits reads as the cautious middle and isn't, because the CLI settles Bash writes inside the workspace under it without consulting the backend, so `rm <vault>/...` runs with no card while `task_create` stops for one. `auto` inverts both halves: what it runs unattended is reversible, what can't be taken back still asks. `CONSOLE_PERMISSION_MODE=default` asks about everything. `SETTING_SOURCES` loads `~/.claude` and vault/.claude (the user's skills). `DEFAULT_MODEL` is Sonnet since most console chats are drafting/quick questions (the picker can still choose bigger); a dev tab is real coding, where the strongest model earns its cost, and `"opus"` is the CLI alias for the newest Opus.
- **`CONSOLE_CLI_PATH`.** The SDK ships its own `claude` under `claude_agent_sdk/_bundled/`, pinned to the version the SDK was cut against (2.1.191 for sdk 0.2.110). That build's WebSearch hangs ~5.5 minutes behind the `ANTHROPIC_BASE_URL` proxy and fails the turn with `502 Unable to safely convert buffered response to SSE`; the installed CLI, same proxy and model, returns in ~13s. `~/.local/bin/claude` is a symlink the installer repoints on upgrade, so this tracks the current version. It is a host path: if missing, fall back to the bundled binary rather than failing to start.
- **Chrome / computer use.** `--chrome` tools appear whenever the flag is set but only work with Chrome open and the extension connected. `COMPUTER_DIR` (`console_terminal.py`) is its own folder because `/mcp` enables computer-use per project, so it is one enable; it is outside this repo on purpose: the CLI's project is the enclosing git root, so in-repo the enable would land on the repo and the job would load its CLAUDE.md.
- **`CONSOLE_ALLOWED_TOOLS`** is passed as SDK `allowed_tools`, which means auto-allowed: the SDK executes these WITHOUT calling `can_use_tool`, so nothing here reaches the approval card; only entries safe to run unattended belong. It applies regardless of the vault cwd (the console never loads this repo's `.claude/settings.json`). Bash is deliberately absent: it used to hold prefix patterns like `Bash(curl -s http://localhost:8788/*)`, but a prefix wildcard matches everything after the URL too (`... /api/tasks -X POST -d @-` is the same prefix), auto-approving reads and writes alike across ~25 mutating endpoints. A prefix string cannot express "and no write flags", so Bash policy lives in `console_policy.is_safe_read()`, which sees the whole command; anything it doesn't recognise prompts. The MCP entries each name one read-only operation, with no wildcard and no argument surface. Read/Grep/Glob are here because leaving them out backfired: they cannot write but still reached `can_use_tool` and prompted, so the model reached for Bash `grep -ril`/`find`/`cat`, which prompted too and arrived without their scoping. Exfiltration is unaffected: it needs a write or outbound request, which still stop at a card.
- **`CONSOLE_BUILTIN_TOOLS`** is a different axis from the allowlist: which built-ins the console is given at all. Unset, `tools=` defaults to the whole claude_code preset: 30 built-ins whose schemas cost 14,165 tokens, re-read on every assistant message. Over every console session on record only ten were ever called (ToolSearch, Bash, WebSearch, Grep, AskUserQuestion, Read, WebFetch, Glob, Write, Edit); the other twenty are CLI furniture (Task/TaskOutput/TaskStop, Cron*, EnterWorktree/ExitWorktree, PlanMode, Monitor, SendMessage, NotebookEdit, ReportFindings, DesignSync, ...) meaningless in a vault console that cannot spawn agents or manage worktrees. Naming the ten saves 6,440 tokens/message (28,561 -> 22,121 via `get_context_usage`), ~16% of console spend. ToolSearch must stay: the 31 vault tools and 51 claude.ai connector tools are deferred, so they cost nothing until asked for, and ToolSearch is how a session asks; dropping it strands every `mcp__vault__` writer. `Skill` is deliberately absent, the one real behaviour change: its omission drops the Skills block (1,990 of the 6,440). All 14 skills load through `setting_sources`, 13 are this repo's dev skills (dataviz, frontend-design, artifact-*, code-review) leaking into a console that never invoked one; the 14th, the vault's daily-brief, is built backend-side by `brief.py` through `agent.py`, not here. If a console session ever needs a skill, add "Skill" back at ~2k tokens/message.
- **Dev tabs** (`console_profiles.py`) are Claude Code on a repo, so `DEV_BUILTIN_TOOLS` adds the furniture the vault console strips: subagents, task list, notebooks and Skill (a repo's own skills, e.g. `verify`, are the point there). The task list is TaskCreate/TaskUpdate/TaskList/TaskGet: TodoWrite is gone from the CLI (2.1.312 drops it from the tool list silently; the model went looking via ToolSearch and found nothing). `EFFORT_LEVELS` are the `--effort` values the start message may ask for; anything else is dropped and the CLI default stands. `DEV_PROJECT_ROOTS`: each immediate subdirectory is offered in the new-tab picker and nothing outside is accepted as a cwd.
- **`DESK_BASELINE_DAYS`**=28 (`lib/notable.js`) is the history a desk card reads a reading against: four weeks, the same for every card, so "unusual" means one thing across the desk; four weeks carries four of each weekday, so a weekday/weekend shape isn't mistaken for signal (a seven-day baseline calls every Saturday remarkable).

### `app/console.py`: WebSocket wire protocol

The console WebSocket speaks JSON frames. Reference, moved from the module docstring (field names are the contract with `Console.jsx`):

```
Wire protocol (JSON over WS):
  client -> server:
    {"type": "start", "session_id": "..."|null, "model": "sonnet"|null,
     "permission_mode": "auto"|"default"|"plan"|"bypassPermissions"|null,
                             -- "auto" is ours, not the SDK's: no approval
                                cards except for calls that can't be undone
                                (console_changes.Change.guard), and a
                                `changes` summary at the end of every turn.
                                "bypassPermissions" ("never ask" in the
                                picker) drops can_use_tool altogether; the
                                summary still arrives, being built from the
                                tool stream rather than the approval path.
                                Any SDK mode string is still accepted, so a
                                client pinned to "acceptEdits" keeps working
                                — the picker just no longer offers it.
     "voice": true|false,    -- speech out; fixes the prompt rule that goes
                                with it, so changing it means reconnecting
     "effort": "low"|…|"max"|null, "output_style": "Explanatory"|…|null,
     "resume_at": uuid|null} -- start-time like the rest. resume_at rewinds
                                the conversation: resume session_id cut just
                                after that record, as a fork (rewind_point).
    {"type": "mode", "permission_mode": "auto"|"default"|"plan"|"bypassPermissions"}
        -- switch the live session's mode, mid-turn too (set_permission_mode):
           no reconnect, so history, the turn and its cards all stay. Answered
           with a `mode` frame (the old mode again if the switch failed).
    {"type": "rewind", "id": uuid}   -- dev: restore files to that checkpoint
    {"type": "mcp", "action": "status"|"reconnect"|"toggle", "name", "enabled"}
    {"type": "user", "text": "...", "context": [{"label": "...", "data": {...}}]}
    {"type": "permission", "request_id": "...", "allow": true|false,
     "always": true|false, "reason": "..."|null}
        -- always:true adds a session-scoped rule so the same tool stops
           asking (Bash narrows to the approved command's first word); reason
           is the "tell Claude what to do differently" text on a denial.
    {"type": "question", "request_id": "...",
     "answers": {"<question text>": "label"|["label", ...]}|null}
        -- the reply to a question_request; null means dismissed unanswered.
    {"type": "interrupt"}
  server -> client:
    {"type": "idle"}
        -- the answer to an interrupt when no turn is running: the page's
           turn died with an earlier socket, so it settles it itself.
    {"type": "session", "id": "..."}
    {"type": "text_delta", "text": "..."}
    {"type": "phase", "phase": "thinking"|"tool", "name": "..."|null}
        -- a thinking block or a tool call (name set) started streaming;
           the live status line until text or the tool's chip arrives.
    {"type": "speech", "text": "...", "wav": "<base64 WAV>"}
        -- one spoken sentence, sent only when the session started with
           voice:true. Arrives in the order it was written; a turn that gets
           interrupted stops mid-backlog and the rest is never sent.
    {"type": "tool_use", "id": "...", "name": "...", "detail": "...", "raw": "{...}"}
        -- `detail` is the one-line summary the log shows (sessions.tool_detail);
           `raw` is the full input, shown only when a run is expanded.
    {"type": "tool_result", "id": "...", "is_error": bool, "image"?: "data:…"}
        -- that call came back. The plan view's map lights a line per call
           from tool_use to here, red when is_error. `image` is the picture
           the call handed the model, when it did (result_image).
    {"type": "refresh", "topics": ["tasks", ...]}
        -- a call changed what these cards show (console_changes.refresh_topics),
           so they reload now, and the needs-you list with them. Sent as each
           call comes back clean, and once more for the whole turn just before
           its result: what a hook or the note watcher does after the call
           lands behind the first one.
    {"type": "permission_answered", "request_id": "...", "allow": bool, "by": "phone"}
        -- a card answered somewhere other than the page: the phone door
           (app/phone.py) puts the answer on the tab's inbox and sends this
           so the card at the desk settles too.
    {"type": "permission_request", "request_id": "...", "tool": "...",
     "input": {...}, "plan": "..."|null,          -- plan set for ExitPlanMode
     "always_label": "..."|null,                  -- what "don't ask again" grants
     "always_scope": "session"|"turn",            -- for how long (turn: Chrome)
     "guard": "..."|null,                         -- why auto mode asked anyway
     "inputs": [{...}, ...]}                      -- the whole burst; see below
        -- One turn's parallel calls to the same tool arrive as one card, and
           one answer settles all of them. `input` is the first of `inputs`, so
           a client that only reads `input` still renders something correct.

    {"type": "question_request", "request_id": "...", "questions": [
        {"question": "...", "header": "...", "multiSelect": bool,
         "options": [{"label": "...", "description": "..."}]}]}
        -- AskUserQuestion, which the CLI can't prompt for in SDK mode and
           auto-declines in seconds; see ask_questions() for the interception.
    {"type": "changes", "items": [{"verb": "created", "scope": "task",
                                   "detail": "...", "tool": "..."}, ...]}
        -- what the turn changed, sent just before its `result` and only when
           there was something. Built from the tool stream (see
           console_changes), so it counts calls that ran and came back clean
           — not calls the model asked for. Every mode sends it; auto mode is
           where it stops being a nicety.
    {"type": "result", "duration_ms": ..., "is_error": ..., "interrupted": bool,
     "tokens_in": ...|null, "tokens_out": ...|null}   -- in includes cache reads;
        interrupted = you pressed Stop (is_error is usually also true then)
    {"type": "status", "text": "..."}
    {"type": "commands", "commands": ["compact", "daily-brief", ...]}
        -- slash commands the live session actually loaded, from the SDK init
           message; sent once. GET /api/commands supplies descriptions.
    {"type": "session_info", "version": "2.1.312", "commands":
     [{"name", "description", "argument_hint", "builtin"}], "agents": [...]}
        -- sent on connect, from the CLI's initialize answer: the commands and
           skills *this* session loaded (a dev tab's repo skills, plugins,
           built-ins), described, before any turn. Supersedes the scan.
           Also `models` ([{"value", "resolved", "label", "efforts"}],
           session_models): what the pickers offer, cached per profile.
    {"type": "model", "model": "claude-fable-5-2"}
        -- the model the session is running, when the stream first names it
           and whenever that changes (a /model switch); note_model.
    {"type": "context", "total": n, "max": n, "percentage": 0-100,
     "categories": [{"name", "tokens"}], "autocompact_at": n|null}
        -- the window as /context sees it; on connect and after every turn.
    {"type": "tasks", "items": [{"id", "subject", "active_form", "status",
                                 "blocked_by"}]}
        -- the CLI's task list (console_tasks), on connect and after every
           TaskCreate/TaskUpdate result.
    {"type": "checkpoint", "id": uuid}  -- your message's uuid: the point a
           conversation rewind cuts before (start's resume_at, via GET
           /api/sessions/{id}/rewind) and, in a dev tab, the point
           `{"type": "rewind", "id"}` restores files to; that one is
           answered by {"type": "rewound", "id", "ok", "error"?}.
    {"type": "mcp", "servers": [{"name", "status", "scope", "source",
     "transport", "tools", "error"}], "error": ...|null}
        -- answer to {"type": "mcp", "action": "status"|"reconnect"|"toggle",
           "name"?, "enabled"?}.
    {"type": "rate_limit", "rate_limit_type": "five_hour"|"seven_day"|...,
     "status": "allowed"|"allowed_warning"|"rejected", "utilization": 0.0-1.0,
     "resets_at": unix_ts|null, "overage_status": ...|null,
     "overage_resets_at": unix_ts|null}
        -- subscription /usage-limit status; the CLI emits this whenever a
           window's status changes, not on every turn, so the client should
           hold onto the last value per rate_limit_type rather than clear it.
           It carries a status and a reset time but rarely a utilization, and
           only for the window currently binding — percentages come from
           GET /api/console/usage instead, and the bar merges the two.
```

### `app/console_agents.py` and `app/console_tasks.py`: the agents strip and task list

- **Agents strip.** The Agent tool runs its subagent in the background by default: the call returns at once ("launched"), the parent's turn can end and the subagent keeps going. Its tool calls don't ride the parent's stream, so the only signs are the CLI's task lifecycle messages (`task_started`, `task_progress`, `task_notification`, `task_updated`; `task_updated` is sometimes the only word that a task finished). They are read off the raw `data` dict rather than the SDK's typed subclasses so an older or newer SDK that types them differently still feeds the strip. `apply` folds one into `agents` (task_id → row); the console then sends the whole list (like background `!` commands) so a reconnecting page gets it whole. `task_started`'s description is the assignment; `task_progress` reuses the field for what it's doing now ("Running echo hi"), kept as `activity`. Finished rows are trimmed to `KEEP_FINISHED`=6 for the view pane; running rows are never dropped.
- **Task list.** A dev tab's task list is read from the CLI's own files (`~/.claude/tasks/<session id>/<n>.json`), not rebuilt from tool calls: TaskUpdate carries only what changed, so a replay would have to model every field the CLI merges, and a resumed session's list was written by calls this socket never saw. Read-only, like the transcripts; a file caught mid-write is skipped and the next change re-reads it.

### `app/console_changes.py`: the change ledger and the guard list

Two questions share one table (whether a call writes, and whether what it wrote to can be taken back). `classify()` returns None for a read or the row the summary prints; it is built from the tool stream rather than the approval path, which makes it mode-independent and honest (denied or failed calls never get a result block); nothing is asked of the model. `Change.guard` is the second question: auto mode's summary arrives after the fact, and "you sent that email an hour ago" is not a review. The test is "does this leave the machine, or leave nothing behind to undo it with": a vault note is on disk and in the log, a task row can be edited back; a sent mail, a trashed thread and a deleted calendar event are gone. Guarding costs about one click a week; not guarding costs an apology to someone else. The tables are explicit (a tool in `WRITE_TOOLS` not here is a write no summary mentions; the test fails on it); the name-shaped fallback (`_UNKNOWN_DESTRUCTIVE`/`_UNKNOWN_WRITE`) covers claude.ai connectors, whose tool sets change without notice, biased the same way (looks destructive means guarded; unmatched means a read).
- A guard is a sentence because it is shown on the card that interrupts auto mode ("auto mode paused — it would <guard>"), so each finishes that sentence and carries no dash of its own.
- Vault-table rationale: all in app.db or the vault and editable afterwards; two exceptions guarded. `contact_update` overwrites a synced macOS address-book card and the old value isn't kept. `email_draft` is the reviewable artifact (drafting unattended is most of what auto mode is for); `email_archive` moves mail and the card's undo puts it back; `email_schedule` is the opposite of a draft (nobody presses Send, the mail leaves at `send_at`; the card is the last check). `event_rsvp` mails the organizer but can be re-sent with another answer, so unattended. `file_read` is in `WRITE_TOOLS` only for the vision cost: not a change. `theme_set` is a READ_TOOLS entry (no card; colour roles only, undone from the Look lab) but is recorded because it changes the window. Gmail untrash/unspam/label calls are reversible by their opposites.
- `_CARD_TOPICS`: refresh-bus topics per scope (lib/refresh.js); cards reload when the call returns clean and the "needs you" list follows. Topics, not a broadcast, which would also re-read provider-backed cards nothing touched. Keyed on scope so a connector write and a vault tool on the same thing hit the same card; a missing scope fails the test; an empty tuple means no card shows it (drafts and scheduled mail live in Gmail/email_schedule, not the queue). `db` and `shell` map to every card because SQL/scripts can touch any table; `look` is added after, because colours reload on a theme set, not every shell command. In a dev tab only calls on app.db or the dashboard's own API (`:8788`) reload anything; its files and scripts say nothing about the desk.
- `_SHELL_READS`: programs that only read, so a pipeline of them stays out of the summary. `sed`/`awk` absent (they write files), as is anything that starts something (`open`, `npm`, `python`). `sqlite3` and `curl` are absent on purpose: their reads already returned via `is_safe_read`, so what reaches the set is a non-SELECT sqlite3 and a non-localhost-GET curl, which the summary exists to show. Shell guards: deleting files, history rewrites, anything on another machine, `osascript` (how an iMessage gets sent). curl/wget with a body or method is a write on someone else's machine; a plain GET of a web page is a read wherever it points.
- `_SQL_DESTRUCTIVE`: an INSERT can be re-edited; DROP/DELETE leaves nothing to edit, so only those interrupt auto mode (`console_policy._SQL_WRITE` is the wider net deciding what may run unattended at all). A sqlite3 past `is_safe_read` records as scope "db", since that is what the row means to a reader.
- `_shell_guard` reads every token, not just the first: `cd /tmp && rm -rf x` runs `rm` whatever it starts with, and a word merely named `rm` costs one card (the file errs this way everywhere). `_program` skips a leading `VAR=value` (the trap `_always_rule` fell into: `VAULT_PATH=/abs uvicorn …` was named after a word the command never ran). An unparseable command is recorded and guarded (unknown contents).
- Dev tabs waive `_DEV_WAIVED_SHELL` (`rm`, `rmdir`, `gh`, `pkill`, `killall`) and `git push`: deleting build leftovers, killing a stuck dev server, `gh` and pushing (CLAUDE.md: commit and push once verified) are the work there, and a tab that cards each is one you must sit and watch. Mail, AppleScript, other machines, sudo, launchctl and history rewrites still stop. `dev_guard` re-reads only Bash; an MCP send/delete stops in a dev tab like anywhere; the summary row is unchanged (a waived push still lists).
- `is_safe_read` decides what may run without a card; `_shell_change` decides what goes in the summary. Different questions (`ls` fails the first allowlist and isn't interesting to the second). A `Write` row says "rewrote" vs "created", since "wrote 3 notes" reads differently once one already existed.

### `app/console_link.py`: a session that outlives its socket

Before the Link, the session lived inside the websocket handler, and the handler's `finally` denied waiting cards and disconnected the CLI the moment the socket closed, so a reload (⌘R) ended every turn in flight. Now the session talks to a `Link` registered by tab id (`links`); the socket of the moment attaches, and when it goes the session keeps working with nobody listening. The next page to open the tab attaches and carries on. What a Link records for a page that just arrived:
- **The current turn so far** (`turn`, None between turns). Everything before the turn is on disk in the transcript, so the Link notes the transcript's length when the turn began (`cut`, with `cut_session`) and keeps the turn's own events; the page reads the transcript to the cut and replays events after it: no overlap, nothing missing. Consecutive `text_delta`s are joined as recorded (one entry, not thousands). A new session is named during its first turn; before that there was no transcript, so `cut` is 0 and all of it is replay. At `result`/`idle` the turn is dropped (it's on disk).
- **Sticky state** (`STICKY`): the last of session, session_info, mode, model, worktree, commands, held, tasks, agents, context, mcp, shells: what a fresh start would have sent.
- **Only requests still open** (`open_request`): an answered card (or open_tab/tabs request) stays answered; one still waiting is replayed and its answer goes to the future that has been waiting. A `queued_in` replays as `replay_user` from `queued_text`, because the page that queued it is gone with its text.
- `UNRECORDED` (speech, phase, refresh) is never replayed: audio for words already logged, and momentary nudges. `WORK_EVENTS` (turn_start, result, idle, agents, session) call `on_work` (console_work.mark); bookkeeping failure never breaks a turn.
- **Reaping.** A detached session ends once idle (`busy()` false) for `IDLE_GRACE_S` (20s: long enough for a reload, short enough that closing a browser tab doesn't leave a CLI running all evening); a busy one is left to finish up to `DETACHED_MAX_S` (3h). A Link with no key (nobody can come back) ends as soon as it's alone. The reaper polls every 2s while links exist.
- `attach` closes a previous socket with code 4001 (another page took the tab; the client must not retry). `register` ends a Link already on that tab: a start that didn't ask to attach is deliberate (New chat, another session, a model change).

### `app/console_policy.py`: the Bash allowlist, rule by rule

Everything is an allowlist: a false negative costs one click, a false positive is an unattended write. Why each list is shaped as it is:
- **Curl flags.** The first version was a blocklist testing `flag.startswith("-X")` per token; a bundled short flag slipped past (`-sX` doesn't start with `-X`), so `curl -sX DELETE http://localhost:8788/api/plaid/items/<id>` was auto-approved with no card. Now only `_CURL_SAFE_FLAGS` pass; option-attached values like `-m5` prompt (write `-m 5`).
- **`-L`/`--location`** is allowed on one standing assumption: the host check is a prefix test on the URL as written, and curl follows a 3xx wherever it points. It is safe only while the backend issues no redirects (currently no RedirectResponse and no 30x anywhere in `app/`). If a route gains one, drop `-L`, or an auto-approved localhost read can be walked off-host.
- **Provider-CLI flags.** argparse accepts unambiguous abbreviations, so it must be an allowlist: `--ref` reaches `--refresh` (re-pulls from an external API), `--extract N` runs the attachment extractor; both are side effects worth a click. `--body <id>`/`--chars` fetch one Gmail message: a read, nothing stored, and the `email_body` tool is already allowed without a click, so gating the CLI call would only add friction.
- **Pager pipes.** The pipe was never the danger, the right-hand side is, so pagers are named individually (`_PAGER_CMDS`); everything else still disqualifies. `sed` is absent on purpose (its `w` writes files), as is any pager whose job is to write output elsewhere. Pager flags are allowlisted too: file-taking options are the risk (`sort -o`, `grep -f`, jq's `--rawfile` family) and prompt. A bare numeric flag (`head -20`) is matched by `_NUMERIC_FLAG`.
- **sqlite3** is a general SQL shell, so three things are checked: database (app.db, index.db, chat.db by basename; chat.db is Messages' and read-only to us anyway), options (`-cmd`/`-init` are excluded because they execute arbitrary SQL before the inspected statement), and the SQL itself. The breakout scan skips the SQL, because `PRAGMA busy_timeout=5000; SELECT ...` (the CLAUDE.md form) legitimately holds `;` that shlex already unquoted; the SQL is judged as SQL. Options are not run through `_flags()`, which would split `-json` into `-j -s -o -n`.
- **SQL write regex** covers DML/DDL plus functions the sqlite3 CLI adds: `SELECT writefile('/tmp/x', ...)` starts with SELECT, has no write keyword, and writes a file; `readfile()` and `load_extension()` are the same shape the other way. Only `PRAGMA busy_timeout=N` is allowed; other pragmas aren't harmless (`writable_schema=ON` turns the schema into a table). A write keyword inside a string literal still rejects: costs a click, never grants one.
- **Statement splitting** tracks quotes: a chat_guid is literally `any;+;chat5831...`, so a naive `split(';')` tears one WHERE clause apart. Unterminated quotes return None (not approved). `shell_words` uses shlex for the same reason (a `|` or `;` inside a quoted URL/SQL string stays in its token); unbalanced quotes return None.
- **Accepted wrappers**, because refusing them made `is_safe_read` unreachable in practice (every real read carried one): a leading `cd <path> &&` (the provider CLI only resolves from the backend dir while the console's cwd is the vault, and SYSTEM_APPEND prescribes that form), a trailing `2>/dev/null` or `2>&1` (the model appends it reflexively; any other redirect still contains `>`), and a pipe into a pager. Only one `cd` prefix is stripped, and only when the path is an ordinary word, so `cd x && cd y && rm` still trips on `&&`.
- **Chrome.** Claude in Chrome acts with real logins from a session that reads untrusted mail, so anything touching a page asks in every mode; looking doesn't, a new tab starts empty, `resize_window` changes the window not the page. `javascript_tool`, `file_upload`, `upload_image` are never covered by a standing yes (page script can do anything the page can; an upload sends a local file to whoever the page is); each asks on its own (New Features #55). The standing yes ("don't ask again until this turn ends") exists because one web task was a card per click. Navigating to localhost is the dev server (auto-verify): no login to act under.

### `app/console_preview.py`: what an approval card shows

A card exists to be answered, and the only honest way to answer "may I delete this?" is to see the thing. The raw input can't show it: `event_delete` is addressed by `event_id` and `calendar_id`, opaque strings (a 64-hex id, not the title, time or calendar), so approving was a guess dressed as consent. Every card is built in `console_preview.py` instead: ids are resolved against the same caches the tool writes through. Three rules:
- **Resolve the way the tool resolves.** The preview and the call must address the same event, or the card describes one event while another is deleted. `_event_for` mirrors `routes/calendar._address` exactly, including its ambiguity rule: where that function raises on an ambiguous match, the card reports "matches N events" rather than picking a row, because the call is about to be refused and a silent card would make the refusal look like it came from nowhere. `MAX_CANDIDATES` caps the listed candidates.
- **Never raise, never block.** It runs inside `can_use_tool`, between the model asking and the card appearing; a failed lookup costs one card its detail, an exception would cost the approval path. Every resolver is wrapped (`describe` falls back to `_generic`) and reads the local cache only, never an API, so nothing hangs the prompt on a network round trip.
- **Absence is a fact.** A field the cache lacks is left out, not rendered empty: "no description" and "we didn't look" differ, and a blank row beside a delete button implies the first.
Other decisions: long fields are clipped server-side (`MAX_NOTE`, `MAX_VALUE`) because a card that scrolls is a card whose buttons moved. The time carries the zone label (`start_at` strings have mixed offsets; a bare 6:00 is the number a reader acts on unchecked). Requested times from create/update (`_new_when`) are what the model typed, formatted as weekday + clock because "2026-09-23T18:00" must be decoded to notice the wrong day; they bypass `_when` since the zone is the route's decision. `end` comes as bare HH:MM, bare date (last day of an all-day span) or full datetime; reading a date as a time once rendered a four-day trip as "Wed Sep 23–12:00 AM". Bash cards show the whole command (clip 4000, longer folds): a clipped command is one approved unread; `lead` is Claude's description, read first, and an `rm` has its target paths pulled out. Unknown connector tools render as labelled rows (`_generic`), which scans faster than pretty-printed JSON.

### `app/console_prompt.py`: why the prompt rules exist

Reasons removed from the `#` comments above prompt rules (the prompt text itself is unchanged):
- **Math**: the CLI's own prompt says output goes to a terminal, so without the TeX rule the model writes math as Unicode and the KaTeX renderer has nothing to render.
- **Clock vs day-key**: they disagree four hours a night, so stating the day-key isn't enough; the prompt says which wins and forbids the arithmetic. Zone is the same argument on another axis: the user moves coasts mid-year, so "which zone" is a property of the date being talked about, not of today, and is a lookup (`event_zone`). The provider returns one zone per date with a label; the rule stops the model "converting" an already-converted time or dropping the label (also true of the write direction: after reading times in one zone the model writes the user's bare clock time in that zone).
- **Named tools, blind calls**: a read through the provider CLI's `cd … &&` can't be approved by the read allowlist, so it is named. Deferral means the model often calls a tool it never loaded, from prose alone, guessing argument names (`id` for msg_id/task_id, `title` for summary). Every schema is closed (`console_tools._schema`), so a guess is refused by name, and the parenthesised key arg fixes the first try; only on tools seen guessed, since a full signature per tool is the eager-schema cost the prompt avoids. Short names don't resolve: a `select:` with a bare name or a keyword search comes back empty (read by the model as "that capability doesn't exist"), a fully-qualified `select:mcp__vault__x` works; saying the rule once is cheaper than renaming mentions. Tools stay deferred: loading all schemas eagerly roughly triples the prefix, re-read every turn, to save one ToolSearch round trip.
- **Code Mode text**: a chain of reads is N results pasted through context; the threshold is the measured break-even. "Before the first call" and "the answer itself" exist because without them the model fetches and probes first, then scripts, and returns whole bodies.
- **Where things live**: from inside a SELECT, "(contact card)" is indistinguishable from an empty message, and a tool description is read only once the model suspects something is there. The app.db table the model can see is the cache, not the record, and that table is where it goes looking. Without the live-search tool named, it's reachable only from the CLI, which the prompt steers away from. Named writers become the catch-all: with only task tools named, things to buy get filed as tasks, so each kind gets its store; unnamed, a debt lands in a task's note. A habit filed as a completed task still reads as missed, breaking the streak. A flight visible on the calendar isn't tracked. A one-week change written into the recurring rule is wrong every week after. `emails` holds a snippet and no body, so the model otherwise stops at the snippet believing it was the thread; Review files every message, so a range read with a substring filter finds it only if you know when it came. A pinned message read without the turn it answered can be read confidently backwards: a pill is a view, never the record.
- **Gmail/Calendar writes**: the claude.ai connectors report needs-auth in non-interactive sessions (while `claude mcp list` says connected), so the writes go through the backend's own gmail.modify / calendar token. A sample of how the user writes to a person beats a list of don'ts. Google stores one event with a rule, and one call per week leaves unrelated events. A retyped calendar id loses its `@group.calendar.google.com` suffix and every write 404s; getting it from a tool prevents it. Without a usable location there's no leave-by to compute and no alarm; the card looks broken. Durable facts the user mentions are lost without the rule: what they did today is not a note, who they are is a Brain fact. Auto-logging an offhand mention writes to the real calendar unasked and duplicates events, so calendar writes are explicit.
- **Approval narration**: the card quotes whatever was written immediately before the call as the reason (`console._take_narration`), so a call made silently arrives as a bare "allow this?". `AskUserQuestion` refuses a single option (InputValidationError).
- **Citations**: the ref already sits at the end of the row that carried the fact, so citing is copying. The prose prevents two degradations: citing the answer rather than the claim (a ref at the end of a paragraph backs no sentence) and citing everything (a pill on every clause equals none; the user wants the restated detail checked, not the fact that you read their calendar).
- **Documents** (`app/docs.py`): a reply that can only describe a page loses the most when reviewing slides and readings ("slide 12 shows the pipeline" sends the user hunting). The quote is the anchor the browser searches for. Slides drawn above a quiz have scrolled away by the time the question comes.
- **Study blocks** (`ui/StudyBlock.jsx`): asked for in JSON because the renderer needs the answer key and the user must not see it, which prose can't do. A "what does this print" is free-response (a typed answer), since choices give it away and recall should be produced, not recognised. A wrong choice that stands for a real mistake reveals which wrong idea was picked. "How sure?" is asked before the answer and stays open after, which the results then say. A key written before the reasoning gets committed wrong and corrected inside `why`, so `why` comes first. Without the study rules, "is this on the test" is grepped or guessed and nothing the user got wrong is kept. A syllabus has policy, not dates; an older calendar entry can be a day off, the feed has the real deadline.
- **Voice**: VOICE_APPEND is added only when speech out is on: a reply written for the eye is read aloud as pipes and asterisks and the listener can't skim. Steering via SYSTEM_APPEND is the established channel (cwd is the vault, so repo config never reaches it). Study tabs get the stance only. Dev tabs: cwd is a repo whose CLAUDE.md carries its rules, so none of SYSTEM_APPEND applies; they draft client texts but get no Brain. Delegation rule is enforced in `delegates/boundary.py`; the prompt gets the brief right first time.

### `app/console_terminal.py`: computer use on a pty

- Claude Code's computer-use server only exists interactively: 0 tools in an SDK session even with it enabled, 24 on a pty (measured). A console tab can't have it, so `use_computer` hands the task to a real `claude` Kestrel runs itself on a pseudo-terminal and reports the answer back; the user stays in their conversation. Only one job at a time: the CLI holds a single computer-use lock.
- The job's transcript says everything the tab needs, so nothing reads the screen (a misread prompt there is a wrong click on the Mac). What a transcript can't do is answer the CLI's "allow this app?" prompt, which stays the user's, on the live terminal. It surfaces whenever the transcript goes quiet (`QUIET_S`=7s): the CLI only logs a call once answered, so a prompt looks like silence, as does anything else wanting a person. The cost is the terminal showing through a long wait, which is harmless.
- `TranscriptEvents`: streaming logs a message several times, so calls/results are deduped by id. A message is logged a block at a time, each line carrying the final `stop_reason`, so a thinking block says `end_turn` seconds before the answer exists; ending there would return the last progress note as the result and kill the job. A turn ends only on a text block with `end_turn`.
- The child env drops `ANTHROPIC_API_KEY`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDECODE`: the marker set by a Claude Code running this backend's own turn would, inherited, turn transcript saving off in the child.
- `transcript_path` resolves symlinks first (/tmp is /private/tmp) and munges every non-alphanumeric to `-`, or a folder with a space loses its transcript. `computer_enabled` only reads ~/.claude.json (the CLI rewrites it constantly and it holds the login). The model is passed with `--model`, never `/model`, which writes the user's global default. The pty needs a controlling terminal (`TIOCSCTTY`) or ⌃C and resize reach nothing.

### `app/console_tools.py`: the in-process tool layer

The module is the fourth face of every provider (REST route, CLI, cache table, and these MCP tools), all over one implementation so app.db and the providers stay the one source of truth. Through the CLI in Bash the console got a card for every read (the `cd … &&` form the allowlist can't approve), guessed range syntax, retyped ids wrong, and filed tasks into the harness's own store. A read now has a stable tool name that `CONSOLE_ALLOWED_TOOLS` can auto-approve without a wildcard, and a typed schema that ends the guessing. Writers are here so the console stops hand-rolling `curl -X POST` and raw `UPDATE tasks` SQL, not to write unattended: they stay off the allowlist and arrive as cards (the reads/writes split is the security boundary, made once by which list a tool is written into, not by a parallel name list). **Which stores get a writer is load-bearing**: a store the console can read but not write doesn't stay unwritten; its contents land in whichever store has a writer (things to buy filed as tasks, debts written into a task's note). So every store behind a card gets writers the day it is built (expenses to claim back from Dad, habits). A capability the system has but the console cannot name is, from inside a session, indistinguishable from one nobody built.
- **`_err` uses `is_error`** (snake case): the key the SDK server reads off a handler's dict (`create_sdk_mcp_server`'s `run_tool`). The MCP wire spelling `isError` is dropped, so every refusal would reach the CLI as a success: the card stays green and a failed write lands in the turn's change summary, which `console.py` builds from non-error results.
- **`_blocking`/`_backend_call`**: handlers are async on uvicorn's loop (shared with every request and the console websocket), so a provider backfill, a Gmail fetch or a 30s osascript called directly freezes the backend; anything past a single-row read goes to a worker thread. An `HTTPException` is a refusal already phrased for a reader (its detail goes back as-is); anything else is `"{failure}: {e}"`. Network-bound tools (reddit_search, venue tools) run on the same loop as the session websocket, so they use it too.
- **`_with_refs`/`_cited` (citations)**: the ref must travel with the row in the same read: a model that has to make a second call to find what to cite mostly won't, and the feature is worth the fraction of claims carrying one (six characters a line). A provider without `cite()`, or a row it declines (an aggregate, a day count), renders as before: an uncitable number is better bare than pointed at something arbitrary. The raw-JSON read gets a `cite` field per row on a shallow copy, since `out` comes off the provider and some hand back rows still referenced by their caches. The full read (`brief=false`) needs refs too: asked for a detail out of an email, the model went `brief=false`, got rows whose only handle was the raw Gmail `id`, and cited that (a 16-hex string where a ref belongs). `email_body` carries a ref again even though the brief line did: a body is read exactly when the answer will quote it, and the brief line is then furthest back. The raw `id` rides its own labelled line, for `email_body` only.
- **Closed schemas (`_schema`)**: the SDK's bare `{name: Annotated[...]}` shorthand ends in `required: list(properties.keys())`, i.e. everything required. A parameter documented as optional was refused when omitted, and the retry filled every remaining blank (a `provider` call without `metric` retried with `metric: ""` and `brief: false`, dumping a week of raw JSON into the context). It was fixed before the contact writers were added: `contacts.update()` reads a present-but-empty field as "clear it", so under the shorthand an edit adding a phone number would have blanked name, org and note and synced that to the phone. `_schema` also sets `additionalProperties: false`: a misnamed argument used to be silently dropped (`brain_read {"slug": ...}` for `page`, so Kestrel told the user it didn't know their USC ID while the fact sat on the page; `task_update {"due_time": ...}` set nothing). The MCP server jsonschema-validates every call (mcp/server/lowlevel/server.py `call_tool`), so a wrong name now returns "Additional properties are not allowed ('slug' was unexpected)". Of 1,875 audited calls the only unknown keys (22) were all wrong-name guesses. A schema is a full JSON Schema because `create_sdk_mcp_server` passes it through verbatim (its shorthand only fires when "type"/"properties" are absent). `_prop` raises on unmapped annotations so a gap fails at import, not as an untyped parameter. `FactId`: the `<brain>` block prints `[f815]` and the model copies it, which a plain integer schema refused before the handler (which strips the f) ran; the schema takes 815 or "f815"; `pattern` only constrains the string branch.
- **Optional/alias params**: a required msg_id refused `{"id": ...}` before the handler ran (15 times in audited transcripts, all blind calls on the deferred tool; the handler had taken `id` since August), so `msg_id` is declared but not required; `id` is what the provider's email output calls the field, and calls arrived under it about weekly through August, so both are accepted (`thread_id` is another id space and the closed schema refuses it by name). The same for tasks: a db_query row names the column `id`, and `{"id": "130"}` was refused by a required `task_id` 3 times (Sep-Oct transcripts). `provider` `brief` is omitted by default; the instruction tone survives from when the schema made the model send a value.
- **Batches**: `course_review_add`, `list_add` and `food_log` come in runs of 3+, each a round trip through the model; `items` carries the run in one call, each item takes the single call's fields, top-level ones are defaults (one `code`/`list`/`meal` for all), and a single-item call is unchanged. A batch returns every line with failures marked and an error only when nothing landed. `_batched` strips item descriptions (already on the top-level fields) and makes nothing top-level required.
- **reddit_search**: the archive 400s a query naming no subreddit, so "search all of Reddit" isn't offered (a model that believes otherwise burns a call). The archive's outage message is "Timeout. Maybe slow down a bit", which reads as a rate limit and invites an immediate retry; the result must not advise narrowing (the search is down, so a narrower retry 422s the same, and the listing fallback `reddit.search` uses refused as well). When the search is down and `reddit._scan` answers (the phrase matched in the newest few hundred posts, maybe a day of a big subreddit), the header says so. A title alone can't answer a research question and there is no second hop (no fetch tool, Reddit 403s it), so each hit carries a body snippet (`_SNIPPET_CHARS`, shorter than `REDDIT_SELFTEXT_CHARS`, which sizes the cache) and its permalink; 25 posts x 600 chars is a screenful of context. It always goes to the network (30s timeout and a retry), hence a thread.
- **db_query**: opened read-only with a `mode=ro` URI so SQLite itself refuses a write and nothing depends on parsing SQL as the Bash allowlist must. Run off the loop (a model-written query can scan every table; busy_timeout waits up to 5s). Timeout constant: reads take ms, but one correlated subquery per message row over 65k rows held a call until the CLI gave up at 1,260s (the general tab's MCP timeout, sized for use_computer). `_schema_hint`: the commonest failure is a guessed name (`list_items.text` for `name`, `messages.date` for `day`/`ts`) followed by a schema probe or another guess, so the error carries the columns of every table the statement reads and near names for a missing table. Table names are taken from FROM/JOIN, which also matches CTEs and table-valued functions; those are dropped as non-tables.
- **Calendar tools**: the calendar list comes from `calendarList`, which the sync rewrites on each pull, so a rename shows up once and an empty calendar is still listed; the DISTINCT over event rows behind it is the pre-first-sync fallback (right after the migration the table is empty and "no matching calendar" would send the model to `primary`). Calendar writers go through the backend's Google token because the claude.ai connector's writers go needs-auth in every headless session. Each takes `match` as an alternative to `event_id`, on purpose: the id is looked up in the cache instead of retyped from a read, the slip that once produced 18 consecutive 404s. `_repeats`: a series is the write whose result the model can't see all of ("created it" is equally true of a rule that fired once and of one that filled the autumn), so the report says which, and an open-ended rule says so in words (`MAX_OCCURRENCES` is where enumeration stopped, not a count). location and description may be blanked, so `""` survives as clear there and means "not given" elsewhere (`is not None`, not truthiness: `contacts.update()` distinguishes the two). `_as_int`/`_as_list` accept `"6"` / a bare value / a number-as-phone because the schema asks otherwise but a refusal only costs a turn. A thunk is passed to `_backend_call` so `_as_int`'s HTTPException(400) is raised inside it and comes back as a plain error.
- **message_files / file_read**: a file arrives as a message whose text is the sync's placeholder ("(photo)", "(contact card)"), and nothing else in that row says a file exists or what it was called. A session asked to draft messages to two people whose contact cards sat in `attachments` read `messages`, saw one "(contact card)" and no names, and told the user the second contact never came through; both had, and the filenames ("<Name>.vcf") were the whole answer, one join away (it also split one person into two). Listing and opening are separate tools because they cost differently: `message_files` is a join and free; `file_read` parses bytes and may bill a vision call, so only the first is auto-approved. Cached text short enough is returned in the listing (a vCard is 150 bytes and is the answer). The unreadable-format message names what IS read, since a bare "nothing reads that format" sends a session off on `find /`. Stickers and `dyn.*` are dropped as the provider drops them (tapbacks and rich-link payloads aren't files anyone chose to send). `chat_guid` is included so a widget's own field pastes in verbatim. The carrier's own text ("here's the club president") says who the card is and lives on a different row than the file.
- **Habits/tasks/contacts**: SYSTEM_APPEND points the model at habits ("mood/workout journaling moved to habits"), so without a writer a spoken "I worked out today" has only the task board as its door. Habits take a name, not an id (`db.find_habit`), and deliberately no cadence/target/unit: nothing reads them (the widget's create form takes a name, every row daily/1/NULL), and a `weekly` habit would render a daily streak that misses six days in seven; a parameter the model can set and the card can't honour is worse than an absent one. A check-in day past the day-key is nearly always the 4am rollover (wall-clock date is one ahead of the card's day between midnight and 4am), not a future date, so it's refused with the day-key in the message (a check on that day counts in no streak). Archived habits keep their check-in history, so re-creating by name would strand it and restart the streak: resume instead. `set_task_status` carries the cascade rules for subtasks, so status goes through it. Task writes regenerate `tasks.md`, hence off the loop. Updating an already-done task leaves `done_at` alone so a note doesn't move the completion date. Debt `direction` carries the sign, so a negative amount inverts it (an `i_owe` of -40 subtracts from what the user is owed) and zero is the missing-amount failure. `_list_names`: a model told "add it to my packing list" can't see the chips, so a wrong guess is answered with the existing names. `_contact_failure`: contacts writes go through osascript, so the usual failure is Automation permission never granted to whichever binary launched uvicorn, which surfaces as an opaque osascript error. `_resync_identities`: `contact_identities` caches the AddressBook files, refreshed on each messages sync; refreshing here shows the new name without waiting (best-effort: the sync engine may not have caught up).
- **Courses/study**: office-hours amendments arrive by mail and announcement ("2pm this week, not 1pm"), and retyping is what leaves the card stale; the console could Edit the note, but the tools add the schema: a weekly `when` the card can place, a date that resolves, and the right person's block picked before overwrite. A missing parameter is deliberately not schema-required, so it returns prose the model can act on rather than a rejection it can't read, and blanking a syllabus fact stays something you must ask for. Study tools exist because tutoring is the heaviest console use and had no idea where in the term the material sat or what the user had got wrong (`app/study.py`).
- **`SESSION` ContextVar**: console.py sets it to the socket's SessionState before `client.connect()`; the SDK's reader task is created inside `connect()` and copies the context, so in-process handlers see the same object (and its `announced_id` once the CLI named the session). None outside the console.
- `_tab_model` checks open_tab's `model`/`effort` against the CLI's own list (`console.session_models`); empty means the profile's own. `provider` range vocabulary and source list are rendered into descriptions so the schema teaches what the CLI made the model discover with `--list`. Brain-fact/contact-search result is capped: a two-letter query matches hundreds of the 2000-odd cards and the model needs only the one it asked about plus enough to see it picked the wrong person.

### `app/console_work.py`: resuming interrupted work

A tab with a turn running, or background agents/shells going, has a row in `console_work` stamped with this process's `BOOT` id; the row goes when the work does. Quitting Kestrel (or the backend dying) leaves rows where they were, so on next launch a row from another boot is an interrupted tab and what it was doing. The page offers one Resume for all of them (and per tab), which sends each a "continue" naming what was cut off; nothing resumes without the click. Work after relaunch is recorded under the new boot, apart from the old row, which only Resume or Dismiss clears (unclaimed rows are dropped after `KEEP_DAYS`=7). Written from console_link on every turn start/end and every agents-strip change.
- **Delayed delete.** A row is deleted `DELETE_DELAY_S` (4s) after its work ends, not at once: quitting kills the CLI too, and on its way out the CLI marks its background tasks killed, which reads exactly like the work ending. Within the delay the server is gone and the timer with it, so the row survives to be offered back; a live server deletes it a moment later. With a caller's connection (tests) or no running loop it deletes at once.
- **Keyed by (tab, boot):** this launch's work never overwrites what an earlier one left. The CLI starts a turn of its own on resume (its "stopped" notifications), which used to erase the very record of what was cut off.
- `remember_session` writes `console_sessions` (tab/profile/project/label), kept after the work row goes: the usage page's only way to tell a study tab's tokens from a general one's, since both run in the vault. Bookkeeping, never raises.

(Dev tabs, PR watch addendum, `app/console_pr.py`)
- Motivation: client-site repos land through PRs someone else reviews, and their deploy checks fail without anyone looking; a tab that opened a PR used to finish and forget it. The console polls every two minutes while the tab is mounted; anything worth a turn (checks passing/failing, a review or comment, a merge conflict, merged/closed) comes back as a short note that wakes the tab like a child tab's handoff report. Merged or closed is `final`, and the console stops polling.
- Stateless on purpose so a backend restart (every merge on this repo) loses nothing, and a closed or settled tab simply stops asking. `gh` holds the login and infers the repo from the tree's remote; it runs in the tab's folder so `gh pr view` with no number finds the PR for the tree's branch.
- Check outcomes fold to pass/fail/pending; neutral and skipped count as passing (a site repo's `visual` job skipped on a non-Dependabot PR isn't a failure). A PR ref may be a number, `#12`, a github.com pull URL or a head branch, never with a leading `-` (gh would read a flag). Checks news = a settled verdict differing from the last settled one for this commit; a new push resets checks to pending, so a pass after a push is news even if the previous commit passed. `viewer()` is cached; the user's own reviews/comments don't wake the tab.

### `app/contacts.py`: creating and editing Contacts cards

- The AddressBook sqlite files are the sync engine's private store: `providers/messages.py` only reads them (`contact_name`, identity clusters); writing behind CardDAV's back clobbers records or wedges sync. Writes therefore take the long way: osascript -> Contacts.app -> CardDAV -> iCloud -> phone. Slower and permission-gated (Automation permission must be granted to whichever binary launched uvicorn; `available()` is False on a non-Mac, headless session, or without that grant), and the only route that ends anywhere but this machine.
- Shaped after `reminders.py` (arguments never interpolation, one `_run`, a permission probe) but imports nothing from it, so a contact write never depends on the alarm reconciler. `_TIMEOUT` 30s: Contacts is slow on first launch.
- Checked against the real dictionary (`/System/Applications/Contacts.app/Contents/Resources/Contacts.sdef`): (1) there is no `account` class: `make new person` lands in whichever account Contacts.app has as default and AppleScript can't name one (this machine has five sources, three CardDAV and two Exchange, so "did it reach the phone?" is a real question); `locate(contact_id)` answers after the fact by returning the AddressBook source directory UUID holding the card (None = no source admits to it, i.e. Contacts hasn't flushed yet). (2) `id` is read-only, `UUID:ABPerson`, byte-identical to `ZABCDRECORD.ZUNIQUEID`, so `locate()` is a plain lookup, not a name match. (3) `save` is required: without it the card lives only in Contacts.app memory and vanishes on quit; every mutating script ends with one.
- Labels: Contacts accepts bare words (mobile, work, ...) mapped onto internal `_$!<Mobile>!$_` constants; anything else becomes a custom label. `_pairs` accepts "555-1234" or ("work", "555-1234") interchangeably (CLI passes strings, callers with label data pass tuples).
- Script shape: items go in as (kind, label, value) triples; the `repeat while i < n` loop advances by exactly 3 so the last triple lands on the end, and Python always emits whole triples. The update script uses the same walk, offset by the id in item 1, and every op takes two slots even when it needs one, so a mis-sized op can't shift the rest (the failure being avoided: a phone number landing in a label field on a card that syncs to the phone).
- **Dedupe** reuses `normalize_handle` and `addressbook_records` so a card created here unifies with the CRM's existing people on the same rules; otherwise it appears as a second copy of someone texted for years. `matches_for(phones, emails, exclude_id)` is best-effort by inheritance (unreadable AddressBook yields nothing, so the write goes through rather than blocking); `exclude_id` drops the card being edited so its own numbers don't count as a duplicate and block re-labelling. `DuplicateContact` carries the matches so callers can show the collision. `ContactError` is surfaced to callers (no retry loop to swallow it, unlike alarm delivery).
- `find(query)` reads the AddressBook files, not Contacts.app (substring scan over ~2000 cards is a sqlite scan; an app launch + Automation prompt for a read the process can already do is waste). `_raw_identifiers(contact_id)` returns phone/email values **as stored** ("(202) 555-1111"), because AppleScript matches `every phone whose value is ...` on the literal string and can't normalize; `addressbook_records` yields only normalized identifiers on purpose (identity, not presentation).

### `app/courses.py`: office hours and dated exceptions

(Complements the Courses row in the Source-of-truth map.)
- No table, no migration: index.db stores frontmatter as JSON, so a course edited in Obsidian shows on the next reindex. The only computed thing is *when*: "is anyone in right now, and who is next".
- Rule vs exception: a weekly `when` cannot say "2pm this Tuesday only" (amend the rule: wrong next week; leave it: wrong tomorrow). A dated `except:` entry overrides the rule on one date with new hours, `cancelled: true`, or a room (`where`). Hours that move weekday are two exceptions (cancel the old date, hours on the new). An exception whose `when` doesn't parse keeps the rule's hours and only annotates them: say the odd thing rather than drop the person (same for an unparseable weekly `when` such as "TBA": it comes back with `day_i` None). An undated exception is dropped. An exception's `when` may omit the weekday.
- Week view shows the rule annotated with the next exception; today's rows show the hours actually held, plus `moved_from` (the usual weekday, overwritten in `day`; None, not False, when not moved). A cancellation dated today stays on today's board ("don't walk over" is the same decision as hours). A cancelled slot keeps the rule's start so it sorts; a dated exception on TBA hours has no start.
- Today is `db.day_key()`; "open now" compares against the real datetime of the slot (between midnight and the 4am rollover "today" is yesterday, and comparing 01:00 against a 09:30 slot would call yesterday's hours upcoming) and the wall clock (nobody holds hours at 3am).
- `_next_occurrence` walks dates (22 days, not 7): exceptions mean a cancelled week must be skipped and moved hours found on another day; a cancellation pushes the next occurrence past the 7-day mark and a shorter horizon would report no upcoming hours.
- Term filter applies only when that term has notes (mid-August the calendar says fall while the vault holds only spring). Seasons: Aug-Dec fall, Jan-May spring, Jun-Jul summer.
- Writers exist because amendments arrive by mail ("no hours Thursday, I'm away"); retyping into Obsidian leaves the card stale, and a stale office-hours card is worse than none. They edit the note and reindex (never a table); over a plain Edit they add the `when` grammar the card can place, date resolution, and who-matching before overwriting. `EDITABLE` is deliberately closed to the syllabus facts the card reads; a writer able to rewrite any key would eventually rewrite the schedule table or the user's notes.
- `known_codes` is handed to the office-hours mail extractor instead of asking it to produce codes: a model writes "CS 103" for "CSCI 103", pointing the proposal at a note `find_course` can't open; the real list turns that miss into an honest `relevant: false`.
- `_save` uses `sort_keys=False` (python-frontmatter alphabetizes by default, reordering a hand-written note) and rebuilds the index itself (the watcher would a moment later; this makes the card right before answering).
- Matching `who`: substring, or every word on file inside the given name ("Martin James Halvorsen" vs "Martin Halvorsen": a middle name made a move unapprovable, 409 every try). One person often holds several blocks, so `who` alone is not an address. `_one_on`: on a given date only one of a TA's Mon/Thu blocks meets, so the date settles it; falls through to `_one` when no slot is given and zero or several blocks fall on that weekday.
- `_amendable`: same person + same weekdays = same standing block (edit); different weekdays = another block. Matching on `who` alone once lost three of a TA's four blocks (each call amended the entry the previous call wrote, each said "updated"). An explicit `slot` is an address: missing/ambiguous is a question, never an append. An unparseable `when` has no weekdays to compare, so falls back to `who` alone (that's how a TBA gets replaced).
- `set_hours` reports added vs updated so a caller writing a schedule one call at a time can tell a third block from an overwrite; an unplaceable existing block (often a TBA placeholder) is not overwritten but the note says to remove it if it was a placeholder.
- `standing_block` serves the mail proposals: tell a post that only restates hours on file from news; approval keeps the copy so undo puts the replaced block back rather than deleting it.
- `units` is written as an int (numeric in every note that has it).

### `app/db.py`: tasks, habits, debts, lists helpers

Reasons behind the non-obvious helper behavior.

- **`day_instant(day, hhmm)`.** A day key runs 04:00-03:59, so 01:00 on key D
  is the calendar day after D. Resolving it as D 01:00 would put a task filed
  for "tonight at 1am" 23 hours in the past and break
  `day_key(day_instant(d, t)) == d`. The due sort order follows: per key,
  timed tasks in clock order, untimed last (due by end of day, not 00:00), and
  times before 04:00 sort after 23:00.
- **Due parsing.** `split_due` accepts "YYYY-MM-DD", "YYYY-MM-DDTHH:MM",
  "YYYY-MM-DD HH:MM" and a trailing ":SS" so one field carries an hour without
  every caller (console tools, API, skills) needing a second; anything else
  raises ValueError rather than filing an incomparable date. `_normalize_due`:
  a `due` naming no hour clears `due_time` (so a reschedule doesn't leave
  yesterday's 9am), but only when `due` is being set; an explicit `due_time`
  in the same call wins (a date+time picker sends both); `current_due` lets an
  hour-only PATCH pass.
- **Read-time fields.** `deferred` and `due_at` come from `day_key()` on the
  server because a browser clock disagrees with the 4am rollover for four
  hours a night; `tasks_today` returns its `today` so the client resolves
  quick-add times against it. A timed task due today past its hour is
  `overdue` (a 9am deadline used to sit below a 5pm one all afternoon);
  untimed waits for the key to roll. 'Available' is a read-time
  `defer <= day_key()` comparison (lazy over scheduled).
- **Subtasks.** One level by convention (`_check_parent` rejects
  self-parenting, a parent that has a parent, and reparenting a task that has
  children). `list_tasks` returns top-level only, since subtasks ride nested.
  A closed child drops out of an open parent's list (re-serving it made
  checking a subtask look like it did nothing); a closed parent keeps all.
  Closing a parent cascades to open subtasks, reopening reverses it (cascaded
  children carry the parent's exact `completed_at`); waiting/someday does not
  cascade (children stay hidden until the parent reopens).
- **Task write hooks.** Callbacks run after every committed task write from
  the writer's thread (REST route, console tool, or a provider sync opening a
  task) so `tasks.md` stays current without `db` importing `vault_views`;
  a raising hook is logged and skipped, since a stale derived view must never
  fail the write. `task_exists_for_source` matches any status so a done or
  dropped auto-task isn't recreated; only a new `source_ref` makes a new one.
- **Habits.** `_created_day` is the birth day_key, not the date of
  `created_at` (3am belongs to the previous key; comparing raw dates made a
  habit's first day look pre-birth, drawing a miss). `habit_history` returns
  `created_day` for the same reason. `find_habit` matches in Python because
  SQLite LOWER/NOCASE are ASCII-only (user-typed names like '$50 DoorDash'),
  and lives here so check-in and create agree on identity (else "exercise"
  beside "Exercise" splits a streak). `delete_habit` vs archive: archive is for
  a habit you stopped (history and streaks kept, reversible), delete for one
  that should never have existed (typo, duplicate); check-ins are deleted
  explicitly because `foreign_keys` isn't on for every connection and orphans
  reappear in `habit_history` as a nameless habit. `_streak` walks the
  `(habit_id, day)` index backwards from today or yesterday, stopping at the
  first gap. `checkin`/`auto_checkin`: see v29.
- **Small lists.** Debts, shopping, watchlist, beers share add / flip
  done-flag / drop; the flag column differs per table (settled / done /
  watched) and the helpers take the spec. `update_debt` exists so correcting
  or merging debts doesn't go through settling: `settled_at` reads as "they
  paid". `remind_debt` overwrites `reminded_at` (the nudge asks only "how long
  since the last ask"); `_debt_nudge` has three states, never | waiting |
  overdue, because "never asked" and "asked, no answer" are different actions
  (the card must not say "remind again" for someone never asked); only
  `owed_to_me` is nudged (the verb is "I asked them"); measured in day_key
  steps so "a week" is seven calendar days and a late-night reminder doesn't
  come due an evening early.
- **Lists.** `all_lists` is two queries, not one per list: the card draws
  every chip's count at once and the tables are tiny, so shipping everything
  beats a round trip per click. `find_list` is case-insensitive because the
  console has words, not ids. `set_project_folder` keeps an existing folder's
  spelling ("client projects" joins "Client Projects"). `add_list_item_note`
  appends a line so a completion note doesn't erase the original.
- **`like_contains`.** `%` and `_` are LIKE wildcards and callers interpolate
  human or model text ("Jean_Luc", "50%"), which would silently match wrong
  rows. `session()`/`borrow()` exist because a bare connect()/close() leaked
  the connection and its WAL read lock when the body raised.

### `app/expenses.py`: expenses claimed back

- Three steps, each a table (schema/why in db.py's migration). **Mark:** an expense is a claim, not a transaction; it comes off a bank charge (whole or partial) or is typed by hand (cash, Venmo), and snapshots what it was made from so the Plaid cache stays droppable. A pending charge can be claimed: Plaid gives it a new id on posting, and `follow_posted` (called inside finance's sync write transaction with Plaid's raw `added` rows) moves the claim. Unsent claim takes the posted day; whole-charge claim follows the new total, partial keeps its amount capped at the new total. A sent claim keeps amount and day (already in the spreadsheet); a different final amount is stored in `posted_amount` for the card to flag. If the posted id is already claimed it is skipped (UNIQUE would fail inside the sync's write, which must never be what fails it).
- **Send:** export writes waiting rows to .xlsx and stamps them with a batch; sent rows are frozen so they keep matching Dad's file. File is written first, stamping after: a failed write leaves nothing marked sent, a failed stamp deletes the file so the next export doesn't send the same rows twice under two names. The same export records every posted transaction as reviewed (the Money sheet's "reviewed through here" line); pending ones are skipped since they get a new id when posting. The by-category sheet uses SUMIF formulas, not typed numbers, so the sheets can't disagree after a row edit; a blank category criterion matches blank cells and quotes double up inside the formula string. `unsend` leaves the file in place (it may already be in an email) and is refused once paid.
- **Paid:** `_wire_for` suggests the deposit that looks like the batch's payment (on/after sent day, within $1 or 2% of the total, not taken by another batch; closest amount, then earliest day); only the user marks paid, because a coincidental $200 transfer closing a $200 batch on its own would be a silent wrong answer about money. The slack is tight enough that the weekly $40 allowance doesn't match small batches. Inflows exclude credit-card accounts (a card payment is an inflow on the card, never from Dad) and LOAN_PAYMENTS. Unpaid batches are matched oldest first so the longer-waiting batch gets a contested deposit. The card shows every unpaid batch plus the last 3 paid.
- `_pending_state`: `pending` while the charge waits to post, `vanished` once it is gone with no posted replacement (released hold, cancelled order); an empty fin_transactions means the cache was dropped, so nothing is called vanished.
- The backend writes the file into EXPENSES_DIR (~/Downloads) rather than the page downloading it: the desktop shell's webview has downloads switched off, so browser and app behave the same.
- `pretty_category` mirrors prettyCategory in MoneySheet.jsx so a claimed row is named as its charge reads on screen.

### `app/files.py`: clickable files in replies

- A reply says `~/Desktop/resume.pdf`, `/Users/.../shot.png`, a vault-relative `01-Projects/.../x.pdf` or a project-relative `out/SCHEDULE.md`. The frontend offers anything path-shaped; the backend decides what is real. Only a path resolving to an existing file under the home directory becomes a link. Routes: `POST /api/files/resolve {paths, bases}`, `GET /api/files/raw?path=` (bytes for the in-app viewer), `POST /api/files/open {path, reveal}`. LocalGuard (Host + Origin on writes) covers all three.
- Rules apply to the fully resolved path (`..` and symlinks judged by where they land): (1) under home or nothing: realpath(target) must sit inside realpath(~), so a project symlink to /etc is refused like /etc; (2) not the secrets: ~/.ssh, ~/.gnupg, keychains, cloud credentials and this app's data dir (app.db holds OAuth tokens) don't resolve at all; (3) never executed: `open` on an app, script or anything with the exec bit would run it, so those are revealed in Finder whatever the caller asked (EXECUTABLE extension set, plus the exec bit for extensionless scripts that open in Terminal); viewing a script's source is fine; (4) served as what we say: raw serves only the viewer's kinds, with a type picked from the extension (any text as text/plain so .html / .svg-as-text never render as a page), nosniff and a sandbox CSP on everything but PDF (the browser can't show it sandboxed).
- The client sends back what resolve returned but it is re-judged on every call, never trusted. Relative paths are tried against each base (the tab's worktree, its project) then the vault, first hit wins; each base is itself held to the home rule. Text over `TEXT_MAX` (2 MiB) opens with the default app instead of the viewer.

### `app/indexer.py`: derived vault index

- Markdown is the source of truth; index.db is a disposable cache rebuilt FROM the files and never written first, so terminal skills editing markdown directly stay consistent after a reindex. Rebuild is a full walk (fast enough for a personal vault).
- `resolve_link` applies the same rules as `vault_views.resolve_note` (exact vault path with/without .md, then a bare name unique across the vault; ambiguous is None) so the backlinks list and the click agree. Markdown links ending .md are real relative paths, so they are also tried against the linking note's folder; a wikilink never is (`[[README]]` from a folder holding one of three READMEs still names none). Wikilinks with a non-.md extension (embeds of images/PDFs) aren't note links. In a markdown table the alias pipe is escaped (`[[Note\|alias]]`) and the backslash is not part of the name.
- `session()` always closes: a rebuild walks the vault inside one connection, so a file that failed in a new way used to leave the handle and its write lock open until the traceback was collected, and the next widget read waited out busy_timeout.
- Checkbox tasks in notes are indexed only for a by-hand reconcile sweep (the tasks DB is truth); the generated tasks.md is skipped so we never re-ingest our own output.
- `_jsonable` recurses: the shallow version tested the whole value and fell back to str(), so one nested date flattened its container to a Python repr. Office hours were hit: `except: [{date: 2026-09-01}]` is a real date to YAML, the hours list reached the widget as a string whose entries are not dicts and were dropped; the course looked like one with no hours posted.
- Notes with unparseable frontmatter are invisible to the dashboard, so `rebuild()` names them (`skipped`) rather than quietly inflating the count.

### `app/keys.py`: kind-prefixed keys

- Every alarm and notification is named by a `kind:part:part` string, load-bearing twice: the prefix lets the alarm reconciler partition written rows by source, so a source that failed to read can only ever hold its own alarms (see alarms.py); and the join must be injective, because two different facts landing on one key means one silently inherits the other's delivery record (recorded as already sent, never fires). Its own module because notify.py needs the same rule for the same reason, and the reason is subtle enough that two copies would be one too many.
- `packages.order_key` is literally `merchant:order_id`, so the natural identity of a package can't be a key part unescaped. Escape rather than strip or replace: the ban on ':' is what makes the join injective (with colons replaced, ('a:b','c') and ('a','b:c') collide and two packages share a row). Percent-encoding keeps distinct ids distinct and readable in a log line. `kind_of` is total: an unparseable key returns a kind no source claims, so it is held rather than attributed to one. `allowed` is per-channel (alarm kinds differ from notification kinds); `label` only shapes the error message, which earns a parameter because these raise inside a source where the message is the whole diagnosis. A colon in a part would forge a kind boundary on read-back.

### `app/leave.py`: time-to-leave

Not a provider (no range/metric axis, nothing for the poller to cache): a live join of today's remaining located events and a traffic-aware route, computed on demand (precedent: `brief.py`). The `/api/leave` row in the Route map has the formula. Facts not there: geocode order is Google, then weather's Open-Meteo geocode, then one retry with `LEAVE_GEOCODE_CONTEXT` appended. The retry exists because half the room codes on a university feed arrive bare: "SOS B44, 3502 Trousdale Pkwy, Los Angeles, CA 90089" geocodes but plain "KDC 240" is nothing to Google, and those events got no `leave_by`; with the campus appended they resolve, sometimes to the building, sometimes only the campus centroid (close enough for a drive time, much closer than null). Every external call degrades to None; failures are cached as `()` so a flaky call doesn't hammer the API. A connection dropped mid-response surfaces as `http.client.RemoteDisconnected` (a `ConnectionResetError`), which urlopen doesn't wrap and which 500'd `/api/leave` twice, so `_http_json` catches `OSError` and `HTTPException` (covers `IncompleteRead`).

`is_virtual`: Google geocodes non-places confidently: a bare "TBD" comes back at (2.77, -77.67) in the Pacific off Colombia and a `usc.zoom.us` URL comes back as the USC campus centroid, so a video call produced a real drive time and an alarm to leave for it. A URL matches anywhere but the word markers (tbd, zoom, teams...) only as a prefix, so a real venue that merely contains one ("The Virtual Reality Lab") still geocodes.

`compute_leaves(day, strict)`: `day` anchors the calendar range (it was once assigned and dropped, so `brief.py`'s `compute_leaves(day)` returned today's leaves whatever day it was building); a past day now returns empty. `strict=True` re-raises a failed calendar read: a card that can't reach the calendar should render empty, but the alarm reconciler withdraws alarms on an empty list, so for it a calendar outage must stay distinguishable from a clear afternoon. `event_id` is carried because an alarm keyed on summary+start loses identity (withdraw and recreate on the phone) whenever the event is renamed or moved.

- `compute_leaves(day)` must honour its day argument (the range was once hardcoded to today, so brief.py's call answered about today). `strict=True` lets a calendar outage raise: the card wants `[]` but the alarm reconciler must not get one, since an empty list reads as "nothing to leave for" and withdraws live alarms. Virtual-location markers match as a prefix word, not a substring (a place starting "Na" is not "n/a"). A bare room code resolves only with the campus appended, and the campus is not appended to a string that already names it. Errors urlopen does not wrap in URLError are caught in `_http_json` (they once took /api/leave down).

### `app/lists.py`: debts, beers, lists, watchlist

- app.db is truth for debts, beers, user lists and the watchlist; nothing reads or writes the old vault markdown tables. Each fixed domain has the same four routes (read/add/flip done/delete) over shared db.list_rows / set_row_done / delete_row; only create is per-domain. The user-list section started as a single shopping list and is now `lists` + `list_items`, where the list is a row the user makes (shopping, packing for college, a party), so it carries a second layer of list-level routes.
- Beers ride on the debts card but are their own table: a beer has no dollar value, so it stays out of the owed-to-you total and out of the nudges. `GET /api/lists` returns every list with items nested because the card draws all the chips and switches without a second fetch.

### `app/localguard.py`: the browser guard

Binding 127.0.0.1 stops other machines, not a web page open in a tab on this one. `LocalGuard` is pure ASGI (BaseHTTPMiddleware doesn't cover WebSockets) and closes two holes on every HTTP request and WebSocket:
- **DNS rebinding.** A page on evil.example re-points its name at 127.0.0.1; its requests to evil.example:8788 are then same-origin to the browser and CORS lets it read every response. The Host header still says evil.example, so any Host that isn't a loopback name (`localhost`, `127.0.0.1`, any port, since the Vite proxy forwards its own; `testserver` for Starlette's TestClient) is refused with 403.
- **Simple requests.** A body-less or form/multipart POST needs no CORS preflight, so it runs (the page just can't read the answer). Browsers always send Origin on those, so POST/PUT/PATCH/DELETE (and every WebSocket handshake) from an Origin we didn't serve is refused. A missing Origin isn't a browser (curl, skills) and passes. Allowed origins: http://localhost|127.0.0.1 on :5173 (Vite) and the backend port (the .app loads the backend).
- **Dev-proxy exception.** Origin == `http://<Host>` passes when Host is itself loopback: a Vite dev server on any port (the verify rig's :5199) forwarding with its own Host. A browser only sends that Origin when the page posts back to the server that served it, and a request straight to the backend carries Host :8788, already allowed. So it only fires through a local proxy, which a local process could skip by sending no Origin: no page off this machine gains anything. `host_ok` is checked first.
- A WebSocket refusal closes with code 1008 before accept (the client sees a 403); HTTP refusals are 403 text/plain.

### `app/macos.py`: osascript and open wrappers

- Every caller used to carry its own copy of the subprocess call and the copies drifted: reminders.py let a TimeoutExpired escape as itself instead of its own error type, and the Obsidian hand-off ran `open` with no timeout at all. One wrapper each, so rules are written once: arguments never interpolation (osascript gets the script as `-e` and user-shaped values after `--`, read through `on run argv`; `open` gets the URL as an argv element; nothing a user typed is parsed as script or shell); always a timeout, and every failure (missing binary, timeout, non-zero exit; osascript's stderr is the message for the last) comes back as one exception type the caller chose. `open -g` keeps focus where it is. Probes answering "is this machine able to..." (notify._probe, wake._run) stay where they are: they swallow failure by design and share nothing but the call.

### `app/place_suggest.py`: name candidates for a cluster

Proposes, never decides: nothing here writes to the vault or changes a metric. Measured on seven weeks of fixes (93 places, 2 named) the guess is right ~a third of the time, wrong-but-quiet a third, confidently wrong the rest; a wrong suggestion costs a click, an auto-applied name would corrupt `time_at_home_min`. Accepting writes a `type: place` note; `location._group_stays` then claims every stay inside it.

Sources, all asked at once in a 4-thread pool (slowest source used to be summed with the rest):
- **Calendar recurrence** (`_calendar_candidates`): the located event recurring across a place's visits; strongest, and the only source that knows anything about the user (one lecture-hall cluster overlaps THH 102 on 5 of 7 event-overlapping visits, another SAL 101 on 6 of 8). Per-stay it is noise: only 11% of stays overlap a located event, one long SPARK! event matched five stays hundreds of metres apart, and the declared home dorm "recurs" with DMC 155 on 4 of 14 overlaps because you are home while a class you didn't attend is on the calendar. Concentration separates them: true answers took 71% and 75% of overlaps, every wrong one <= 29%. Gates: `CAL_MIN_SHARE` 0.5 and `CAL_MIN_OVERLAPS` 3 (the flat 17/17/17 three-way tie at the busiest hub clears neither, correctly: no single event names a hub), plus a distance gate `CAL_MAX_DIST_M` 200 since event locations geocode. The distance gate catches an event you didn't attend overlapping every visit to the place you stayed home in; share alone cannot see that. Geocoder is `leave.geocode` (cached, context-retrying), not a second one. A *campus-level* geocode ("SAL 101", "KAP 140" and "KAP 267" all resolve to the same campus centroid 510-570m from any real building, per `leave.geocode`'s documented failure for bare room codes) makes the distance gate abstain instead of vote (vetoing on it rejected both of this vault's strongest true answers, 75% and 60% share): no information is not evidence against; confidence x0.85 and the `why` says "position unverified". `_is_campus_centroid` detects it by geocoding `LEAVE_GEOCODE_CONTEXT` alone and comparing within 30m.
- **Google reverse geocoding** (`_google_candidates`): already enabled for leave, no new key. One good POI when the point is on one, otherwise a street address. Never ranked first (a guess about the world, not about you). `GOOGLE_SKIP_TYPES` removes things typed `establishment`/`point_of_interest` that are not places you stand in ("Los Angeles" locality, bus stops "Jefferson / Royal", "Vermont / 36th"). `GOOGLE_MAX_ADDRESSES` 2: reverse-geocoding a campus returns five interchangeable doors on one forecourt, and extras only push real candidates off the list. POI confidence 0.8, address 0.35.
- **Overpass** (`_overpass_candidates`): every named feature within the cluster's span; keyless; honest breadth (80m around one point holds Leavey, McCarthy Quad, Everybody's Kitchen, Birnkrant, SSCI), so alone it is a menu, never an answer. Nominatim reverse was tried first and is wrong: it returns the single nearest feature, an unnamed fountain. Radius = cluster span clamped to `OSM_MIN_RADIUS_M` 60 / `OSM_MAX_RADIUS_M` 150 (a tight cluster still catches its building; a sprawling one doesn't drag in the next block). Roads (`OSM_SKIP_KEYS`) and settlements (`OSM_SKIP_PLACES`; "Manhattan Beach" would come back on every query there; `place=square` stays) are never the answer. `OSM_RANK_KEYS` order is a prior only. Overpass `around` matches any part of a way, so a large polygon qualifies on an edge while its centre is far outside ("University Park Campus" came back 509m from a 60m ask); the centre is what is ranked, so it must be inside the radius too. `building=yes` is most of OSM's buildings, so the key is the kind; `_kind_text` maps a cached legacy `yes`. Mirrors (`OVERPASS_URLS`) are raced, not tried in turn, each `TIMEOUT_S` 10: tried serially at 25s each, a 504 plus a read-timeout made naming a place sit ~50s then show Google's addresses alone; a healthy answer takes 1-8s. The main instance is free, public and regularly saturated (it 504'd a verification run). All failing is a soft loss of that source.
- **Google Places nearby** (`_nearby_candidates`): same breadth as Overpass from a source that stays up, each place with a category ("Parking Lot", "Library"), which settles the parking-deck-vs-building question. At a Manhattan Beach cluster where reverse geocoding gave three street addresses it returned 20 places, including the two civic-centre parking lots, in 0.5s. Field mask is names/positions/types (Pro SKU). `NEARBY_MAX` 6: a strip of offices returns a lawyer, a jeweller and a skincare clinic per storefront, and the rest push Overpass and the calendar off the shortlist. Overpass stays for what Google doesn't list (named quads, OSM-only buildings) and as fallback when the API is off. Bus stops and `GOOGLE_SKIP_TYPES` are excluded.

**Caching.** `geo_lookup_cache` per (source, ~11m cell), forever: buildings don't move, so no TTL. A failed fetch (`None`) is never stored as an empty result, but is remembered in memory (`_failed_at`) for `FAIL_BACKOFF_S` 300 so reopening the panel doesn't wait on the same dead mirror twice; in-memory on purpose (a reload forgetting it costs one slow ask; a persisted failure would be the cached negative refused above). `unavailable` in the response says which sources are in backoff so a shortlist of bare addresses reads as "the map was down", not "nothing here".

**Ranking.** `score = bucket weight x candidate confidence`. The weighted unit is the *kind of evidence* (`_bucket`: `calendar`, `google_poi`, `google_address`, `google_nearby`, `overpass`, `typed`), not the API: Google's POIs and addresses are one endpoint and two qualities of claim, and lumping them averages a good signal with a bad one. `PRIOR_WEIGHT` = {calendar 1.0, google_poi 0.55, google_address 0.25, google_nearby 0.5, overpass 0.45, typed 0}; with no feedback the prior is the whole answer. One row per name (casefolded): the best-scoring source wins the row and the others are recorded in `also`; corroboration adds 0.05 per distinct extra source as a tie-break, not a multiplier (three sources naming one building is one building). Overpass/nearby confidence = `1 - min(dist, 150)/300`, Overpass further scaled by `1 - 0.04*rank`. `suggested_radius_m` (`_radius_for`): span x 1.25, min 60, max 250, rounded up to 10s; never below the GPS floor (p90 47m): a radius tighter than the error makes `at_home` flicker. The 250m default is sized for a home and swallows a neighbour at campus density. `weights` are exposed in the response because a weight learned from six clicks should be visible before the order it produced is trusted.

**Learning (`learned_weights`, `record_feedback`, `place_feedback` table).** It learns which *kind of evidence* the user accepts: one number per bucket, honest at a few dozen clicks. It does not learn names, cluster better or predict where you'll be; declaring a place already does the first two permanently. Estimator is a Laplace-smoothed accept rate `(hits + PRIOR_STRENGTH*prior) / (shown + PRIOR_STRENGTH)`: at zero feedback it is the prior, at ten observations most of the way to the observed rate, never divides by zero, never swings to 0/1 on one click. `PRIOR_STRENGTH` 8: a solo vault names a handful a month, so a prior dissolving after two clicks swings on noise and one needing a hundred never moves. It is a per-*candidate* rate, so a bucket offering eight guesses scores at most ~1/8 while the calendar (one candidate) can approach 1; observed after three shortlists Overpass 0/19 -> 0.13, Google POIs 1/3 -> 0.49. That is the honest reading (eight names are weaker evidence than one); treating breadth as confidence would rank a car park over a lecture hall the calendar places you in. Outcomes: `accepted`, `passed` (a name chosen, this one not), `dismissed` (panel closed); a typed name matching nothing is an accept in the `typed` bucket, which says the shortlist was no help. **No per-cluster "you rejected this here before" penalty, on purpose:** accept a name and the cluster is declared (never suggested again, so the negative can never be used); walk away and every candidate is rejected equally, a uniform penalty that re-ranks nothing. The honest home for a dismissal is the bucket weight.

- Stay times handed to the calendar-overlap comparison come from `location.parse_ts` and are tz-aware on both sides: the listener stores UTC with a `Z`, Google stores an offset, and a naive datetime breaks the comparison.
- Accepting a name writes the place note undated (no `from`): an undated place is true on every day, so it resolves fixes from before it was named; a `from` taken from the range being viewed would orphan its own history.
- Agreement bonus (0.05) must stay small enough that corroboration cannot overturn a real gap between buckets (a calendar hit at 0.5 still outranks it).
- The failure backoff `_failed_at` is module state; tests reset it.

### `app/places.py`: declared places

Mechanics and the dated-home rationale are in Source-of-truth map ("Named places, and home"). Extra facts: a place note is `type: place` frontmatter (`name`, `lat`, `lon`, `radius_m`, `kind`, `from`/`until`), read from index.db's frontmatter JSON with no table or migration. Why declared places exist instead of a tighter clustering constant: at campus density two genuinely different places (dorm and the gym at its foot) are closer than the GPS error on the fixes (p50 17m / p90 47m vs ~15m separation), so no radius separates them. Home is a place with `kind: home`; the old single config coordinate silently assumed you never move, and after the cross-country move every earlier day was scored against the new coordinate 3,561km away (flat 0.0 `time_at_home_min` for a month). With no `kind: home` note anywhere the module falls back to the config constants; once any exists the notes are the whole truth (a half-declared history is the bug being replaced). `home_on` returns None for an uncovered day, which callers must treat as "unknown" (metric omitted, not 0); `home_now` always answers (config fallback) because weather's and leave's origins need somewhere to start. Narrowest entry wins when several cover a day (dated beats open-ended, independent of note order). `Place.covers_day` bounds are inclusive, so a move is the old place's `until` and the new place's `from` on adjacent days. Frontmatter dates arrive as `date` objects (YAML) or strings; both are normal. A note missing lat/lon is skipped: the vault is hand-edited and a half-typed note should cost that place, not the card. `DEFAULT_RADIUS_M` = `HOME_RADIUS_M`, sized to hold one building plus the scatter of its own fixes. `_mark_ambiguous` flags overlapping radii (both in effect) rather than erroring or deduplicating: two places a short walk apart are real, and the honest answer is that fixes there can't be attributed; nearest centroid is a tie-break, not a claim.

**Writer (`create`).** Naming a place from the map writes the same note you would write in Obsidian into `02-Areas/places/`, so the vault stays the one source of truth (no app-created variant). It is not `06-Generated/`: that folder is for unvetted agent notes and its hook stamps provenance; a place note is the user's own assertion and clustering defers to it forever. It indexes immediately instead of waiting for the watcher so the map redraws with the name on the next request. An existing file raises `FileExistsError`: renaming or moving a place is a visible edit, not a silent overwrite from a map click. Frontmatter is written `sort_keys=False` so `type`/`name` come first like hand-written notes.

### `app/providers/api.py`: REST face

- `_serve` runs a hand-written read the way `get()` runs a registry one: inside `offline.collect()` so a cache fallback stamps `stale`/`offline` (best_effort with no collect around it has nowhere to write its note, which is how `/health/night` once served cache-only nights as if fresh), with `ValueError` as a 400. Pass a lambda when parsing an argument can itself raise. Envelope first, payload over it (the precedence `get()` uses).
- Routes are generated from the registry; registered names must not collide with hand-written routes in widgets.py/main.py (the health provider owns `/api/health`; the liveness probe is `/api/healthz`).

### `app/providers/athletics.py` and `coursework.py`: iCal feeds

- Athletics: USC's schedule from Sidearm's public iCal feed (`ATHLETICS_ICS_URL`). It exists because over two months the console spent ~15 web searches on kickoff times and once gave up on a water polo game the feed had all along; search results for a schedule are a week stale or a different season, and the athletics department's calendar is the source those pages copy. The feed is truth; `athletics_events` is derived, fully replaced on refetch (the feed is a rolling window, roughly a month back through the end of the academic year). A read refetches when the cache is older than the feed's X-PUBLISHED-TTL (2h), lazily, inside `offline.best_effort` so a dead network serves the last copy marked stale.
- Times: Sidearm writes DTSTART in UTC; a game shows on its Pacific date and clock (where the user watches). A game with a date but no time (football kickoffs are set ~12 days out) arrives as `DTSTART;VALUE=DATE` and is stored with `start` null: "TBA", never midnight.
- `parse_summary`: Sidearm writes "at" for away and "vs" for both home and neutral (the first live read had a cross-country meet in Riverside and a swim meet in Illinois as "home"), so "vs" is home only when the location contains Los Angeles, else neutral. A tournament ("Men's Golf  Vuori Invitational", a double space where vs/at goes) has neither.
- Coursework: every Brightspace due date from the D2L calendar feed (`BRIGHTSPACE_ICS_URL`, a token URL in .env). It exists because "how many WPs do I have left", "how many AWAs", "when is each essay due" had no source (syllabus text gives policy, not the dated list) and a calendar entry for an ENGR pitch sat a day early while Brightspace had the real deadline. The feed carries assignment due dates (" - Due"), content release dates (" - Available") and unsuffixed items. It does NOT carry submitted/graded state, so "how many have I missed" stays a question for the user (the console is told to say so). Same cache shape as athletics (`providers/ics.py`): derived `coursework_items`, replaced on refetch past TTL (1h), `details` cut to `DETAILS_CHARS`. The URL prefers the assignment's own page (`/dropbox/`, `/quizzing/`) over the calendar event view.

### `app/providers/http.py`: shared JSON request

- `json_request` is the one JSON-over-HTTP call (Calendar, Gmail, Health, Weather, Spotify, Last.fm each used to carry a copy). Each provider keeps a thin `_request`/`_api` wrapper because tests monkeypatch those names and they are where a provider's quirks sit; Last.fm (errors in a 200 body), Reddit (retry), Plaid (`PlaidError`) and News (redirect-following) stay in their own modules because they don't fit this shape. An HTTPError becomes `RuntimeError("{label} {code}: {body}")` (the body is where Google puts the reason), cut to `detail_limit`; network failures (URLError and friends) pass through untouched so `offline.py` can tell them from an answer.
- Retry: one retry (`RETRIES = 1`, `RETRY_BACKOFF` 0.5s) for transient failures on idempotent calls. Measured on the health poller (2026-10): ~8% of cycles (of ~11 calls) died on a single read timeout, connection reset or 503, each a blip the next attempt clears; one retry makes that a rare double blip, more only stretches a real outage (each attempt can sit out the full timeout). `RETRY_STATUS` = 500/502/503/504; 4xx including 429 is an answer about the request or quota and repeating it changes nothing. `_transient` covers a stalled/dropped socket (timeouts, reset, abort, broken pipe, RemoteDisconnected, IncompleteRead; a stalled TLS handshake arrives as TimeoutError) and excludes resolver/unreachable failures (offline: the next attempt meets the same dead wifi, and offline.py serves the cache) and certificate SSLErrors.
- `idempotent` defaults True for GET/HEAD only. A POST retries only when the caller says it is a read in POST's clothing (Google Health's rollUp), since re-sending a POST that landed would do it twice.
- `reauth` (with `token`) is called once on a 401 for a fresh token and the call re-sent, whatever the method (a 401 means it never landed; not counted as a retry; a second 401 is a real answer). Google sometimes rejects an access token its own expiry vouches for (24 single-cycle 401s across calendar, Gmail and Health in three weeks, each gone the next cycle); a forced refresh clears it.

### `app/providers/location.py`: stays, places, coverage

**Data.** `location_points` (raw fixes, from the relay) is the source of truth and never droppable, like Spotify's `plays`. `location_days` is the derived cache `_compute_days` rewrites per day. Day = `day_key` (4am rollover): a 1am fix belongs to yesterday. `location_sync(day, points)` marks a day as computed, because a day with no fixes writes no metric rows and would otherwise read as new on every call. A marked day is redone only when its fix count has moved (a late-flushed OwnTracks queue, or today accruing), so a read recomputes at the rate fixes arrive.

**Readers backfill their own window** (`_ensure_days`): `refresh()` only touches today, yesterday and the days the pulled backlog landed on, so a day the backend was down for stays absent until something asks for it. Safe here and nowhere else because computing a location day is local arithmetic over points already written; no card render can reach the network. Future days are skipped (no points, would recompute every call).

**Dwell** per fix = gap to the next fix, capped at `MAX_GAP_MIN` (20), so an offline stretch (dead zone, phone off) does not inflate whichever place the phone was last seen in. The last fix of a stay books the gap to the first fix of the next one, which keeps total booked time equal to total covered time (so stay-based `entropy` stays comparable to the grid-based one it replaced). `entropy` = Shannon bits over time-per-place, over *every* group unfiltered: it is already dwell-weighted, so transit barely moves it, and filtering first would make a quiet day look more varied. A routine day is low, a novel day high. `ENTROPY_BANDS` cuts at 1.5/3.0 (observed spread 0.97 for one errand to 4.49 for a travel day; a normal day lands near 2.5).

**Home metrics are absent, not zero, when the home is unknown** (see Source-of-truth map, named places): `at_home`, `time_at_home_min`, `first_leave`, `last_return` are scored against the home in effect that day; `at_home` is None (not False) for such a day. `first_leave`/`last_return` are minutes since 4am and absent when every fix was inside the home radius. A zero is indistinguishable from a real day out and reaches the correlation matrix looking like a measurement. The `leave_observed`/`return_observed` flags are `bool(at_home)` and only gate those two metrics, so None-as-False mislabels nothing.

**Places are stays, not grid cells.** The old grid (lat/lon rounded to 3dp, ~110m) filed one building as several places when a stationary hour sat near a cell boundary: September held 55 places, 12 inside the home radius, and the three largest non-home places sat 57m, 64m and 93m apart (one campus building, 677 min split three ways). A stay is a contiguous run of fixes that never leaves `STAY_RADIUS_M` (100) of the run's running centroid; stays group into places greedily from the longest within `PLACE_MERGE_M` (130), which keeps a campus from chaining into one blob as single-linkage would. Same month: 55 places -> 23, home 12 -> 5. Greedy grouping alone left two halves of one building 77m apart in separate groups (whether a stay merged depended on where the group centroid sat when it was processed, an ordering artifact), so `_converge` then merges the closest eligible pair until none qualify. Eligible = centroids within `PLACE_MERGE_M` and merged span within `PLACE_SPAN_MAX_M` (175; at USC scale that holds one building and its forecourt without reaching the next). Closest-pair-first so list order does not matter; a pair rejected for sprawl only disqualifies itself, because span is not monotone in centroid distance. Declared places never merge: a name asserts one place, two names assert two.

**Not fixed on purpose:** two places closer than the GPS error between them. The gym at the foot of the dorm is ~15m away and accuracy near home is p50 17m / p90 47m, so no radius separates them; declared places (`app/places.py`) are the answer, and a declaration overlapping another says so (`ambiguous_with`). `_group_stays` resolves declared places first and not merely as a label: naming is the only thing that holds one flat together when its own fixes scatter further than the distance to the next building. Two visits to one place are two `visits` and one place, which is what makes the map a map of a routine. Each group keeps its `stays` alongside `members` (lat/lon pairs `_span` measures): `place_suggest` needs the stays' times to ask which calendar event recurs across a place's visits.

**`_is_place`** (one predicate for the `places` metric and the map): dwell > `PLACE_MIN_DWELL_SEC` (180) and >= `PLACE_MIN_FIXES` (2). The dwell floor: on a ~100km driving day every ~110m cell the car passed booked ~1 min gap-to-next-fix as dwell, so transit read as dozens of places; the transit tail was <=~2 min/cell and genuine stops >=~4 min, so 3 min separates them (42 raw clusters -> 15). The two-fix floor: a lone fix followed by silence books up to 20 min on one observation, a sighting not a stay. The cross-country move day reported 43 places from 58 fixes; requiring two dropped that day to 7, the daily mean from 10.8 to 5.4, and the `places ~ coverage_min` correlation (a known bias) from +0.73 to +0.63. Three zeroes that travel day outright, discarding real stops.

**Coverage floor** (`COVERAGE_FLOOR_MIN` = 180, `is_thin`, `coverage_minutes`, `_covered_minutes`): see the `/api/location/summary` row for what it gates. The gate is on coverage minutes, not fix count: every metric is a whole-day quantity, so trust depends on how much of the day was watched. The two orderings disagree on real data: the day with the highest fix count (121) has 2.4h coverage (a dense burst on one drive) and a count gate passes it; a count gate refuses 4 fixes over 49 min and passes 7 fixes over 25 min. Coverage subsumes a count floor (clearing 180 min with 20-min-capped gaps takes >= 9 fixes). It is an absolute number, not a fraction of the day, because a stationary phone on wifi stops reporting and a fraction would refuse every honest day at home. 180 is where the 35 days of history separated: everything below was a burst inside one errand or a day still accruing. `_covered_minutes` is the single definition (card display and gate), so they cannot quote different coverage. `is_thin(None)` is True here, opposite of `screen.is_thin`, deliberately: this coverage derives from `location_points`, which is never dropped, so a day absent from the map has no fixes at all, whereas screen's absence means a day synced before its `coverage_min` existed. Callers go through `is_thin`, not a raw comparison.

`TREND_METRIC` = `km_traveled`, not `time_at_home_min`: time at home is the number the coverage gaps bias hardest (a stationary phone on wifi is exactly when OwnTracks goes quiet); distance only accrues from fixes that arrived. `SUMMARY_DAYS` = `DESK_BASELINE_DAYS`, wider than screen's 7 because only 8 of the last 14 days cleared the floor; a week at home is a week of holes.

**Changing `COVERAGE_FLOOR_MIN` needs `python -m app.providers.location --recompute-history`.** `features._stale_days` triggers on a source writing after a day's features were computed and a constant changing writes nothing, so days the new gate should drop would keep their features indefinitely. Re-stamping `location_days` makes them stale and the next matrix read rebuilds them. Safe any time: `_compute_days` rebuilds a day from scratch from the raw points. The `__main__` block is that repair entry, not the provider CLI (`python -m app.providers location`, which only reads).

**Health.** `health()`: the relay pull succeeding says the relay is fine, not that the phone is sending, so what matters is the age of the newest fix and *where* it was. OwnTracks reports movement, so a stay at home is silent and silence at home reads as "still here"; `HOME_QUIET_MAX_MIN` (7 days) is the at-home ceiling, because a source that cries broken every weekend at home teaches you to ignore the panel. Away, silence past `STALE_FIX_MIN` (6h) is a lost thread. The relay's `last_contact` (any request from the phone) proves the app is alive and overrides. `home_stretch()` exists because the first card version told a user who had been home 40h that their phone had stopped posting; the two are indistinguishable from the fix stream alone and where the last fix was breaks the tie. `HOME_STRETCH_LOOKBACK` (500 fixes) only has to outrun a day's driving. `refresh()` notifies once per silence (keyed by the newest fix id) because the dot alone let a four-day outage pass; a relay refusal (bad token, Worker gone) notifies once a day since it won't fix itself, while a network error is just the lid closed.

**Map (`trace`, `cluster_at`).** The map uses the same stay clustering and dwell floor as the `places` metric, not a second prettier one: a map that drew its own idea of where you stopped would disagree with the card (two answers to one question). The known bias travels with it (`places` correlates with coverage at +0.76), which is why a place carries `minutes` and `visits` and no claim of importance. `MAP_MAX_POINTS` (6000, ~4 months of ordinary fixes) thins the drawn path by keeping every nth fix (path keeps shape and ends); places are always computed over the full set, since dropping fixes drops dwell. Coordinates round to 5dp (~1m), cutting the payload by a third. `home` in the payload is the home in effect at the range end (a range straddling a move draws the address moved to); `named` carries every declared place either way. A place `id` is the name or the centroid string, never the clustering unit. `cluster_at` is shared by the name suggester and the per-visit labeller so both talk about the place the map drew; it raises `LookupError` (callers map to 404) and returns `span_m` because half the callers want it and recomputing from `members` is how the two drift.

`_load_points` is shared by `_compute_days` and `summary()` so "at home" cannot drift. Scoring an old day against today's address is what made a month of days read `time_at_home_min = 0` after the move.

### `app/providers/location_relay.py`: pull, ack, parse

The relay (`relay/`, a Cloudflare Worker) replaced the phone -> Tailscale Funnel -> Mac listener push chain, which needed the Mac awake, Tailscale connected, Funnel configured and the listener running all at once, and went silently dark for days when any one failed. Now the Mac being asleep only delays a pull and nothing on this machine is reachable from the internet. Order is pull -> insert -> ack: a lost ack re-delivers the same points and `INSERT OR IGNORE` on the `ts` primary key drops them, so at-least-once is enough and no watermark is kept. A crash between insert and ack re-pulls the same page next cycle for the same reason.

The relay stores OwnTracks messages verbatim, so `row()` (not the Worker) owns parsing and units: only `_type: location` is a fix (a `transition` has coordinates but describes a waypoint crossing and would double-count the fix that triggered it); `speed` m/s (OwnTracks sends km/h), `battery` 0..1 (sends %), matching what the Overland-era rows mean; `ts` has the Overland string shape (`%Y-%m-%dT%H:%M:%SZ`) so the PK keeps de-duplicating across the seam. `_scaled` maps absent and negative values to None: some builds send -1 for unknown, and -1 stored as m/s would read as backwards motion. `CONTACT_KEY` stores the relay's `last_contact` (epoch ms of the phone's newest request of any type) so `health()` can tell a phone that is alive but still from one that stopped talking. Cloudflare's edge 403s urllib's default `Python-urllib/3.x` agent (error 1010, banned-client signature) before the Worker runs, hence the custom User-Agent. `pull()` raises on a relay error so the poller heartbeat records it; a network failure passes through for `offline.py` to name. The `days` it returns are recomputed by the caller since a backlog flushed after a week offline touches more than today.

### `app/providers/offline.py`: serving the cache offline

- A widget read backfills never-fetched days before answering (health, weather, calendar, email, news). That is the one part of a read that fails for a reason the cache can outlive: lid opened with DNS not up, wifi mid-handover, a refused connection. Unguarded it raised through `providers.get()` into a bare 500 on every provider-backed widget at once, which reads on the desk exactly like the backend having died.
- The backfill runs inside `best_effort(what)`: a network-shaped failure (`NETWORK_ERRORS`: URLError, http.client.HTTPException, ConnectionError, TimeoutError, gaierror, SSLError) is noted and swallowed, the read answers from cache, and `providers.get()` stamps the envelope `stale: true` with the reason (`what` names the fetch, e.g. "health backfill", because it reaches a tooltip). `HTTPError` is not a network error: it means the source replied (401 dead token, 429 rate limit), and hiding that behind stale data is how a credential stays expired for a week. An expired token or a bug still raises.
- Nothing here writes `provider_health`: the poller meets the same dead network on its own cycle and records it there (what `/api/status` and the glance dot read); a read path that also wrote would count one outage twice, and again on every page load.
- `collect()` is thread-local (FastAPI runs sync endpoints in a worker pool, so a shared list would stamp one provider's outage onto another's envelope) and nests (an inner collect takes its own notes and the outer keeps what it had, so a provider reading through another can't swallow the caller's).

### `app/providers/poller.py`: the refresh loop

- One asyncio task in the FastAPI lifespan, no celery. Every `PROVIDER_REFRESH_SECONDS` each provider's `refresh()` runs in a worker thread, concurrently: cycle time is the slowest provider, not the sum, and one hung provider doesn't stall the rest. The health write happens inside the worker thread too (`_refresh_sync`), else the sqlite write would block the event loop up to busy_timeout while twelve providers land at once.
- `sync_all()` (whole cycle) is distinct from `refresh_all()` ("provider name -> info"; alarms are not a provider, no range/metric/source, and an extra key would make the map mean two things). `refresh_stream()` yields an event per provider so the UI shows a sync in progress; providers still run concurrently. Post-provider steps are their own SSE events (alarms are not a source, the UI's pending list comes from the `start` roster; an unrecognised event is ignored, so additive).
- `POST_STEPS` order, shared by `sync_all` and `refresh_stream`: alarms reconcile (after providers: alarms derive from the caches they write, diffing mid-cycle reads half-updated trips and churns a fire time that was never wrong), notify flush (drained after so a banner enqueued this cycle goes out this cycle; shells out to osascript, more ways to hang than fail), wake (reads the alarms table just written; running first would schedule wakes for the previous cycle's alarms; shells out to pmset, a refusal must not cost the cycle), backup (this month's app.db snapshot if not yet taken; the lazy stand-in for a never-installed nightly cron; last because it is the only step copying the whole database, ~100MB I/O, and would otherwise sit in front of a banner eleven times out of twelve; printed only on the cycle that takes one so "did it ever run" is answerable from the log), selftexts (after providers because it reads what messages just synced; last because it may spend a model call per burst and nothing should wait on that).
- Steps are lambdas, not the functions: the name is looked up at run time because sync-route tests stub these module attributes, and a tuple of originals would run the real ones against the real app.db.
- `_guarded`: a post step failure becomes `{"error": ...}` plus a `failures` row (`failures.record("poller", "step", ...)`), never a lost cycle; the steps used to only print, so an alarm reconcile failing every cycle for a week left no trace outside a /tmp log.
- `refresh_loop` is guarded as a whole, not only per provider: a failure outside `_refresh_sync`'s try (a thread pool that can't hand out a worker, an OOM in gather) would end the task silently, and a dead poller looks exactly like a quiet day (caches stop advancing, nothing on /api/status says why). `CancelledError` is re-raised (shutdown, not a fault).

### `app/providers/reddit.py`: Arctic Shift

- Reddit closed anonymous access. From this machine `www.reddit.com/*.json` and `api.reddit.com` answer 403 with any User-Agent; `old.reddit.com/*.json` 302s to a login; every `.rss` feed (search, subreddit, /new, thread) answers 404 with any User-Agent. Not a header problem. The `.rss` today-only fallback was removed rather than left failing quietly into every sync's error list.
- Arctic Shift (arctic-shift.photon-reddit.com, Pushshift's successor) is the sync engine: no key, full post objects, filterable by subreddit/query/author and an epoch window. Weakness: fresh scores (recorded at creation, revisited later). Anything younger than `REDDIT_SCORE_MATURE_DAYS` is re-synced on later refreshes and reported `provisional`; `provisional` also covers NULL scores (removed posts).
- `reddit_posts` + `reddit_sync` are a derived cache; drop both together (v40 migration). Counts are overwritten on every pull, which is how a provisional score matures; a missing count stays NULL (the archive omits them on removed posts), never 0.
- Days are day_keys (4am rollover): a day's window is [04:00, 04:00) local, converted to epoch seconds.
- `refresh()` keeps the maturation window warm, at most once per `REDDIT_REFRESH_MIN`: the poller runs every five minutes and a free third-party archive should not get three days of history re-requested that often for numbers that move hourly. Age is measured from the *stalest* day in the window, so a never-synced day pulls immediately. `get()` backfills only never-synced days; maturing days belong to `refresh()` (a read must not re-fetch three days because a widget opened).
- Sync marks every day in the span synced, empty ones included (a quiet Tuesday is an answer; without the marker every read refetches it), but only when no subreddit errored: a marked day is never revisited, so a 429 marked synced would cache "no posts" forever. Future days are never marked (a this-week read would mark the rest of the week empty).
- A total failure (nothing got through) raises, which is what puts reddit on `/api/status`: the poller records whatever `refresh()` raises, and a provider that swallows its outage looks healthy. A partial failure stays soft (the subs that answered are real data).
- Retry: the busy answer is a 422 "Timeout. Maybe slow down a bit", load not the query (`cover` in r/onebag failed twice seconds apart and worked a minute later). One retry, two attempts not five: a sync walks up to `REDDIT_MAX_PAGES` per subreddit and a long backoff inside the page loop stalls the poller. `ArchiveBusy` (separate from RuntimeError) lets a caller tell "come back in a minute" from a 400 (query wrong, never succeeds); the console tool turns it into advice. HTTPError (429/5xx) is deliberately not "network down" for `offline.best_effort`: "slow down" is a real answer and swallowing it leaves the cache quietly empty.
- Pagination is on `created_utc` (no cursor) and stops on a page adding no unseen id: a page of identical timestamps would otherwise re-request itself forever since `after` cannot advance without skipping them.
- `search()` is a live, uncached, never-written query into one subreddit (research: "what has r/onebag said about rain covers"); CLI `--search/--sub`, console `reddit_search`. The subreddit is required by the API (`posts/search` with a bare `query` 400s: "requires one of: author, subreddit"); it shipped optional and every call omitting it failed. Scores are as the archive has them, so recent hits read low. `_scan` reads 300 newest posts (three requests, ~2s each) when the archive's search refuses.

### `app/providers/venues.py`: live Places lookup

Not `app/places.py` (the user's own dwelt places): this is the outside world (restaurants, cafes, parks, an event's venue). It exists because an audit of console sessions (2026-08..10) found web search failing at exactly what a map knows: addresses never resolved; distance ("closest KBBQ to Maple Hall" was the top search hit and every "~12 min Uber" in a comparison table was invented); open/closed (three of a dinner shortlist were closed, found only on a re-check; at 9pm a study-spot list offered two libraries that shut at 8). A search returns per place the address, `businessStatus`, open-now and when that changes, rating/count, price, reservable/good-for-groups, then one Routes matrix call per mode adds real walk and drive minutes. What it cannot know (an AYCE tier's price, a campground's alcohol rule) needs the venue's own page. Live, uncached, no range axis, so not in REGISTRY. CLI: `python -m app.providers.venues "ayce kbbq" --near "Maple Hall, USC"` (also `--open-now`, `--details <place_id>` for reviews and hours).

Billing: asking for `reservable`/`goodForGroups`/reviews puts a request in Google's Enterprise + Atmosphere SKU, which has the smallest free monthly cap; that is the point of the call so they are asked for, but reviews are left to `details()` so a ten-result search doesn't pay for fifty. `DEFAULT_RADIUS_M` 5000 is a bias, not a fence (farther hits still come back with their real distance); `MAX_LIMIT` 20 and `MAX_RADIUS_M` 50000 are the API's ceilings. `FIX_MAX_MIN` 60 (looser than leave's `LEAVE_FIX_MAX_MIN` 30): a leave-by alarm acts on the number, a "near me" list only ranks by it. `WALK_MAX_MIN` 90: past it a walk time is noise next to the drive and is left out of the brief line. `api_request` is the name tests monkeypatch; a Google 403 for an API not switched on is a wall of JSON ~600 chars in (read whole, trimmed to 400) and the one useful line is which API to enable. `resolve` uses Places itself (it knows "Maple Hall" the way Geocoding knows street numbers), biased toward the user's origin so a common name resolves locally, falls back to `leave.geocode`, and returns the resolved address as the label so a wrong resolution is visible rather than silently skewing every distance. `_local_hm` renders hours in the *place's* zone via its own UTC offset (a search from NC about LA reads LA hours); 00:00 tomorrow prints "midnight" because "12am tomorrow" reads as a day off. `open_now` None means Google has no hours, not closed. `_add_travel` returns an error string instead of raising: a list without travel times beats no list. Brief line uses "min", not "m" ("walk 892m" read as metres).

### `app/sessions.py`: session list and transcript replay

- Sessions are the JSONL under `~/.claude/projects/<munged-cwd>/` (separators and dots become `-`). The console resumes via the SDK's `resume` against the same store, so terminal and browser sessions are one list. A dev tab's cwd is its repo/worktree, so its sessions are the repo's (the same list `claude --resume` shows there); `_session_dirs` adds one dir per dev-tab worktree because Claude Code files a session under its cwd. `limit=None` lists every session: the vault accumulates a few dozen a month and previews are cheap (each stops at the first user message).
- One tool-call formatter (`tool_detail`) serves both the live websocket stream and the replay; before, the live path dumped raw JSON input so a session looked nothing like itself once resumed. `_DETAIL_KEYS` is ordered by how much the argument says; the tail holds the vault MCP tools' vocabulary (a task has `title`, an event `summary`, a draft `subject`), otherwise those calls read as `title=pay tuition` from the fallback branch in the tool log and change summary. `tool_desc` (Bash's `description`) leads in the console: "List the vault's generated notes" reads faster than the find pipeline.
- `rewind_point`: `--resume-session-at X` keeps everything up to and including X (checked: resumed at a prompt's own uuid, the model still knew what it said), so the cut is the message's parent. A message with no parent was first: resume as a new session (`at` None).
- Replay: the SDK's `resume` restores model context but not the visible log, so the transcript is replayed. `until` is a byte offset (a file size), cutting what's written before the turn a reloaded page is about to be replayed (console_link). Only conversation records count: an `api-request-blob` carries a `message` with role "user", and its "Tool loaded." once landed in the log as something the user said. A background task's notification is the CLI talking, so it's a compact row (task_notes) not a bubble of XML. The model is recorded per reply (the log marks changes, the retry menu says what answered); the CLI's own error turns say "<synthetic>", which is no model. `raw` carries an Agent call's input (card), scripts (console_code) and delegate rows (who, the brief, what came back) whole, live and replay alike.
- `subagent_run`: the Agent tool runs subagents in the background and their calls never reach the parent's stream; the CLI writes `<session>/subagents/agent-<id>.jsonl` with a `.meta.json` naming the Agent call (`toolUseId`). It reads the file rather than waiting for messages that don't come (#77). None when not on disk yet: the meta lands a moment after the call, so the page asks again. Time since last write is returned because a subagent that died with its session is never marked finished, and that is how the page stops polling.
- Preview text (`_first_user_text`) goes through `text_blocks`/`strip_context`: a console message has an `<attached_context>` widget dump in front, so reading the raw first block previewed every pinned widget's JSON; a message that is only attached context is skipped for the next. Search hits are listing rows plus matching snippets, so the browser resumes straight from a result; tool calls are searched by name and detail (finds the session where a file was touched).

### `app/study.py`: syllabus context and the review log

- Why it exists: tutoring (proofs, C++, exam review) had two blind spots. The console didn't know where in the term material sat ("is this on the test?", "did I miss a lecture?" were answered by grepping or guessing), and nothing outlived a session of what the user got wrong, so "review everything I've missed" had nothing to review.
- **Context** is read off the extracted syllabus text under `syllabi/text/`, never written anywhere: its dated rows ("10/1, 10/5 Recursion"), week blocks ("Week 7 / 10/05-09 / ..."), and any line naming an exam beside a date. Layout is lost in extraction, so this is a *reader*, not a parser of record: every row is the syllabus's own words with a resolved date beside it. A syllabus it can't date (CSCI 103's table comes out as two unaligned columns) falls back to the course note's curated tables (curated from the same PDF, next-best reading), then to the verbatim schedule excerpt. Nothing is invented: no rows is reported as no rows. Three rows is the bar for "a schedule" (one stray "Week 3" isn't).
- Dates: a term filter on month range (fall Aug-Dec, spring Jan-May, summer May-Aug) is what lets the patterns stay loose: "24/7", fractions and extensions fall outside it. A slash range ("9/28-10/02", "week 6 (9/28-10/2)") is one token, otherwise a midterm would land on both ends. Month words are checked against `_MONTH_WORDS` ("Marx 2" is not March).
- Week blocks trust only the start date (ENGR 102's end date splits across lines), so a block is 7 days. The last block has no next header and ran into the grading policy (MATH 229's "quizzes" became an exam), so it stops at the median block length. Column-wrapped extraction ("Artificial In- telligence", "8-24 thru 8-" / "28") is repaired.
- Exams: "Final Presentations / Project Reports / exam review", "Exam Information" contain the word but aren't exams. They are struck out (`_NOT_EXAM`) before searching; a lookahead would backtrack "Final exam review" into a match on "Final". Week blocks use `_BLOCK_EXAM` with no bare "final" (final presentations/projects, "FINAL- Team Report" in ENGR 102). Paragraph-per-page extractions (CSCI 103's whole page is one line) are split into sentences, only before a capital ("Sept. 24th, Oct. 16th" is one sentence); a sentence wrapped to the next line borrows the previous line's exam keyword, but a schedule row never does (else the lecture after "10/6 7pm Midterm 1" becomes an exam). A week-range mention is only approximate and folds into an exact date inside it. Quotes: syllabus first, at most two (evidence, not transcript). `term_start` spans every course note so one undatable syllabus still gets a week. `_excerpt` stops at a page footer or a >300-char line (policy text after the table).
- `course_context` states the schedule between exams as schedule, not coverage: a final is often cumulative and CSCI 103's exams follow projects; what an exam covers is the syllabus's to say. An undatable syllabus may still list topics week by week; it is shown as is beside what the note could date (usually labs, not lectures).
- **Review log** is vault markdown (`type: course-review`, one per course, found through index.db frontmatter like course notes), so the user can read/annotate it in Obsidian and move it out of `06-Generated/` without the console losing it. A mistake is study material, not a fact about their life (that's the Brain) and not a row any card joins against, so no table and no second copy. Each entry: a `###` heading plus `- **Key:** value` bullets; anything else the user writes inside an entry is kept and read back as notes. The note may be not yet indexed (watcher debounce) but on disk: `_default_path` is the fallback. It's written from the backend, so the PostToolUse provenance hook never stamps it: `origin: generated` etc. are set by hand. The same idea missed again is counted on the first entry (`Again:`), the strongest signal the log carries, not filed as a second entry that would read as unrelated. `since` counts an entry from its latest miss. List order: most-missed first, then course order.
- **Spaced review**: schedule derived from the entry's own history (heading and `Again:` miss dates, `Reviewed:` lines the console appends for right answers); a right answer moves one step along `INTERVALS = (1, 3, 7, 14)` days, a miss anywhere restarts, past the last step it is learned. A second right on the same day isn't a second interval survived. An exam within `CRAM_DAYS = 2` makes every unlearned item due (the night before isn't the time to wait for day 7).
- **Coverage**: `StudyBlock.jsx` posts every answered quiz question/flashcard with its source page, so "have I gone through Unit 2b?" and "where do my misses sit?" come from attempts, not memory. Confidence order `dont_know < guess < unsure < sure`, normally given before answering so it can't be fitted to the verdict; `rated_after` marks ratings given after, counted separately. Sure-and-wrong is a misconception to undo; right-but-guessed is a pass that won't hold. "Idle docs" (PDFs nothing has been asked from) aren't filtered by course, since a doc has no course until a quiz is tagged with one.

### `app/task_notes.py`: task-notification rows

- When a background agent or shell finishes, fails, is killed at its time limit, or turns out to have been cut off when the last session ended, the CLI injects a user message of raw XML: `<task-notification>` with `<task-id>` (repeatable), `<tool-use-id>`, `<output-file>`, `<status>` (e.g. stopped), `<summary>` (e.g. "2 background agents didn't finish before the previous session ended: "name" (id)..."), `<note>`, `<result>` (the agent's report). Replayed as a user message it was a bubble of XML in the user's own voice, so `parse` splits those into notes for a compact row; whatever else the message said stays text. `from_system` builds the same row from the live stream's `task_notification` system message. A cut-off run's title names what was cut off; others keep the CLI's own summary (already reads well, e.g. `Background command "npm run build" failed with exit code 1`).

### `app/tiles.py`: basemap tiles

- The map sheet needs something under the dots (a route across campus reads as a route only if the streets are there). The frontend can't fetch tiles itself (image hosts outside the CDN allowlist are blocked; localhost app anyway), so the backend fetches and serves them from our origin.
- Manners, because we are a client of somebody else's tile server: cached forever by (z, x, y) (street geometry doesn't move; the cache is derived/droppable, costing one re-fetch per tile looked at again); a real User-Agent naming the app, as the OSM tile usage policy requires; zoom clamped to MIN_Z..MAX_Z (3..19) and each request is one tile from one viewport (no prefetching or bounding-box walking); an unreachable server returns 204 so the map draws vector-only (degraded, not broken). 204 rather than 404 because the map legitimately asks for tiles it may not get (world edges, dead network) and red 404s would make a working map look broken.
- Source is openstreetmap.org's own tiles, the one basemap still serving anonymous requests. Carto's light_all was first choice (grey cartography meant to sit under data) but now returns tiles stamped "API KEY REQUIRED" with a 200, so the map rendered with a watermark instead of failing loudly; found by looking at the pixels. The standard style is more colourful than the paper palette wants, so MapSheet desaturates it at draw time (ctx.filter) rather than using a styled basemap that needs an account.
- Cache writes need `with con:` as well as the session: session() closes but commits nothing, so without it every tile was re-fetched forever and the cache stayed empty. A failed cache write is logged, not a failed map.

### `app/trends.py`: energy score and habit series

- Internal series for the trends widget: a derived daily energy score and per-habit checkins (app.db). Health series stay on /api/health (the health provider owns them); this serves only what no provider covers. Not a provider (nothing external, nothing to refresh); range tokens parse in providers/ranges.py.
- energy = 0-100 readiness blend: sleep (weight 0.5, vs a fixed 8h target), resting HR (0.25, lower than baseline is better) and HRV (0.25), each scored 0..1; resting HR and HRV are scored against the median of the trailing `BASELINE_DAYS` (30), which is synced along with the range so the baseline isn't just the displayed days scoring themselves. Missing signals drop out and weights renormalize; a day with no signals is a null gap, not 0 (unlike habits: no checkin row means genuinely not done = 0).
- One provider read over range + baseline window syncs every health metric for its cold days (get() backfills whole days, not single metrics; health_sync markers make it a one-time pull per day). The sync only tops up cold days and the score is computed from health_days either way; a lapsed Google login (a Testing-mode refresh token lasts ~7 days) is no reason to withhold days already cached (this used to 502 for as long as the account was disconnected; the status panel already says so with a Connect button). Energy reads the health provider directly rather than through providers.get(), so the envelope stamping (degraded notes) lives in the route too, otherwise a backfill that couldn't reach Fitbit would degrade silently here while /api/health said so.

### `app/tz.py`: which timezone the user is in

Every timestamp is unambiguous as an instant (Google stores each event with its own UTC offset) but that is not the same as readable: a calendar slice mixes offsets (a club call at -04:00, a USC block at -07:00), and a console asked to lay it out produced a table where 9:00 AM and 8:30 PM were three hours apart in different zones with nothing marking the switch. So rendering picks one zone per event *date* and says which. The zone is where the user *will be*, not where the event was filed, since that is the frame a person plans in. The machine clock (macOS follows you) is right only for today and wrong for every other day, most of what a calendar shows (in NC in August, move-in day renders Eastern for an event that will happen in Pacific).

`TIMELINE` is explicit, not derived from flights: the RDU->LAX flight that starts it is in the calendar but not in the `trips` table, so a flight-driven rule would miss the transition that matters most. Add a line when a move is booked and name the flight in the comment so a wrong entry is traceable. `AIRPORT_ZONES` is a different axis: what frame an airline's clock is printed in. `trips.dep_local`/`arr_local` are each local to their own airport (checkable: AA 2299 leaves RDU 18:00, reaches LAX 20:19, a 5h19m block; AA 2308 confirms eastbound), so on the moving day `zone_on()` says Los_Angeles while `dep_local`'s 18:00 is Eastern; reading a departure through TIMELINE puts it three hours out. Explicit because derivation needs an airport database and a wrong guess is silent; unknown codes fall back to the user's zone that day (right more often than UTC, wrong exactly where worth checking) and `airport_zone_known()` lets a caller abstain. `label_for_zone` is split from `label` for a zone that is not the user's that day (a departure airport's), where going back through `zone_on` would undo the point. `_SHORT` prints coast (ET/PT), not %Z, since DST state is noise.

### `app/vault_views.py`: vault views, note open and search

- `tasks.md` is a generated, read-only render of the tasks DB, kept in the vault so Obsidian search/wikilinks still see tasks; regenerated after every task mutation. Hand edits are overwritten.
- Count and render happen in one traversal so the header total matches the emitted task lines (open subtasks included).
- Writes arrive from any thread (a provider sync opening a task races a UI click): they are serialized and the file is swapped in whole (temp file + replace), so Obsidian and the indexer never see half a view. The temp file is dot-named to stay out of Obsidian and the watcher.
- The db task-write hook refuses an app.db that isn't the configured one (a test that redirected `db.APP_DB`) paired with the configured vault: it would overwrite the real tasks.md with fixture tasks. A test wanting regeneration points VAULT_PATH at something disposable too.
- Open-note resolution: the console writes a path or wikilink, not a URL, so the dashboard resolves that text to a file before handing it to Obsidian. Generous about the form, strict about the result: anything not landing on a real file inside the vault is a miss (a repo `README.md` mentioned in talk must never open an unrelated note).
- A bare `[[name]]` follows Obsidian's rule (match on file name anywhere in the vault, via the index) but only when unique: the vault holds several `README.md`, and picking the first-listed would open the wrong note and look like a wrong link rather than an ambiguous one.
- The anchor (`#heading` / `^block`) is read off the link, not the resolved path; the `|alias` half is display text. It is kept because a `#Costs` link that lands at the top lied about where it goes.
- URI is `obsidian://open` addressed by vault name (folder basename, Obsidian's default name) plus relative file, not absolute path: survives a symlinked vault and is the documented form. Opened with `open -g` so the click doesn't steal focus from the dashboard when Obsidian already shows the note on another space; Obsidian still raises itself when it must launch. macOS-only.
- Search reads index.db only (derived): Obsidian edits appear after the watcher's next rebuild; nothing here writes. Match score by field (title, then path, then text), summed over query terms, so a note with both words in the title outranks body matches; ties by most recent edit. Under two characters the answer is [] (every note holds "a").
- `generated_notes(folder)`: the Vault notes view's "Written by the console" list is `06-Generated/`.

### `app/visits.py`: which place, on which visit

At campus density a cluster is not a place (see the v53 note in the schema history for the three failed automatic routes and their numbers). Facts not stated there: the busiest unnamed cluster spans 168m (Leavey Library, McCarthy Quad, Everybody's Kitchen, Birnkrant, SSCI). The calendar as a *position* claim is rejected (KAP/DMC/ACC labels against mid-campus visits are events happening elsewhere), but it returns for *identity* only: two visits coinciding with the same recurring event are probably the same place, whichever place that is. That is safe where position was not: at the home dorm 4 visits overlap CSCI 103 because you were home during a lecture, and "these four are alike" is harmless when all four are labelled home. Measured over 261 visits: 28% coincide with any timed event (the ceiling for offering one as a one-click chip), 9% with one recurring >= 3 times at the same cluster (the ceiling as a learning signal), and it is strongest where least needed (6 of 8 visits to the SAL cluster, 5 of 13 to THH, already single buildings); the 168m hub has 40 real visits, 9 with any event, nothing recurring but a one-off "Bid Day".

So the label comes from the user; what is learned is the *shape* of visits (evening-and-long vs midday-and-short). `predict` is a similarity-weighted vote over the user's own labelled visits, not a model: with a few dozen labels per cluster a model would hide how little evidence there is. Every prediction carries the count behind it (`from_labels`, `total_labels`, `runner_up`) and `share` (vote share, not a probability, and named so nobody reads it as confidence) paired with `support` (resemblance to the closest labelled visit, which share cannot say).

Constants: `EVIDENCE_MIN_FIXES` = 2: 25 of the hub's 60 "visits" are a single fix followed by `MAX_GAP_MIN` of booked silence, a sighting. They are listed and marked (hiding them would be its own lie) but never used as evidence or predicted for. This is deliberately NOT applied to dwell totals on the card: at home the phone legitimately stops reporting, and 1111 of the home dorm's 2636 minutes are on single-fix stays, so dropping them would gut `time_at_home_min`. `HOUR_SIGMA` 2.5 h, circular (23:00 and 01:00 are 2 h apart). `DURATION_SIGMA` 0.7 in natural-log units (~2x ratio; 20m vs 40m equals 40m vs 80m). `WEEKEND_MISMATCH` 0.5. `EVENT_AGREEMENT` 0.5: agreeing recurring series pulls similarity toward 1 (`base + (1-base)*0.5`, so it stays in [0,1] and can only raise it); disagreement changes nothing since one building holds many lectures. `EVENT_MIN_RECURRENCE` 3: a series must recur at the cluster at least this often, which excludes the one-off "Bid Day" that spanned three hub visits and would have glued unrelated visits. `MIN_LABELS_TO_PREDICT` 3: two labels can only say "one of the two things you named". `MIN_SUPPORT` 0.05: vote share is relative and would hand a 03:00 four-minute stay to "Everybody's Kitchen" at 90% because lunch is also short; a genuine match (20:00/100m vs 19:00/90m) scores 0.91 and that stay scores 0.0001. `_series` strips "(Lecture)/(Discussion)/..." so one course is one series. `_event_for` takes the shortest overlapping event (an all-afternoon block says less than the matching 50 minutes) and skips virtual locations (a Zoom URL geocodes confidently and wrongly; here position is never read, but "standup" is not a place to offer as a name). An event is offered as a picker candidate, never an answer: it says what you were doing, only sometimes where.

**Label matching** (`_match_labels`): stay boundaries are recomputed from whatever fixes the asked-for range holds, so a range starting mid-visit yields a stay beginning after the instant the label was written against. A containment test is wrong in exactly that direction, so: distance from the label to the stay's span, nearest wins, one label never labels two visits, tolerance `LABEL_MATCH_TOLERANCE_MIN` = `MAX_GAP_MIN`. `labels_at` is not range-limited: last month's labels inform this week's visits. Labels key on `_key` (4dp, ~11m) lat/lon of the cluster.

- Event identity pulls similarity halfway to certainty (never past 1.0; identical visits stay 1.0); a different event leaves similarity unchanged because one building holds many lectures. A multi-day one-off event ("Bid Day" spanned three hub visits) is not identity: it must recur `EVENT_MIN_RECURRENCE` times at the cluster. Virtual events ("standup") are never offered as places. `support` is absolute (0.0001 for an unlike visit vs 0.91 for a genuine match) where vote share is relative and would hand a 03:00 4-minute visit to the lunch place at ~90%. Hour distance is circular; durations compare as ratios.

### `app/watcher.py`: auto-reindex

- index.db is a disposable cache over the vault (indexer.py), so no incremental updates: watchfiles debounces a burst of filesystem events into one batch and the answer is the same full rebuild() that startup and POST /api/reindex use. watchfiles ships with uvicorn[standard]; the awatch loop runs as a lifespan task beside the provider poller. Ignored: non-.md files, dot-directories (.obsidian, .claude, .trash; same rule as rebuild()) and the generated tasks.md view (never re-ingest our own output).

### `app/webpush.py`: Web Push sender

- Written against `cryptography` rather than taking pywebpush: it is four primitives (ECDH, HKDF, AES-GCM, ES256), the RFC publishes a worked example to hold them to (test_webpush.py; salt and sender key are pinnable for that), and it keeps the list of things that talk to the network short. What leaves is one outbound HTTPS POST to the push service the phone's browser registered with; the body is encrypted to a key only that browser holds, so the service relays ciphertext. `send()` refuses any endpoint not in `PUSH_HOSTS` (Apple incl. every iOS browser, Google/Chrome, Mozilla/Firefox) because the endpoint is a URL a paired device handed us and this server must not become a way to POST at arbitrary hosts. `TTL_SECONDS` 600: an ask older than that isn't worth waking a phone for. VAPID JWT is ES256, 12 hours, for the endpoint's origin; the last (only) record is marked with 0x02.

### `routes/__init__.py`: route package

- Nothing here owns state or rules. A module lands here when its whole job is exposing something that already exists (`providers/`, `db`) over HTTP; domain modules that happen to carry a router (`widgets`, `people`, `lists`, `brief`, `leave`, `trends`, `console`, `voice`) stay at `app/` top level, because moving them here would file the logic under a name claiming it's only routing. main.py is the one place the router list lives.

### `routes/attachments.py`: attachment bytes

- Bytes pass through and are dropped: an email's come from Gmail on each open, and `attachments` keeps its promise that sync never stores them. What the browser may render is the danger, not what's read: a sender picks the filename and mime and this origin holds every other /api route. So a raster image or PDF goes inline, nosniff keeps the browser to the declared type, anything else (SVG and HTML above all) downloads instead, and CORP keeps another site from embedding these while the app runs.

### `routes/extract.py`: deliveries and travel routes

- GETs are read-only like every provider GET; extraction/refresh happens inside email.refresh()'s cadence. The two package writers (delivered, delete) are the exception: `packages` is not a derived cache but the only record of order state this app keeps, and the only hands on it had been the extractor and the console, leaving the card read-only for the two corrections it most needs (this arrived; this was never a real parcel).
- `WIDGET_TRIP_HOURS` is a year: the brief asks "am I flying tomorrow?" (48h answers that), but a card asks "what's booked?", and at 48h a flight bought four months ahead is invisible until the night before, when a card telling you about it has stopped being useful. `MAX_TRIP_HOURS` is two years.

### `routes/places.py`: place writes

- Naming a cluster the map drew lives here rather than in the location provider for the same reason calendar writes are in routes/calendar.py: the provider reads, and `location_points` is a log of what the phone reported; a name is not something the phone reported. The write target is a vault note, so this is the narrow end of an existing path, not a new store: `app/places.py` owns the note format, this owns the HTTP. It creates and never overwrites; renaming, moving or ending a place with `until` is an edit to a note you can open (Obsidian does it better than a POST body). `since`/`until` bounds are usually absent: most places are simply true, only a home has a history worth dating (places.home_on). `GET` lists the vault's own declared places, not a provider range/metric slice. `/places/feedback` records what became of one shortlist, the only record a candidate was turned down; it is separate from create because the interesting half is when nothing was created (a closed shortlist is evidence about the ranking, and a create-only endpoint would only hear about the times it worked); `chosen` is the name kept or null.

### `frontend/src/lib/time.js`: clock times

- Every API timestamp carries its own UTC offset, and for calendar events the backend re-expresses it in the zone the user will be in on the event's date (`app/tz.py`: Pacific for a USC block in September, Eastern for a club call in August). `new Date(iso).toLocaleTimeString()` discards that and renders in the browser's zone, which agrees only until you move (a tomorrow view could disagree with the same event's brief line). So `fmtTime` reads the wall clock straight out of the string; `ev.tz` labels which zone. `timeRange` compares dates as strings for the same reason (re-parsing moves wall clocks).
- `dayDate` parses a day key at noon, never midnight: where DST springs forward at 00:00 (Santiago, Beirut) that local time doesn't exist, and what a nonexistent time parses to is the engine's call.
- `dayKeyLabel(day, isToday)`: `isToday` comes from the backend (the 4am rollover is its call); other days print as dates since "yesterday" is wrong for four hours a night and by days if a source was down.
- `fmtDur` prints sub-minute as "<1 min" (a six-second Instagram visit is still a visit); `fmtHM` rounds to the minute before splitting (rounding only the remainder gave "1h 60m"); `fmtUntil` drops minutes past 6h; `fmtAgo` says "refreshed ..." because providers cache and a panel must not claim "now".
- Range pieces: `start` (the column a card is scanned down), `end` (a second, quieter trailing column: two clock times in one column double the reading and push every summary out of line behind a noon-crossing event), `inline` (both, for one-slot places). The meridiem is written once when shared ("9:30-10:30 AM") and twice only when the span crosses noon/midnight. `+1` marks an end on the next day (a 10pm-1am block is real on the day it starts; reading 1am as this morning would put it in the past).
- `dueCountdown` returns null with no hour on the due date or further than `COUNTDOWN_WINDOW_MS` (24h; a date reads better than "37h"). `due_at` is a bare local stamp read in the browser zone, fine because the dashboard only serves its own machine; the *day key* still comes from the server.

### `frontend/src/lib/drag.js`: arming

- Arming keys: ⌘ and ⌥ (`ARM_KEYS`); a ⌘K or ⌥1 arms only while held, which shows as an outline under the pointer. Letting go of one while holding the other stays armed; blur (⌘-Tab) disarms; a modifier pressed while another app had focus sends no keydown, so `pointerdown` re-reads the held state in capture before the browser chooses drag vs selection. A drag in flight disarms when it lands (`dragend`).
- `effectAllowed = "copyMove"` and `pinDropEffect` ("move" under ⌘ alone, else "copy"): the arming key is still down at the drop and macOS reads ⌘ as a request to move; a copy-only source shares no operation with it and WebKit refuses the drop (it worked only if you let go of ⌘ first). Nothing actually moves; the console only reads the payload. `text/plain` carries the label so a drop outside the console leaves something readable.
- Why dragging next to buttons: a pin button is one payload per surface; extra buttons per level would cost more chrome than the levels are worth. The browser's drag image shows exactly the element that won.

### `frontend/src/lib/obsidian.js`: opening notes

- A plain `<a href="obsidian://...">` fails on both surfaces: Chrome shows an "Open Obsidian?" interstitial on every click, and the .app's WKWebView (pywebview allows the navigation) can't load an unknown scheme, so the click does nothing. The anchor keeps a URI (worth seeing on hover and copying; built display-only here, the real one backend-side where the vault name lives) but the click POSTs `/api/vault/open`, which hands the real URI to `open`: same behaviour in a tab and the app.
- The backend owns resolution (it has the vault and note index); the regexes here only decide what is worth offering as a link. Misses come back 404 and are shown on the link, not thrown (a sentence naming a file not in the vault is ordinary). Wikilinks are linkified whatever they name (only Obsidian writes them). A bare path needs a slash (`DESIGN.md` stays repo talk), excludes a leading slash (absolute path = disk file, goes via `lib/paths.js` FILE_PATH_SRC) and excludes spaces (those arrive as wikilinks or backticks); `looksLikeNote` applies the same rule to an isolated span where spaces are fine.

### `frontend/src/lib/cite.js`: resolving `[cite:...]`

- A ref is six characters of hash; everything worth showing is behind `/api/cite/{ref}`, read only when a pill is opened: a long answer can carry a dozen citations and almost none get clicked, so resolving on render spends twelve round trips to change nothing. The collapsed pill's text is the ref's kind, read off its first letter (e/v/m/d).
- The cache is per page load and permanent: a ref is a hash of what it points at, so only the row's own fields can change underneath, and an hour-old subject line on a transcript isn't worth a refetch. Misses are cached too: a 404 means "never handed out", a fact about the ref, not the moment; the pill renders dead.

### `frontend/src/lib/pin.js`: dragging into the console

(heading is a bullet in the pin notes; add to the Pills bullet) `pin.js`: plumbing fields dropped (`refreshed_at`, `guid` (chat.db row identity, not addressable), Spotify `track_id`/`artist_id`/`artist_key`, `logo_url`, `continued`) but `id` is kept since the console acts through it (POST /api/email/{id}/action). Row lists are byte-budgeted (`ROW_BUDGET` 12,000 serialized chars, under PILL_LIMIT 20k so the wrapper keys fit) rather than a fixed count because row sizes differ by ~10x (200 health readings cost less than 20 messages). A message's files go as name + `extracted`/`not extracted` (text extraction is opt-in, `attachments --extract`), so the console never claims to have read a PDF it only sees the name of.

### `frontend/vite.config.js`: build plugins
- `pruneOldAssets`: a window outlives the build it loaded. Dev tabs build all day under an open Kestrel window whose page still imports its own hashed chunks on demand, so a build that empties dist 404s whatever that page hasn't fetched yet (KaTeX, say, which blanks the desk on the next formula). `emptyOutDir` is off and old chunks age out after `ASSET_KEEP_MS` (7 days; Rollup rewrites everything a build emits, so an mtime that old means no build wants it). A week outlasts an open window in practice; past it `ui/markdown.jsx` fails soft.
- `pdfjsHairlineGlyphs`: TeX PDFs built from PK fonts carry some glyphs as Type 3 bitmaps (CSCI 170's handouts draw the listing minus as a 54x2 image mask, 0.24pt tall). pdf.js traces the mask into a path and fills it at true size, which at side-panel scale is half a device pixel and antialiases to a smear or nothing ("n 1" for "n-1"). Patch 1 (drawOutline) gives the mask at least one whole pixel per axis, snapped to the pixel row/column under the glyph's centre, as Poppler and Preview do; pdfjs-dist 6.5.438 still has the bug. Patch 2 (setCharWidthAndBounds): the glyph's `d1` bbox (5 20 60 22 for the minus) is just as thin and pdf.js clips to it; Chrome antialiases the clip (bar 50-75% grey) and WebKit, which Kestrel.app runs, drops it outright where the hairline straddles pixel rows. So the clip grows one device pixel each way on any axis under a pixel (nothing outside the mask paints anyway). Anchors are pinned to the exact pdfjs-dist in package.json; a bump that moves one throws and fails the build rather than silently losing the patch.
- Sourcemaps are `"hidden"`: written next to each bundle with no sourceMappingURL, for the backend's `failures.py` to map a desk report's minified frames back to `src/file:line`; the map for a hash only lasts as long as `pruneOldAssets` keeps it; `main.py` refuses to serve them.
- Dev proxy: `changeOrigin: false` (the string shorthand turns it on) so the backend sees the Host the page was served from; a page on any Vite port, the verify rig's :5199 included, is then same-origin through the proxy, which `localguard.origin_ok` lets write. `KESTREL_BACKEND_PORT` points it at a worktree backend.

### `frontend/src/lib/{tabs,children,tabRest,handoff,autoResume}.js`: tab state
- `tabs.js` keeps the tab list in localStorage (`console_tabs`); first load adopts the legacy `console_session` key as the first tab's session; tabs from before modes (or the retired terminal profile) migrate to `general`, and a tab without `lastActive` counts from now (else the first load would settle every tab as a week idle). `MAX_TABS` 10 mounted; `MAX_WORKING` 5 mid-turn (all draw on one 5-hour window). Tab fields: id, title, profile, project, spawned, parent, brief (cleared once sent so a reload doesn't resend), reported, held ("error"/"stopped"/"cut off"/"queued"/"agents"), engaged (user typed: never closed for room); child fields queued/queuedAt, done/doneAt, needs, worktree, model/effort; arrangement fields pinned, name, briefPills, lastActive, merged, snoozedUntil, settled/settledAt/settledWhy.
- Engaging a tab (typing) clears done/needs/merged and sets lastActive, which moves at most once a minute so sends don't churn the list. Closing: last live tab can't close; a parent takes its done children, the rest move up a level (queued/working children still owe a report). Going to a queued child starts it past the cap, to a resting one wakes it. Side effects of close read `latest.current`, not the updater, which may run after the call returns.
- `children.js` states: queued / working / needs (question, dead turn, or finished brief that didn't land: commits ahead, dirty files, or its own `Needs you:` line) / done / idle. `admit` puts newcomers behind a non-empty queue so briefs start in order. `DONE_KEEP` 12 done children per group (sessions panel keeps the rest). `followUp` only for the tab's own children.
- `tabRest.js`: resting = snoozed or settled, unmounted like a done child; a tab mid-turn or waiting can't rest. Self-settle: merged and looked away from (waits until read), or idle `INACTIVE_DAYS` 7 (doesn't wait for unread); never pinned, active, child, or pre-first-message. `SETTLED_KEEP` 30. Snooze choices use the 4am rollover hour (db.day_key): "tomorrow" at 2am is this morning.
- `handoff.js`: a turn that ends isn't always work that ends (an API error mid-work would report its last narration line, and a brief reports once), so the report waits through error/cut off/stopped/queued/agents turns. A turn nobody typed (subagent woke it) can slice in an earlier result; only a result that closes the log counts. `clip` cuts from the middle (done opens a summary, what's left to decide closes it; a head-only cut lands on "## For you to decide"). Backend bounds the report again (`console._bounded`, 20k) over `REPORT_LIMIT` 12,000.
- `autoResume.js`: per-tab jitter 20s-2m20s after the reset so cut-off tabs don't fire together; gap doubles from `MIN_GAP_MS` 5 min to a 60 min cap; every case anchors to a fixed time, never `now`; own rate_limit events beat the poll (arrive instantly; poll is minutes behind and pauses hidden); per-model weeks bind only tabs on that model.

### `frontend/src/lib/turn.js`: turn bus
- A module-level bus rather than props because the console is memoised on stable props while a turn changes state a dozen times a second. Each tab has its own socket and session, all mounted; every event carries `tab`. Late subscribers are caught up on tabs whose last "limited" said on (a tab mounts and reports before the plan view subscribes). Calls charge `answered` wait time to the latest live call of the waiting tool. A socket close emits `end` with no turn in flight and must not read as an error. Pictures kept for the view pane: last 8 (data URLs). A preview event carries a counter so asking for the same URL twice still opens it. Node-use counts are browser-local.

### `frontend/src/lib/misc`: small lib modules
- `api.js`: `fetchJSON` coalesces identical in-flight bodiless GETs (the glance strip and cards read the same four endpoints in one tick); entry dropped on settle, so no staleness; each sharer gets a `structuredClone`. `.then(done, done)` not `.finally` (its derived promise would reject unhandled). `postJSON` fixes Content-Type because FastAPI rejects a JSON body without it and a hand-written call drops it silently. `staleReason`: provider reads that can't reach their source answer 200 from the cache with `stale: true` (`providers/offline.py`), previously a 500 per card that broke the desk on a lid opened before wifi.
- `escape.js`: in the desktop app WKWebView hands an unhandled Escape back to AppKit, which exits a fullscreen NSWindow, so every Escape is cancelled (capture, not while an element is fullscreen: that exit is WebKit's own). `useStackEscape`: `fullscreenElement` alone races (may already be cleared), so the last fullscreen exit is remembered (`FULLSCREEN_ESCAPE_MS` 300); Escape in `.console-pane`/`.prompt-bar` belongs to the composer; a field with a draft blurs on the first Escape.
- `report.js`/`usage.js`: bare `fetch` on purpose (a report through `fetchJSON` would loop on a down backend; telemetry must never break the desk), `keepalive` since flushes happen as the page goes away; `usage.js` buffers (15s or page hide, 100-event flood flushes early) and `seen` dedupes per (label, band) per tab (impressions, not render counts).
- `sheet.js`: `useSheetData` runs N views per range with one generation counter (only the newest request writes; reads sync their provider first so a slow earlier answer could land over the latest click) and shows one error when all views fail for the same reason. `views` must be module-level (effect dependency). `/api/providers` is fetched once per page load.
- `undo.js`: toast window 8s; `holdDelete` hides the row until the toast ends and releases only after the write and reload land (no flash back).
- `look.js`: themes are validated again client side (hex values, token names) before becoming CSS; `:root:root[data-look]` doubles specificity so a theme outranks base palettes whatever the sheet order; `look_pair_r6` and `desk_layout` are new keys per round/default change so stale values don't hide the new ones.
- `palette.js`: one command per subject (the first draft's separate panel/card rows left nine of eleven panels with a near-duplicate tying on score); console slash commands are deliberately not here (⌘K drives the desk, `/` drives the agent).
- `fuzzy.js`: keywords match as substrings (subsequence over the keyword blob picks letters out of "Debts" for "debug").
- `taskDue.js`: `today` is the backend day key; a bare small hour (1-7) means evening; a past date isn't rolled forward; an unparseable `@` returns the whole string as title with `unparsed`.
- `prWatch.js`: backend is stateless; last snapshot lives in localStorage per tab; one poll in flight (the mount poll and a refresh-bus broadcast used to double-report).
- `pdf.js`/`files.js`/`consolePrefs.js`: see Documents and Models sections; failed doc loads aren't cached; Enter mid-turn is "steer" (default) or "queue" (`app/console_queue.py`), ⌥Enter does the other.

### `frontend/src/plan/desk.css`, `agents.css`, `delegates.css` and `looks/*.css`

- **Rooms desk** (`desk.css`, `html[data-layout=hybrid]`): the default desk (New Features #46, look lab round 5). Sidebar of rooms with a page per room, Today as home; a floating glass top bar with the day as a sentence, the glance chips, and the focus line (the most urgent ask) under them; the console as a centred ask bar over the page, ⌘J opens it as a column. Classic (no rules in desk.css) is still in the look lab. Judged dark first, light only kept legible; checked in WebKit at 1417px (the usual window) down to 700px.
- **Cards in a multi-column flow** (WebKit): a card is `inline-flex` because WebKit ignores `break-inside` on a flex box in multi-column, so a card split across columns and left a sliver of its foot atop the next column; an inline box can't split. And nothing on or in a card may be `position`ed: WebKit then paints nothing of an inline box past the first column (three of the Plans page's five cards were laid out and invisible). Widget header buttons ride up into the card head by negative margin for the same reason.
- **Top bar sentence** names the day, the next event and the leave time, so the strip's own day and "next" chips go and the leave chip keeps only its time; the "N need you" chip opened the side column, which this desk doesn't have (the focus line and Today's badge say it).
- **Full views (⌘K sheets) and full-screen console (⌘F)** use the classic rules: left in, the rooms rules made stray grid cells and stretched the sidebar across the full-screen conversation.
- **Ask bar**: one row, always whole. Round 5 hid it until hover and wrapped what it showed, which never let the priority fold (`lib/barHost.js`) run: unreachable by keyboard and a jumble on hover. Now the fold moves the least-needed controls into ⋯, a real button. The folded dock is bar plus composer in one in-flow stack so a growing composer (queued messages, task strip, long draft) pushes the bar up instead of sliding under it. The page fades under the bar so the gap isn't a slice of half-read text. The column opens only as a sheet holding the panel, since every ask is already on its room's page and Today.
- **Agent visualizer** (`agents.css`): label pills are tinted by the agent's hue because a neutral near-black is a dark card's own colour and the label vanished into it.
- **Delegate frames** (`delegates.css`): a screenshot frame keeps a white ground in every theme because it shows a page someone looks at in daylight; a page is laid out at desktop width (300%) and scaled to a third (150% to two thirds when it's the only one). Providers differ by name, never by hue: the one accent on this surface means "live".
- **Looks** (`looks/*.css`, the look lab's rounds): each variant moves one variable so the vote is about that alone. `accents.css`: hues at the blue's OKLCH lightness and roughly its chroma; green sits on `--good` and rose next to `--bad`, so a primary button and a status colour read alike in those looks, which is part of what's judged; hexes, not `oklch()`, because the map canvas reads them with `hexToRgb`. `amber.css`: Kestrel's old orange, same lightness and contrast as the blue. `flexoki.css`: the pre-round-2 palette and face (JetBrains Mono UI, paper grain, 4/8px corners, wide-tracked caps) kept so the baseline's "before" is one keystroke away; it rides the current type/spacing scale, so it is not a pixel replay. `map-narrow.css`: a fixed 360px map column (docs/ui-overhaul.md P2.1, option b), side by side from a 680px work column instead of 800. `grounds.css`: map grounds re-point the paper tokens on `.map-wrap` only, so nodes, labels and halos follow and nothing outside the map changes; quiet nodes get a ring because their grey dot is lost in the grid and dots grounds. `lab.css`: `.ll4 .ll-axis` beats `.ll-seg`'s `flex: 1`, which squeezed the axis to "L… M… C…".

### `frontend/src/styles.css` and the look/desk/agent/delegate CSS: why the rules are shaped this way

Tokens and materials
- Two materials: the desk is paper (the vault), the console is ink (the agent); the seam is the app's identity. Round 2 moved the desk to OKLCH near-neutral greys (hue 286, chroma <= 0.008), blue accent instead of orange, system sans instead of mono; the old look is `looks/flexoki.css` (palette and face only, not a pixel replay: `git show a4736a2:frontend/src/styles.css`). Research and sources: docs/ui-overhaul.md.
- Accent: as a fill it means one thing, context flowing to the agent (pins, pills, your messages, pending permissions); as a hairline edge it means "this one wants you" (NEEDS band, unread mail). A non-agent emphasized button takes an ink fill. Data marks are indigo; status is green/red and always paired with text.
- Tokens are hex because the map canvas parses them with hexToRgb (lib/tokens.js, plan/MapTerrain.jsx). Contrast: ink 17.5:1, mid 5.5:1, faint 3.9:1 (incidental text only: timestamps, "later"), accent 4.6:1.
- Ink side: graphite in light mode, a step below the desk in dark so the console reads as a sunken pane either way. Dark mode: elevation by lightness (card lighter than desk); the dark palette is written twice (prefers-color-scheme and `data-theme` override from lib/theme.js) and the copies must stay equal. `color-scheme` is set so native controls, scrollbars and pickers follow.
- No paper grain by default (texture was the one decoration; flat reads calm once edges and type carry structure); flexoki.css keeps it. Type scale = macOS text styles (Body 13/16, Callout 12, Subheadline 11, Footnote 10, Title 3 15, Title 2 17, Title 1 22, Large Title 26), 10px floor. One caps tracking (there were nine). Spacing 4px grid with 2 and 6 half-steps. Elevation: hairline at rest, shadow only for what floats. Motion 100/160/240ms, one ease-out curve (Carbon productive).
- Tabular figures replace the mono face for numbers.
Icons
- ui/icons.jsx replaces emoji: they render in currentColor so they take color, hover, disabled and accent state; emoji ignored Flexoki/dark mode and shared no weight or baseline. Sizes are px, not em, because the UI runs at 10-13px and an em-sized icon drops below the stroke's legible floor. A lone icon flex item has no baseline in `align-items: baseline` rows (hangs off the bottom), so it is centred.
Glance, status, sync
- The masthead is the one display size on the desk besides a lede's number; a chip draws its edge only when it has something to say or you're on it. Sync names the source it's on and drops a per-provider log: a full cycle spans a dozen APIs and one "syncing…" is indistinguishable from a hang. StatusPanel renders a fragment into .sheet-body with no wrapper, so `.status-list` is scoped to it alone. Status detail lines truncate (one-line clips keep fourteen healthy rows scannable) except faults, which wrap: a clip ate "aw-server unreachable (mac) — is…", the sentence the panel exists to show. The event log is dimmer than live state (history).
Badges and chrome
- .ai-badge (a model wrote this text) is quiet so it doesn't compete with the brief. The stale badge (source unreachable, showing app.db's copy; providers/offline.py) is shaped like .ai-badge on purpose: both say "this text isn't quite what it appears", neither is a problem to solve; reason on hover; the status dot escalates if it lasts. `flex: none` so a crowded header can't squeeze it out. `.thin-badge` (sensor barely watched the day) went with the screen card; its name stays in the selector because location computes the same coverage floor server-side and has no badge yet: drop it if none arrives.
- An in-flight refresh must look different from a click that did nothing: the icon spins and stays visible when unhovered. A dead credential is a chore, not a failure: one muted line and the fixing button; red is kept for errors you can't act on so it still means something.
Widgets
- Brief is prose as a full view (no card border/fill). Food nutrient bars: bar = share of target; met minimums green, an exceeded cap red; ranges (energy, fat, carbs) are "met" near target and "over" well past it.
- Event end time is a second clock column at the row's trailing edge (ui/EventTime.jsx), deliberately not beside the start: the card is scanned down its start times, and a span there doubles the reading and widens the column for the one event crossing noon, misaligning every summary below. At the edge the ends align with each other, --paper-faint keeps them off the scan path, rows without one (all-day, "TBA") leave it empty. The per-row pin has its own auto margin; two would split free space and strand the end mid-row. The zone label is quiet but a clock time is never shown without it.
- Card lede (lib/notable.js, ui/Lede.jsx) replaced the .health-grid inventory as the default measurement card body. Tone follows the metric, not the arithmetic: a direction is bad only if the widget says which is bad (distance, minutes listened stay neutral). Colour lands on the delta line, never the value: a red 76 reads as an alarm the card isn't entitled to raise.
Cards (continued)
- Lede "other numbers" is the same string the collapsed slot shows, so an open quiet card and a folded one agree and it reads as reference. A lede with nothing led is the whole body and takes ordinary text colour: faint made a quiet day look like a broken fetch.
- Plaid enrichment: 16px logo chip before the merchant, hidden via JS onError on a dead URL; location muted, in-store only; merchant links out, underline on hover only. Deliveries/travel share .event-list's key · what · when shape so the cards read alike; `soon` is the accent, not a background (a flight tomorrow shouldn't turn the row into a banner). Location's coverage footnote is the quietest text on the card because it qualifies every number; its stale/quiet variant is one step up, not an alarm: the status dot escalates (same rule as .stale-badge). Finance chart marks use data/status tokens; text stays in ink tokens.
- Trends: the min-max and value boxes are `flex: none` because they are nowrap, so a shrunk box overflows (the min-max ran through the last value); only the chart gives up width; min-max is the first thing to go in tight columns. The mixed-basis correlation badge keeps .corr-meta's muted colour (a caveat on r, not a weak sample, unlike .corr-tent). Sleep-stage bars: single hue by depth (deep darkest), awake neutral grey; a surface-coloured stroke opens a 1.5px gap between segments without distorting true heights.
- Habits: done is green like every other done on the row (an accent fill would show "Sleep, logged today" in two colours); accent stays on hover where it means "this click does something". The 7-day strip's button is a 24px target around the 11px mark; no `position` there (a positioned box inside a card vanishes in WebKit's columns, see desk.css). The habits expand's last column holds two buttons (archive, delete) and must be two wide. The destructive .pin-mini doesn't take the accent on hover.
- Office-hours amendments read from mail are framed, not listed: they claim the rows below are wrong, and the accent edge says so before the text is read; the quoted source is indented, italic, quiet (someone else's words).
- Tasks: within a day of the deadline the date becomes a countdown, stops being reference, and takes the row's weight; `late` is the overdue dot's red. The quick-add echo shows what the trailing `@ …` was understood to mean, quiet under the input. The title is the drill-in toggle (bare button, hover underline, ⌄ flips). The one caret CSS draws itself is icons.jsx's chevron as a mask so it takes currentColor. `Folded` sits above its rows (the earlier half of the day), so its margin is on the other side.
- The committing button (save an edit) keeps an edge you aim at, unlike `.widget-footer-btn` (navigation, quiet text + arrow). The border earns its keep in the disabled state: the button is mostly clean, and a save that looks pressable when pressing does nothing is worse than none. (It was `.email-review-btn`, worn by two navigating footers; the footers went quiet and the name says what's left.)
- Activity ("While you were away", ActivityWidget.jsx): two columns (tag with accent dot when unseen; text with source beneath on one quiet line); actions are text so five rows don't read as a form; undone rows fade and strike. Brain facts wrap, never ellipsis: the claim is what you judge. Sheet bodies: the container owns the frame; the sheet head is sticky over the body's scroll; USC sports squares carry a letter so a result is never colour alone; RangePicker is words, not pills, so the data reads first.
Sheets
- A band is a named question with its headline answer in the header, so a sheet body can be a chart, list or receipts. Booked-hours bars use the data hue (data, not a call to act; a week of accent bars would outshout what wants you). Email sheet: unread (still owed something) is the accent, the rest recedes; location distance is data; music uses the music card's data blue so bar and card read as one subject.
- An openable bar (DaySheet.jsx) gets only a pointer and hover lift (every bar is already draggable; no row of buttons). A night is drawn on its own clock with four lanes, awake on top, and the rail labels an empty lane so "no deep sleep" doesn't read as a missing row. Trend rows are per-row grids with fixed column widths: `auto` sized per row and misaligned the sparklines.
- Location map (MapSheet.jsx): CSS is the frame, two controls and the tile licence's credit line; the canvas draws. Fullscreen must claim its ground (the UA's black backdrop shows through a translucent basemap and drags the palette dark while tokens stay light) and override the 380px height (letterboxing on a 1080px screen). Map list: the row number matches the map pin (tabular). Close declared places that no coordinate separates show a faint qualifier. Naming an unnamed cluster is indented off a rule inside the picked-place panel (part of the place, not a dialog). A single fix is dimmed, not hidden (not a stay, never learned from). A learner guess is dashed (not a label until clicked); a guess resembling nothing labelled is still offered (something wins the vote) but must look unconfident. One row per visit, since a cluster isn't one place.
Email sheet
- The triage pick is a control (click, or ↵ while the card is hovered), inline in the summary sentence and text-shaped until you're on the card. It uses an ink fill, not accent: accent is reserved for context flowing to the agent and eight accent buttons down a queue read like eight pins. The "why not the triage pick" note must not look like a second action; the evidence line ("you archived every one of the last 4 from this sender") is a quiet mono footnote (counted things). The reason icon aligns flex-start (centering floats it between wrapped lines).
- The subject is the read-here control (text-shaped until hovered, like .email-rec). Plain-text bodies use pre-wrap to keep the sender's shape and anywhere-breaking so a tracking URL can't widen the card; lines are capped by a measure (uncapped they pass 150 characters). The reader sits above ⌘K's backdrop (z 40) because a console cite card opens it. Mail is designed on white, so the reader body stays white (emailDark.js supplies the dark variant). The filed-where tag sits at the action row's far end as a pill (qualifies all three buttons rather than being a fourth). The user's correction is a solid ink pill (visibly settled).
People
- The owed-reply message is on its own line (`flex: 0 0 100%` forces the wrap in the peek list; inert in the expansion). Conversations rows: label on top, snippet muted below; per-message group-chat tags (1:1 untagged). Files sent with a message are dim, full weight once text is extracted (searchable; the console can answer from it). Hover labels on the five glyph buttons (pin, draft, remind, dismiss, mute; check hides a thread, bell a person forever) avoid the native-title delay and anchor to the button's right edge because .sheet-body clips horizontal overflow. Only rows holding drafts wrap. The dismiss tick is hover-only (always-on ticks read as checkboxes). The Messages.app hand-off is a real button: the one action on a read-only page.
Ink side (console)
- Dark seam: on paper the desk/console edge is a 242→13 cliff; in the dark it's 35→13 and only works if nothing bright crosses it. The desk's right edge goes to ink (a line lighter than both sides welds them into one slab with a scratch), and a shadow gutter inside the console restores paper-above-ink depth (same device as --panel-shadow).
- The thread is one travelling 2px mark on the seam while a turn runs, the only animation that isn't a 140ms state change; reduced motion shows it static ("working" by being there). A <select> sizes to its widest option (caret floated 40px past "ask" in a box cut for "auto-edit"); `field-sizing: content` tracks the selection. "never ask" costs after the fact, so the toolbar keeps saying so while it's on (same red as a rejected usage window); the warning at the switch is read once and the session runs all day. A cached usage number is readable but visibly not live; the tooltip has the age.
Console log and cards
- The log's unit is the turn block: one column centred and capped at --measure (past ~90 characters at 13px a line is hard to track back); capping alone leaves dead margin on the right that reads as a broken pane, so the column centres and turns, permission cards, status lines and composer share edges. `justify-content: center` on the grid keeps the scrollbar on the pane. "Jump to latest" is a zero-height sticky row so it adds nothing to the content it is measured against. Undo stays visible (not hover-revealed), being the escape hatch.
- Vault links: dotted underline (they hand the note to Obsidian and leave the dashboard where it is, so solid web-link styling would lie); a backticked path keeps its code chip and borrows only the underline; a miss is marked, not hidden (text stays, the title says why, so "no such note" never looks dead). File links (ui/FileLink.jsx) wear the same dress; the reveal-in-Finder folder shows on hover and sits out of flow so a reply naming a dozen files doesn't grow a dozen buttons and spacing is the same resolved or not. Unparseable TeX keeps its source on screen, as a missing vault link keeps its text.
- Citations: the collapsed marker is the glyph alone at --ink-faint (a bordered chip per claim would turn an answer into a table of contents), accent on hover, shifted slightly below baseline so it reads as an annotation. A ref that was never handed out is struck like a missing vault link. The opened card (Cite.jsx) is anchored under the marker and shifted on-screen by a measured translate (end-of-line markers would open off the right edge); the width is capped in CSS so that measurement is stable. Snippet text is clamped (evidence, not content; a newsletter snippet would push the actions off). A document citation's pill carries its page ("p12").
- DocPage: marks are the quote in page fractions so they track the canvas at any width; the page stays paper-white in dark mode (inverting would negate every figure). DocPane is absolute under the bar like the diff pane, with the log and composer stepping aside; in a console too narrow to share (DocPane measures it, class `doc-cover`) it covers the log (Esc or × returns). It is not a container query: container-type brings layout containment, which makes .console the containing block of every position: fixed thing drawn inside it.
- Study blocks: with slides the block takes the room it's given (full-screen console); slide and question stack until side by side fit, the slide getting the larger share; side by side the slide stays in view while questions scroll (stacked it would cover them). A multi-line code field renders as blocks with the number to their left. The confidence prompt is quieter than the choices (skipping records "didn't say").
- Block markdown: each block owns its spacing, so paragraphs keep their pre-wrap while tables and lists lay out. Tables wrap cells (a sideways scrollbar hides a line's end); one that still can't fit scrolls in its own box. KaTeX is lifted out of the paragraph's pre-wrap or its positioned boxes stagger; a display formula scrolls like a table; KaTeX's thin serif reads small, so 1.08em (subscripts/exponents near 9px) and one more step for a display formula.
- Automated activity: a run of tool calls is one nowrap ellipsed line (count, tools) that opens into the calls, so the log's rhythm follows what you asked and what came back, not how many files were read. The call in flight pulses its count, not colour: accent means context flowing to the agent and this is traffic coming back. Tool names form a column so arguments start at the same x (a long MCP name pushes only its row). Standby rows (not chips): the preview line identifies a past chat and gets the width.
- Permission cards: amber/accent while the agent waits on you. `.perm-guard` explains why a card appeared in a mode that promised not to; the cost line ("what saying yes costs") is quieter, being a standing property, but the same colour because the same word ("delete", "send") does the work in both. Claude's sentence for why is quoted, indented off a rule: testimony, not the card's description. Calendar notes are bounded so a pasted agenda can't push the answer buttons off screen; the facts that change the answer (a whole series not one occurrence, guests emailed) stand out. The turn receipt (one row per call that ran) is a surface, not a status line; only rows you'd want to catch get colour ("created"/"edited" are expected). AskUserQuestion shares the approval card's shell; its option list's cursor column holds its width so the list doesn't shift as the arrow moves.
- Composer: the rule spans the pane, the controls sit on the log's column. A dropped file reads differently from a pinned widget (user-supplied, may still be resolving).
- Drag: rail and slide-over items are drag sources at three nested sizes (card/list/row, lib/drag.js); nothing says so, so `grab` is the hint, on the element that would win the drag; `grabbing` is the browser's. Wrapped in :where() so any element naming its own cursor keeps it, but form controls take their cursor from the UA sheet, which any author rule outranks, so those two are named. While a pin is dragged out of an expanded widget, the slide-over backdrop (covering the whole window, console included) stops taking hits, or the drop lands on the dimming layer; it also lightens so the console shows as the target. Lightening not restacking: below 1040px the shell is one column and the fixed panel overlaps the console pane, so lifting the console would cover the panel being dragged from. The panel keeps its own hits so dropping back onto it is a no-op, not a fall-through to the desk.
- The prompt box grows (Console.jsx fitInput) to under half the window then scrolls; controls sit on its last line so they stay put as text grows.
Composer, lists, expenses
- Voice: an open mic must be visible, so listening is the one composer state that moves. Desktop tray thumbnails: every screenshot has the same name, the picture tells them apart. A running `!` command you can type into gets a fixed box (xterm fits itself to its container, not the reverse). Lists chip row: a chip per list with its open count; "+ list" is held apart because it makes a list rather than selecting one. debts/user lists/watchlist share one row (CheckList.jsx: round toggle, label block, drop); the label wraps (a title plus its streaming services). Owed beers sit under the money, headed apart so a count never reads as dollars.
- The chase button has two weights on purpose: "ask"/"ask again" are why the card is in `needs` and read as a control; `.waiting` is a receipt for something done, faint like row metadata. Expenses (ExpensesWidget.jsx): a waiting row is a check-row with the date in place of a toggle (sending is one button for the lot); the label block is a button because it opens the editor. A row missing its by-category sheet entry is a gap visible in the spreadsheet, so it's drawn in the accent to be filled before export. A sent batch that no longer matches the bank is flagged (rows in the hover title; reopen the batch to fix). On the Money sheet the claim control is hover-only (most charges aren't expenses; 60 "+ Expense" buttons would be noise) while a claimed charge's tag stays; hover is a JS class (`.txn-hover`, useHoveredRow in MoneySheet.jsx) because WKWebView left li:hover stuck on rows in the desktop app. A late-posted charge is marked by a dot, not a colour change.
Command palette, office hours, usage
- ⌘K drives the desk, so it is vault-side paper; the console keeps its own slash commands and push-to-talk and the two lists stay apart on purpose. No accent except the one row that hands you to the agent ("Focus the console", wearing a user message's left rail); selection is a paper fill, not a colour. The panel is its own focus indicator (the global accent ring would read as the agent thread). Matched characters show as weight plus an underline rule (no spare colour). z-index 56 sits over the habit nudge (55), which can pop up while it is open.
- Office hours rows reuse .event-list. Amended hours stay quiet (the row shows the real time); a cancellation is the one thing that stops you leaving the building, so it takes the chip, and is struck not faded (faded reads "already over"; cancelled hours aren't happening).
- Desk usage (usage.py): the touch count is the eye-catching column but misleads (the cards that work best are read at a glance and never touched), so the notes around the table carry as much weight as the numbers; only the narrow `idle` case is greyed, not struck or red (a suggestion to look, not a verdict). Model usage rows have a second label line, so the label takes the width. A price no list has is said in words in red (a gap in the total), never $0.
Plan view
- The plan view is one window that never scrolls (only the conversation, a long list and a sheet body do); stage = ink, side column = paper. The grid uses minmax(0, 1fr) not the implicit auto column: an auto track sizes to min-content, so `.plan-main`'s min(520px, 42%) side track has no width to take 42% of and falls back to auto too, then one unbroken nowrap string in the side column (a long list note, a bare URL) sets the page width (a 1417px window laid out 1882px wide; New Features #76, #78). The side column widens for a card or Sources to a reading width, not half the window (at half of a wide window a date column sat 700px from its row and a three-line card left the pane empty); anything needing more is a full view.
- The work column: the stage holds console, prompt and map as grid siblings so nothing is drawn over anything else; layout follows the column's own width, not the window's (opening a card narrows it as narrowing the window does). The status bar says what the console is doing, so a second green dot speaks up only when the socket is gone; the console's bar inside it is one row of chrome and the status line ellipsizes first. The bar's narrow widths are its fold's (lib/barHost.js), measured, since what fits depends on the tab. The fold: fitBar sets data-cut and CSS hides what has no room, lowest priority first, into ⋯; the status line and tab strip keep a minimum until "squeeze". The tab chip already names a dev tab's project, so the bar doesn't repeat it.
- An open email (EmailReader.jsx) dims everything but the prompt, which sits over the reader's backdrop (the reader stops above it) so a question about the mail can be typed while it's on screen. A document the console is talking about takes the map's place, over a watched turn too (the newer ask); at full sheet the stage splits (conversation left, document right). A computer-use terminal in the view pane is pinned, not flowed: in flow xterm's width sets the pane's, so it could grow but never shrink and a narrower column scrolled the sheet sideways. xterm.css paints its viewport black, which showed as a frame in the padding.
Tabs, diff, map, pane
- Tab mode classes are `mode-` prefixed: a bare `study` class picks up the quiz block's `.study` rule (border, padding, wrapping, inline-size containment), which folded the bar's "study · handed off" into a one-letter-wide column. A tab and the tabs it opened (open_tab) form one group: parent chip, a badge for how the children stand (click folds them), then smaller borderless children in a tray; "needs you" is the bar's one warm colour. A pinned tab shows the tack before its dot, no × (unpin first) and a little more ink so the pinned run reads as one; a drag shows where it lands. The project/PR chip's icon carries the tone so the short form ("#14") still says how it's going.
- Diff view: the branch view shows the commits a merge lands, then what's only in the worktree (dashed, dimmed: real work, not landing). Syntax colour (lib/syntax.js) is the shell palette lifted toward ink text so it holds on add/remove tints in both themes (--good and --data are paper colours, too dark on the light-theme console); a coloured file's text goes neutral and the tint, gutter and sign carry added/removed, while a file with no language keeps tinted text. Line comments are your words to the agent, so they wear the accent edge of .entry.user; sticky so a sideways-scrolled long line doesn't take the comment. Conflict display mirrors git: base lines in the hunk blue, the tab's in added green, markers red.
- Map (plan/PlanMap.jsx): lines are hidden at rest except the last turn's (faint); hovering a node shows its own; the key shows all, weighted by use; during a turn a call lights its line (dashed while it waits on you, red if it failed). Quick on, slow off: calls arrive in 100ms bursts and equal speeds both ways flicker. Ink, not colour, for a node that has something but doesn't need you: the accent is the map's only hue and means "wants you". A quiet card is a dot until pointed at; a broken one keeps its name in any band. Machine nodes (where the console works) stay hidden until a call uses them; the vault stays whole as the way into notes. The accent halo fades over the grey one; the pulse is on the inner circle and only pauses (ending an animation jumps to full opacity and flashes the halo; paused it draws no frames at rest); working and asking share one animation so it carries across the switch. Kestrel dots' movement is entirely plan/dotMotion.js, so no transform or transition belongs in CSS.
- The side column holds the list plus an open card or Sources over it; the card hugs content up to 62% of the column then scrolls, and the list keeps a floor. A card in the pane (DESIGN.md → Card anatomy) draws no box (a bordered box floating atop an empty column was the rail's card shape carried into a space it didn't fit); its header is the pane's title and its controls stay visible (hover-reveal was for eighteen cards sharing a column; the pointer is on this card whenever it's in the pane); scoped to the pane's direct child so sheets reusing .widget pieces keep theirs. The primary action is neutral at rest and takes the accent on the row you're on (hover, keyboard cursor, focus): eight accent buttons in a column meant none stood out; alone it keeps the accent edge. The red dot carries "late" while the reason line stays grey and says "N d overdue" (four red lines read as an alarm); a broken source still goes red (a fault). "Later" is hidden until hover/focus/keyboard cursor and always shown where there's no hover. Callouts: tone is a left rule and title colour with an ink body, so a callout never reads as an error by hue alone. Sentence case for the undo toast's labels (seven stacked caps labels read as a terminal readout).
- Full views: sheets were drawn for the dock's ~500px column and their charts are viewBox SVGs that scale with it, so stretched across the window axis labels come out at display size; they keep a column's measure, centred, until one is laid out for the width. Auto margins don't centre an inline box (a bare top-level <input>, People's search, sat flush left). With the top bar hidden the main area is the grid's first child and would land in its auto-height row, so it gets one row. The side column never floats: put away (Needs you's ×), it gives its width to the stage; below the breakpoint (NARROW in PlanApp.jsx) there's no third column, so the window is console and map alone and an open panel takes the map's place; the leave chip's "→ ENGR 102" repeats the chip beside it so it is hidden.
- Subagents (console/AgentStrip.jsx): a quiet row each under the log; running breathes, finished keeps its first report line until the next message. An Agent call row's calls come from the subagent's own transcript. A background task note is coloured only by how it ended. The look lab wears the app's own paper so it changes with the look it judges; the look ON SCREEN is the accent (radio dot, ring, badge) while A/B PINS are neutral ink so a pin never reads as "this one is showing". The card-waiting modal (Console.jsx renderCardModal) sits over everything including the email reader (z 50): a turn blocked on an answer outranks what was being read; the card keeps its own look and the modal is only the frame. The habit check-in (HabitNudge.jsx) wears ⌘K's paper look, centred, under an approval card (z 60).

### `frontend/src/ui/`: shared primitives, reasons behind the shapes

- **icons.jsx**: one system on a 24px grid, one stroke, `currentColor` (emoji ignored Flexoki and dark mode and each carried its own weight/baseline). Adding one: keep the grid, no fills or stroke-width overrides: the shared weight is why a car and a checkmark read as one set. `size` is px, not em (tracking font-size makes the small ones illegible). `trash` differs from `close` because `close` already means delete on delete-only cards and a card offering archive and delete (habits) needs two distinct buttons. Cake: the frosting scallop separates it from a rounded rectangle and the flame is a dot (a filled teardrop smudges at 12px). The citation set: the envelope needs its flap and the calendar its two rings at 11px or they read as a framed box like `expand`. Precipitation uses a compact cloud (a scaled copy thins the stroke).
- **Mark.jsx**: the app mark (Malar: yellow-ringed eye, black tear stripe, rufous tile; `scripts/gen-icon.py`, `public/favicon.svg`) is the one place amber fills a whole shape (elsewhere amber is a thread to the agent). Not in icons.jsx and its colours are literals because the mark must look identical in any theme. Tile is a superellipse |x|^5+|y|^5=1 (a rounded rect looks wrong beside real app icons); the stripe tapers and leans back (upright or flat it reads as an "i" at 20px). The clipPath id is fixed because instances share identical geometry.
- **math.jsx**: KaTeX from npm, not a CDN (offline). `copy-tex` makes a selection touching a formula copy its TeX whole (plain copy gave both KaTeX layers glyph by glyph, `x2x^2`); TeX because `\frac` has no flat form. `throwOnError` off marks unparseable TeX in place (error colour `var(--bad)` so it follows light/dark); `strict` off because the model writes real-world TeX (`\text` with unicode, stray `\newline`). Default output keeps both layers (html-only leaves no accessible text); the TeX lives in the `<annotation>` copy-tex reads. Inline math is set in `\displaystyle` (TeX's text style shrinks an inline `\frac` to ~8px beside 13px mono and crams `\sum` limits); the prefix is ours so it stays out of the annotation. Display blocks scroll rather than widen the column. `MathInput.jsx` (MathLive) loads on first use (about as big as the rest of the page), uses the console's bundled KaTeX fonts (`fontsDirectory` off) to work offline, and a failed load forgets itself so the next open retries; MathLive brackets single-atom fraction terms (`tidy` strips `(1)/(2)` to `1/2`); `onChange(latex, ascii)` ascii is the typed form that a quiz's `accept` list uses.
- **charts.jsx**: hand-rolled SVG, no chart library (it would bring its own colour, tooltip and font). Charts lay out in the box's measured pixel width (`useChartWidth`): a fixed viewBox stretched to 100% scales text with it (9px labels came out 16px). The legend swatch is drawn with the bar's own classes (`seg-*` set `fill`; a CSS-background swatch fell back to `--data` for any series without its own `.swatch` rule: Email's read/unread both showed blue). `finite()` filters rows because providers report a missing day as absent/null ("gap, not zero") and a null in arithmetic reads "we didn't measure" as "nothing". `SeriesRow` is for series that shouldn't start at zero (resting HR in a 20-beat band). `RankBars` takes `key` where labels collide (two artists, one track name); `rankNote` says which N of the total so count and rows agree. `SpanBars` `originHour`: providers measure from the 4am rollover, so a midnight axis puts every morning four hours early and clips the small hours; `h:mm` is truncated, not rounded, to agree with the backend's strftime (a card at 2:19 beside a sheet at 2:20 is one number disagreeing). `onPick` makes a bar openable and keyboard-reachable (focus + Enter/Space). Sparkline `last={false}` when the caller prints the latest value (else "13.8 13.8 km").
- **EmailReader.jsx**: formatted by default when there's an HTML part (newsletters are tables; stripped text is orphaned fragments). The iframe is the containment, not the backend's scrub: sandbox without `allow-scripts` (`allow-same-origin` only so Escape pressed inside is heard), a CSP ahead of the mail's markup allowing no remote loads until "Load images" (no tracking pixel on open), `<base target=_blank>` so links go to the system browser. Bodies are never stored; each open is a Gmail round-trip, cached per page load. Escape is a capture-phase window listener ahead of every other Escape handler (sheet stack, ⌘K, cite card) and `preventDefault`s so AppKit doesn't take the window out of fullscreen; an open attachment lightbox closes first. The prompt stays usable (styles.css lifts it over the backdrop, the reader ends above it, measured since the prompt grows). In dark theme, mail with no colours of its own reads dark (`emailDark.js`): the frame stays hidden until the first load is judged (no white flash) and the verdict is frozen per `[html, images]` so reaching it doesn't reload the frame. `emailDark`: all-or-nothing (if any background is non-white/transparent the mail was designed on its colours and none flips); read from computed styles because colour arrives as bgcolor=, inline, `<style>`, `<font>`; whiteish includes #fafafa/#fdfdfd but not #f4f4f4 (a wrapper design); lightness is flipped into 0.1–0.9 and readable text gets a brightness floor (HSL lightness isn't brightness: #0000EE flipped is still too deep); all computed values are read before any write (colour inherits).
- **DocPage.jsx** outcomes: `here`, `moved` (words on another page: show it and say the cited number was wrong), `notext` (scanned: "can't check", never "not found"), `missing` (unsupported claim). No paging in an embed: the answer chose its pages, flipping loses the place, "Open" browses; a click enlarges (a half-width slide isn't legible for code). Lightbox is portaled to `<body>` so no card's overflow/transform clips it. In a console a click opens `DocPane` beside the chat.
- **EventTime.jsx**: the end time is the second question every event row gets asked (is the 3pm still running, is the gap before dinner real), so it's on the row, but "2:00–3:20 PM" in one column doubles the clock times read while scanning and an event crossing noon needs both meridiems, widening its column and misaligning every summary below. So the start column keeps its width and the end sits at the far right in `--paper-faint`; `EventEnd` renders last in the row so its auto margin pins it.
- **StaleBadge** is not a `WidgetError`: after wake (DNS not back, reads fall to cache) nothing is broken, so it carries the range label's weight with the reason on hover; the glance strip's status dot escalates. **WidgetError** recognises the lost-credential shape (the commonest widget error; `oauth.py` words it for a terminal, four lines, and health and trends both printed it) and answers with a line plus the fix button; the backend opens the consent tab (`app/browser.py`) because `window.open` returns null in the desktop shell; no poll after (the focus refresh reloads). **ErrorBoundary** wraps each card and full view, not the app, so one throw doesn't blank 28 working cards; it is deliberately not `WidgetError` (that runs hooks, unsafe when the render broke); reports both stacks (component: which card; JS top frame: which line, mapped to src/ by `failures.resolve_trace`); `resetKey` for in-place changes (a streaming entry); a class because `getDerivedStateFromError` has no hook form.
- **Smaller**: `TaskDue` uses one shared interval for every countdown (per-row timers drifted out of step, "23m" beside "22m"); the first countdown re-reads the clock (an overnight tab holds a stale time); subscribes whenever a due hour exists so a task 25h out switches over by itself; an all-day task has no instant (see the `due_time` migration). `CalendarHeat`: one hue in quartile steps so a huge day can't wash out the rest; no reading draws nothing while zero draws a faint square. `RangePicker`: the `start..end` token grammar is parsed by `providers/ranges.py`, so never hand-roll a copy; `ranges` null = meta not loaded, `[]` = none offered so the current range is custom. `SheetHead`: providers cache, so the age is stated; `false` drops the slot (CSS reads it as an empty left); no `onRefresh` means no button. `Peek`: `load` runs once the pointer settles (most refs are never hovered), portaled and fixed so panes don't clip it, takes no pointer events, leaves on any scroll. `Files.jsx`: bytes load on click (for email a Gmail round-trip per chip); the title carries extracted-text state. `FileLink`: plain text until the backend (`app/files.py`) says the file is real. `ThemeSpecimen` is a page of its own, not the desk, because the desk opens a console socket and syncs providers and its picture would carry personal data; it uses no requestAnimationFrame because WebKit runs no frames for an unseen window (webshot waits a beat after the ready flag); swatch hexes are the rendered values. `CheckList`: every row drawn (no six-row cut). `Lede`: `rest` is the same string `Slot` shows folded; the lede's own metric leaves the line under it but the fold summary keeps the unfiltered `restLine`; `tail` never leads.

### `frontend/src/ui/markdown.jsx`: the console's markdown renderer

Stateless; one inline pass (`renderInline`) and one block pass (`renderMarkdown`). BriefWidget uses the inline half.
- **One inline matcher, ordered.** Code spans and explicit links come first so their innards aren't re-formatted; lookarounds keep snake_case and `3*4*5` out. Wikilinks, `[text](note.md)`, bare paths, math, citations and fact ids live in the same matcher (not separate passes) so the outer construct claims them whole: a path inside a link href or code span is never linkified twice, a formula's `_ * \[ \]` are never chewed on, and a backticked `$x$` loses to the leftmost code span. `$$` precedes `$`; inline `$…$` needs non-space ends and no digit after the closer (`$5 to $10`); `\(…\)` is the other spelling. A backticked path or fact id becomes the link and keeps its monospace.
- **`SPAN` (bold/strike body) steps over code spans whole** (`**\`char**\`**` must not close inside the code). Its lookahead keeps the alternatives disjoint and is not optional: with both able to match a code span the regex backtracks 2.6x per span (an unclosed `**` with 22 code spans took 10s on the main thread), and every bold line is unclosed while it streams. A boundary can't catch a hang.
- **Citations are matched loosely.** `[cite:…]` is not anchored to the kind letters: whatever the model writes (once a raw Gmail id from a `brief=false` read) must render as a dead citation, never raw markup; `Cite.jsx` renders anything unref-shaped as dead without asking the network. `DOC_CITE_SRC` (`[cite:d3f9a1#p12 "quote"]`, also `[page:…]` mid-sentence) sits ahead of it because it would decline the space. `PAGE_LINE` (alone on a line = a page embed) is strict: only a real doc ref makes a block, looser falls through as a dead pill instead of a page that can never load. Inline keys are character offsets, and offsets collide across answers, so the ref rides in the key (React reused one Cite for another citation; see Cite.jsx).
- **KaTeX loads on the first formula** (over a third of the page's JS); until then raw source shows in the dress `math.jsx` gives unparseable TeX. The import must not reject: `lazy()` caches a rejection and rethrows it every render, so one failed import (a window open across a rebuild asks for a deleted chunk) would blank the desk until restart. A miss costs typesetting only; there's no retry because the chunk is gone from the server, only ⌘R brings it back.
- **Block pass details.** Fences close only on the same marker (``` inside ~~~ is content). `quiz`/`cards` fences become interactive `StudyBlock`s (a quiz as prose gives answers away); unclosed = still streaming. A display formula (`$$`/`\[`, one line or open) is a block; unclosed at the end of the text shows source, not KaTeX's red parse mark per token. Callouts (`> [!tip] Title`) accept and ignore the fold marker. Tables: the first row is a header only if a rule follows; `\|` is a literal pipe (`[[note\|alias]]`). Lists: a marker switch at one level ("- a" then "1. b") starts a new list (merging renumbered the second); indented continuation lines join the item (notes hard-wrap).
- **`renderMarkdownCached`**: same text returns the same element objects so React skips the subtree; the console re-renders its whole log on every keystroke and streamed token and re-parsing/re-typesetting is the lag. Output depends only on text; 600 entries, oldest out.
- `renderUserMath`: a user's prompt stays literal except math-looking spans (`MATHISH`; `$HOME` isn't math); formulas the math field wrote (`known`) count however plain.
- Quiz/card text containing a line break (a code snippet as a ``` fence in the JSON string) takes the block pass (`renderRich`).

### `frontend/src/widgets/` desk cards: Finance, Expenses, Travel, Status, Glance

- **FinanceWidget.** The spend series is zero-filled to today (`finance.py`), so a zero is a real zero (a day you didn't spend, not one the bank didn't answer): safe as a baseline where health's is not. It reads this month (the card says whether today's outflow is unusual and seven days can't support that; same this-month/last-month fallback as the trends card; spans resolve server-side from `db.day_key()`). Money is skewed by construction (most days near zero, rent once a month), the distribution the median/MAD scorer and rarity gate were chosen for; mean/stdev would call the week after payday normal and the rest a drought. Only a bank needing reconnect, or an unusual day's spending, earns the "today" band; `needs_consent` (linked before liabilities existed) is fixed by the same Link update-mode reconnect. The week total rides the Lede quiet line rather than a second paragraph (two lines for one unremarkable fact). Plaid sign is flipped for display.
- **ExpensesWidget.** Charges are claimed from the Money sheet's list and checked here, so amount/category/note are editable (claiming part of a charge is an edit, not a different claim). Both reload on the `expenses` topic; the refresh button broadcasts so the Money sheet's claim tags and reviewed line re-read. `pending`: the claim follows the charge until export; `vanished`: a pending charge dropped without posting (a released hold). `batchDrift` flags sent rows that settled at a different amount or never posted. Only a deposit that looks like the payback wire is NEEDS; waiting or unpaid batches are content, nothing owed. Marking a wire paid is the same POST as the batch's button; undo marks it unpaid. The reopen button is labelled, not a hover icon: resending an unpaid batch with more on it is the normal use. Quick-add grammar "Venmo 24.50 haircut" matches the debts box.
- **TravelWidget.** Deliveries and flights share one card because each is empty most days (two usually-blank cards is the "everything at equal weight" the salience pass ended); a package arriving today would otherwise show only if that morning's brief mentioned it, a flight only in prose. `SOON_MS` (48h) is the brief's Travel horizon. Full weight: a parcel landing today (`eta_day` is a day_key, compare to the backend's day, not a local Date), a flight inside 48h, or anything AeroAPI flagged (delay, gate change, cancellation); everything else folds ("2 in transit · 1 trip booked") because a parcel next Tuesday is real but not actionable today. `dep_local`/`arr_local` are each local to their own airport (`app/tz.py`) so the row shows both clocks and never the span; `+1` compares the date strings. Delivered rows stay three days for returns; both corrections (delivered, drop) reload instead of patching, since a delete can free the merchant's one open order for the extractor's next follow-up match. Drop is for a row that was never a parcel (a promo read as an order, or one parcel twice because confirmation and shipping notice shared no id); no confirm, a later email re-opens it. The arrow in JSX text must be the character: an escape prints six literal characters.
- **StatusPanel.** Doubles as the debug view (last error verbatim, newest cached day, row counts). The fault list is a separate read that must never fail the source list; hidden when empty. A fault is something that already happened (read or clear), unlike a source's state; `count` separates a one-off during a migration from a fault firing every render. Fault timestamps carry microseconds for the backend's trim ordering and are cut to seconds for display. OAuth hand-off goes through the backend (`app/browser.py`) because `window.open` returns null in the desktop shell; the consent poll runs until the source flips and stops on unmount. The pin carries the event log only for sources that aren't ok (14 sources of noise otherwise).
- **GlanceStrip.** No task/to-review/habit counts: the plan view's "needs you" list is that summary, so the strip keeps what nothing else shows; the pin carries the day frame so one pin replaces five cards. Date is the day key, not the wall clock (at 1am the list below is still yesterday's). Leave-by shows only the soonest departure inside 90 min. Sync streams over SSE so the strip names the source it waits on (a bare "syncing…" reads as a hang), then widgets re-read, with no page reload so the console transcript survives. The status chip is one roll-up that replaced per-account chips which only spoke for Google auth.

### `frontend/src/widgets/CalendarSheet.jsx` and `CalendarWidget.jsx`: the calendar

- **Sheet.** A schedule, not a list: the three questions asked of a calendar are what's next, how full is it, where are the holes, so it leads with Next up and countdowns, charts booked hours per day, and draws gaps as rows (a free afternoon is information; a list shows only an absence). Gaps under 30 min (`GAP_MIN`) are the walk between meetings. Wall-clock times come from the ISO string, not a Date, so the backend's per-date zone choice (`app/tz.py`, `lib/time`) survives; durations are real elapsed time and go through Date. The day's midnight uses the ISO's own offset. The provider emits one row per day an event touches with the full start/end, so `minutes()` clips to the row's day (10pm-1am is 2h then 1h); such an event is "next up" once. Banners are all-day events and timed ones over a day long, handed out as all-day with `span` (`providers/calendar.py SPAN_SQL`); a mis-dated 1h appointment once read as 696h booked for the week. Rows carry no duration at the end (the span already says it; the day total is in the header). "Next up" shows only while part of the range is ahead (a countdown to nothing is clutter); a past event has no countdown.
- **Card.** One date per view so one zone: the label sits in the header and is always shown, not only when it differs from this machine's (a time you must remember to doubt is worse than none). A leave-by within `LEAVE_SOON_MIN` (30; the row goes red inside 15) or past with the event not started is the card's one clock-tier ask; leave times are polled every 3 min while visible (`LEAVE_POLL_MS`) and the backend caches Routes lookups 5 minutes, so the glance strip asking costs nothing extra. "Later" on a leave ask offers minutes (`clock`). A spill-over from the previous day carries yesterday's start (raw, the day looked like it began at 10pm), so it shows its end; all-day and spill-over rows get no trailing end. All-day events are never "earlier" (they cover the part still to come). The folded summary is the card, so it carries the next thing on the day; a meeting in progress says "till <end>" (a stub "9:30 AM Standup" at 9:50 reads like something missed), from the same now-vs-end comparison as `left`. Ended events fold away (inline at full weight under a "now" rule, a nine-meeting day was mostly things you could no longer attend); the fold line is the boundary. Switching view clears the payload first so the skeleton shows rather than the other day.

### `frontend/src/widgets/CoursesWidget.jsx`: the office-hours card

- The card answers "who's in right now, who's next" first and keeps the whole week one click behind it; it is reference, not a to-do. The only state that asks something of you is a pending amendment read out of mail (the card is showing hours mail says are wrong), which sets the NEEDS level and an ask with a Confirm action (the card's approve POST). Proposals render above the hours with their quote: every other line is the extractor's reading of that sentence, and the quote makes approving a decision rather than a guess. Reject stays on the card, where the quote is.
- **Approve/undo.** Approve writes the override into the course note; `/undo` takes an approved override back out and the proposal is pending again. Both buttons of a proposal disable mid-decision because approve writes a vault note and a second click before the reload would be a second write. A 409 means it couldn't be applied (note moved, entry unmatched): it stays pending and the message is shown, never swallowed. An add's day is when the weekly block starts, not a one-off date.
- **Amended/cancelled rows.** The label is built from parsed minutes, not the note's raw `when` (the card is a 12-hour clock; "14:00-15:00" beside "1:00 PM" makes the reader convert). "cancelled" is never traded away for the reason (a row reading only "away at a conference" beside a live-looking time was the bug); an amended row's time is already right so the reason is what's worth the space; hours moved onto today from another weekday say "moved from X" because the row otherwise looks ordinary. In the week view the times are the standing rule, so the label carries its date (`dated`) or it reads as a correction to every week, and strike-through applies only to today's rows (`state` is about today).
- **Next.** A bare weekday is only unambiguous inside the coming week; with today's block struck, "Next · Tue" named the block above it. So 7+ whole days out (measured off the day-key, not minutes: next Tuesday after a cancelled one is 6d23h away) gets its date. Called-off hours stay on the board ("don't walk over" is this card's decision) but don't count as live; hours already run out fold away so the live one isn't hunted for. Folded, one open slot quotes its close time (the whole "worth walking over?" decision); two can't share one so they name themselves. Each row shows the close time because 2:00 PM doesn't say whether 2:40 is too late.
- Week view groups by class (the question is "when are CSCI 170's hours"); a class with none posted still appears or it looks forgotten. Only a TA's hours name whose they are. Study mode shows an exam inside two weeks and review counts; `course_context` has the rest.

### `frontend/src/widgets/DaySheet.jsx`: one day at hourly resolution

- Every other health surface answers "how have I been"; this answers "what happened on Tuesday". `health_days` kept per-night stage totals and `health_hours` only heart rate, so a night was four numbers and a day one. It opens from a bar on the Health sheet and is one sheet, not two: a night is keyed by the morning it ended on, so night and day share a key (the sleep you started the day with, then the day). Both halves fill from the API on demand the first time a day is opened and cache (`providers/health.py`, which also holds why a cold day's heart rate can be missing: rollUp caps heart-rate spans at a fortnight, steps have no cap).
- Hypnogram lanes top to bottom: awake above the sleep it interrupts, depth increasing downward. Clock times are read straight off the stored offset (`ts.slice(11,16)`), never through the browser's zone: that is what the offset is stored for. Hour ticks sit on real hour boundaries over elapsed time (segment starts are uneven and collide into "02:0003:00"; a long light stretch looks long).
- Asleep and in bed are shown apart: `sleep_hours` is asleep only, so a night half awake in bed read as a short night. Steps has no average line: a mean over the sleeping hours, or hours that haven't happened, is a reference line to nothing. All 24 hour slots draw (a skipped axis restates the day as shorter), and the number of unmeasured hours is said in the band note; gaps count only up to the last hour anything was recorded, since later hours of a live day haven't happened (it once called a Monday afternoon "16h unmeasured").
- The night and day reads share one provider, so both failing is one error (same rule as `useSheetData`). The effect is the only loader; refresh bumps a nonce; `views.jsx` keys the sheet by day so another day starts loading.

### `frontend/src/widgets/EmailReview.jsx`: the review queue

- One card per inbox message: AI summary, recommended action, filing label. Archive, unsubscribe and keep all file the message under its label, because after review the only thing a message is for is being found. The label arrives picked (the sender's record, else the model's); picking another replaces it without acting (correction and action are one gesture each, either first), and two corrections teach the sender. Everything mutates Gmail via `POST /api/email/{id}/action`; the card leaves on success.
- **Recommendation reason.** Shown in the user's own history's words (`card.why`): a recommendation drawn from two clicks has to admit that before it is trusted (same rule as the place shortlist); `basis == "default"` (unknown sender) is not worth a line. The note is one span: as separate flex items it drifted to its own column when the reason wrapped.
- **Unsub with no header.** The recommendation used to come from the body alone and never saw List-Unsubscribe; it now comes from what you did with that sender (`providers/email_learn.py`), but a learned "unsubscribe" can still hit a message without the header. `unsubDead` substitutes archive so the highlight lands on a pressable button, not an ink-filled disabled one.
- **Unsubscribe is held.** A request that reaches the sender (`one_click`, `mailto`) can't be recalled, so the card leaves now and the request goes out when the undo toast expires (`hold`); undo just brings the card back. `link` only opens a page and goes straight through.
- **Undo stack.** Newest last, so the Undo button can walk back a run of fast archives; ⌘Z belongs to the toast (`lib/undo.js`) so one keypress can't undo twice. The ref is the stack and state only mirrors it: reading the top inside a setState updater runs during render and saw `undefined`, so undo popped the card and returned before sending anything (looked like an undo that lost the mail). Removal from the stack is optimistic so a double press can't fire twice; a failed undo restores the entry. An undo after an already-sent unsubscribe says so.
- ↵ acts on the hovered card (the queue is mouse-first and an acted card leaves, which would strand a selection); only the hovered card listens, and ↵ on a focused control is that control's click. `hide` fires a targeted `broadcastRefresh("email")` for the card and glance-chip count, not in `load()` (which runs on mount and would reload the email card for opening the panel). Refresh is a Gmail sync plus server-side Haiku summaries and can take a minute, so the sheet has no pin or plain refresh.

### `frontend/src/widgets/MapPlaces.jsx`: places on the map sheet

(MapPlaces.jsx, the two panels under the map; the map owns the picked place.)
- **VisitList: which place each visit was.** A cluster is not a place at campus density (the busiest unnamed one spans 168m and holds five buildings) and nothing automatic separates them (`app/visits.py` has the three measurements), so the label comes from the user one visit at a time and the learner picks up the shape (evening-and-long vs midday-and-short). A prediction is greyed in the picker and never stored: it is true when clicked. The calendar event during the visit (28% of visits have one) is offered as a chip to click, never a label: it names what you were doing, only sometimes where. The chip truncates at 22 chars; the label written is the whole name.
- **Weak guesses look weak.** `support` (resemblance to the nearest labelled visit), not `share` (which label won the vote, relative), drives the weight: a 09:00 visit against midday labels and a 00:05 stay against evening ones both take ~100% share while resembling nothing. `WEAK_SUPPORT` 0.35, measured on the hub cluster with six labels: genuine match ~0.9, evening vs evening labels 0.6-0.9, the two bad cases ~0.2.
- **PlaceNamer: nothing applies without a click.** A ranked shortlist fills an always-editable field (never a dropdown: the calendar knows the room "SAL 101" when you want the building). `app/place_suggest.py` measures the top candidate right about a third of the time, and an accepted name changes `time_at_home_min` and every place count after it. Feedback to `/api/places/feedback` is fire-and-forget (a learning signal must never fail the save) and is sent only after the note is written. The note carries no `from`: the first day a place appears is the first day of the viewed range, not when the building went up, and dating it so would stop it resolving earlier fixes; `from`/`until` are for a home that moved and are written in the note by hand (`app/places.py`). `learnedNote` states the ranking's belief in a sentence, and says nothing is learned yet rather than imply it knows; no winner is named off zero accepts.

### `frontend/src/widgets/MapSheet.jsx`: the location map

Every other location surface is a number per day; the map draws the fixes themselves (one path per day, a disc at every stay) so "where was I" has an answer.
- **Basemap from our own origin.** Tiles are fetched once by `app/tiles.py` and cached; the frontend never talks to a tile server (blocked image hosts, localhost app). Panning is free after the first look and a dead network degrades to the vector-only map. Tiles are desaturated at draw time (`ctx.filter`, absent filter = full colour) because osm's standard style is a finished map and the basemap should be paper under the fixes.
- **Integer zoom only.** Fractional zoom needs scaled tile blits and a resample per frame; whole levels keep tiles pixel-aligned, and the thing read is a campus at four or five fixed scales. So smoothing happens on input: wheel deltas are banked and one level spent per `WHEEL_STEP` (90, about one mouse notch; a lazy two-finger trackpad swipe emits ~40 events of deltaY 1-4 and should move one or two levels, not six). A gap over `WHEEL_IDLE_MS` (180) starts a new gesture at zero so the second flick doesn't over-shoot. Pinch (ctrl+wheel on macOS) is deliberate so it uses the lower `WHEEL_STEP_PINCH`. The wheel listener is bound by hand: React's onWheel is passive, so preventDefault is ignored and the dock scrolls under the map.
- **Places are the provider's.** Same stay detection and 3-minute dwell floor as `location.places`; a map that clustered its own way would disagree with the card invisibly. A place has `name` when the vault declares one (`app/places.py`) and `ambiguous_with` when another declared place is too close to tell apart: show that rather than pick.
- **Pins.** The disc area says how long (area ∝ dwell); the pin's number is the row in the list beside the map, so the two halves are one view. `PIN_TOP_N` (8) equals the `.map-list` slice: a number pointing at nothing is worse than none. Pins: the list's places, anything named, the selection (21 pins is a thicket). Name labels draw only where they fit, in rank order, so the longer stay wins a clash; the selected pin always labels.
- Default range is a month: a routine needs several of each weekday (on a Monday "this week" is a map of this morning). The home radius is drawn in real metres per pixel at the view's latitude. Path breaks wherever the phone went quiet: joining across a gap would draw a line through an unrecorded journey.
- Fullscreen goes on the frame, not the sheet (the places list is a separate scroll region); `full` follows `fullscreenchange`, never the click, since Escape leaves fullscreen silently. Only the canvas starts a drag: pointer capture on the frame retargets `click` to the frame and kills the zoom buttons. Naming a place reloads with `keepView` so the pan/zoom survives. OSM licence requires the on-map credit.

### `frontend/src/widgets/PeopleExpand.jsx`: the People slide-over

- Scope: search by name, waiting-on threads, birthdays/watch-due, one person's merged email/message/meeting history, and edits of the only real facts (name, birthday, `watch_cadence_days`, notes) via `PATCH /api/people/{person_key}`. Texts/emails/events only; vault notes are out of scope.
- A waiting row's id is `kind-key-last_at`: a newer message from the same person is a different row and deserves a new draft, so a draft stays attached to the message it was drafted for.
- **Pin payload.** `waitingPin` is shared by the row's pin button and its drag so the two gestures can't send different things. The identifier the row was drawn from rides along (`chat_guid` / `thread_id`, what a `db_query` over `messages`/`emails` needs) because a pin carries exactly one message and that message is a reply: a pinned "here just in case +1 (555) …" reads as the sender offering a second number when the turn above asked for someone else's contact, and without a pointer the console can't find that turn. The field is named `their_latest_message` for what it is.
- **Dismiss vs mute vs not-now.** Mute kills a sender forever (bots, shops). Dismiss writes off one dangling thread and lets the person back the moment they send something newer, which is why `through` is the row's own `last_at`. "Not now" keeps the row (the reply is still owed) and adds a task to today. Pin is the fourth verb: take it to the console when the answer needs thought.
- Drafts are one model call per press (seconds), so the row shows "drafting…" rather than freezing the panel; they open under their row, click copies, and nothing here sends (sending stays in Messages/Gmail). "Reply" for a person hands off through the backend `open-messages` endpoint, not an `imessage://` link, since webviews treat non-http schemes differently (pywebview window vs browser tab).
- Only the detail head is the drag handle: a draggable panel would take text selection away from the history, the thing read here. The edit form is keyed on the stored values it copies, so a save resets it and a refetch that changed nothing leaves an in-progress edit alone. Search results guard with `stale()` so a slow "jo" can't land over "john".

### `app/console.py`: context pills (`build_prompt`, `PILL_LIMIT`)

A pill is built from whatever the widget was holding, and some slices dwarf the context window (a month of messages serializes to ~538KB). The frontend shapes them (`pin.js`) but is the part a future widget will forget to call, so `build_prompt` is the floor: it caps a pill at `PILL_LIMIT` and a trimmed pill announces itself with a marker, because a silently cut list is worse than a refused one (the model totals it and reports a confident wrong number). The marker is the load-bearing half of the cap. Labels keep pills distinguishable (the model learns which slice a number came from). Pills are serialized with `default=str` so a datetime or stray object cannot take down the send. A dropped file is a stranger's PDF, so it goes in an untrusted fence, never a `<widget>` (which carries the dashboard's own standing). Attachment decoration matches rows by the carrier's own key (chat.db `guid`, Gmail `id`; the email queue passes `key="id"`), filters by source (the same ref string can exist in two carriers), and `COALESCE`s a NULL mime (`NULL NOT LIKE` is NULL, which dropped Gmail parts that arrive without a mime).

### `app/food.py`: test-pinned behaviours

- Suggestions on an empty day once put three entrees and ~117 g protein on one lunch plate because the day still needed it: a single plate is capped regardless of day need.
- A Kestrel-created food at a seeded venue (e.g. "Orange Chicken" at Panda) must land on the seeded row, not beside it. Panda's label (confidence 1) outranks model recall (0.95), so label numbers stay; user-typed numbers (confidence 1) land, and thereafter the seed leaves them alone. Panda publishes macros only: the estimator fills vitamins/minerals, never touches label numbers, and doesn't re-estimate every cycle; drinks are skipped.
- When kcal is fixed by the user (e.g. 230), unstated macros share what the stated ones leave rather than keeping the old 24 g fat / 17 g carbs (400 kcal of macros); history is kept.
- Maintenance estimate follows the scale: logging 2000 kcal/day while gaining 0.25 kg/week implies ~2275 (logging runs low or burn is high); the formula (light, 70 kg, 178 cm, age 20) gives ~2400 vs observed ~1725. A custom food with only macros must not read as "zero vitamin C" (unknown, not zero).
- The Dining Dollars "under" warning fires when the pace ($14.29/day over the last 7 vs a $40/day allowance) means the dollars will be forfeited.

### `app/health.py`: night and hourly drill-downs, test-pinned

- Both fill on demand then cache; what is stored matters more than what is drawn. A segment stores its UTC offset: a machine that later moves coasts must not restate a night in the zone it stands in now (the terrain-sheet bug at one night's scale). Example: 2026-07-20 04:00Z with the desk on the east coast is midnight ET on the 20th, but would file as 21:00 on the 19th in Pacific.
- `asleep_min` is minutesAsleep (light+deep+rem); time in bed was never on screen so a restless night looked like a short one: the night shows both.
- Absence is recorded (a night with no data is still a night asked about), or an empty night re-hits Google on every open (same reason `health_sync.refreshed_at` exists). `_sync` writes the window whole; a night that gained a nap must not hold both readings.
- Steps and heart rate share `health_hours` over different windows, so the delete is scoped by metric or each sync evicts the other's rows. A gap hour is `{"hour": h}`, not zero.
- The hand-written routes read health directly, not through `providers.get()`, so they open `offline.collect()` themselves; otherwise `best_effort`'s "network down, serving cache" note had nowhere to land and an offline-fetched night looked fresh. `ValueError` (malformed day or range token) is the caller's fault and `_serve` answers it as such.

### `app/health.py`: refresh failure modes, test-pinned

- Before the retry work (2026-10), ~8% of refresh cycles died on one read timeout/reset/503 among eleven sequential calls, discarding every metric's window. Now a transient blip is retried and a series that still fails costs only its own metrics.
- Google sometimes rejects a token its expiry still vouches for; a 401 never landed, so even a non-idempotent POST is safe to resend. A dead resolver half a second later is still dead (offline.py serves the cache).
- A partial failure marks nothing, so the next `get()` asks again instead of trusting a day with no heart rate (a mark would claim heart rate was fetched). When `best_effort()` swallows network errors, a 401 beside a timeout is the error raised, or the expired credential hides behind stale data.
- The nightly trio may not exist for a given watch: the API refusing one is "no data", the sync completes, and it no longer wipes that metric's rows. A metric the source stops reporting for a day goes away (the scoped delete covers every series that answered).
- `_auto_exercise` (AZM Exercise check-in): volume alone is not a workout (a long stretch of merely-elevated HR can bank more AZM than a real session: alcohol, illness, stress); the peak-HR gate is a requirement not a tiebreak (no cached `hr_max` means no check-in). Zeroing the peak threshold drops the gate to AZM-only, including for days with no `hr_max`.

### `app/lists.py`: user lists, test-pinned

- v41 turned the shopping list into `lists` + `list_items` (four routes became seven; the list is a user-made, renamable, deletable row). Pinned: v41 seeds one list (an empty Lists card would make the first add a two-step ritual); a packing list and a buy list stay separate; chip order is creation order; FK cascade on delete (orphan items are unreachable: no chip shows them); a duplicate name is refused before it splits one list into two chips; the payload stays one fetch.

### `app/localguard.py`: same-origin through a loopback dev proxy

The verify rig's Vite on :5199 forwards with its own Host (`changeOrigin` off in vite.config.js), so its pages are same-origin through the proxy and may write. Same-origin only counts on a loopback Host: a direct hit with another local port's Origin (Host :8788), a proxy hit with a foreign Origin, and a rebound name whose Origin matches its own Host are all refused.

### `app/macos.py`: osascript/open wrapper

Every subprocess has a timeout: `TimeoutExpired` surfaces as the caller's error type (`reminders._run` once let it escape past the `ReminderError` callers catch when Reminders hung on launch), and `open -g` has one because a wedged LaunchServices hung the request thread forever (vault_views ran it with none).

### `app/model_catalog.py`: test-pinned behaviours

- Pinned: the access filter (a Pro account asking for Fable gets Opus with a note, in open_tab, the console and agent.ask alike), the fallback (a retired id or a whole family gone lands on the newest left), alias resolution (newest by release date, deprecated out). A spent usage window says nothing about access. A Fable not yet known off-limits is refused by the CLI, retried on Opus, and the usage breaker stays shut; the refusal is learnt so later calls go straight to Opus.

### `app/providers/health.py`: `_auto_sleep`

`_auto_sleep`: a cleared night (value=0 tombstone) stays cleared even when the hours qualify; the auto pass never resurrects it.

### `app/providers/weather.py`: backfill horizon

`weather.get()` only backfills days Open-Meteo can answer for. A gap in `weather_days` past the API horizon (more than `MAX_PAST_DAYS` back, or beyond the `MAX_FORECAST_DAYS` window) can never fill, so counting it as missing made every read of a long range a live call on every page load. A fetched day with a null metric (`precip_prob`, which the real `_fetch` drops) is not refetched on every read.

### `app/tasks.py`: tasks.md follows every write

- The view was once regenerated by each REST route and console tool by hand, so tasks opened by a provider sync (a bill due, a flight check-in, a note-to-self) or by the People panel's remind button left tasks.md stale. It now regenerates on any task write. The `dbfile` test fixture redirects `db.APP_DB` but leaves `VAULT_PATH` on the real vault, so a regenerated view there would overwrite the user's real tasks.md with fixture tasks: tests that go through `dbfile` must point VAULT_PATH elsewhere.

### `app/usage.py`: desk usage counters, test-pinned

- Counters judge whether salience.js is right; naive versions lie in three ways. A card read at a glance is never clicked, so an untouched card must not look dead: a card that had something to say every day and was never clicked is working (an interaction-only metric would file it with the dead). A card that appeared last Tuesday must not be scored against a month it wasn't there for (three days of history convicts nothing: a floor). The window is measured back from the newest recorded day, not the wall clock, or a fortnight with the backend off makes the whole desk look abandoned.
- Rows are per (day, kind, label, band), not per event: the desk flushes a buffer all day. A card going quiet -> needs in one day is two rows (collapsing erases the distinction that keeps this honest). Unfolding a folded card means the ranking got it wrong: the strongest signal, one is enough to spare the card.
- The cut list surfaces the bottom (not buried under obvious cards). A cmd-K command has no band/card and would add a `days_seen=0` row among cut candidates; `status`, `review` and the usage sheet are openable but not desk cards (permanent 0-of-0 rows), so both are excluded. Telemetry is not load-bearing: a broken DB costs a log line, never a 500.

### `app/vault_open.py`: resolving console-said note names, test-pinned

- The console names files in four shapes (wikilink, wikilink with alias/heading, vault-relative path, absolute path); all must reach the same file before anything is handed to Obsidian. A name that isn't a note must miss, never fall back to something nearby: a link opening the wrong note is worse than one saying not found (`README.md` exists in several notes, so a bare name fitting four fits none). Nothing outside the vault resolves, whatever `..` or a leading slash do. Alias, heading and block id are display/position, not part of the name. Obsidian opens attachments too (the console links a PDF it just filed), so only the index name fallback is markdown-only, since only it guesses; a bare attachment name does not resolve without its folder. The error message is the link's tooltip, so it names the problem. Obsidian is launched with `-g` so the click doesn't pull focus off the dashboard when Obsidian is up on another space.

### `app/weather.py`: current() fallback, test-pinned

- The glance chip went blank on four separate days (per `failures`) when Open-Meteo answered 502 and the route returned its own 502. Fix: retry once (the 502 is transient), then serve a cached reading marked `stale` with `as_of`; the cache is never passed off as live. A reading too old to mean anything is not served at all (a blank chip beats an hours-old temperature shown as now); the route's 502 stays reachable for the genuinely-nothing case.
- The fallback returns a copy; mutating the cache in place would flip its own `stale` and lose `as_of`, so a second outage would serve a reading that no longer knows when it was taken.
- The cache was once fillable only by `current()` (a page load), and a served-stale reading expires after an hour, so the fallback covered a blip on a repeat visit but never the first load of the day (when all four 502s landed). `refresh()` (poller cycle) therefore also takes the reading; it is a passenger of the day window: a reading failure is reported (`refresh()["current"] is None`), never raised, since failing the cycle would trade a blank chip for a stale forecast.
- Page-load path: upstream costs ~0.8s and moves in 15-minute steps, so a reading the poller just took is returned as live. Origin follows the latest location fix (another city's reading is wrong however young). A 200 with no `current` key is a proxy error page shape and must reach the cache fallback, not return a payload of Nones. `current()` memoises into a module global (tests reset it).

### `backend/desktop_shell.py`: macOS window shell

The AppKit bits pywebview doesn't give: a real menu bar, window frame memory, microphone, notifications. macOS-only; the AppKit import is guarded so elsewhere (or without PyObjC) the launcher runs as before.

- **Menu bar.** pywebview's `MenuAction` takes no accelerator (click-only), and ⌘R/⌘Q are most of the point, so the menu is built against AppKit directly, scheduled onto the main runloop (pywebview runs `start(func=…)` on a worker thread and AppKit is main-thread-only). Edit items carry a nil target so they travel the responder chain to the web view and act on its selection. ⌘Q is routed to the same close path as the red button, not `terminate:`, which would skip the uvicorn drain and the process-group sweep. Settings opens `backend/.env` in the editor (config lives there). `_Actions` (the ObjC target for Python callbacks) is held in a module global: a menu item's target is weak and a collected target silently greys the item out.
- **Frame memory.** Reopening at 1280×860 centred every time reads as a script. `load_frame` falls back to the centred default when the saved frame isn't reachable on a current screen (an unplugged external display leaves macOS happy to open an unreachable window); the check is the top-left corner within each screen (8px slack out, 40px in), not full containment, since hanging off the right edge is fine and an ungrabbable title bar is not. `track_frame` mirrors geometry from the resize/move events and writes on `closing`; polling `window.x` at close reads a frame already being torn down.
- **Microphone.** WKWebView routes `getUserMedia` through a `WKUIDelegate` method pywebview (6.2.1) doesn't implement, and an unimplemented method is not a denial: the promise never settles, so the voice console hung with the mic lit and no error (a browser tab works). `allow_media_capture` adds the selector to pywebview's delegate class, guarded so failure reports and returns False (a mute app is degraded; a crashed launcher is not). It answers Grant (`WKPermissionDecision` 1) not Prompt: macOS asks for microphone access itself the first time per app, which is the real consent, and the page asking is already a deliberate act.
- **Tab to every control.** WKWebView defaults to Safari's "Tab highlights text fields only", so in the app buttons weren't keyboard-reachable (a browser tab or Playwright Chrome hides this). `setTabFocusesLinks_` on the shared WKPreferences, set once the window exists, takes effect like pywebview's own `developerExtrasEnabled`.
- **Notifications.** `UNUserNotificationCenter` makes banners Kestrel's own (name, icon, its own Settings row, a click that returns here); osascript's `display notification` is attributed to Script Editor (click opens Script Editor, permissions under its name). Found by trial on macOS 26: the process must run from a bundle registered with LaunchServices and code-signed (ad-hoc is enough; unsigned gets UNErrorDomain 1 "Notifications are not allowed for this application"; `scripts/make-app.sh` signs it); a bundle under /tmp is refused even when signed; outside a bundle the center throws, so it is never touched there. `Notifier` falls back to osascript whenever the center can't be used (not a bundle, unsigned, permission denied), and says why once at launch, where the log is read for "why no banners". The delegate is held by `Notifier` (the center holds it weakly); Kestrel in front still shows the banner (the page asks only when it isn't looking, and by delivery that may have changed). Banners group per tab in Notification Center (thread id = target). A test bundle swaps `AUTH_OPTIONS` for Provisional (quiet delivery, no prompt). First launch of a newly signed bundle shows macOS's one-time permission prompt.

### `backend/evals/`: tool_choice and code_mode

`tool_choice`: batches `cases.json` (message plus every acceptable first tool) and asks Haiku which tool it would call first, given the real system prompt and tool catalog. A deliberate proxy (Haiku on a text catalog, not Sonnet on live schemas): ~100k Haiku tokens a pass against the shared window where a real replay costs many times that; weaker and cheaper is the right way round for guarding descriptions, since what survives Haiku isn't relying on a strong model to guess. Routing only, not argument correctness. Answers are a JSON object keyed by message number: a bare array was positional, Haiku sometimes drops a line (13 answers for 14), and when it doubles one elsewhere the count matches while every later answer is scored against the next message. A missing key is a re-ask (3 tries). Only the first JSON object is parsed (some answers trail a second: "Extra data"). `mcp__vault__<tool>` normalises to the tool name. `use_computer` lives in the general tab's server, outside `TOOLS`. `--descriptions` overlays `{tool: text}` without editing console_tools.py.
`code_mode`: runs each question twice through `console.build_options` (general profile, default model, real prompt, Brain hook, vault server): as shipped, and with `run_script` removed from the server and its prompt line cut. Reads auto-approve, anything needing a card is denied, so nothing is written. Arms run concurrently so both see the same window state. Reports medians (one wandering run doubles a mean at n=3); "pre-script calls" are calls before the first `run_script`, excluding ToolSearch.

### `backend/launch_app.py`: the native launcher

Owns the whole lifecycle of the pywebview window: rebuild `frontend/dist` if stale, start the API server as a uvicorn subprocess, open the window, and on close shut down everything it started, including the console's `claude` CLI processes (SDK-spawned grandchildren that nothing in the ASGI shutdown reaches). No browser, no Vite. The location listener is deliberately out of scope: it is a `KeepAlive` launchd agent because location must record whether or not the window is open. Normally run via the generated Kestrel.app (`scripts/make-app.sh`); by hand: `python launch_app.py` from `backend/` with the venv active. Needs pywebview and node/npm; env comes from `backend/.env` via `app.config`.

- **Frontend build.** `ensure_frontend_built` runs at launch and on every ⌘R (git pull, branch switch, hand edit all land the same way; a window left open across a commit is the case launch alone missed). It compares the newest mtime under `SOURCES` (`node_modules` excluded: `npm install` touches thousands of files that don't change the output) with `dist`. A build is ~300ms; the check is a stat walk, so the fresh case is free. A lock serialises it: a second ⌘R mid-build would run a second `vite build` over the same dist; waiting on the lock it finds the fresh dist and returns. A failed build serves the stale dist (one commit behind beats a blank window); the log says which.
- **Server is a subprocess, not in-process.** In-process (`uvicorn.Server(...).run()` on a thread) cannot reload: a reloader restarts its target process, which in-process would be the window. The cost is one more process to shut down, which the exit path already handled for `claude` grandchildren.
- **No `--reload`.** uvicorn's reloader restarts the instant a file changes, and the console runs inside this server: a dev tab editing `backend/app` would restart the process hosting its own turn (socket drops mid-edit, the next approval card dies "Stream closed"). `Backend` watches `backend/app` itself (not `backend/`, which holds `data/` with app.db and its WAL rewritten constantly, and `.venv`), settles `SETTLE_S` (0.5s) after the last write (an editor's temp-and-rename, a multi-file change), then holds while `/api/console/busy` reports turns in flight, and restarts once for however many edits piled up. `MAX_HOLD_S` (3h) is only a backstop for a server that keeps answering busy: `/api/console/busy` stops counting a turn that has gone quiet (`console.TURN_QUIET_S`), which is what frees a wedged one, and a working turn must never reach it because a restart cuts the turn. A server that cannot answer (crashed on a bad edit) counts as nothing to protect. Consequence for a dev tab on this repo: its backend edits take effect when its turn ends, so a curl inside the same turn sees the old code; pytest doesn't care. Frontend edits still need ⌘R.
- **Server env.** `HELD_RELOAD_ENV` marks that edits are held, which `console.py` reads to tell a dev tab (a dev tab on a terminal `uvicorn --reload` drops on every save, and that server is otherwise invisible). `merge_guard.GUARD_ENV` says this launcher runs the merge guard's check at restarts, so a merge may hold its push for it. `NOTIFY_FD_ENV` carries the write end of the notification pipe, inherited by this one child (`pass_fds`) and no further; any value inherited from whatever started the launcher is popped first. stdout/stderr are inherited so uvicorn's log lands in the app log, including the `[reload]` lines that tell a stale window from a stale server.
- **Interpreter.** The server runs under the venv's python, not `sys.executable`: from the .app that is the bundled stub, which gives the process its name and Dock icon, and the reloader spawns the server as a child, so using the stub would put a second Dock icon on screen on every restart.
- **Taking over the port.** A dashboard backend this app didn't start (a stray terminal `uvicorn --reload` can sit under the window for weeks, restarting on every dev-tab edit) is stopped (SIGTERM then SIGKILL) rather than attached to: attaching means ⌘Q leaves it running, and racing for the bind lets the loser exit while the health check passes against the winner. `_port_holders` recognises a uvicorn serving `app.main:app` plus the reloader parent of a `--reload` worker (the worker's own command line is a bare multiprocessing stub); anything else holding the port returns None and the launcher refuses to start over it.
- **Health wait.** `_wait_for_health` ends at once when the process is dead (an import error in `app/` exits uvicorn in under a second; don't spend the other 29s asking a socket that never opens). `/api/healthz?db=1` additionally proves the server can open app.db, i.e. its migrations ran; used by the merge-guard restart.
- **Notification pipe.** `_notification_pipe` returns the write end of a pipe the server writes banners into (`app/notify.py` `_post`, one JSON line each), read here and posted by the window process, which is the bundle, so banners are Kestrel's and not Script Editor's. The launcher keeps its own copy of the write end open so a restart never reads as EOF and one reader serves every server for the window's life. The pipe and `Notifier` exist before the server (its first flush already has the pipe) and before the window. A click brings Kestrel forward and dispatches `kestrel:notification` with `{tab}` to the page (PlanApp.jsx), via `evaluate_js` on its own thread.
- **`evaluate_js` threading.** Menu actions run on the main thread, and `evaluate_js` blocks on a semaphore only the main thread can release, so called from a menu action it deadlocks the window (⌘R froze the app until force-quit). Always off the main thread; ⌘R rebuilds first on a worker thread, then reloads.
- **Window.** `text_select=True`, or pywebview injects `body { user-select: none }` and nothing can be selected or copied (the Edit menu items act on the responder chain and need something selectable). `private_mode=False`: pywebview's default erases web view data each time the window opens, so localStorage (tab list, each tab's session, every pref) would survive ⌘R but not ⌘Q. `target="_blank"` links already go to the system browser via the backend's `OPEN_EXTERNAL_LINKS_IN_BROWSER` default. `allow_media_capture()` runs before the window exists (the delegate class is patched once; the window built later picks it up). The frame (`window.json` in DATA_DIR, per-machine state, gitignored by name since DATA_DIR itself isn't) reopens where it was left.
- **Shutdown order.** ⌘Q takes the same path as the red button (`window.destroy()`, `webview.start()` returns, `main()` runs the one shutdown sequence). `backend.stop()` first (a held restart can't start a fresh server behind us), then SIGTERM the server alone: uvicorn drains the console websockets, which gives `ClaudeSDKClient.disconnect()` its chance to end the `claude` children; a group signal would hit those grandchildren at the same instant. Then `_reap_process_group` SIGTERMs (1.5s grace) then SIGKILLs the stragglers one pid at a time, not with `killpg`, which cannot spare the caller (the SIGKILL sweep would take this process down and turn a clean exit into a crash). Only safe because `main()` called `setpgrp()` first (its own group, so shutdown never reaches anything already running at start); if `setpgrp()` failed the sweep refuses to signal, since signalling an inherited terminal group would kill the launching shell.
- **Merge guard at restart.** `Backend.restart` ends the old server, and when `merge_guard.pending()` says a merge added a migration, snapshots app.db while nothing writes, starts the new server and requires it to open app.db, rolling back otherwise; the outcome is posted as a banner. A failure of the check itself never leaves the window without a server (restart it if dead).

### `backend/scripts/antigravity_home.py`: a home of its own for `agy`

agy keeps its Google sign-in in the keychain (go-keyring, which runs `/usr/bin/security`). Opening the login keychain to the sandbox would open every item that trusts `/usr/bin/security` too (gh's GitHub token, the Cloudflare token, Claude Code's own login). So Kestrel runs agy with HOME set to `registry.AGY_HOME`, which has its own empty-password keychain (no lock timeout: a locked keychain stalls a headless run on an unlock dialog) holding agy's token only; the sandbox reaches that folder only. The script copies an existing sign-in from the login keychain if there is one (the secret goes through `security -i` on stdin, never argv), else starts agy in that home for a browser sign-in. "Signed in" is the keychain item or the token file agy falls back to when a keychain write fails (`registry._agy_signed_in`).

### `backend/tests/` email and delegate test rationale (tests_a_4)

Reasons removed from test comments in `test_email_learn.py`, `test_email_review.py`, `test_email_draft.py`, `test_email_body.py`, `test_docs.py`, `test_delegates.py`, `test_delegate_boundary.py`, `test_expenses.py`:
- **Email learner.** `email_triage` once recorded 931 decisions under five actions; review now offers three, so the learner reads the retired `delete`/`bin` as `archive` without rewriting the record (a sender deleted and archived by turns was a coin flip, now plainly archive). Agreement below the 0.75 floor declines. A domain needs three decisions and never overrides a known address. Two corrections move a sender's label prior (a majority would take three more). `ensure_warm` distinguishes a dropped aggregate table (non-empty record, empty aggregates: rebuild) from a cold start (no decisions: no rescan each queue render). Undo retracts a decision, otherwise it would teach the opposite.
- **Email review.** The label set is closed because a model inventing labels forked free-form bins into near-duplicates; `terms` NULL marks a message owed another summarizer pass, so an invented label still yields `""`. One-click unsubscribe falls back to the header's `mailto` when the POST is refused (loops.so behind Cloudflare 403s it); urllib's default UA is on Cloudflare's banned list (error 1010), so the POST sends its own. Search ranks a subject hit above a newer summary mention; syntax characters are words, not operators; the FTS index rebuilds itself when the cache moves, not by each writer remembering.
- **Email draft.** Gmail threads on `In-Reply-To`/`References`, not `threadId` alone (a reply with only `threadId` opens a fresh thread for the recipient); `References` carries the whole chain oldest-first. Blank `""` optionals fall back to the parent's sender (a model filling every field would otherwise send an empty To). A revision passes `replaces` so the draft is rewritten in place; a second `email_draft` call once left the first (with its `[USC ID]` placeholder) in Gmail beside it. Nothing may reach `messages/send`.
- **Email body.** Gmail's attribution wraps over two lines and arrives CRLF; the quote-stripping pattern must match both or it leaves a dangling "On ... wrote:". `fetch_body` raises (an empty string is indistinguishable from an empty email); `body_excerpt` degrades to `""` so refresh callers survive one unreadable message; a Gmail 404 (message gone since caching) is an answer, 5xx an outage (502). Proofpoint-wrapped URLs with `*` (characters held in the token) are left wrapped because unwrapping gives a dead address.
- **Docs.** The worker keeps blank pages in their slots (dropping one makes every later citation a page early and the highlight misses). Per-page text exceeds the 64KB pipe buffer, so the parent drains stdout while the child runs (it once read only after exit, so a blocked child sat until the wall clock killed an already-finished parse). `doc_pages` results restate the ref and `[page:ref#pN]` outside the untrusted fence (a live session read pages 31-43 and cited none, since the result carried text but not how to point at it); a PDF opened by path is kept like a drop with the same ref so handouts are citable. A document printing `ref="d…"` stays inside the fence: the ref is ours, the text the file's.
- **Delegates.** agy once spent five minutes screenshotting a business card, so a capped agent is told its limits and to write the file early (Claude's single turn isn't). agy's `acp` stderr's last line read as a model name for a bad model, so `explain` names the fix. agy moves its token to a file when a keychain write fails (two runs refreshing at once), so the token file counts as signed in. `codex --json` names no model, so an account run passes the lowest-priority listed model from its catalog and records it; a named model wins. A workspace with the Codex CLI disabled leaves codex signed out with nothing on disk to say why, so the row always offers both sign-in ways. An Edu workspace reports windows and a null balance: no credit count is invented, the credit stop is flagged unable to apply, Kestrel's own counts stand in.
- **Delegate boundary sandbox.** A turn's tool calls run after the line that closes it, so the turn cap stops at the next turn's line and the last allowed turn's writes land. SQLite lstats each path component, so a config dir's parents get `file-read-metadata` literals and no more. Sign-in port tests use the real codex ports: seatbelt refuses ~1 in 8 literal-port allows (see `boundary.login_profile`), so random ports flaked; seatbelt checks a bind before the kernel, so EADDRINUSE means allowed and only EPERM is a refusal (1455 is busy while a real codex sign-in runs). The login keychain stays unreadable (as a file or via `/usr/bin/security`) even with agy's home open and even from a process pointing HOME back at the real one.
- **Expenses.** A claim follows its pending charge when Plaid posts it (a tip changes the total; the posted row shows claimed); a sent claim keeps its exported amount and flags the settled one; `follow_posted` skips a posted id already claimed instead of raising UNIQUE inside finance's write transaction; claims outlive `fin_transactions`, which is droppable.
- **Scheduled send.** The draft lives in Gmail, the time in `app.db`; at `send_at` it sends exactly once, a hand-deleted draft is a cancellation (not a retry loop), nothing goes early, and failures back off rather than hammer. `email_days` marks a day synced only once it has happened: a this-year read would otherwise mark the rest of the year with empty results, and a marked day is never backfilled.
- **Travel card.** `/api/packages` returns the backend `day_key` (eta_day is one; a browser midnight disagrees until 4am); `/api/trips` defaults to a year-long horizon for the card while the brief keeps 48h; `?hours=` is clamped. (The Screen card's tests went with that provider: its honesty rules were centering on the newest day with data, keeping a day with no data as a gap not a zero, and never pulling from aw-server during a render. They are in git history if a screen source returns.)

### `backend/tests/` small modules: why each exists

- `test_agent_budget.py`: the budget gate pauses background work only (interactive calls pass); never on a window that already rolled over; fails open with no reading; a paused call spawns no CLI. The interactive flag must survive the hop to a worker thread (Regenerate, the attachment tool). `blocked()` is called from routes, so it must never do the 10s urlopen (ask(), on a thread, may).
- `test_backup.py`: the failure replaced was the copy never running (a correct script with no schedule left app.db unbacked-up for 59 days), so the assertions are about when it runs. A month key read off the wall clock instead of `day_key()` files a 1am-on-the-1st copy under a month the due-check isn't asking about, taking another copy every cycle until 4am; a half-written file whose name claims the month is the one failure that turns a backup into its absence. Tests restore the disk with a direct setattr, never `monkeypatch.undo()` (per-test, it also reverts `dbfile`'s `db.APP_DB` and backed up the real database once).
- `test_bash_check.py`: blocked commands are copied from transcripts and each failed; passes are near misses. The hook runs on whatever python3 the session PATH has (macOS's 3.9), so it is also run under that.
- `test_brain.py`: `apply_ops` is the only guard between a hallucinated fact (ingest hands the model a year of texts) and a page read every session: cited line in batch, quote in that line, rejected claims never return. Fact ids print as `[f815]` and the model passes "f815": an integer-only schema refused `brain_verdict` before the handler (which strips the f) ran, so every fact-id parameter takes both forms. Claude Code's auto-memory must stay off or it drifts from the Brain (it held a timezone rule the app made false).
- `test_brief_line.py`: a row's `why` grows on its own (the Messages-can't-be-read suffix), so length is clipped, not refused: a 422 hid the whole bar.
- `test_calendar_dedupe.py`: Google issues two ids for one event when an invite arrives by two routes, so the `(day, event_id)` PK can't collapse them and the widget rendered the same class registration twice.
- `test_calendar_free.py`: the console used to answer "when am I free" by eyeballing a day's events; traps: an event started the night before, a day whose zone isn't this machine's, an all-day banner marking a whole day busy. Merging busy blocks needs `max()` (a nested short meeting would move the cursor backwards). A mis-dated 1h appointment (end a week later) took every waking hour and inflated the Load chart to 696h a week, so a timed event over a day long frames the day like an all-day one. Fixtures are Eastern clock times while the real zone table puts the user on the west coast in August, so the zone is pinned.
- `test_calendar_rename.py`: the name used to live only as a copy on each cached event row, so renaming "Gym/Sports" reached only rows inside refresh()'s [today-7, today+35] window; colder rows kept the old name (`calendar_ids` listed both against one id) and the fix was a hand UPDATE. The `calendars` table is the name's home and each sync drags copies along. An empty calendar must be listed (a SELECT DISTINCT over events can't see it, and you need it named before filing onto it). With `GOOGLE_CALENDAR_IDS` set the pull is a subset by configuration, so pruning to it would empty the table. `_normalize` keeps the description (it distinguishes two "Lift" calendars on an approval card).
- `test_activity.py`: a file that creates tasks on its own must record them for the activity card; user-initiated call sites (click or console turn) are exempt.

### `backend/tests/conftest.py`: test isolation

`dbfile` redirects `db.APP_DB` to a copy of one app.db migrated once per session (migrating from nothing is ~17ms: WAL switch plus every migration, and half the suite opens a DB, so per-test migration was about half the suite's wall time). The template is checkpointed and closed before anyone copies it, so the main file alone holds the schema (no -wal to carry) and its header already says WAL. A copy's path is new to `db._ready`, so its first `connect()` still runs `_enable_wal` and `_migrate`, each a cheap no-op. `fresh_dbfile` is for migration tests (`test_migration_race`) and file-creation tests. `freeze_day` keeps the `dt=None` parameter because production calls `day_key(dt)` and a zero-arg stub TypeErrors there; `frozen_day` exists because tests seeding fixed calendar dates and reading back through a trailing window (habit_history, streaks) otherwise pass only until real time walks them out. Router tests use a bare FastAPI so the real lifespan (vault indexing, provider poller) never runs; `client` stubs `regenerate_tasks_view`, which would write into a possibly absent vault.
Autouse isolation, each an isolation bug rather than a convenience: the usage budget gate reads real usage windows (OAuth fetch, `~/.claude.json`), so a test's model call would pass or pause depending on how much of the week the developer spent (`test_agent_budget.py` patches its own); `fake_keychain` stops a sign-in test stashing or swapping the developer's real Claude login and rewriting the CLI config; `empty_model_catalog` stops readers opening the real app.db (`test_model_catalog.py` restores them); `no_price_fetch` stops the ~5MB LiteLLM price file being pulled (`test_model_prices.py` supplies payloads); `no_location_relay` unsets the relay because config loads `backend/.env` and a set `LOCATION_RELAY_URL` would make every `location.refresh()` pull from, and ack (delete from), the real relay; `empty_index` points `indexer.session()` at an empty in-memory notes table because vault-index readers (`places.py`, `courses.py`) otherwise open the real `index.db` and results depend on the developer's vault (two declared homes once made `test_never_left_home` fail: the config fallback it asserts on had been replaced by a note). A test that wants notes patches `indexer.session` itself, which runs after the fixture and wins.

### `backend/tests/test_agent.py`: the failure path of agent.ask

When a turn fails the CLI emits a result message and then exits non-zero on purpose; the SDK replaces that bare exit with the result's error text, which for an API failure is the result's `subtype`, the literal word "success". The desk read "Brief failed: Claude Code returned an error result: success" for a spent usage window, naming neither cause nor fix. So the result message wins over the exit that trails it, and a genuine crash with no error result still propagates untouched (losing a real traceback to this translation would be worse). The swallow is scoped to the exit that trails an error result. The breaker exists because after a 429 every caller (triage each poller cycle, the brain catch-up, a re-asked brief) retried on its own clock and each retry spawned a whole CLI to be told 429 again. `_reset_hint` reads caches that can be half-written or hours old; a stale "rejected" window (reset passed) says nothing rather than promising an expired deadline. The CLI's own "You've hit your session limit" has no 429 status and used to read "the model call failed (success)" and trip nothing.

### `backend/tests/test_alarms.py`: reconcile and delivery

`plan()` is pure and takes its clock as an argument: the dangerous half of the feature is deciding what to withdraw, testable with no database, writer or real time. Sources report three states (alarms / confident-nothing / unknown); an unreadable source holds its alarms (a failed read yields no alarms and would otherwise cancel real ones, cf. `_RETENTION_GATES`), a confident nothing cleans up, partitioning is by key prefix so a broken flight read can't take package alarms down, and a kind declared in `KINDS` with no builder is unknown. Keys: a colon in a part would forge a kind boundary on read-back; `order_key` is `merchant:order_id` so it is escaped, injectively (stripping would collapse two packages). Time: fire_at is an instant (a wall clock shifts across a TIMELINE move and rows outlive one); flight dep_local is read in the airport's zone, not `zone_on()` (user in Los_Angeles, RDU 18:00 Eastern would put check-in 3h late); package ETA days have no clock, so 9am in the user's zone that day. `compute_leaves()` swallows calendar failures into [] for the card; the alarm source must not read it that way (an auth hiccup would withdraw every leave alarm), and unknown is per-event. Deadband: a leave_by moving ~2 minutes is ignored (an amendment withdraws and rewrites the phone alarm) but a flight's stated check-in time is amended even for 2 minutes. Delivery: withdraw-before-write is a correctness property; an amended alarm is re-queued by clearing `written_at` (an existing external_id would read as done and leave the old time); with no writer the row is kept (deleting the only record strands an alarm that can't be withdrawn); a failed withdrawal keeps the row; a reminder deleted by hand counts as withdrawn; AppleScript dates are wall clock so fire_at is converted; Reminders -609 (windowless app quitting mid-exit) is retried and a wedged instance answering -609 repeatedly is killed and relaunched. Past alarms are reaped, not withdrawn (ensure_checkin_tasks covers the task board).

### `backend/tests/test_attachments.py`: containment tests

The failures here are silent: a sandbox that stops enforcing a limit still returns text, and an auto-approve rule that widens still answers correctly, so the assertions are about what must NOT happen.
- **Memory bomb.** The bomb is deliberately bounded (cap `BOMB_CAP` 128MB, child refuses past `BOMB_MAX_MB` 512, 10s clock). It was once `while True: append(100MB)` against the production 1GB cap with a 60s timeout, fine only while the watchdog works; when `ps` could not fork under memory pressure the old sampler reported 0, the cap never tripped, and the child allocated ~5GB/s into swap and took the machine down from a passing-looking suite. A broken watchdog now costs at most `BOMB_MAX_MB` and a failed assertion. Darwin returns EINVAL for RLIMIT_AS, so the parent's RSS watchdog is the only memory guard. An unmeasurable child (sampler returns None) must be killed, not trusted. The wall clock covers hangs RLIMIT_CPU cannot (blocked in a syscall). RLIMIT_FSIZE=0 allows an empty file but not one byte.
- **Console read policy / "don't ask again".** Cases pin shapes the old prefix wildcard or per-token blocklist let through (`-sX` bundled flags don't start with `-X`; argparse abbreviations like `--ref`; SELECTs calling writefile/readfile/load_extension; a `;` inside a string literal such as chat_guid `any;+;chat…`). Only one leading `cd <path> &&` is stepped over; a pipe's right-hand side decides; only stderr-to-null is stepped over. `_always_rule` keyed on the first word made `cd backend && npm test` and `cd / && rm -rf` both `Bash:cd`; an unparseable command must give no rule (a bare "Bash" key once meant one click was a session-wide yes). `Bash(...)` must not appear in `allowed_tools` (entries bypass `can_use_tool` and `is_safe_read`).
- **Incremental sync.** Windowing off "newest chat.db message with an attachment" looks right but the subquery sees the row it should admit, so after the first backfill the window is empty forever; only a second sync with new data distinguishes a correct watermark.
- **Log display.** A file sent with a caption used to show only the caption; absent `attachment_text` reads `not-extracted`, never extracted-and-empty; one attachments query per page of messages.
- **Dropped files (`read_upload`).** No attachments row, so it enforces its own guards. A drop goes to the session as the image, not transcribed (a CAD sketch became "10.00 5.00 10.00 5.00": all the words, none of the picture). History keeps images, so one the API refuses would fail every later turn (PNG magic on junk passes `_sniff`, hence the open check). Unlike `extract()`, a HEIC the user dragged from their own disk is converted (`_is_transcodable` refuses non-Messages HEIC as possibly a stranger's); image/jpeg reaching the model proves sips ran. The system HEIC fixture is 6K so its PNG overruns the vision cap while staying under `ATTACHMENT_MAX_BYTES`; the API refuses large dimensions once a session holds many images, so long edge is held at `TRANSCODE_LONG_EDGE`.
- **Wallet passes.** A .pkpass is the one archive format opened; the artwork beside pass.json, the issuer credential (update-endpoint token reaches prompts otherwise), and over-cap data stay shut. The cap counts bytes read, not the archive's declared size. The failure mode of a ticket parser is a plausible ticket with the wrong seat: real passes put "August 20" under label "12:15 pm", so label/value pairing is kept; `attributedValue` is a fallback (one real pass fills it with "hello"); only scalars render (pass.json is unvalidated); issuers still emit the old `barcode` key alone. A suffix only upgrades a mime that asserts nothing, else it would pick the parser. Gmail repeats the sender's header and chat.db records a UTI, so both spellings route locally and the worker's copy of the pair must match.
- **Word files.** Handouts are often pasted-in label images: pictures come back as undecoded bytes with a marker where they sit.

### `backend/tests/test_calendar_write.py`: calendar write decisions

The API call is one request; the tests pin what is decided before it, because these can corrupt a calendar silently: an all-day end is inclusive on the way in and exclusive out (Google stores the 23rd for "20th through 22nd"); a bare clock time is in the user's zone on the event's date, not this machine's nor the zone the conversation discussed; a PATCH moving one endpoint turns an hour meeting into a 23-hour one (Google validates start against end), and giving an all-day event a 17:00-23:59 time must not 400 (dateTime merged beside the stored date) or the session re-creates the event; `attendees` is replaced wholesale (sending only our entry uninvites everyone); delete is a real delete. Google's 403 insufficientPermissions reads like a request bug rather than missing consent; the post-write cache refresh must not raise after the event moved. Series: the master carries the rule and each Tuesday is an expansion with its own id (`match` resolves to one), so writes decide occurrence vs series, default occurrence; "until December 12" includes that day; two statements of when a series stops are replaced not merged; unknown repeat words are refused before creating a non-repeating event; an occurrence can't repeat differently from its series; splitting a series is two writes; a series keeps its own zone; affected days are collected before a write (after moving Tuesdays to Thursdays nothing says which were stale) and only cached days are re-pulled (a never-synced day can't be stale). Blank location/description means clear; numeric strings are accepted where the schema says integer.

### `backend/tests/test_citations.py` and `test_console_builtin_tools.py`: what the tests pin

Citations: `brief=false` reads must carry refs too. Found live: asked for a detail out of an email, the console read with `brief=false`, got rows whose only handle was the raw Gmail id, and cited that (`[cite:1a0c5286b39d182b]`) because the brief line was the only thing carrying a ref. Refs are a function of their target (minted twice is minted once); a collision never moves an already-issued ref (a transcript holding `eaaaaa` keeps pointing at its email); two instances of a recurring event get two refs; an aggregate row has no one message behind it and declines rather than pointing at whichever row was first; provider rows are in each provider's `get()` shape and are not mutated (caches still hold some).
Built-in tools: `tools=` is easy to drop from `build_options` and nothing visibly fails (the floor silently returns to 28,561 tokens per assistant message), so the tests pin the omission. ToolSearch must stay (vault tools and claude.ai connectors are deferred; without it every writer is stranded and the model seems to claim the capability doesn't exist). The CLI's agent/worktree/cron tools cost ~4.7k tokens per message and no console session has used them. Skill stays out because dropping it drops the Skills block (13 of the 14 are dev skills; the vault's daily-brief is built by `brief.py`); if that changes, delete that assert knowingly. `CONSOLE_ALLOWED_TOOLS` must name only tools `tools=` still hands out (otherwise it reads as a granted permission that is unreachable).

### `backend/tests/test_console_auto_mode.py` and `test_console_changes.py`: auto mode pins

Auto mode is Kestrel's, not the SDK's, so it is tested through the two real paths: `can_use_tool` (where a card is or isn't raised) and `_stream_messages` (where the summary is assembled). The summary must be a record, not a claim: a call the model asked for and never completed, a denied card, a tool that threw, or an interrupted turn's unanswered last call (which may or may not have run) is not in it; the summary belongs to the turn that made it. The SDK mode for auto is `default`, never `acceptEdits` (the CLI then settles in-workspace Bash writes itself and never calls `can_use_tool`, so `rm <vault>/…` ran unattended with the guard list looking on) and never `bypassPermissions` (drops the approval callback, hence the guard list and the ledger). `can_use_tool` is still registered under bypass: the mode switches live (`test_console_mode_switch`), and a CLI started without the callback would deny every call once it left bypass; the summary doesn't depend on it (built from the tool stream). A retired picker mode or old `CONSOLE_PERMISSION_MODE` maps to an SDK mode rather than erroring.
Change classifier: every `WRITE_TOOLS` tool records a change except `file_read` (listed for cost); read tools record nothing except `theme_set` (no card, colour roles only, undone from the Look lab, still changes the window). A WRITE_TOOLS addition that forgets the table is the registry-loop bug the coverage test fails on. Guard cases are the sentences they defend: sending mail isn't undoable, `rm` doesn't move to trash, a DELETE isn't an edit, `untrash` is the undo not the delete, `email_schedule` makes mail leave with no further human look so its card is the last check, an unparseable (shlex) command asks, `VAULT_PATH=/abs uvicorn …` runs uvicorn not a program named `abs`. A file edit never asks (a guard there defeats auto mode); the unguarded side matters as much as the guarded. Guard reasons render as "auto mode paused — it would <guard>": lower case, no full stop, no dash of its own (a second dash stuttered: "delete files — nothing here moves them"). Every scope needs a `_CARD_TOPICS` row (else its card stays stale until the next poll) and every topic needs a frontend listener (a shared string, so a typo is a refresh that goes nowhere).

### `backend/tests/test_console_faults.py`: the failure log

The log answers how often a turn fails and on what; its first four rows answered neither. Three were `error_during_execution` with a NULL detail (the question restated). The fourth was a session limit filed under the label `success`: the CLI reports a quota stop with `is_error` set and subtype "success", and the subtype was recorded verbatim. So the label describes what happened and the detail is never empty while the turn knows anything: the last tool and its output are the detail for an empty result; "no tool calls" has its own detail (a shared one would merge two different failures into one fingerprint); tool results arrive as a bare string or content blocks depending on the tool, and a fallback understanding only one would be empty for the other. After the CLI renamed its windows every quota stop went back under `success`, so the renamed names are limits too; `_LIMIT_RE` wants the phrase, not the word (a turn that failed while talking about rate limits is its own kind). `_turn_fault` is pure and cheap to test, which is where the interesting bug moves one layer out: the fallback is worth nothing unless `last_tool` carries the turn's last tool when the result arrives, so a test drives the real stream loop. A clean turn (`is_error` false) writes no row however many tools it called. Nothing awaits the stream task, so an SDK reader dying mid-turn (a tool result over its JSON buffer) used to end as an asyncio "never retrieved" line and a UI spinning forever; it now closes the turn. A usage-limit stop lets the tab offer to resume after the window resets.

### `backend/tests/test_console_interrupt.py`: Stop, wakes and mode switches

The CLI ends an interrupted turn exactly like a broken one (ResultMessage, `is_error`, subtype `error_during_execution`, usually no result text), so every Stop press was filed in the failure log ("no result text, no tool calls", "no result text; last tool was X": 17 rows, all Stop) and footed "error" in the console. The socket knows which turns it stopped; tests drive the real socket loop and require it to say so only for the turn it stopped: a late second Stop after turn one ended must not mark turn two (a real failure, still filed), and in the voice window (result in, turn task still flushing speech) Stop sets the flag after the result was judged, so the next query clears it. A backgrounded Bash that outlives its timeout makes the CLI run another turn unasked when it finishes; read per query, that reply never renders and every later answer arrives one turn late, so a query is never answered from a wake's leftovers and nothing is announced as unprompted. A mode switch with an approval card up used to reconnect the tab and the turn died with the old socket while the page still showed it running: Stop is then the only control, and Stop on a socket with nothing running must reply or the tab sits until reload. `launch_app` holds a backend restart while `/api/console/busy` is nonzero; a turn counts as busy by time since the CLI last spoke, not turn length (a clock on the turn killed a long dev turn mid-work).
`test_console_mode_switch.py`: a permission-mode switch is live (`set_permission_mode`, same CLI, session and turn, socket stays up). The old reconnect killed the turn in flight, denied every waiting card, and when the first turn hadn't written yet came back as a fresh session; Shift+Tab cycling did it once per press.
`test_console_link.py`: a session outlives its socket; a page arriving mid-turn gets the transcript up to `cut` then the turn's events (the id arrives mid-turn; before it there was no transcript).

### `backend/tests/test_console_permission_bursts.py`: one card per burst, answerable cards

The console asks the browser for permission from inside `can_use_tool`, once per call, and a turn can emit several calls at once. A card apiece meant the user answered the first while the rest sat queued: three `event_create` calls in one turn all came back "AbortError: Stream closed" with the later cards never answered; that error accounts for 15 of the console's failed tool calls on record. One turn's parallel calls therefore coalesce into one card (per tool: two unrelated calls are two decisions; a call arriving after the card went out gets its own card, not a silent ride on an old answer). `input` stays the first of `inputs` for clients reading only the old field. The card used to carry the tool's raw input only, so a delete arrived as a calendar id and an event id with no title, hour, calendar or word on what yes would cost; it now carries a preview (the raw arguments stay, since a preview is a reading and can be wrong), in every mode not only auto's ("ask" rendered a delete and a rename in the same neutral card), and only destructive writes warn (a warning on every card goes unread). A narration reason is used once: the sentence explaining the first delete must not render under a second, different delete the model said nothing about. Claude in Chrome: one card per web task, not per click (#55); later clicks in the same turn pass without a card.

### `backend/tests/test_console_phase.py` and `test_console_profiles.py`: small pins

`phase` lines: on a tool turn the thinking block opens ~0.7s in and the call's block ~0.9s before its full input arrives; without them the page shows a bare "working…" throughout. A subagent's blocks are its own business and send nothing. `open_tab` past the working cap is queued by the page (the tool says so) rather than failed on "N other tabs are already working" for the parent to retry. The per-turn diff keeps a deleted `-- header comment` line, whose hunk line `--- header comment` is shaped like a file header (a prefix filter would drop it). Never-ask has no `can_use_tool`, so the hook holds there; without the gate there is no tool, no hook and only the delegates' call limit (a fan-out waits on several agents inside one call). `test_console_terminal.py`: the final message's thinking arrives first with `end_turn` already on it, the answer ~2s later; ending on the first would return "Clicking the 7 button." as the result of the whole task. CORS doesn't cover WebSockets, so the terminal socket has its own origin door.

### `backend/tests/test_console_preview.py`: what the approval card shows

The card used to render the tool's raw input, so "delete this event?" was answered against a 64-hex calendar id and an opaque event id: nothing recognisable, no way to tell the 9am from the duplicate 9am. Ids resolve to the thing they name, matching what the tool will actually address (an ambiguous match is reported, as `routes/calendar.find_event` refuses it, never guessed); a lookup that fails says so (`unresolved`) instead of a confident blank, otherwise a delete with no detail looks like a delete of nothing. `_` and `%` were LIKE wildcards in both title lookups ("Q_3" matched "Q13 review"), so the route refused an unambiguous call as a two-way match and the card agreed because it ran the same bad query; they are literal now. The preview runs inside `can_use_tool`, between the model asking and the card appearing, so an exception would take the approval path down: it must never raise. The zone is pinned in tests (a bare clock time otherwise reflects the developer's own TIMELINE). Long commands are folded, not cut at 600 chars. The claude.ai Calendar connector names the same events by the same ids and its delete is just as final.

### `backend/tests/test_console_queue.py`: held messages

CLI behaviour measured on 2.1.318: a user message sent while a turn runs is read at the next tool boundary, in the same turn, and replayed with the uuid it was sent under; one sent while the turn writes its last reply runs as its own turn right after; `cancel_async_message` takes one back unread. Holding the message until the whole turn ends would leave Stop as the only way to cut in. Queued messages are sent as typed under their uuid (not refused as "still working"); a queued turn is announced as the queued message's, not as a background task waking; on Stop they are cancelled before the interrupt (else the CLI runs them as a fresh turn the moment it landed). A turn that errors leaves the queue alone (the next would hit the same wall); the backend going away mid-turn leaves the table.

### `backend/tests/test_console_tools.py`: why each console-tool test exists

Each test is named for a console-session failure (transcripts are the spec). The reasons removed from the file:
- **Writes must land in app.db, not the harness.** Session 3077f49f filed a task with the harness's own TaskCreate (went to `~/.claude/tasks/<session>/1.json`, never seen, past due). Session 2d395a20 ran raw `UPDATE tasks ...` against the wrong app.db, then `find`ed the path and re-ran. A db_query row calls the key `id`; three sessions sent `{"id": "130"}` and the required `task_id` refused it before the handler ran.
- **db_query.** A `no such column` guess (`li.text` for `name`, `messages.date` for `day`) is otherwise answered by a schema probe or another guess, so the error carries the columns of every table the statement reads, aliases included. A correlated subquery per message row held its call until the CLI MCP timeout (1,260s; the general tab sizes it for use_computer). Seed first in read-only tests so the DB exists, else a read-only open fails on the wrong error.
- **calendar_ids.** 18 `create_event` calls in session 276ab09d 404'd because the id was retyped without `@group.calendar.google.com`; the model must never type one. Event zone: session 7f73f8db spent ninety turns on PT/ET arithmetic, then wrote 12:30 America/Los_Angeles on a day the user was in North Carolina (three hours off), so the zone is a property of the event's date. Bare "today" is the 4am-rollover day-key (midnight to 4am disagree with the wall clock, and so can their zones); a malformed date must not fall back to today.
- **provider.** Three range separators were guessed in one week (`,` `:` `..`); only `..` is real, so a bad range returns the grammar.
- **reddit_search.** The one read with no second hop (no fetch tool; Reddit 403s anonymous clients), so it must return body and permalink. Arctic Shift refuses an unscoped query, so the schema requires a subreddit. "Timeout. Maybe slow down a bit" reads as a rate limit and invites a retry; "narrow it" is wrong advice because every narrower retry 422s while the search is down, so the tool points past the archive. Its description names the tracked subs, or it can't land as "the read the cache can't serve".
- **Allowlist.** Entries in the auto-approved list bypass `can_use_tool`, so a writer there fires with no card. A handler left out of `TOOLS` is invisible to the model. Every tool that does not write is listed with why (open_tab asks the page for a tab and closing it is the undo; others touch only idle tabs, sandboxes or checked colour roles).
- **Prompt.** Over 88 sessions, 29 of 153 ToolSearch calls came back empty and every one was for a vault tool: 15/15 `select:` calls with the bare name the prompt writes and 14/14 keyword searches; qualified `select:mcp__vault__x` never failed. The prose may keep short names only while it says what the real ones look like. The old pinned phrase "already loaded" was false (vault tools are deferred); what must survive is the qualified `select:` form. A tool the prose never mentions is reached only if its description fires (`email_body` once went unnamed while the prompt sent email questions to `db_query` over a table of snippets). `open_tab` is named by HANDOFF_APPEND, which goes only to sessions given the tool.
- **Lists.** Session 83d02377 said "no dedicated shopping-list write tool" and filed six move-in items as tasks. The migration-seeded list is the default for an unqualified `add milk`. An unknown list names the real ones (the model can't see the chips; a bare "no such list" invites the same guess or a task-board fallback) and writes nothing. Names are unique case-insensitively (else two chips each hold half a list). Batch `items` must pass the MCP jsonschema check and a misnamed inner field is refused like a top-level one. A research tab states where its report is when it ticks its item (same note again kept once).
- **Expenses/debts.** Session 137b2608 ("add them to debts -> ana and ben already paid") had no debt writer, globbed for a debt file and wrote four balances into task 91's note where the card can't see them; it then left task 91 open because the note was the only copy (the full-instruction test holds that the task can now close). A debt with no amount shows on the card and contributes nothing, so the tool asks. `direction` carries the sign (a -40 `i_owe` would subtract from what the user is owed). Session e9215d16, asked for one sum per person, had no edit/delete, so it settled nine split rows and added a combined one each, leaving nine payments that never happened in history. A beer is a favor, not a $5 debt: no effect on the owed-to-you total or chase list.
- **Watchlist/habits/packages/trips.** The drain (a store the console can read but not write) was observed twice; a film filed as a task sits on the board asking to be completed. Optional means omittable: guards the schema-shorthand bug on the newest tools. Habits: SYSTEM_APPEND pointed at habits while only the task board was in reach, and a habit closed as a task leaves the day reading missed. Matching is case/punctuation-insensitive on the user's words ('$50 doordash'); no match returns the real habit names (guessing writes a check to an invented habit). Check-ins file under `day_key()`, not `date.today()`: at 01:30 the civil date is the 12th but the card files under the 11th; a model computing the date itself between midnight and 4am hands over tomorrow, and the refusal quotes the day-key. `value=0` leaves a tombstone so the auto pass can't refill. `habit_add` never duplicates an active habit (two Exercise rows split a streak) and resumes an archived one (a second row would strand history on an unrendered habit and restart the streak). cadence/target/unit are not offered: nothing reads them, and a 'weekly' habit would draw a daily streak missing six days in seven. `package_add`: session 83d02377 pasted an Amazon orders page for orders on a parent's account (no email reaches the mailbox), and with deliveries write-only-by-amendment the console ticked them bought on the shopping list, dates only as prose. The key `package_add` reports is the one `package_update` takes. Session 1f1aaa64 had no write path into `packages` so it filed then dropped a task. A date-only `dep_local` parses at 00:00, dating the T-24h check-in a day early and printing "12:00 AM", so `trip_add` reports it. A flight can be on the calendar and in no other store (`/api/trips` omits it; no message to extract when the booking predates mail sync). `trip_delete` handles an exchanged flight: the airline's change email opens the new leg without closing the old, so both show and the dead leg keeps its check-in task and alarm; a deleted trip is dropped, not removed (dedupe still refuses to reopen it), and the derived alarm simply stops being wanted at the next reconcile.
- **Schemas.** The SDK shorthand `{"name": Annotated[str, ...]}` marks every key required. Session 5719a6e8: `provider` without a metric was refused, then the retry filled every blank (`metric=""`, `brief=false`) and replaced a one-line-per-item read with a week of raw JSON. `create_sdk_mcp_server` passes a schema through untouched only with both `"type"` and `"properties"`. Trip delete takes the whole composite key (`FLZUNL/2308` has two days). The SDK server doesn't read `isError`, so refusals must go out another way or they show as a green card and count in the turn's change summary. `brain_read {"slug": ...}` (param is `page`) used to drop the slug and answer "pass a page slug or a query" while the fact was active; a stray key is refused by name and the handler doesn't run. 15 blind `email_body` calls sent `{"id": ...}`; a `provider source=email` line has a ref and no id, so reading by ref must work.
- **Contacts.** Session 5719a6e8 was told no write path existed (contacts.py had landed minutes earlier). `contacts.update()` reads a given `""` as "clear", so under the old all-required schema adding a phone would blank name/org/note and sync that to the phone. Two cards sharing a number split a person's CRM history. The osascript permission failure is opaque and the "update it in Contacts yourself" fallback is wrong for it. `make new person` lands in Contacts' default account, and only some of five reach the phone. An unquoted int phone used to hit `for v in value`.
- **message_files/file_read.** Session 18517f8d saw a "(contact card)" placeholder and no names, said the contact never came through while both vCards sat in `attachments` named after people, and read "Linus Pryor" as two. The typed message saying who the card is sits on a different row than the file, so the listing carries it over. `waiting`/`unanswered` widgets hand a chat_guid, not a name. `_` is a LIKE wildcard (must match literally). Tapbacks and rich-link blobs are not files anyone sent. vCard contents are fenced (fields are sender-typed); an iCloud-offloaded card must say offloaded, not empty; an already-extracted short card is inlined (no refetch).
- **Event loop.** Handlers run on uvicorn's loop; a provider backfill or 30s osascript inline froze every request and the console websocket. `_as_int` raises HTTPException while arguments are built; that must return as a tool error.

### `backend/tests/test_console_upload.py`: upload route contract

`POST /api/console/upload` is thin (`read_upload` does the work; policy in `test_attachments`). The tests pin the frontend contract: every answer, refusals included, has `label`, `kind`, `note` and text, since a missing key renders "undefined" into the pill. An image gone from memory (a backend restart between drop and send) must say so, not leave the model to answer about a missing picture. A dropped file is never cached (`extract()` caches against a row; a drop has none). The page swallows the notification route's errors, so an unknown kind would mean notifications that silently never arrive.

### `backend/tests/test_console_usage.py`: usage percentages

`GET /api/console/usage` feeds the console bar's chips. Chips once read only `rate_limit` events, which carry a status but usually no utilization and mention only the binding window, so the bar said "5h —" and never the week. Numbers now come from our throttled fetch and from the cache the interactive CLI leaves in `~/.claude.json` (`cachedUsageUtilization.utilization`); whichever looked at the API last wins, and our fetch is not privileged: an hour-old success must not outrank a minute-old CLI cache. Tests pin the shapes that cache can be in when stale (absent, half-written since the CLI rewrites the file wholesale, old) because those would put a confident wrong number on screen. Cache keys match the stream's `rate_limit_type` so a live event and a cached percentage merge onto one chip. A `claude setup-token` token lacks `user:profile` scope (the API 403s); the CLI's login-keychain entry has it, so `_oauth_token` reaches for that, and an exported env token is honoured without touching the keystore. `_oauth_token` falls back to the keychain, which answers on a dev machine, so tests sever only that branch. The CLI rotates its credential every few hours: windows are cached, never the token. Only a new setup-token fixes a 401, so the next poll doesn't retry. Offline on the next attempt, the previous answer stays rather than blanking the chips. The console polls every 60s per tab; the API sees one request per interval; a plain poll waits the full interval and the "turn just ended" read (`fresh=1`) may ask again after `USAGE_FRESH_INTERVAL`. The credential is never returned. Keychain absence, locked keychain and logged-out CLI are normal states and must not raise.

### `backend/tests/test_courses.py` and `test_contacts.py`: office hours exceptions and osascript argv

Courses: a professor's "my Tuesday hours start at 2pm, not 1pm, tomorrow only" must keep both the weekly rule and a dated exception (amending the rule is wrong from next Tuesday, ignoring the mail is wrong tomorrow); today's rows, the week view and "next" each read the right one. A `moved_from` weekday is kept separately because `day` is today's. A shallow json-ability check once flattened a whole list to a repr for one nested date and the hours vanished. `set_hours` reads frontmatter from the file and the path from index.db, so tests need both. A TA's blocks written one call at a time must add, not amend the last; 'TBA' has no weekdays to compare, so posted hours are added beside it and the reply says the placeholder remains; a dated override picks the block meeting that day (no 409); "Martin Paul Halvorsen" finds "Martin Halvorsen".
Contacts: `_run` is stubbed everywhere (reachability is `python -m app.contacts check`); the tests pin the argv layout because the create script walks argv in triples and an off-by-one writes a phone number into a label field that syncs to the phone. Items 1-4 are positional (empty org is ""); order marks the primary number; dedupe normalizes both sides (+1 (202) 555-1111 and 2025551111 are one phone, as in the CRM); a card can be excluded from its own dedupe (update() re-labelling its own number); no Full Disk Access means no dedupe, not a refused write; removals use the stored format (AppleScript matches the literal string) and run before additions (else a swap could remove the new number).

### `backend/tests/test_coursework.py`: Brightspace feed

06:59Z on the 15th is 11:59pm on the 14th Pacific; the calendar had an ENGR pitch a day early before conversion.

### `backend/tests/test_debt_nudge.py`: the debt nudge cycle

An open IOU is two facts: the money, and whether you've said anything about it lately. Only the money was stored, so the card could only sit in HAS ("someone owes you $106" is true daily and actionable on none), earning 44 impressions and 3 opens across nine days. The cycle: never asked -> asked (quiet a week) -> overdue -> asked again (quiet another week); settling is the only exit. The ask count is kept because an overwritten timestamp loses it and it decides whether to keep asking or write off. A debt you owe is never nudged (the other person chases it; a button would do nothing). An unreadable timestamp reads as unasked: the opposite default silences a debt forever on one bad write. `to_chase` counts both `never` and `overdue` (the card's band uses the total). The annotation pass rebuilds every row, so totals are the likeliest silent break.

### `providers/offline.py`: test-pinned behaviours

- Origin bug: lid opens, the app launches ~30s later with DNS not up, and every provider-backed widget 500d because the backfill inside `get()` raised while cached rows sat there. Invariants: a network-shaped failure (urlopen wrapping gaierror) is swallowed and the cached read answers, stamped stale; an answer from a reachable source (401 expired token, bad range, `oauth.NotAuthorized` converted to ValueError = 400) still raises, since stale data must never hide a credential needing reconnect (it could stay expired for a week). A provider reading through another provider must not swallow the caller's notes: `features` reads six sources and the envelope must still say what went missing.

### `providers/reddit`: test-pinned behaviours

(Reasons removed from test_reddit.py; skip any DESIGN.md already states.)
- Pinned because they only bite in production: the 4am fold deciding which day a post lands on, a quiet day not being mistaken for a gap, a missing score staying NULL rather than a real-looking zero.
- A day with no posts is an answer: without the `reddit_sync` marker every later read would re-fetch it forever. Conversely a this-week read on a Wednesday once marked Thu-Sun synced-and-empty; a marked day is never revisited, so those posts never arrived: future days are never marked.
- Pagination advances on `created_utc`; a full page sharing one timestamp cannot advance the cursor, so the loop must notice it learned nothing.
- Never mark a day synced after a 429/outage: nothing would look at it again and the outage becomes a permanent "no posts". One sub failing is not an outage; the subs that replied are real data.
- No `.rss` fallback for a today the archive hasn't ingested (every Reddit feed 404s); an empty archive reply for today is marked synced, error-free, with nothing sent to reddit.com.
- With the archive's `query=` search down (422), the newest-first listing (paged on `before`, no `after`: a two-year `after` 422'd too) still answers and the phrase is matched locally as a phrase; word-by-word matching took "rain ... cover" for "code mode" (55 false hits in r/ClaudeCode). `PAGE_LIMIT` caps the limit.
- Arctic Shift 400s a query scoped to neither subreddit nor author, so an unscoped search is refused rather than offered (the parameter was once optional, wasting a round trip per call and mis-describing the tool as "search all of Reddit").
- The archive's busy answer is a 422 "Timeout. Maybe slow down a bit": load, not a bad query. One retry; what survives is `ArchiveBusy` so callers say "later", not "wrong". A 400 is never retried (it will be wrong again).
- `_api` tests stub urlopen (one layer below `_arctic`, which replaces the retry); the returned dict counts attempts, since a retry that silently didn't happen is the failure to catch. `db.day_key()` with no argument is pinned to DAY while `day_key(dt)` stays real (conftest's frozen_day flattens both, hiding `_day_of`'s job).

### `tests/` small-module notes (poller, token_usage, tz_calendar_shape, tasks, lint, metric_cache, subagent_run)

- poller: every test takes `dbfile` because refresh also writes a `provider_health` row and fake providers ('a', 'bad') don't belong in the real app.db. The four post-steps are stubbed since the real ones write the real app.db and the backup copies it (see test_sync_route notes). sync_all() and refresh_stream() share one POST_STEPS so they can't drift; a failing step is {"error": ...} on both, doesn't stop later steps, and lands in the failure log, not only stderr.
- token_usage: a bad --model or signed-out CLI fails with no tokens spent: no call, no model row named after the typo.
- tz_calendar_shape: three renderers depend on `tz` (calendar card header, expansion day headings, brief_line()). An all-day row has no clock time and gets no label (a lone 'ET' is noise; renderers key off presence).
- tasks: undo of a check-off restores subtasks the close took; one finished on its own earlier stays finished.
- lint: pytest holds the lint checks so the commit bar covers what the lint-on-edit hook missed (an edit outside Claude Code, or a rule changed under old code); frontend ratchet = no ESLint errors (raw fetch outside lib/api.js, raw setTimeout outside lib/timers.js, hooks / you-might-not-need-an-effect rules, reasonless suppression) and no more useEffect than lint-baseline.json.
- metric_cache: the guarded bug is a forgotten `refreshed_at=excluded...` on conflict.
- subagent_run (#77): background subagents never put calls on the parent's stream; the CLI files them under <session>/subagents/ with a meta naming the Agent call.

### `tests/test_failures.py`: what the failure-log tests pin

- The table's shape exists for three things: a failure that repeats costs one row (fingerprint key; a widget throwing on every re-render would otherwise be thousands of rows an hour), two ways for one card to break stay two rows (label collapse would hide the second), and no recording path (middleware, console stream loop, the status-panel read) may raise into what it instruments: a log that can fail a request is worse than none.
- Same label from two layers is two findings (an /api/finance 500 vs a Finance card that won't render). A later trace-less report must not blank the stored trace. Trimming is by recency, not insertion order (a long-standing failure still firing holds the oldest `first_at` and is the one worth keeping).
- Messages built from response bodies have no ceiling (capped). `origin` is never taken from the body (the endpoint is browser-only). Kinds are a closed set so a typo fails loudly. Counts are two numbers: how many things are broken, and whether one is on fire now. An unrecognised origin filter must not match nothing (that reads "all clear").
- Middleware: tests use the real `failures.middleware` main.py registers. providers/api.py maps ValueError to 400 and everything else became a 500 living only in a /tmp log (the gap this closed). Two message ids failing alike are one finding under the route template (5xx and exceptions). A returned 502 never reaches except (FastAPI converts it) but is the same failure as a 500. `raise HTTPException(502) from e`: the row carries the upstream error. Details built from `{e}` carry timeouts, ports, ids: fingerprint the shape, show newest text. 400s are the caller's fault and would be noise within a day.
- Sourcemap test: a two-line bundle mapping into two sources; line 2's segments are deltas from line 1's running state, the part decoders get wrong.

### `tests/test_habits.py`, `tests/test_task_due_time.py`, `tests/test_spotify.py`: reasons

- Habits: streak's quirk is an un-done today doesn't break it until today is missed. Hard delete leaves no rows (orphaned habit_checkins reappear in habit_history as a nameless habit); archive keeps the history the streak is built from; `name` is UNIQUE so a deleted habit's leftover row would block re-adding the name (what one does after deleting a typo).
- Task due_time: `due` stays the day key, `due_time` is optional 'HH:MM'. Rollover: a day key runs 04:00-03:59, so 01:00 belongs to the end of its key (next calendar day; sorts last of the timed three). Seconds are dropped not rejected so callers can echo an ISO stamp. A 9am deadline must not sit in "due today" all afternoon as fresh as 5pm. 23:30 on key D read at 00:30 is next calendar day, still key D, before the deadline. Time stand-in: db.datetime with fixed now(), rest of class intact.
- Spotify summary: reads only the `plays` cache (today's minutes/plays/top track/last play + novelty, rolling-7-day sparkline, week top artist); tests pin `day_key` for "today" but only the no-arg call (day_key(dt) buckets a play's instant and must stay real); novelty is checked against the provider's `novelty` metric so they never drift.

### `tests/test_lastfm.py`: what the Last.fm tests pin

- Spotify stopped returning `genres` on artists, so tags come from Last.fm artist.getTopTags joined on `plays.artist_key` (the name), not a Spotify id: one lookup covers imported history and live rows (which carry different artist_ids). get() is local-only; tests stub _api.
- The documented sample response has no `count`; missing weights must not drop all tags (list is already popularity-ordered). "hip-hop" vs "hip hop" (live Drake data) normalise to one genre or each gets half the plays.
- Queue order: play count alone starves today (once-tried artist behind the whole backlog; today's coverage read 0% for days). Error 6 is a settled answer, not a failure: counting it re-requested three real artists (Cree+, Omar+, Malditho.wav) every poll with a `degraded` status event each. Rate-limit/outage stays retryable, never filed as unknown.
- Raw responses are stored so revising the blocklist doesn't cost another 9.8k lookups; rows fetched before raw was stored can't be re-derived and are skipped, not blanked.
- The genre mix travels with the share of the day it's drawn from (three genres off 25% differs from off all). An all-untagged day is distinct from a no-plays day.

### `tests/test_launcher.py`: why these launcher tests exist

- The .app serves frontend/dist, so a build that never ran is invisible until the UI is a commit behind; the staleness rule that decides when to rebuild is what's pinned. The window and the process-group reap aren't testable headless. npm install touches thousands of files and changes no output, so it must not count as stale.
- Process-group interlock: signalling an inherited group kills the shell; pytest runs in the terminal's group, so the test uses the real condition. killpg can't spare the caller; the pid sweep must.
- Backend server: the .app used to run uvicorn on a thread, where --reload is impossible (the flag is read only by uvicorn.run() and the reloader restarts its target process, which in-process is the window). The tests pin the subprocess shape that replaced it, because a window with a stale server looks like one with a live server. Plain --reload restarts the instant a file changes, under a dev tab whose turn runs inside this server, so `Backend` reloads between turns instead. Banners go through the window process (the bundle, #80): the server receives the pipe's write end by fd and by name and nothing else does. The watch dir is app/, not backend/ (which holds data/ with app.db + WAL, and .venv). A bad edit crashes the server and the next edit must be able to fix it. sys.executable inside the .app is the copied stub giving the process its name and icon; reloader children use it. An import error exits uvicorn immediately, so waiting out the timeout only delays the message.
- Generated bundle: make-app.sh writes the run script into a gitignored bundle, so the template is the only coverage; asserting on it is crude but stands between a one-token edit and a silent regression. The heredoc escapes runtime expansions (`\$STUB`) from the generator's substitution; match the resolved text. Python block-buffers stdout through the log redirect so a crash flushed nothing and the log was empty when needed. Identity comes from the running executable: CoreFoundation resolves the venv python symlink to the framework binary and walks up into Python.framework's own Python.app (app named "Python", generic rocket, org.python.python). The stub links an exact Cellar path, so `brew upgrade python@3.12` leaves a copy that can't load; launching as "Python" beats not launching. Cmd-R reruns the build check (a window left open across a commit served launch-time dist forever); a second Cmd-R during a build waits and then finds dist current.

### `tests/test_location_map.py`: what the map tests pin

- The map must not invent a second answer: same places as the `places` metric (same stay detection, three-minute floor; a map drawing its own would disagree with the card), paths break where day metrics stop counting dwell (same MAX_GAP_MIN ceiling: a fix then four hours of silence books MAX_GAP_MIN), thinning only affects drawing (dwell over a thinned set would shorten stays). The home radius is served as real numbers rather than a frontend copy. Tile-cache failure degrades to vector-only rendering instead of failing the sheet.
- Stay-rewrite regression: a stationary hour of GPS scatter across the 35.900/35.901 and -106.050/-106.051 boundaries filed as up to four places under grid clustering (on the real vault one building became three places and one flat twelve). A stay is cut on distance from its running centroid. Declared places outrank clustering: a flat whose fixes spread wider than STAY_RADIUS_M is still one named place.

### `tests/test_places.py`: the bug these tests pin

- `HOME_LAT`/`HOME_LON` were one constant; the owner moved cross-country and every pre-move day was scored against the new address (3,561 km away). `time_at_home_min` read a flat 0.0 for a month of days at home; twelve reached the correlation matrix and manufactured a `steps ~ time_at_home_min` correlation of +0.70 that collapses to +0.15 once the mis-scored days drop.
- So assertions are mostly about what must NOT happen: a day whose home is unknown produces no home metric (a zero is a measurement of the config, not the day). 08-17 is deliberately covered by neither entry (mid-transit fixes). Fixes at the North Carolina address on a North Carolina day are home. No place notes => behaves exactly as before (notes are an upgrade path). Overlapping declarations (gym 15 s walk from the dorm, closer than GPS error) are reported, not silently resolved; two homes at the same coordinates in different years overlap in space only, which isn't ambiguous. Tests build index.db through the real `_jsonable` so unquoted YAML dates take the real path.

### `tests/test_status.py`: what the status-panel tests pin

- The panel exists for: a refresh that succeeds while writing nothing must not read ok (chat.db unreadable, aw-server down: refresh() returns cleanly, everything else is green); a never-configured source is grey (`off`), never red (a panel that cries wolf stops being read); a benign skip note must not read as failure (news says "already fetched today" daily; no generic way to read a skip as failure). messages: cache looks fine because it synced before Full Disk Access was lost; only the probe knows.
- Fixture: one-provider registry (not the live twelve), auth lookups stubbed off, `_infra` stubbed healthy: live it reads the real VAULT_PATH and data/app.db, so `counts`/`worst` would depend on the machine (two extra broken rows in a bare worktree or CI). The vault and app.db break like providers, so a broken one must reach `worst` and `counts` even with every source fine.
- provider_health is overwritten each cycle, so anything that broke and healed is invisible there; the failure log is the only durable record. E.g. a Plaid leg 500s, refresh() catches it and returns success, provider_health says ok. "429 became invalid_grant" is useful detail: a rate limit turned dead auth needs a different fix.

### `tests/test_sync_route.py`: why every sync step is stubbed

- /api/sync awaits poller.sync_all(): refresh_all, alarm reconcile, at-desk flush, wake schedule, monthly snapshot, self-text routing. db.APP_DB is hardcoded, so any unstubbed step writes the real database from a test run: the alarm reconcile did (first rewiring), then the notify flush (expires real rows, can post a real banner), then wake (opens real app.db, shells out to pmset), then the backup (copies ~100MB into backend/data/backups/ on the month's first run).
- Order is pinned: alarms derive from the caches providers write, so reconcile after providers (else it diffs against a stale store); at-desk flush before wake so a banner enqueued during this cycle's extraction ships this cycle; wake before snapshot because it reads the alarms the reconcile just wrote; snapshot last since it copies the whole database and shouldn't sit in front of a banner eleven cycles out of twelve.

### `tests/test_tz.py`: what the zone tests guard

- The bug isn't a wrong instant (instants were always right): it's a calendar slice that changes frame halfway down, so 9:00 AM Pacific and 8:30 PM Eastern sit adjacent looking three hours apart. Structural tests use a fixture timeline so editing a real flight date in tz.py doesn't break them. The Thanksgiving window is four days wide (a "where do they live now" rule would render it all Pacific). The configured timeline is hand-edited and `zone_on` walks it in order, silently skipping out-of-order entries. A 1am Eastern September event is 10pm Pacific the night before: file it under the day the user lives in. Pins/cached payloads predate the `tz` field and still get a label. The prompt told the console the day-key but not the zone, so the write path had nothing to anchor; any placeholder left unfilled reaches the model as literal braces.

### `tests/test_wake.py`: why the wake tests are shaped this way

- plan()/_coalesce() are pure (all inputs as args), so the cancel decision, the only one that can touch state we don't own, is tested with no subprocess or clock. Parsing runs against real captured `pmset -g sched` output because the format isn't ours.
- TZ pinned to America/New_York: pmset prints bare local wall-clock times, so expectations depend on machine zone (TIMELINE moves the laptop to Pacific 8/17; a silently shifting test is worse than a failing one).
- REAL_SCHED has system rows (calendar travel engine, Do Not Disturb): three of four events belong to macOS; cancels must only come from our-owner rows. Row [2] carries `User visible: true` after the owner, so the regex is unanchored at the end (a parse failure would read "not scheduled" and re-add every cycle). The repeating-events block has no [N] index; matching it would invent an event to cancel.
- Spent wake (time slid while asleep): pmset reaps it; not cancelled, not counted as covering an alarm.
- ALARM_HORIZON_DAYS=14 vs WAKE_HORIZON_HOURS=36 differ deliberately (a Thanksgiving flight has an alarm, no wake yet). A wake already in the past is dropped, not clamped to now (would churn every cycle).
- Coalescing: check-in + leave_by for one trip share a wake; keep the earliest (latest would wake after the phone alarm); window is measured against the wake kept, not the neighbour, so spans stay bounded. Over the cap: truncate (opposite of ALARM_MAX_WITHDRAW_PER_CYCLE refusal) since the least urgent fall off and a later cycle picks them up. A hand-edited bad fire_at skips only that row.
- Reconciler: pmset schedule is append-only, so re-adding each cycle leaves hundreds of duplicates. Withdrawn alarm => cancel its wake. Deadband absorbs leave_by jitter of a minute or two. A new early alarm must not re-pair later wakes positionally (02:00 vs 05:40 would read as a move and churn).
- Apply: dry run still reads `-g sched`; cancels go before adds (a failed add leaves an alarm unrefreshed, the thing the step exists to prevent). Only `pmset schedule` asks for root (euid check); reading with root would make status() depend on the sudoers drop-in. pmset is named by absolute path and `-n` is passed: a NOPASSWD rule on a bare $PATH word over-grants, and `-n` makes a missing rule fail instead of blocking the poll on a password prompt. pmset failures count as `failed`, never raise into the poll cycle.

## Deferred

Reconcile sweep (stray checkboxes in notes → tasks DB).
