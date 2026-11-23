"""app.db — the app-owned source of truth for tasks and habits.

Concurrency: this file is written by BOTH the FastAPI backend and terminal
Claude Code skills (via `sqlite3` in bash). That's safe because of two
settings applied on every connection here — and which any skill using the
sqlite3 CLI should also set:

    PRAGMA journal_mode=WAL;      -- readers don't block writers
    PRAGMA busy_timeout=5000;     -- wait instead of "database is locked"

Day boundary: `day_key()` is the ONLY way to compute "today" anywhere in this
app. Local time; hours before 04:00 count toward the previous day.
"""

import hashlib
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, date

from .config import APP_DB, DAY_ROLLOVER_HOUR, DEBT_NUDGE_DAYS

MIGRATIONS: list[str] = [
    # v1 — tasks + habits
    """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',   -- open | done | dropped
        due TEXT,                              -- YYYY-MM-DD
        priority INTEGER,                      -- 1 high .. 3 low, NULL none
        project TEXT,                          -- PARA project name
        source TEXT NOT NULL DEFAULT 'ui',     -- ui | console | migrated | reconcile
        created_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE TABLE habits (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        cadence TEXT NOT NULL DEFAULT 'daily', -- daily | weekly | x-per-week
        target REAL DEFAULT 1,
        unit TEXT,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE habit_checkins (
        habit_id INTEGER NOT NULL REFERENCES habits(id),
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        value REAL NOT NULL DEFAULT 1,
        note TEXT,
        PRIMARY KEY (habit_id, day)
    );
    """,
    # v2 — provider foundation (IDEAS §0): oauth tokens + stub provider cache.
    # tokens holds OAuth secrets — app.db must never leave this machine, and
    # token values must never appear in API responses (see providers/oauth.py).
    """
    CREATE TABLE tokens (
        provider TEXT NOT NULL,                -- 'google'
        account TEXT NOT NULL,                 -- 'school' | 'personal'
        client_id TEXT NOT NULL,
        client_secret TEXT NOT NULL,
        access_token TEXT,
        refresh_token TEXT,
        token_uri TEXT NOT NULL,
        scopes TEXT,                           -- space-separated
        expires_at TEXT,                       -- UTC ISO, access_token expiry
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, account)
    );
    CREATE TABLE stub_days (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, metric)
    );
    """,
    # v3 — calendar provider cache (IDEAS §2). Google Calendar is truth; these
    # rows are derived and droppable. A multi-day event gets one row per local
    # date it touches. calendar_days records which days have been synced, so
    # "no events rows" is distinguishable from "never fetched".
    """
    CREATE TABLE events (
        day TEXT NOT NULL,                     -- local date, YYYY-MM-DD
        event_id TEXT NOT NULL,
        calendar_id TEXT NOT NULL,
        summary TEXT,
        start_at TEXT NOT NULL,                -- ISO datetime; date if all_day
        end_at TEXT NOT NULL,
        all_day INTEGER NOT NULL DEFAULT 0,
        location TEXT,
        PRIMARY KEY (day, event_id)
    );
    CREATE TABLE calendar_days (
        day TEXT PRIMARY KEY,
        refreshed_at TEXT NOT NULL
    );
    """,
    # v4 — multi-calendar (§2 follow-up): human-readable calendar name per
    # event (calendar_id is an opaque email-ish id once there's more than
    # 'primary'). NULLs backfill on the next sync — the table is derived.
    """
    ALTER TABLE events ADD COLUMN calendar_name TEXT;
    """,
    # v5 — health provider cache (IDEAS §3). Google Health is truth; these
    # rows are derived and droppable. One row per (day, metric); health_sync
    # marks fetched days, so "no row" (watch not worn) is distinguishable
    # from "never fetched".
    """
    CREATE TABLE health_days (
        day TEXT NOT NULL,                     -- civil date, YYYY-MM-DD
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, metric)
    );
    CREATE TABLE health_sync (
        day TEXT PRIMARY KEY,
        refreshed_at TEXT NOT NULL
    );
    """,
    # v6 — email provider (IDEAS §5). Gmail is truth; emails/email_days are
    # derived and droppable (metadata only — bodies are never stored).
    # email_triage holds app-generated summaries + your review decisions:
    # summaries are regenerable (at token cost), user_action is real data —
    # but the Gmail mailbox state it mirrors (archived/labeled/trashed) is
    # the durable record, so the whole table survives a drop philosophically.
    """
    CREATE TABLE emails (
        msg_id TEXT PRIMARY KEY,
        thread_id TEXT,
        day TEXT NOT NULL,                     -- local date received, YYYY-MM-DD
        received_at TEXT NOT NULL,             -- ISO local datetime
        from_name TEXT,
        from_addr TEXT,
        subject TEXT,
        snippet TEXT,
        in_inbox INTEGER NOT NULL DEFAULT 0,
        unread INTEGER NOT NULL DEFAULT 0,
        list_unsubscribe TEXT,                 -- raw List-Unsubscribe header
        list_unsubscribe_post INTEGER NOT NULL DEFAULT 0,  -- RFC 8058 one-click
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX emails_day ON emails (day);
    CREATE TABLE email_days (
        day TEXT PRIMARY KEY,
        refreshed_at TEXT NOT NULL
    );
    CREATE TABLE email_triage (
        msg_id TEXT PRIMARY KEY,
        summary TEXT,
        action TEXT,                           -- recommended: archive|bin|unsubscribe|keep
        bin TEXT,                              -- suggested label when action='bin'
        reason TEXT,
        model TEXT,
        summarized_at TEXT,
        user_action TEXT,                      -- what you did: archive|delete|bin|unsubscribe|keep
        user_action_at TEXT
    );
    """,
    # v7 — daily brief (IDEAS round 2). Widget-first: generated lazily on the
    # first dashboard open of each day_key; the /daily-brief skill is a thin
    # wrapper over GET /api/brief. body is app-generated content (like
    # email_triage summaries): regenerable at token cost, DB-only — never
    # written into the vault. The row doubles as the generation lock: an
    # atomic claim flips status to 'generating' (see app/brief.py).
    """
    CREATE TABLE daily_brief (
        day TEXT PRIMARY KEY,                  -- day_key, YYYY-MM-DD
        status TEXT NOT NULL,                  -- generating | ready | error
        body TEXT,                             -- markdown; NULL until ready
        error TEXT,                            -- message when status='error'
        model TEXT,
        sources TEXT,                          -- JSON {loaded:[...], failed:{name: err}}
        started_at TEXT NOT NULL,
        generated_at TEXT                      -- when status became ready
    );
    """,
    # v8 — finance provider (archive/PLAID_PLAN.md). Plaid is truth; fin_accounts and
    # fin_transactions are derived cache (droppable, re-pulled by a cursor
    # sync). plaid_items is NOT derived: access_token is a real credential
    # (like the tokens table) and must survive cache drops — never in API
    # responses or logs. Amounts keep Plaid's sign: positive = money OUT.
    """
    CREATE TABLE plaid_items (
        item_id TEXT PRIMARY KEY,
        access_token TEXT NOT NULL,            -- credential: never leaves the backend
        env TEXT NOT NULL,                     -- sandbox | production
        institution_id TEXT,
        institution_name TEXT,
        cursor TEXT,                           -- transactions/sync cursor; NULL = full pull next
        status TEXT NOT NULL DEFAULT 'ok',     -- ok | login_required | needs_consent | error
        error TEXT,
        created_at TEXT NOT NULL,
        last_synced_at TEXT
    );
    CREATE TABLE fin_accounts (
        account_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES plaid_items(item_id),
        name TEXT,
        official_name TEXT,
        mask TEXT,
        type TEXT,
        subtype TEXT,
        available REAL,
        current REAL,
        iso_currency TEXT,
        refreshed_at TEXT NOT NULL
    );
    CREATE TABLE fin_transactions (
        transaction_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        day TEXT NOT NULL,                     -- Plaid's civil date, YYYY-MM-DD
        name TEXT,
        merchant TEXT,
        amount REAL NOT NULL,                  -- Plaid sign: positive = money OUT
        iso_currency TEXT,
        category TEXT,                         -- personal_finance_category.primary
        category_detailed TEXT,
        pending INTEGER NOT NULL DEFAULT 0,
        channel TEXT,
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX fin_transactions_day ON fin_transactions (day);
    """,
    # v9 — weather provider (IDEAS §6). Open-Meteo is truth; both tables are
    # derived and droppable. Day = civil date (Open-Meteo's own local day for
    # the queried lat/lon, timezone=auto) — same camp as calendar/health, not
    # day_key. sunrise/sunset store as minutes-since-midnight REAL; render
    # HH:MM at display. weather_code stays the numeric WMO code (map to text
    # in the widget/brief layer). geo_cache memoizes name -> (lat, lon)
    # geocoding lookups (event locations, HOME_LAT/LON overrides by name).
    """
    CREATE TABLE weather_days (
        day TEXT NOT NULL,                     -- civil date, YYYY-MM-DD
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, metric)
    );
    CREATE TABLE geo_cache (
        name TEXT PRIMARY KEY,
        lat REAL NOT NULL,
        lon REAL NOT NULL
    );
    """,
    # v10 — tasks v2 (archive/TASKS_PLAN.md, IDEAS §13): triage fields, one-level
    # subtasks, provenance. All new columns nullable so v1 writers (skills,
    # console) keep working unmodified. `source` (v1) keeps meaning who
    # created the row (ui|console|migrated|reconcile|capture|travel|
    # finance); source_kind/source_ref/source_excerpt are a different axis —
    # the provenance obligation (what real-world thing this task came from).
    # source_excerpt is denormalized on purpose: the cache tables it might
    # otherwise point at (emails, chat.db) are droppable/pruning, so the
    # excerpt has to survive on its own. Status vocabulary grows to open |
    # waiting | someday | done | dropped — no CHECK constraint (v1 had none;
    # skills write raw SQL). The trigger keeps updated_at correct for BOTH
    # the FastAPI app and terminal skills writing raw UPDATE statements.
    """
    ALTER TABLE tasks ADD COLUMN defer TEXT;          -- YYYY-MM-DD; hidden from today-view before this date
    ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id);
    ALTER TABLE tasks ADD COLUMN note TEXT;            -- freeform body / breakdown notes
    ALTER TABLE tasks ADD COLUMN waiting_on TEXT;      -- person/thing; paired with status='waiting'
    ALTER TABLE tasks ADD COLUMN owed_to TEXT;         -- person a commitment task is owed to
    ALTER TABLE tasks ADD COLUMN source_kind TEXT;     -- email | message | capture | travel | finance
    ALTER TABLE tasks ADD COLUMN source_ref TEXT;      -- opaque pointer: msg_id / chat.db guid / etc
    ALTER TABLE tasks ADD COLUMN source_excerpt TEXT;  -- short display copy, survives source cache drops
    ALTER TABLE tasks ADD COLUMN updated_at TEXT;
    CREATE INDEX tasks_triage ON tasks (status, defer, due);
    CREATE TRIGGER tasks_touch AFTER UPDATE ON tasks BEGIN
        UPDATE tasks SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
    END;
    UPDATE tasks SET updated_at = created_at;
    """,
    # v11 — task triage queue (Round 4 follow-up): the migrated backlog
    # landed with no defer/due/priority, so everything piled into
    # "available" undifferentiated. Same shape as email_triage — an app-
    # generated suggestion (regenerable) + your real decision (user_action,
    # durable). One row per task; queue = status='open' AND defer IS NULL
    # AND user_action IS NULL.
    """
    CREATE TABLE task_triage (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
        suggested_action TEXT,      -- keep | defer | someday | drop | done
        suggested_defer TEXT,       -- YYYY-MM-DD, set when suggested_action='defer'
        suggested_due TEXT,         -- YYYY-MM-DD, optional regardless of action
        reason TEXT,
        model TEXT,
        suggested_at TEXT,
        user_action TEXT,           -- what you actually did; NULL = still pending
        user_action_at TEXT
    );
    """,
    # v12 — task triage queue removed: one-time backlog cleanup after the
    # vault migration, not a standing feature (no more mass task imports).
    "DROP TABLE task_triage;",
    # v13 — screen time provider (IDEAS §7). DROPPED AGAIN IN v46 (2026-09-13)
    # — kept here because migrations are replayed in order on a fresh app.db,
    # so this still has to create what v46 removes. Nothing reads these tables
    # any more.
    #
    # ActivityWatch (aw-server, one per configured host) is truth; both tables
    # are derived/droppable. Day is **day_key** (4am rollover), not civil date
    # — a behavioral source
    # like calendar/health/weather are NOT: activity at 1am belongs to
    # yesterday's day_key. screen_apps holds per (day, host, app) seconds so
    # the `apps` metric and DOOMSCROLL_APPS matching don't re-walk raw AW
    # events on every read.
    """
    CREATE TABLE screen_days (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, metric)
    );
    CREATE TABLE screen_apps (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        host TEXT NOT NULL,
        app TEXT NOT NULL,
        seconds REAL NOT NULL,
        PRIMARY KEY (day, host, app)
    );
    """,
    # v14 — news provider (IDEAS §20). Kagi Kite (kite.kagi.com/kite.json)
    # is truth; table is derived/droppable. day = day_key of the *fetch*,
    # not an article date — Kite has no per-day historical API, it just
    # serves whatever its current daily edition is (refreshes ~12:00 UTC),
    # so a pre-noon fetch tags that content under today's day_key rather
    # than erroring. cluster_i is Kite's own cluster_number within the
    # category, stable for a given edition. sources is a JSON list of
    # domain names for attribution (Kite content is CC BY-NC).
    """
    CREATE TABLE news_items (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        category TEXT NOT NULL,                -- NEWS_CATEGORIES slug, e.g. "world"
        cluster_i INTEGER NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        sources TEXT NOT NULL,                 -- JSON list of domain names
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, category, cluster_i)
    );
    """,
    # v15 — finance deeper (archive/ROUND34_PLAN.md §18): recurring transactions +
    # liabilities. Plaid is truth; both derived/droppable, re-synced by
    # finance.refresh() alongside the existing transactions cursor sync.
    # fin_recurring.predicted_next_date is Plaid's own prediction, taken
    # as-is rather than reimplemented from last_day+frequency (the plan's
    # original sketch) — Plaid already handles weekday/month-end drift,
    # so recomputing it would just be a second, worse implementation.
    # fin_liabilities.kind is Plaid's own bucket (credit | student |
    # mortgage); field names vary a lot by kind upstream, normalized here
    # to one shape (statement_balance/min_payment/due_day/apr) so the
    # bills_due metric doesn't need to branch on kind.
    """
    CREATE TABLE fin_recurring (
        stream_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        account_id TEXT,
        flow TEXT NOT NULL,                    -- inflow | outflow
        desc TEXT,
        merchant TEXT,
        frequency TEXT,                        -- Plaid enum: WEEKLY | BIWEEKLY | MONTHLY | ...
        avg_amount REAL,                       -- Plaid sign: positive = money OUT (outflow streams)
        last_amount REAL,
        last_day TEXT,                         -- civil date, YYYY-MM-DD
        predicted_next_date TEXT,              -- civil date, YYYY-MM-DD; Plaid's own prediction
        is_active INTEGER NOT NULL DEFAULT 1,
        status TEXT,                           -- MATURE | EARLY_DETECTION | TOMBSTONED
        refreshed_at TEXT NOT NULL
    );
    CREATE TABLE fin_liabilities (
        account_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        kind TEXT NOT NULL,                    -- credit | student | mortgage
        statement_balance REAL,
        min_payment REAL,
        due_day TEXT,                          -- civil date, YYYY-MM-DD
        apr REAL,                              -- percent, purchase/primary APR when Plaid gives several
        is_overdue INTEGER NOT NULL DEFAULT 0,
        refreshed_at TEXT NOT NULL
    );
    """,
    # v16 — Spotify provider (IDEAS §8). Day is **day_key** (4am rollover),
    # a behavioral source like §7/§9/§10. Unlike other providers' caches,
    # `plays` is NOT purely derived: Spotify's recently-played endpoint only
    # returns the last 50 tracks, so this table is the only record of
    # anything older than the polling window — losing it loses history, not
    # just cache freshness (the planned GDPR-export importer backfills into
    # this same table, one time, once the export arrives). artist_genres
    # *is* a genuine cache (droppable, re-fetched from /artists).
    """
    CREATE TABLE plays (
        played_at TEXT PRIMARY KEY,            -- ISO instant, Spotify's played_at
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        track_id TEXT NOT NULL,
        track TEXT NOT NULL,
        artist TEXT NOT NULL,
        artist_id TEXT NOT NULL,
        album TEXT,
        ms INTEGER NOT NULL,
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX plays_day ON plays (day);
    CREATE INDEX plays_artist ON plays (artist_id);
    CREATE TABLE artist_genres (
        artist_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        genres TEXT NOT NULL,                  -- JSON list
        refreshed_at TEXT NOT NULL
    );
    """,
    # v17 — messages provider (archive/ROUND34_PLAN.md §9) + generic sync_state.
    # chat.db is truth; unlike Spotify's `plays`, `messages` IS a genuine
    # derived cache — chat.db keeps full local history, so a drop only costs
    # a re-sync (up to MESSAGES_BACKFILL_DAYS back) on the next refresh, not
    # lost data. Day is **day_key** (4am rollover), a behavioral source like
    # screen/spotify/location — 1am texting belongs to yesterday. text is
    # capped at 2000 chars on write; it's already on this disk in chat.db,
    # so caching it here changes nothing about exposure (localhost-only
    # stands). sync_state is a generic (key, value) incremental-sync
    # watermark table, not messages-specific — other providers may use it.
    # NOTE: `messages` and `sync_state` have independent lifecycles — to
    # force a clean re-sync, drop/clear BOTH (see providers/messages.py
    # module docstring); dropping only `messages` leaves the old watermark
    # in place and the table never re-backfills.
    """
    CREATE TABLE sync_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE messages (
        guid TEXT PRIMARY KEY,
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        ts TEXT NOT NULL,                      -- ISO local datetime
        chat_guid TEXT,
        handle TEXT,                            -- normalized: phone last-10 | lowercase email
        contact_name TEXT,
        is_from_me INTEGER NOT NULL DEFAULT 0,
        text TEXT,                              -- capped 2000 chars
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX messages_day ON messages (day);
    CREATE INDEX messages_chat ON messages (chat_guid);
    """,
    # v18 — email extractors (archive/ROUND34_PLAN.md §15 packages + §16 travel):
    # shared pipeline in providers/email_extract.py over the already-synced
    # `emails` table. email_extract caches the raw model output per message
    # (kind = package | trip) so nothing is re-parsed/re-billed once seen,
    # mirroring email_triage's cache-by-msg_id posture. packages/trips are
    # the typed results — unlike most provider caches, these are NOT purely
    # derived: they're the only record of order/flight state this app keeps,
    # so dropping them loses real state, not just freshness (a full
    # email_extract wipe would let them rebuild, at model cost, as long as
    # the source emails are still in `emails`). packages.status only ever
    # moves forward through ordered->shipped->out_for_delivery->delivered
    # (see _upsert_package); trips.status/gate are owned by the AeroAPI
    # poller once it starts (poll_flight_status) — later confirmation
    # emails update itinerary details but never downgrade status/gate back
    # to 'confirmed'.
    """
    CREATE TABLE email_extract (
        msg_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,                    -- package | trip
        payload TEXT NOT NULL,                 -- JSON, the raw extracted object(s)
        extracted_at TEXT NOT NULL
    );
    CREATE TABLE packages (
        order_key TEXT PRIMARY KEY,            -- merchant:order_id, else tracking_no
        merchant TEXT,
        item_desc TEXT,
        order_id TEXT,
        tracking_no TEXT,
        carrier TEXT,
        eta_day TEXT,                          -- civil date, YYYY-MM-DD
        status TEXT NOT NULL,                  -- ordered | shipped | out_for_delivery | delivered
        last_msg_id TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX packages_eta ON packages (eta_day);
    CREATE TABLE trips (
        pnr TEXT NOT NULL,
        flight_no TEXT NOT NULL,
        dep_day TEXT NOT NULL,                 -- civil date, YYYY-MM-DD (from dep_local)
        carrier TEXT,
        dep_airport TEXT,
        arr_airport TEXT,
        dep_local TEXT,                        -- ISO local datetime
        arr_local TEXT,
        status TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed, else AeroAPI's live status
        gate TEXT,
        last_checked_at TEXT,                  -- last AeroAPI poll; NULL until the flight is inside FLIGHT_POLL_HORIZON_HOURS (48h, the API's own ceiling)
        last_msg_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (pnr, flight_no, dep_day)
    );
    CREATE INDEX trips_dep ON trips (dep_local);
    """,
    # v19 — people / CRM-lite (archive/ROUND34_PLAN.md §17). `people` is NOT
    # droppable — birthday/watch_cadence_days/notes are real facts with no
    # other source of truth (unlike every other table in this file, a drop
    # loses data, not just freshness). person_key is a normalized email
    # (lowercase) or phone (last 10 digits) — same normalization as
    # providers/messages.py's normalize_handle, so a person's key lines up
    # across emails.from_addr/to_addr and messages.handle without a join
    # table. last_email_at/last_msg_at/last_event_at are NOT stored here —
    # computed live in app/people.py from emails/messages/events, which stay
    # the source of truth for contact history.
    #
    # is_sent/to_addr on emails: the existing day-window sync in email.py
    # already fetches every message in range regardless of folder (Gmail's
    # after:/before: search isn't inbox-scoped), so no second "list
    # label:SENT" pass is needed — just persist the SENT label and the To
    # header, both already available where in_inbox/unread are computed.
    # to_addr holds only the first recipient (best effort, like messages.py's
    # contact_name) — enough to match a sent message back to a person_key.
    """
    CREATE TABLE people (
        person_key TEXT PRIMARY KEY,
        name TEXT,
        birthday TEXT,                         -- MM-DD, or YYYY-MM-DD if a real year is known
        watch_cadence_days INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    ALTER TABLE emails ADD COLUMN is_sent INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE emails ADD COLUMN to_addr TEXT;
    """,
    # v20 — location provider (archive/ROUND34_PLAN.md §10). OwnTracks (iOS)
    # posts fixes to a separate listener process (app/location_listener.py —
    # NOT this FastAPI app; it is the only port Tailscale Funnel exposes, so
    # the main backend never is). The `--` comments below still name Overland's
    # fields, which is what wrote every row before 2026-08-14: migration text
    # is fingerprinted, so editing them makes v20 re-run and fail. Read the
    # units off app/location_listener.py instead. Like Spotify's
    # `plays` and the messages/email_extract tables, `location_points` is
    # NOT a derived cache — it's the only record of raw location history
    # this app keeps, so a drop loses data, not just freshness.
    # `location_days` IS a derived/droppable cache, recomputed from
    # location_points by providers/location.py. Day is **day_key** (4am
    # rollover), a behavioral source like screen/spotify/messages — a 1am
    # fix belongs to yesterday.
    """
    CREATE TABLE location_points (
        ts TEXT PRIMARY KEY,                   -- ISO instant, Overland's own timestamp
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        speed REAL,
        motion TEXT,                           -- comma-joined motion[] activity types
        battery REAL,                          -- battery_level, 0..1
        accuracy REAL                          -- horizontal_accuracy, meters
    );
    CREATE INDEX location_points_day ON location_points (day);
    CREATE TABLE location_days (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, metric)
    );
    """,
    # v21 — net-worth history. finance.py overwrites
    # fin_accounts on every sync (DELETE+INSERT), so a point-in-time balance
    # is all Plaid gives us. This table is the ONLY record of balance history
    # — NOT a derived/droppable cache (like location_points/messages): a drop
    # loses the net-worth line for good and the past can't be backfilled.
    # One row per day_key (4am rollover) per env; repeated syncs within a day
    # upsert the latest snapshot. net_worth = assets − liabilities, where
    # assets are depository/investment balances and liabilities are the
    # positive amounts owed on credit/loan accounts.
    """
    CREATE TABLE fin_balance_history (
        day TEXT NOT NULL,                     -- day_key, YYYY-MM-DD
        env TEXT NOT NULL,                     -- sandbox | production
        assets REAL NOT NULL,
        liabilities REAL NOT NULL,
        net_worth REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, env)
    );
    """,
    # v22 — mute spam/automated contacts. `hidden` flags a person_key the user
    # dismissed (marketing/OTP/no-reply senders that keep surfacing under
    # "waiting on reply"). Marking one upserts a people row so it sticks even
    # for keys never CRM'd; the row is kept (not deleted) so it stays
    # reversible. Filtered out of waiting_on(), the people list, and brief
    # signals.
    """
    ALTER TABLE people ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    """,
    # v23 — transaction enrichment (BUG_CHECKS pass; archive/ROUND34_PLAN.md).
    # Plaid also exposes a brand logo_url and an in-store location; surface
    # both when present. Both are derived cache like fin_transactions — the
    # cursor sync only fills them on newly added/modified rows, so existing
    # cached rows stay NULL until a cursor reset re-pulls history (droppable).
    # location is a pre-joined "City, ST" display string (NULL when Plaid has
    # no location, which is the norm for online spend).
    """
    ALTER TABLE fin_transactions ADD COLUMN logo_url TEXT;
    ALTER TABLE fin_transactions ADD COLUMN location TEXT;
    """,
    # v24 — chat metadata cache for the conversations view (BUG_CHECKS pass;
    # archive/ROUND34_PLAN.md). The `messages` cache has no room for a chat's
    # *own* attributes (custom
    # group name, group flag) — those live once per chat_guid, not per message.
    # This table caches chat.db's `chat` rows so the conversations view can show
    # a real group name ("Family 👨‍👩‍👧") instead of a participant list. Derived
    # cache like `messages`: full-replace each sync, droppable, no watermark.
    """
    CREATE TABLE chats (
        chat_guid TEXT PRIMARY KEY,
        display_name TEXT,                     -- custom group name; NULL when unnamed
        is_group INTEGER NOT NULL DEFAULT 0,   -- chat.db style 43 = group, 45 = 1:1
        refreshed_at TEXT NOT NULL
    );
    """,
    # v25 — contact identity clusters (BUG_CHECKS pass; archive/ROUND34_PLAN.md).
    # A person owns many identifiers (two numbers + a couple emails); `people`
    # rows and
    # messages/emails each key off ONE, so the same human fragments across
    # tables. This maps every normalized identifier to a stable cluster_key,
    # built from macOS Contacts' OWN record grouping (providers/messages.py
    # sync_identities) — the authoritative "same person" signal. Merges follow
    # a shared Contacts card / shared identifier, NEVER a shared name, so two
    # distinct people with the same name never collapse. Derived cache like
    # `chats`: full-replace each sync, droppable.
    """
    CREATE TABLE contact_identities (
        identifier TEXT PRIMARY KEY,           -- normalized handle/email
        cluster_key TEXT NOT NULL,             -- canonical identifier for the person
        name TEXT,                             -- Contacts display name for the cluster
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX contact_identities_cluster ON contact_identities (cluster_key);
    """,
    # v26 — features cross-provider day matrix (archive/ROUND34_PLAN.md §11, the
    # keystone). One row per (day, feature): a pure denormalization of the
    # other providers' cache tables (health/screen/spotify/messages/location/
    # finance/events/tasks/weather) so the brief/console can reason over
    # passive signals in one place. Fully derived/droppable — providers/
    # features.py recomputes it from those tables with SQL/python only, no
    # external calls. A source with no data for a day is simply absent (a gap,
    # never a 0), so a real zero and "no signal" stay distinguishable. `day` is
    # each source's own key (day_key for behavioral sources + tasks, civil date
    # for health/weather/calendar/finance) — not re-bucketed.
    """
    CREATE TABLE features (
        day TEXT NOT NULL,                     -- YYYY-MM-DD (source's own key)
        feature TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, feature)
    );
    """,
    # v27 — intraday heart rate (archive/ROUND34_PLAN.md §12). One row per (day, hour):
    # the hourly mean bpm from Google Health's heart-rate rollUp endpoint, kept
    # for a short trailing window (the API caps heart-rate rollups at 14 days).
    # Derived/droppable like health_days — health.py refills it from the API,
    # and features.py folds it into the `hr_var_day` feature (spread of a day's
    # hourly HR = a same-day physiological-load proxy). `day`/`hour` are LOCAL
    # (the rollup's UTC buckets converted to the user's tz), so they line up
    # with health_days' civil dates. No sync marker: the window is small and
    # simply recomputed each refresh.
    """
    CREATE TABLE health_hours (
        day TEXT NOT NULL,                     -- local civil date, YYYY-MM-DD
        hour INTEGER NOT NULL,                 -- local hour, 0–23
        metric TEXT NOT NULL,                  -- 'hr_hourly'
        value REAL NOT NULL,                   -- mean bpm in the hour bucket
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, hour, metric)
    );
    """,
    # v28 — transaction website + counterparties (archive/ROUND34_PLAN.md §11 polish,
    # same cache-and-render pattern as v23's logo_url/location). Plaid also
    # returns a merchant `website` and a `counterparties` array (payment apps,
    # marketplaces, processors — the "how you paid" beyond the merchant name).
    # website is stored raw; counterparties is pre-joined to a display string of
    # the non-merchant parties (providers/finance._format_counterparties), NULL
    # when there are none. Derived cache like the rest of fin_transactions — the
    # cursor sync only fills them on newly added/modified rows, so existing rows
    # stay NULL until a cursor reset re-pulls history (droppable).
    """
    ALTER TABLE fin_transactions ADD COLUMN website TEXT;
    ALTER TABLE fin_transactions ADD COLUMN counterparties TEXT;
    """,
    # v29 — habit check-in provenance (auto exercise check-in). `source` mirrors
    # tasks.source: who created the row — 'ui'/'console' (a person did it) vs
    # 'auto' (a provider signal did, e.g. health AZM ≥ threshold checks the
    # Exercise habit). Auto check-ins never overwrite a person's row (INSERT ...
    # DO NOTHING), and an un-check now leaves a value=0 tombstone instead of
    # deleting, so an auto pass can't resurrect a day the user deliberately
    # cleared — the habit analog of §16's dropped-auto-task-not-recreated rule.
    # value=0 rows read as "not done" everywhere (streak filters value>0).
    """
    ALTER TABLE habit_checkins ADD COLUMN source TEXT NOT NULL DEFAULT 'ui';
    """,
    # v30 — email undo. Gmail mutations are one-way from the API's side (you
    # can re-add INBOX, but nothing tells you what the message looked like
    # before), so the action route records what it changed: JSON
    # {"unread": 0|1, "label_id": "..."} written at action time and replayed in
    # reverse by POST /api/email/{id}/undo. NULL for rows triaged before this
    # existed — those undo on a best-effort basis (back to inbox, read).
    """
    ALTER TABLE email_triage ADD COLUMN undo_state TEXT;
    """,
    # v31 — "not something to respond to". Distinct from people.hidden, which
    # mutes a sender forever (bots, spam): this dismisses ONE dangling thread
    # while keeping the person. `through_ts` is the last_at that was dismissed,
    # so the row stays gone only while it's the newest thing they said — write
    # again and they resurface on their own, no un-dismissing needed. Keyed
    # (kind, key) to match the waiting feed's own identifiers: a person_key
    # for messages, a to_addr for email.
    """
    CREATE TABLE IF NOT EXISTS waiting_dismissed (
      kind        TEXT NOT NULL,
      key         TEXT NOT NULL,
      through_ts  TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (kind, key)
    );
    """,
    # v32 — tapbacks, which are neither messages nor nothing. Direction is
    # what decides: a ❤️ *they* put on something you said is not them asking
    # you anything, but a ❤️ *you* left is you answering. Storing both with
    # a flag lets the waiting feed read that asymmetry while volume metrics
    # (sent/received/participant counts) keep excluding them — reactions
    # outnumber real messages here and would swamp any count.
    """
    ALTER TABLE messages ADD COLUMN is_reaction INTEGER NOT NULL DEFAULT 0;
    """,
    # v33 — provider health. The poller already learns, every cycle, whether
    # each provider's refresh() raised; that outcome only ever reached stderr,
    # so a died-in-the-night Fitbit token was something you noticed by seeing
    # a widget look wrong. One row per provider, overwritten each cycle —
    # fully derived/droppable (the next cycle rebuilds it, and a missing row
    # just means "hasn't run yet", which is what a cold boot should show).
    # `fails` counts *consecutive* failures so the UI can tell a blip apart
    # from a source that's been down for hours.
    """
    CREATE TABLE provider_health (
      provider         TEXT PRIMARY KEY,
      ok               INTEGER NOT NULL,   -- did the last attempt succeed
      error            TEXT,               -- repr of the failure, NULL when ok
      info             TEXT,               -- JSON: refresh()'s own info dict
      last_attempt_at  TEXT NOT NULL,
      last_ok_at       TEXT,               -- survives later failures
      duration_ms      INTEGER,
      fails            INTEGER NOT NULL DEFAULT 0
    );
    """,

    # v34 — provider event log. provider_health holds only the *latest* cycle,
    # so a source that broke at 3am and recovered by 9 leaves no trace at all
    # (`fails` resets on the first success). This is the append-only companion:
    # one row per state change, plus the sub-step failures that never reach
    # provider_health because refresh() caught them and returned success
    # anyway — a Plaid leg 500ing, chat.db schema drift, an AeroAPI timeout.
    # Transitions only, never every cycle: a source down all night would
    # otherwise write 288 identical rows. Derived/droppable, trimmed to
    # EVENT_LOG_KEEP so it can't grow without bound.
    """
    CREATE TABLE provider_events (
      id        INTEGER PRIMARY KEY,
      provider  TEXT NOT NULL,
      at        TEXT NOT NULL,
      kind      TEXT NOT NULL,   -- broke | recovered | degraded
      detail    TEXT             -- the error, or the sub-step that failed
    );
    CREATE INDEX idx_provider_events ON provider_events (provider, id DESC);
    """,

    # v35 — artist identity that survives the import seam. `plays.artist_id`
    # holds Spotify's real id on rows the poller captured and a synthetic
    # "sha:"+sha1(name) on the ~266k rows the GDPR importer wrote (neither
    # export carries an artist id). Grouping on it therefore splits every
    # artist in two at 2026-07-14, which pinned `novelty` at ~1.0 for twelve
    # days — a six-year favourite read as brand-new the first time the poller
    # saw it. artist_key is the honest join key: the artist *name*, normalized
    # in Python (str.casefold — SQLite's lower() is ASCII-only and mangles
    # e.g. "Vîęzë Ãsbæk"), so both sides agree. Backfilled and kept current by
    # spotify._ensure_artist_keys(); nullable so the DDL stays pure SQL.
    #
    # artist_genres goes at the same time: it has never held a row. It was fed
    # by the batch GET /artists?ids= endpoint, which 403s for this app (only
    # single-resource GETs are permitted), and Spotify has since stopped
    # returning the `genres` field on the artist object at all — the response
    # is now id/name/images/urls. Genres come from Last.fm instead, keyed on
    # artist_key rather than any Spotify id, in artist_tags.
    """
    ALTER TABLE plays ADD COLUMN artist_key TEXT;
    CREATE INDEX plays_artist_key ON plays (artist_key);

    DROP TABLE IF EXISTS artist_genres;

    CREATE TABLE artist_tags (
      artist_key    TEXT PRIMARY KEY,   -- normalized artist name (plays.artist_key)
      name          TEXT NOT NULL,      -- Last.fm's canonical spelling
      tags          TEXT NOT NULL,      -- JSON list, most-weighted first
      mbid          TEXT,
      refreshed_at  TEXT NOT NULL,
      missing       INTEGER NOT NULL DEFAULT 0  -- looked up, Last.fm knows nothing
    );
    """,

    # v36 — keep Last.fm's untouched reply next to the cleaned list. `tags` is
    # the product of a blocklist, a weight floor and a cap, all of which are
    # judgement calls that will be revised as the tag noise shows itself
    # (nationalities, "seen live", an artist tagged with their own name).
    # Without the raw list every such revision costs a full 9.8k-artist refetch
    # of data Last.fm already gave us; with it, `lastfm reclean` re-derives
    # every row offline in a second. Nullable: rows written before v36 simply
    # don't participate in a reclean until they're next fetched.
    """
    ALTER TABLE artist_tags ADD COLUMN raw TEXT;   -- JSON: toptags.tag verbatim
    """,

    # v37 — screen sync markers, so a day AW saw nothing on can say nothing.
    # screen.py used `active_min`'s row presence to mean "this day was
    # fetched", which forced it to write active_min=0.0 even on days AW had
    # no events at all (asleep, aw-server down, not yet installed) — a false
    # zero that features.py passed straight into the correlation matrix.
    # screen_sync carries that meaning instead (health_days/health_sync's
    # arrangement), freeing every metric to be absent on an uncovered day.
    # Backfill marks every day already in the cache as synced (nothing
    # re-pulls) and drops the historical false zeros: a day with no
    # screen_apps rows had no active window events, so its active_min /
    # doomscroll_min zeros were never observations. The same rows are
    # deleted from `features`, which already copied them — it backfills
    # per day, not per feature, so a day computed once would keep its zeros
    # forever otherwise.
    """
    CREATE TABLE screen_sync (
        day TEXT PRIMARY KEY,                  -- day_key, YYYY-MM-DD
        refreshed_at TEXT NOT NULL
    );
    INSERT INTO screen_sync (day, refreshed_at)
        SELECT day, MAX(refreshed_at) FROM screen_days GROUP BY day;
    DELETE FROM screen_days
     WHERE metric IN ('active_min', 'doomscroll_min')
       AND day NOT IN (SELECT day FROM screen_apps);
    DELETE FROM features
     WHERE feature IN ('active_min', 'doomscroll_min')
       AND day NOT IN (SELECT day FROM screen_apps);
    """,
    # v38 — attachments: what was sent, separate from what it says.
    # `messages.text` already names attachments ("(photo)", "(2 PDFs)") but a
    # label isn't addressable — you can't ask "the packet Dad sent" without a
    # filename. This table makes them referenceable; `attachment_text` caches
    # what extraction found, and stays empty until something asks (extraction
    # costs model tokens for images, and parses untrusted bytes for PDFs).
    #
    # Derived and droppable, both tables: iMessage rows rebuild from chat.db
    # on the next full sync, Gmail rows from the API. Losing attachment_text
    # costs only re-extraction.
    #
    # `locator` is deliberately source-shaped, not a path: for messages it's
    # an absolute local path (chat.db stores it '~'-prefixed; expanded on the
    # way in), for email a Gmail attachmentId that must be exchanged for
    # bytes through the API. Nothing downloads during sync — email bytes are
    # fetched only on demand, so an inbox of hostile PDFs never lands on disk
    # by itself. iMessage attachments are a different case: Messages.app has
    # already written them, so a row here points at a file that exists.
    #
    # text is capped at extraction time; status distinguishes "extracted,
    # nothing there" (empty) from "never tried" (no row) and from the several
    # ways parsing can refuse — see providers/attachments.py.
    """
    CREATE TABLE attachments (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,                  -- message | email
        source_ref TEXT NOT NULL,              -- messages.guid | emails.msg_id
        ord INTEGER NOT NULL DEFAULT 0,        -- position within the message
        day TEXT NOT NULL,                     -- day_key of the parent message
        filename TEXT,                         -- original name as sent
        mime TEXT,
        size_bytes INTEGER,
        locator TEXT,                          -- abs path (message) | attachmentId (email)
        is_sticker INTEGER NOT NULL DEFAULT 0,
        seen_at TEXT NOT NULL,
        UNIQUE (source, source_ref, ord)
    );
    CREATE INDEX attachments_day ON attachments (day);
    CREATE INDEX attachments_ref ON attachments (source, source_ref);

    CREATE TABLE attachment_text (
        attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
        status TEXT NOT NULL,                  -- ok|empty|unsupported|too_big|missing|timeout|error
        method TEXT,                           -- pypdf | vision | plain
        text TEXT,                             -- extracted; UNTRUSTED (attacker-authored)
        chars INTEGER,
        extracted_at TEXT NOT NULL
    );
    """,
    # v39 — three lists the old vault kept as markdown tables (debts.md,
    # shopping.md, watchlist.md), plus the day-one tag from day-ones.md.
    # They move into app.db for the reason tasks did: the markdown was both
    # hand-edited and agent-edited, so neither copy was truth. The old files
    # are NOT read back after this — same rule as tasks.md.
    #
    # `debts.person` is free text with an optional `person_key`: the CRM only
    # knows people who have messaged or emailed, and most of these owe from
    # a beach week, not a thread. Amounts are stored as one row per debt
    # rather than a running total, so "Iris owes $24 + $25.95" keeps its two
    # reasons and either half can settle on its own.
    #
    # people.day_one is the filter asked for; people.crew keeps what a bare
    # boolean would have thrown away — day-ones.md grouped them by where
    # they're known from (Kramden / NCAAT / Triangle Insights Group), which
    # is a different axis from "close" and useful on its own.
    """
    CREATE TABLE debts (
        id INTEGER PRIMARY KEY,
        person TEXT NOT NULL,                  -- free text; not everyone is in the CRM
        person_key TEXT,                       -- people.person_key when they are
        direction TEXT NOT NULL,               -- owed_to_me | i_owe
        amount REAL NOT NULL,                  -- dollars
        reason TEXT,
        settled INTEGER NOT NULL DEFAULT 0,
        incurred_on TEXT,                      -- YYYY-MM-DD, best known
        settled_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX debts_open ON debts (settled, direction);

    CREATE TABLE shopping_items (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT,                         -- Clothing | Groceries | ...
        note TEXT,
        bought INTEGER NOT NULL DEFAULT 0,
        bought_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX shopping_open ON shopping_items (bought);

    CREATE TABLE watchlist (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        year INTEGER,
        kind TEXT NOT NULL DEFAULT 'movie',    -- movie | show
        services TEXT,                         -- where it streams, comma-separated
        note TEXT,
        watched INTEGER NOT NULL DEFAULT 0,
        watched_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX watchlist_open ON watchlist (watched);

    ALTER TABLE people ADD COLUMN day_one INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE people ADD COLUMN crew TEXT;
    """,
    # v40 — reddit provider cache. Arctic Shift is truth; both tables are
    # derived and droppable (drop BOTH together — reddit_sync alone would leave
    # days marked fetched with no rows behind them).
    #
    # `day` is the day_key of created_utc, not its civil date: a post made at
    # 1am belongs to the night before, the same fold screen/spotify/messages
    # use. So a day's window is [04:00, 04:00) local, which is what _sync asks
    # Arctic Shift for in epoch seconds.
    #
    # score/num_comments are NULLABLE and that is the point. A post pulled from
    # the .rss fallback (removed 11-22) had no score at all, and the archive
    # omits counts on removed posts; storing 0 for "not known yet"
    # is how the screen provider got its historical false zeros (see v37) — the
    # number is indistinguishable from a real one once it's in the table. NULL
    # sorts last in the `top` metric and reads back as null, not as unpopular.
    #
    # reddit_sync marks a day as fetched even when it produced no rows, so a
    # genuinely quiet day doesn't look like a gap and get re-fetched on every
    # single read.
    """
    CREATE TABLE reddit_posts (
        day TEXT NOT NULL,                     -- day_key of created_utc
        id TEXT NOT NULL,                      -- reddit base36 id, e.g. 1vm1q3x
        subreddit TEXT NOT NULL,
        title TEXT NOT NULL,
        author TEXT NOT NULL,
        selftext TEXT NOT NULL DEFAULT '',
        url TEXT,                              -- outbound target for link posts
        permalink TEXT NOT NULL,
        score INTEGER,                         -- NULL = not known yet
        num_comments INTEGER,                  -- NULL = not known yet
        created_utc INTEGER NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, id)
    );
    CREATE INDEX reddit_posts_sub ON reddit_posts (subreddit, day);

    CREATE TABLE reddit_sync (
        day TEXT PRIMARY KEY,                  -- day_key, YYYY-MM-DD
        refreshed_at TEXT NOT NULL
    );
    """,
    # v41 — the shopping list generalizes. v39 gave `shopping_items` its own
    # table because the vault had exactly one buy-list; the second list wanted
    # (packing for college, a party) would have arrived as either a second
    # near-identical table or a `kind` column nobody could keep straight. So
    # the list itself becomes a row: `lists` holds the names, `list_items`
    # holds what used to be shopping_items, one list_id deeper.
    #
    # shopping_items is DROPPED here rather than left behind, and its rows are
    # carried over ids-and-all into the seeded 'Shopping' list. Ids survive
    # because they are what the console addresses an item by (`list_item_done`
    # takes the id a db_query handed back); renumbering them would strand
    # every id in an open session's history. Nothing reads shopping_items
    # after this migration — it is not a second copy, it is gone.
    #
    # `name` is UNIQUE COLLATE NOCASE so "College" and "college" can't become
    # two chips holding half the packing list each. The item rows carry no
    # such constraint: two "milk"s on a buy list is a legitimate thing to type
    # twice, and de-duping them is the user's call, not the schema's.
    """
    CREATE TABLE lists (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        position INTEGER NOT NULL DEFAULT 0,   -- chip order; ties break on id
        created_at TEXT NOT NULL
    );

    CREATE TABLE list_items (
        id INTEGER PRIMARY KEY,
        list_id INTEGER NOT NULL REFERENCES lists (id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        category TEXT,                         -- Groceries | Dorm | ...
        note TEXT,
        done INTEGER NOT NULL DEFAULT 0,
        done_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX list_items_open ON list_items (list_id, done);

    INSERT INTO lists (name, position, created_at)
        VALUES ('Shopping', 0, strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'));

    INSERT INTO list_items (id, list_id, name, category, note, done, done_at,
                            created_at)
        SELECT id, (SELECT id FROM lists WHERE name = 'Shopping'),
               name, category, note, bought, bought_at, created_at
        FROM shopping_items;

    DROP INDEX shopping_open;
    DROP TABLE shopping_items;
    """,
    # v42 — §21 alarms. What the dashboard has never had is a way to reach the
    # user when the laptop is shut, and the fix isn't a notifier: it's writing a
    # *future-dated* alarm to a surface the phone syncs, while the lid is open.
    # The machine computes; the phone delivers.
    #
    # NOT derived, despite looking it. Every other cache table here can be
    # dropped and rebuilt from its source, but `external_id` is the only record
    # of what was written to Reminders — lose it and the alarms already on the
    # phone are unreachable, un-withdrawable, and will fire anyway. Back it up
    # with app.db, and never DELETE a row without withdrawing the alarm first.
    #
    # `key` is 'kind:...' and the prefix is load-bearing: the reconciler
    # partitions written rows by kind so a source that failed to read only ever
    # holds its *own* alarms. Built in one place (alarms._alarm_key).
    #
    # `fire_at` is offset-aware ISO, not wall clock. An alarm is a promise about
    # an instant, and this row can outlive a move across TIMELINE — a bare
    # '09:00' would silently mean a different moment after 8/17.
    """
    CREATE TABLE alarms (
        key         TEXT PRIMARY KEY,          -- 'kind:...', built by _alarm_key
        kind        TEXT NOT NULL,             -- flight | package | leave
        fire_at     TEXT NOT NULL,             -- ISO 8601 *with* offset
        title       TEXT NOT NULL,
        body        TEXT,
        external_id TEXT,                      -- Reminders item; NULL while dry-run
        source_ref  TEXT,                      -- the row this came from, for tracing
        written_at  TEXT,                      -- when the writer last succeeded
        refreshed_at TEXT NOT NULL             -- when the reconciler last saw it desired
    );
    CREATE INDEX alarms_kind ON alarms (kind);
    CREATE INDEX alarms_fire ON alarms (fire_at);
    """,
    # v43 — §21 step 4, the at-desk channel. The alarms table above is for
    # alerts with a known future time, which the phone can be handed hours in
    # advance. This one is for the fact that just became true and matters within
    # minutes (a flight slipped, a package is out for delivery): there is no
    # future time to date it to, so it is delivered here, now, or not at all.
    #
    # Persisted rather than fired inline for two reasons. Dedupe has to survive a
    # restart — an in-memory set would re-announce everything on every reload,
    # and this backend runs --reload. And a row is what lets delivery *wait*: a
    # banner posted while nobody is at the desk is indistinguishable from one
    # that was seen, so the queue holds until presence says otherwise.
    #
    # `key` is 'kind:...' like alarms (built by app/keys.py) and identifies the
    # *fact*, not the moment — a status re-observed on every poll cycle is one
    # row, while a status that flaps back and forth is two facts and two rows.
    #
    # sent_at and expired_at are both nullable timestamps rather than one state
    # column: each records when something happened, so there is no state string
    # that can hold a value no code writes. Queued means both are NULL.
    """
    CREATE TABLE notifications (
        key        TEXT PRIMARY KEY,           -- 'kind:...', one row per fact
        kind       TEXT NOT NULL,              -- flight | package
        title      TEXT NOT NULL,
        body       TEXT,
        queued_at  TEXT NOT NULL,              -- when the sender saw the fact
        sent_at    TEXT,                       -- when a banner actually posted
        expired_at TEXT                        -- set instead, if it went stale
    );
    CREATE INDEX notifications_queue ON notifications (sent_at, expired_at, queued_at);
    """,

    # v44 — failures. `provider_health`/`provider_events` cover the poller and
    # nothing else, so three layers still fail into a log that /tmp eventually
    # deletes: an on-demand read raising inside `providers/api.py` (the poller
    # path is instrumented, the path the desk hits on every load is not), a
    # widget throwing during render, and a console turn coming back is_error.
    # Same argument v34 already made for sub-step failures, one layer out.
    #
    # Keyed by fingerprint rather than appended per occurrence, which is the
    # only shape that survives the failure mode this exists to catch: a widget
    # that throws on render throws again on every re-render, and an event
    # stream would be a thousand identical rows an hour — v34's "288 identical
    # rows" argument, except here the loop is tight enough to bloat the DB.
    # So one row per distinct failure, `first_at`/`last_at` bracketing it and
    # `count` carrying the volume. That also makes the ranking question ("what
    # breaks most") an ORDER BY instead of a GROUP BY.
    #
    # Derived/droppable like its two siblings: nothing reads it back into the
    # app, it is trimmed to FAILURE_KEEP, and losing it costs only history.
    """
    CREATE TABLE failures (
        fingerprint TEXT PRIMARY KEY,   -- origin+label+detail, hashed in Python
        origin      TEXT NOT NULL,      -- api | desk | console
        kind        TEXT NOT NULL,      -- unhandled | render | promise | turn
        label       TEXT NOT NULL,      -- the route, widget or tool that failed
        detail      TEXT,               -- the message, verbatim
        trace       TEXT,               -- traceback / component stack, newest
        first_at    TEXT NOT NULL,
        last_at     TEXT NOT NULL,
        last_day    TEXT NOT NULL,      -- db.day_key(), for slicing by day
        count       INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX failures_recent ON failures (last_at DESC);
    """,

    # v45 — usage. The desk ranks itself (`lib/salience.js`) and nothing has
    # ever checked whether the ranking is right, or whether a card still earns
    # its slot. Seventeen cards render every morning and some of them have not
    # been worth reading since the week they shipped; from inside the app they
    # are indistinguishable from the ones you live in.
    #
    # Counted per **day**, not as a running total, because the question is
    # "still used?" and a total cannot tell fifty opens last March from five a
    # week since. Distinct days is the honest unit of a habit; the raw `count`
    # is kept alongside it but is the weaker number.
    #
    # `band` is what stops this lying. A card read at a glance leaves no trace
    # at all — no click, no unfold — so interaction alone would convict every
    # card that does its job in one line, which is most of the good ones. The
    # band it was sitting in is the tie-breaker: a card that has been in
    # `quiet` every day for a month and never opened is dead weight, while one
    # that sits in `needs`/`has` unopened is being read exactly as designed.
    # Same shape as the "silence is a signal, but only if the source would
    # have sent something" rule in Recurring lessons.
    #
    # kinds: seen (rendered on the desk, band set) | open (you unfolded a card
    # the desk had folded — the ranking being told it was wrong) | sheet (the
    # full slide-over) | command (⌘K). Derived/droppable: nothing reads it
    # back into the app, and losing it costs only the history.
    """
    CREATE TABLE usage (
        day     TEXT NOT NULL,          -- db.day_key()
        kind    TEXT NOT NULL,          -- seen | open | sheet | command
        label   TEXT NOT NULL,
        band    TEXT NOT NULL DEFAULT '',  -- needs|has|quiet for `seen`, else ''
        count   INTEGER NOT NULL DEFAULT 1,
        last_at TEXT NOT NULL,
        PRIMARY KEY (day, kind, label, band)
    );
    CREATE INDEX usage_kind ON usage (kind, label);
    """,

    # v46 — drop §7. The screen-time provider measured the laptop only, and the
    # laptop is not where the time goes: the honest reading of a card showing
    # 4h of Mac use next to an unmeasured phone is that it answers the wrong
    # question, however well it answers it. The phone half has no route to
    # build — Apple ships no Screen Time API, and knowledgeC.db is
    # undocumented, needs Full Disk Access the app bundle doesn't have, and
    # gets its schema reshuffled between releases. Removed 2026-09-13 by
    # decision, not by defect.
    #
    # The 332 days collected since 2026-01-01 are dumped to
    # backend/data/backups/screen-tables-*.sql before this runs, which is the
    # only copy — these three tables were the source of truth for §7, not a
    # cache of something upstream (aw-server keeps its own peewee db, but the
    # LaunchAgent goes too). DROP is therefore genuinely destructive here in a
    # way dropping a provider cache table normally is not.
    #
    # IF EXISTS on all three: a fresh app.db built after this migration never
    # created them, and a plain DROP would make first boot fail.
    """
    DROP TABLE IF EXISTS screen_apps;
    DROP TABLE IF EXISTS screen_days;
    DROP TABLE IF EXISTS screen_sync;
    DELETE FROM provider_health WHERE provider = 'screen';
    DELETE FROM provider_events WHERE provider = 'screen';
    DELETE FROM features WHERE feature IN
        ('active_min', 'doomscroll_min', 'focus_min', 'longest_focus_min',
         'app_switches');
    """,

    # v47 — debts remember being chased. An open IOU is not one fact but two:
    # the money, and whether you've said anything about it lately. Without the
    # second the card could only ever sit in HAS — "someone owes you $106" is
    # true every day and actionable on none of them, which is how it took 44
    # impressions and 3 opens.
    #
    # `reminded_at` is the whole mechanism: NULL means never asked (act now),
    # recent means asked and it's their turn (stay quiet), and older than
    # DEBT_NUDGE_DAYS means the ask has gone unanswered and it's yours again.
    # Stored as a timestamp but always *read* through day_key, like every other
    # "how many days ago" in this schema.
    #
    # `reminded_count` is not used for the nudge and is kept anyway: the number
    # of times you've had to ask is the part that decides whether to keep
    # asking or write it off, and it cannot be recovered later from a single
    # overwritten timestamp.
    """
    ALTER TABLE debts ADD COLUMN reminded_at TEXT;
    ALTER TABLE debts ADD COLUMN reminded_count INTEGER NOT NULL DEFAULT 0;
    """,

    # v48 — proposed office-hours amendments, waiting on you.
    #
    # A Piazza post saying "no office hours Thursday" is the one piece of
    # course data that arrives by mail and goes stale in the vault, and the
    # answer to it is already built (courses.set_exception writes a dated
    # override). What was missing is the gap between reading the mail and
    # trusting it: an extractor confident enough to rewrite Thursday's hours
    # on its own can also send you across campus to a cancelled session, and
    # the failure is silent — you find out by standing outside a locked door.
    #
    # So this table is the waiting room. Nothing here has touched the vault;
    # approving is what calls set_exception, and the vault stays the only
    # source of truth for what the hours *are*.
    #
    # `quote` is required and is why the table earns its keep: the proposal is
    # only checkable if you can see the sentence it came from, verbatim, next
    # to it. Everything else in the row is the model's reading of that
    # sentence. Note that it holds email body text, which the extract pipeline
    # otherwise deliberately never stores (email_triage.body_excerpt fetches
    # transiently) — one sentence per proposal is the whole exception, and it
    # is deleted with the row.
    #
    # Rejections are kept rather than deleted: the same message must not come
    # back next cycle, and `email_extract` alone can't carry that because one
    # message can propose amendments for several dates.
    """
    CREATE TABLE oh_proposals (
        id         INTEGER PRIMARY KEY,
        msg_id     TEXT NOT NULL,             -- the email it was read from
        course     TEXT NOT NULL,             -- course code, e.g. "CSCI 103"
        who        TEXT,                      -- instructor/TA, NULL = the only entry
        day        TEXT NOT NULL,             -- YYYY-MM-DD the override applies to
        action     TEXT NOT NULL,             -- cancel | change
        when_str   TEXT,                      -- "HH:MM-HH:MM" for change, else NULL
        where_str  TEXT,                      -- new room, when the mail says one
        note       TEXT,                      -- short human reason, for the override
        quote      TEXT NOT NULL,             -- the sentence, verbatim
        subject    TEXT,
        from_addr  TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
        applied    TEXT,                      -- set_exception's own summary line
        decided_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (msg_id, course, day, action)
    );
    CREATE INDEX oh_proposals_pending ON oh_proposals (status, day);
    """,
    # v49 — one night and one day, drawn instead of summed (IDEAS, 09-13).
    #
    # `health_days` has always held per-night stage *totals*, which is enough
    # to say "5.2h, of which 66m deep" and not enough to draw the night: when
    # you fell asleep, how the stages interleaved, where the 3am wake sits. The
    # segments were never missing from the API — the `sleep` dataPoint that
    # health.py already fetches carries a full `sleep.stages` array at 30-second
    # resolution, and the provider read `summary.stagesSummary` off that same
    # point and dropped the rest. This table stops dropping it.
    #
    # Keyed on the wake-up civil date exactly like the sleep_* metrics in
    # health_days, so a night joins to its own totals. Naps land on the same day
    # as the main session by that rule, which is why `seq` orders segments
    # across the whole day rather than within one session — the sheet draws a
    # day's sleep, not a session's.
    #
    # Timestamps are stored as local ISO8601 *with offset* rather than UTC. The
    # hypnogram is read against the clock you slept by, and the terrain sheet
    # (09-05) is the cautionary tale: converting stored UTC with the browser's
    # current zone silently moved six years of evenings into the small hours
    # after the desk changed coasts. The offset travels with the row.
    #
    # Derived and droppable like health_days: health.py refills from the API.
    #
    # The two marks on health_sync are here for the same reason
    # `health_sync.refreshed_at` exists: an empty read has to be able to mean
    # "asked, and there was nothing" instead of "never asked". Both drill-downs
    # fill on demand (a night from July was synced long before this table
    # existed), and without a mark a night you genuinely didn't wear the watch
    # for would re-hit the API on every open, forever. They're two columns
    # because they're two pulls: segments ride the daily sleep fetch, hourly
    # steps are their own rollUp call.
    """
    CREATE TABLE sleep_segments (
        day         TEXT NOT NULL,     -- wake-up civil date, YYYY-MM-DD
        seq         INTEGER NOT NULL,  -- order within the day, by start time
        stage       TEXT NOT NULL,     -- deep | rem | light | awake
        start_ts    TEXT NOT NULL,     -- local ISO8601 with offset
        end_ts      TEXT NOT NULL,
        minutes     REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, seq)
    );
    ALTER TABLE health_sync ADD COLUMN segments_at TEXT;
    ALTER TABLE health_sync ADD COLUMN steps_at TEXT;
    """,
    # v50 — basemap tiles, so the location map has something under the dots.
    #
    # The frontend can't fetch these (image hosts outside the CDN allowlist are
    # blocked) and shouldn't refetch them (somebody else's tile server), so the
    # backend fetches each tile once and keeps it. Street geometry doesn't
    # move, which is why there is no TTL: `fetched_at` is for reading, not for
    # expiring.
    #
    # Derived and droppable, like every provider cache — dropping this table
    # costs one re-fetch per tile you look at again, and nothing else. It is
    # the only table in the app holding a blob, so it's also the only one worth
    # watching the size of: ~15KB a tile, a few hundred tiles for a city you
    # actually pan around.
    """
    CREATE TABLE tile_cache (
        z          INTEGER NOT NULL,
        x          INTEGER NOT NULL,
        y          INTEGER NOT NULL,
        png        BLOB NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (z, x, y)
    );
    """,

    # v51 — what the outside world calls a coordinate, remembered.
    #
    # Naming an unnamed cluster asks two strangers (Google's reverse geocoder,
    # Overpass) what is at a point. Both answers are about buildings, which do
    # not move, so like the tiles above there is no TTL: `fetched_at` is a
    # reading and not an expiry. Keyed by the coordinate rounded to 4dp (~11m,
    # finer than the fixes and coarser than the jitter between two recomputes
    # of the same cluster's centroid) so re-opening the same place is free.
    #
    # Derived and droppable — dropping it costs one lookup per place you ask
    # about again. The *answers you chose* are not here: those are `type:
    # place` notes in the vault, which is the one source of truth for a name.
    """
    CREATE TABLE geo_lookup_cache (
        source     TEXT NOT NULL,           -- 'google' | 'overpass'
        lat        REAL NOT NULL,
        lon        REAL NOT NULL,
        payload    TEXT NOT NULL,           -- candidates as JSON
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, lat, lon)
    );
    """,

    # v52 — which kind of evidence you actually accept.
    #
    # §24's suggester ranks name candidates with fixed per-source weights, and
    # fixed is the wrong shape for a number nobody can know in advance: whether
    # the calendar beats OpenStreetMap depends on whose calendar and whose
    # streets. Every shortlist you act on is a labelled example — one candidate
    # accepted, the others shown and passed over — so the weights can be the
    # accept rate per evidence kind instead of a guess.
    #
    # One row per candidate *shown*, not per suggestion, because that is the
    # quantity the ranking needs: given a candidate from this kind of evidence,
    # how often is it the one you take.
    #
    # NOT a derived cache, unlike every other table §24 added. A rejection
    # exists nowhere else — the vault records the names you chose and is silent
    # about the ones you turned down — so dropping this loses real information
    # and the weights fall back to their priors.
    """
    CREATE TABLE place_feedback (
        id      INTEGER PRIMARY KEY,
        at      TEXT NOT NULL,
        lat     REAL NOT NULL,          -- the cluster asked about, 4dp
        lon     REAL NOT NULL,
        name    TEXT NOT NULL,          -- the candidate shown
        bucket  TEXT NOT NULL,          -- kind of evidence, not which API
        rank    INTEGER NOT NULL,       -- where it sat in the shortlist
        outcome TEXT NOT NULL           -- accepted | passed | dismissed
    );
    CREATE INDEX place_feedback_bucket ON place_feedback (bucket);
    CREATE INDEX place_feedback_cell ON place_feedback (lat, lon);
    """,

    # v53 — which place, on which visit.
    #
    # §24 assumed a cluster is a place. At campus density it is not: the
    # busiest unnamed cluster here spans 168m and contains Leavey Library,
    # McCarthy Quad, Everybody's Kitchen, Birnkrant and the SSCI building, and
    # which one a given visit was is a different answer each time.
    #
    # Nothing automatic can separate them, and all three routes were measured
    # before this table was added rather than after:
    #   * coordinates — p50 17m / p90 47m of error against ~15m of separation,
    #     the measurement app/places.py was founded on;
    #   * time of day — no modes; visits run 08:00 to 02:00;
    #   * the calendar — of 71 visit/event overlaps, 8 were spatially usable.
    #     48% geocode to the campus centroid and 41% merely coincide in time.
    # So the label comes from the person, and this is where it lands.
    #
    # In app.db and not the vault because this is not a note: it is one
    # assertion per visit, hundreds of them, keyed by an instant — the same
    # shape as a checkin, which lives here for the same reason. The *names*
    # it refers to are still vault places.
    #
    # `started_at` is the visit's first fix. Stay boundaries are computed from
    # `location_points` alone, which is append-only, so a past visit's start is
    # stable; a label is matched back to any stay whose span contains it, so a
    # stay split by a range boundary keeps its label rather than orphaning it.
    """
    CREATE TABLE visit_labels (
        started_at TEXT PRIMARY KEY,   -- the visit's first fix, ISO
        lat        REAL NOT NULL,      -- the cluster it was made in, 4dp
        lon        REAL NOT NULL,
        name       TEXT NOT NULL,      -- which place, as you said it
        at         TEXT NOT NULL       -- when you said so
    );
    CREATE INDEX visit_labels_cell ON visit_labels (lat, lon);
    """,

    # v54 — what a sentence was read off.
    #
    # The console restates detail that came from somewhere: an address out of
    # an email, a room change out of an event, a number someone texted. The
    # sentence keeps the fact and drops the source, and checking it meant
    # searching Gmail for whatever you half-remembered the subject was. This
    # table is the handle that gets the source back.
    #
    # `ref` is derived, not assigned: sha1(kind + target), truncated. That
    # matters more than it looks. A counter would need session state at the
    # point the brief line is rendered, would renumber across sessions, and
    # would make a replayed transcript resolve its refs to different rows
    # than it did when it was live. A derived ref is stable forever, so the
    # same email is `e7a3f1` in every session that ever reads it.
    #
    # It is also the validation. The model emits the ref from context; a ref
    # it invented does not resolve, and the pill renders dead rather than
    # linking confidently to the wrong thing. A *collision* would link to the
    # wrong thing silently, so `target` is stored and the insert is
    # OR IGNORE — a second target claiming a taken ref never overwrites the
    # first, and `resolve()` returns the row that was actually handed out.
    #
    # Derived and droppable: every row can be rebuilt by re-reading the
    # provider caches it points into. Nothing is a source of truth here.
    """
    CREATE TABLE cite_refs (
        ref     TEXT PRIMARY KEY,       -- kind letter + hex, e.g. e7a3f1
        kind    TEXT NOT NULL,          -- email | event | message
        target  TEXT NOT NULL,          -- msg_id | 'day/event_id' | guid
        label   TEXT NOT NULL,          -- what the pill says on hover
        day     TEXT,                   -- YYYY-MM-DD, for the pill's date
        seen_at TEXT NOT NULL           -- first time it was handed to a turn
    );
    CREATE UNIQUE INDEX cite_refs_target ON cite_refs (kind, target);
    """,
    # --- v-next: what you actually do with email (IDEAS §5 learner) ---
    #
    # The recommender was worse than a constant: 28.9% agreement over 875
    # decisions, against 78.3% for always saying "archive". It recommended
    # `keep` 331 times and the mailbox owner kept 8. The fix is not a better
    # prompt, it is a prior taken from the record instead of from the model's
    # idea of a person.
    #
    # Nothing here is a source of truth. Every row is an aggregate of
    # email_triage.user_action, which is the real record, so this table can be
    # dropped and rebuilt by rebuild() at any time. It exists because the
    # aggregate is read on every queue render and recomputing it from 875+
    # rows each time is work we can do once per action instead.
    """
    CREATE TABLE email_sender_stats (
        scope      TEXT NOT NULL,       -- 'addr' | 'domain'
        key        TEXT NOT NULL,       -- lowercased sender address or domain
        action     TEXT NOT NULL,       -- archive | delete | bin | unsubscribe | keep
        n          INTEGER NOT NULL DEFAULT 0,   -- decayed count, not a tally
        last_at    TEXT,                -- most recent decision of this shape
        PRIMARY KEY (scope, key, action)
    );
    """,

    # `n` decays and `obs` does not, because the two answer different
    # questions. `obs` is "how many times have you decided about this sender"
    # — evidence, which does not evaporate: two decisions ninety days apart
    # are still two decisions. `n` is "how recently did you decide *this* way"
    # — which of several actions is current.
    #
    # Collapsing both into the decayed count made a sender decided twice a day
    # apart sum to 1.992 and never clear a floor of two, and one decided twice
    # a quarter apart sum to 1.5 and never clear it at all. The backtest that
    # justified this feature gated on a raw tally and used the weights only to
    # pick the winner; this restores that.
    """
    ALTER TABLE email_sender_stats ADD COLUMN obs INTEGER NOT NULL DEFAULT 0;
    """,
    # --- v-next: the hour a task is due, not just the day ---
    #
    # `due` stays exactly what it was — a day key — so every comparison
    # already written against day_key() keeps its meaning. Widening it to
    # "YYYY-MM-DDTHH:MM" instead would have broken the equality test that
    # decides the due-today group (`due == today`) and every
    # COALESCE(due,'9999') ordering with it, silently and everywhere.
    #
    # So the clock time is a separate optional refinement of that day, not a
    # second copy of the date: one datum per column, and NULL keeps meaning
    # what it always meant — due that day, no hour named, nothing overdue
    # until the day itself rolls over.
    """
    ALTER TABLE tasks ADD COLUMN due_time TEXT;   -- 'HH:MM' local, NULL = all-day
    """,
    # --- v-next: the calendar list, instead of a name copied onto every event ---
    #
    # `events.calendar_name` was the only record of what a calendar is called,
    # which made the name a property of each cached event rather than of the
    # calendar. Renaming "Gym/Sports" to "Gym/Workouts" in Google therefore
    # only reached the rows inside refresh()'s [today-7, today+35] window on
    # the next sync; every colder row kept the old name forever, and
    # `calendar_ids` — a SELECT DISTINCT over those rows — listed both names
    # against one id.
    #
    # Same shape of bug in the other direction: a calendar with no events in
    # the cache had no row to be distinct over, so it was invisible to
    # `calendar_ids` and nothing could be filed onto it.
    #
    # One row per calendar fixes both. Derived and droppable like every other
    # provider cache — `_sync` rewrites it from calendarList on each pull, and
    # backfills the denormalized copy on `events` from it.
    """
    CREATE TABLE calendars (
        calendar_id  TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        refreshed_at TEXT NOT NULL
    );
    """,
    # --- v-next: what an event says, not just when it is ---
    #
    # The approval card for a delete could only show what the cache held —
    # title, time, place — so "delete this" was answered without the notes
    # that say which event it actually is (the Zoom link, the agenda, "bring
    # the reader"). The API already returns `description` on every event the
    # sync pulls; it was being dropped on the floor by _normalize.
    #
    # Truncation is the reader's job, not the cache's: the column holds what
    # Google holds, and console_preview cuts it to a card's worth.
    """
    ALTER TABLE events ADD COLUMN description TEXT;
    """,
    # --- v-next: a location day computed to nothing is still computed ---
    #
    # location._ensure_days took "has location_days rows" to mean "computed",
    # so a day with no fixes — which rightly writes no rows, a gap and not a
    # zero — read as never-computed on every GET, and every summary()/get()/
    # trace() re-ran a DELETE write transaction across all of them.
    #
    # One row per computed day, like health_sync/reddit_sync. `points` is the
    # fix count the day was computed from: OwnTracks flushes a queue of
    # offline fixes late, so a marked day (gap or not) is recomputed when its
    # count moves, which a bare timestamp couldn't notice. Derived and
    # droppable along with location_days — dropping it costs one recompute.
    #
    # The backfill marks the days location_days already holds, at their
    # current counts, so the migration doesn't trigger a sweep; gap days get
    # marked on the next read that covers them.
    """
    CREATE TABLE location_sync (
        day          TEXT PRIMARY KEY,         -- day_key, YYYY-MM-DD
        points       INTEGER NOT NULL,         -- location_points rows it was computed from
        refreshed_at TEXT NOT NULL
    );
    INSERT INTO location_sync (day, points, refreshed_at)
        SELECT d.day,
               (SELECT COUNT(*) FROM location_points p WHERE p.day = d.day),
               MAX(d.refreshed_at)
        FROM location_days d GROUP BY d.day;
    """,
    # --- v-next: review files every message, so it can be found again (§5) ---
    #
    # Review collapses to archive / unsubscribe / keep, and every one of them
    # files the message under a label from a fixed set (email_triage.TAXONOMY),
    # because after review the only use a message has left is being found.
    #
    # `label` is the model's pick and `terms` the search handles it pulled out
    # of the body (order numbers, amounts, people, dates) — both app-generated
    # like `summary`, regenerable at token cost. `user_label` is what the
    # message was actually filed under: real data, the label-side twin of
    # `user_action`, and what the sender prior for labels is read from.
    #
    # email_fts is derived and droppable: email_search.ensure_index() rebuilds
    # it whole whenever email_fts_state's signature stops matching the tables
    # it indexes. No body text goes in — `terms` is as close as it gets.
    """
    ALTER TABLE email_triage ADD COLUMN label TEXT;
    ALTER TABLE email_triage ADD COLUMN terms TEXT;
    ALTER TABLE email_triage ADD COLUMN user_label TEXT;
    CREATE VIRTUAL TABLE email_fts USING fts5(
        msg_id UNINDEXED, subject, sender, summary, terms, label,
        tokenize = 'porter unicode61');
    CREATE TABLE email_fts_state (
        id        INTEGER PRIMARY KEY CHECK (id = 1),
        signature TEXT NOT NULL
    );
    """,
    # email_fts without the porter stemmer. A search box matches as you type,
    # so every word is a prefix query — and porter indexes "Dining" as "dine",
    # which "dini*" can never reach. Prefix matching covers what stemming was
    # for ("receipt" finds "receipts"). Derived, so dropping it loses nothing;
    # the cleared signature makes the next search rebuild it.
    """
    DROP TABLE email_fts;
    CREATE VIRTUAL TABLE email_fts USING fts5(
        msg_id UNINDEXED, subject, sender, summary, terms, label,
        tokenize = 'unicode61 remove_diacritics 2');
    DELETE FROM email_fts_state;
    """,
    # --- v-next: expenses claimed back from Dad (app/expenses.py) ---
    #
    # All three tables are real data, not cache. An expense is what you asked
    # to be paid back, which is its own fact rather than a view of
    # fin_transactions: part of a grocery run, or cash that never touched a
    # card. So each row snapshots day, merchant and the charge's amount, and
    # `transaction_id` is only a soft link back. The Plaid cache stays
    # droppable, and a claim outlives the row it was made from.
    #
    # A batch is one spreadsheet sent. Rows whose batch_id is NULL are still
    # waiting to go out. Once sent, a row is frozen, because it has to match
    # the file Dad actually has.
    #
    # `expense_reviewed` is "I've been through this charge". It is kept per
    # transaction because a date watermark would be wrong: Plaid dates a
    # posted charge by when it was authorised, so a card swipe that posts
    # after an export lands *below* a date line without anyone having looked
    # at it.
    """
    CREATE TABLE expense_batches (
        id          INTEGER PRIMARY KEY,
        sent_at     TEXT NOT NULL,
        sent_day    TEXT NOT NULL,          -- day_key
        total       REAL NOT NULL,
        file        TEXT,                   -- where the spreadsheet was written
        paid_at     TEXT,
        paid_txn_id TEXT,                   -- the inflow that paid it, if matched
        paid_amount REAL
    );
    CREATE TABLE expenses (
        id             INTEGER PRIMARY KEY,
        transaction_id TEXT UNIQUE,         -- fin_transactions id; NULL = manual
        day            TEXT NOT NULL,       -- YYYY-MM-DD
        merchant       TEXT NOT NULL,
        txn_amount     REAL,                -- the whole charge, when from the bank
        amount         REAL NOT NULL,       -- what's claimed, <= txn_amount
        category       TEXT,
        note           TEXT,
        batch_id       INTEGER REFERENCES expense_batches(id),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX expenses_batch ON expenses (batch_id);
    CREATE TABLE expense_reviewed (
        transaction_id TEXT PRIMARY KEY,
        reviewed_at    TEXT NOT NULL
    );
    """,
    # --- v-next: expenses claimed while the charge is still pending ---
    #
    # Plaid posts a pending charge as a new transaction (new id, and maybe a
    # new amount: tips, fuel holds, hotel deposits). The posted one names the
    # pending one it replaces, and expenses.follow_posted moves the claim
    # over on the sync that delivers it.
    #
    # `claimed_pending` is 1 until that happens. A claim still at 1 whose
    # charge is gone from fin_transactions was a hold that never posted.
    # `posted_amount` is set only on a claim that was already sent when its
    # charge settled at a different number: the sent row stays frozen to
    # match the spreadsheet, and this is how the card knows to say so.
    #
    # No backfill: until now a pending charge couldn't be claimed, so every
    # existing row is correctly 0 / NULL.
    """
    ALTER TABLE expenses ADD COLUMN claimed_pending INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE expenses ADD COLUMN posted_amount REAL;
    """,
    # --- v-next: snoozes (plan view, "needs you" list) ---
    #
    # One row per ask pushed out of the list until `until` (local ISO
    # datetime). `ask_id` is the card hook's stable key for the ask
    # (`task:<id>`, `email:review`, `people`, `debts:<id>`), so a snooze
    # outlives a reload but not the ask: a task that's done leaves a row
    # nobody reads, and expired rows are pruned on the next read.
    #
    # No backfill: nothing could be snoozed before this.
    """
    CREATE TABLE snoozes (
        ask_id  TEXT PRIMARY KEY,
        until   TEXT NOT NULL,
        created TEXT NOT NULL
    );
    """,
    # --- v-next: the last 24 refresh outcomes per provider (plan view's map) ---
    #
    # provider_health holds one cycle and provider_events only transitions, so
    # nothing could draw "how has this source been doing": a source that fails
    # one cycle in ten looks healthy on both. `recent` is the last 24 outcomes,
    # oldest first, one character each — o ok, x failed, . nothing to do (the
    # refresh skipped) — written by heartbeat.record(). Derived and droppable
    # like the rest of the table.
    #
    # No backfill: the past cycles were never kept, so every row starts empty
    # and fills over the next two hours of polling.
    """
    ALTER TABLE provider_health ADD COLUMN recent TEXT NOT NULL DEFAULT '';
    """,
    # --- v-next: the Brain (app/brain.py) — what Kestrel knows about the user ---
    #
    # One row per fact, rendered into the vault's BRAIN_DIR as a read-only view
    # (the tasks.md arrangement). Facts are learned from sources that stay
    # where they live — console transcripts, messages, email, notes — and
    # cite them by `source_ref` with a verbatim `quote`, so every page can be
    # rebuilt from the sources. The one thing that can't be is the user's own
    # ruling: `verdict` (confirmed|rejected) is NOT droppable, and a rebuild
    # deletes only unruled ingest rows. A rejected fact stays as a tombstone
    # that ingest is shown so it doesn't learn the same wrong thing again.
    #
    # status: active (told to Kestrel directly, or confirmed) | pending (read
    # off a source; waits for a ✓, usable but labelled unconfirmed) |
    # superseded (a newer fact replaced it — `superseded_by`) | rejected.
    # `prior_status` is what a supersede overwrote, so rejecting the
    # replacement can put the old fact back the way it was.
    #
    # brain_runs is the ingest/lint ledger and the watermark lazy ingest and
    # the backfill resume from. Derived; droppable.
    #
    # No backfill: nothing was learned before this.
    """
    CREATE TABLE brain_pages (
        slug        TEXT PRIMARY KEY,       -- 'school', 'people/alex-smith'
        title       TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'topic',   -- topic | person
        aliases     TEXT NOT NULL DEFAULT '[]',      -- JSON list, for prompt matching
        summary     TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE brain_facts (
        id            INTEGER PRIMARY KEY,
        page          TEXT NOT NULL REFERENCES brain_pages(slug) ON UPDATE CASCADE,
        claim         TEXT NOT NULL,
        as_of         TEXT NOT NULL,        -- day_key the source dates it to
        source_kind   TEXT NOT NULL,        -- chat|message|email|note|memory|user
        source_ref    TEXT,                 -- session id | message guid | msg_id | note path
        quote         TEXT,                 -- verbatim span of the source
        status        TEXT NOT NULL DEFAULT 'pending',
        prior_status  TEXT,
        superseded_by INTEGER REFERENCES brain_facts(id) ON DELETE SET NULL,
        core          INTEGER NOT NULL DEFAULT 0,    -- belongs in the profile
        origin        TEXT NOT NULL,        -- chat|ingest|backfill|lint|user
        verdict       TEXT,                 -- confirmed|rejected — the user's; NOT droppable
        verdict_at    TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX brain_facts_page ON brain_facts (page, status);
    CREATE INDEX brain_facts_status ON brain_facts (status, created_at);
    CREATE TABLE brain_runs (
        id           INTEGER PRIMARY KEY,
        kind         TEXT NOT NULL,         -- ingest | backfill | lint
        window_start TEXT,                  -- day_key, inclusive
        window_end   TEXT,                  -- day_key, inclusive
        status       TEXT NOT NULL,         -- running | ok | failed
        stats        TEXT NOT NULL DEFAULT '{}',
        error        TEXT,
        started_at   TEXT NOT NULL,
        finished_at  TEXT
    );
    """,
    # --- v-next: where a brain fact came from, in words ---
    #
    # `source_ref` is a guid or a session id — checkable, not readable. The
    # label ("texts with Jamie", "Kestrel chat", "email from USC Housing")
    # is fixed at ingest from the source itself, so a page can say where a
    # fact came from without a join into a droppable cache.
    #
    # No backfill: the table was empty when this landed.
    """
    ALTER TABLE brain_facts ADD COLUMN source_label TEXT;
    """,
    # --- §22: proposals can add a weekly block, not just override one date ---
    #
    # A TA posting "starting next week, OH Thursdays 12-2 in GCS" (the 10-02
    # CSCI 103 digest) was read, judged a standing schedule, and dropped:
    # `oh_proposals` could only say cancel/change on a date. action='add' is
    # the third shape — `when_str` holds the weekly rule ("Thu 12:00-14:00"),
    # `day` the first date it applies, and approval calls courses.set_hours.
    #
    # `role` is the new block's (TA vs instructor), which set_hours needs and
    # a dated override never did. `prior` is the JSON of the entry an approved
    # add replaced — set_hours amends a person's same-weekday block in place,
    # so undo has to put that back rather than delete the block outright.
    #
    # No backfill: existing rows are all cancel/change and need neither.
    """
    ALTER TABLE oh_proposals ADD COLUMN role TEXT;
    ALTER TABLE oh_proposals ADD COLUMN prior TEXT;
    """,
    # --- owed beers, beside the debts card ---
    #
    # "Mo owes me a beer" is a favor, not money. Filed as a $5 debt it would
    # inflate the owed-to-you total and get nudged like rent, so it gets its
    # own table. One row per beer: settling one is the same flag-flip as any
    # small list, and the card groups them into "Mo ×2".
    #
    # No backfill: there were no beers anywhere before this.
    """
    CREATE TABLE beers (
        id         INTEGER PRIMARY KEY,
        person     TEXT NOT NULL,
        direction  TEXT NOT NULL,           -- owed_to_me | i_owe
        reason     TEXT,
        settled    INTEGER NOT NULL DEFAULT 0,
        settled_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX beers_open ON beers (settled, direction);
    """,
    # --- USC athletics schedule (providers/athletics.py) ---
    #
    # Sidearm's public iCal feed for every USC sport is truth; this is a
    # derived cache, safe to drop and re-pull (the provider refetches when
    # its newest fetched_at is past the feed's own 2h TTL). `day` is the
    # Pacific civil date of the game; `start` is null when the feed has a
    # date but no time yet (a TBA kickoff arrives as DTSTART;VALUE=DATE).
    #
    # No backfill: nothing held this before; the first read fetches it.
    """
    CREATE TABLE athletics_events (
        uid        TEXT PRIMARY KEY,
        day        TEXT NOT NULL,           -- YYYY-MM-DD, America/Los_Angeles
        start      TEXT,                    -- YYYY-MM-DDTHH:MM PT, null = TBA
        sport      TEXT NOT NULL,           -- "Men's Water Polo"
        opponent   TEXT,                    -- null for a tournament
        side       TEXT,                    -- home (vs) | away (at) | null
        note       TEXT,                    -- promo/giveaway after " - "
        location   TEXT,
        result     TEXT,                    -- "W 12-10" once played
        tv         TEXT,
        url        TEXT,
        summary    TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    CREATE INDEX athletics_events_day ON athletics_events (day);
    """,
    # --- Brightspace due dates (providers/coursework.py) ---
    #
    # The user's D2L calendar feed (BRIGHTSPACE_ICS_URL, a per-user token
    # URL kept in .env) is truth; derived cache, safe to drop, refetched past
    # its TTL. One row per calendar item across every course: assignment due
    # dates (kind 'due'), content release dates ('available'), and the odd
    # item that is neither ('event' — some instructors' deadlines arrive
    # without the " - Due" suffix). It has no submission status; D2L doesn't
    # publish that in the feed.
    #
    # No backfill: nothing held this before; the first read fetches it.
    """
    CREATE TABLE coursework_items (
        uid         TEXT PRIMARY KEY,
        day         TEXT NOT NULL,          -- YYYY-MM-DD, America/Los_Angeles
        start       TEXT,                   -- YYYY-MM-DDTHH:MM PT, null = date-only
        kind        TEXT NOT NULL,          -- due | available | event
        title       TEXT NOT NULL,
        course_code TEXT,                   -- "WRIT-150"
        course      TEXT,                   -- full course title
        section     TEXT,                   -- "64775"
        details     TEXT,                   -- description, HTML stripped, capped
        url         TEXT,
        fetched_at  TEXT NOT NULL
    );
    CREATE INDEX coursework_items_day ON coursework_items (day);
    """,
    # --- Documents the console can point into (app/docs.py) ---
    #
    # A PDF dropped on the console used to be read to text and thrown away,
    # so an answer about it could only describe a page, never show it. Now
    # the bytes are kept at DATA_DIR/docs/<sha>.pdf, which is the source of
    # truth; this row is what's known about them. `pages` is pypdf's text
    # per page (a JSON array, index 0 = page 1) and is derived — re-reading
    # the file rebuilds it. The citation ref (`d…`) lives in cite_refs like
    # every other kind, minted over `sha`.
    #
    # No backfill: nothing kept these files before.
    """
    CREATE TABLE docs (
        sha        TEXT PRIMARY KEY,        -- sha256 of the bytes
        filename   TEXT NOT NULL,           -- as dropped; the first name wins
        mime       TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        page_count INTEGER NOT NULL,
        pages      TEXT NOT NULL,           -- JSON array of per-page text
        added_at   TEXT NOT NULL,
        opened_at  TEXT NOT NULL            -- last time it was attached
    );
    """,
    # §25 food tracker (FOOD_PLAN.md). foods/food_log/food_profile/
    # food_targets/food_days/weigh_ins are truth; food_venues, usc_menu_items,
    # usc_menu_days and food_estimates are the usc_dining provider's derived
    # cache — droppable, refetched. A food's nutrients are a JSON object per
    # serving; a nutrient it doesn't know is absent, never 0.
    #
    # foods is keyed (source, source_id): sr28 → the SR28 id; usc_estimate →
    # the normalized menu-item name (estimated once, shared by every hall and
    # day that serves it); custom → "venue|normalized name". Every source sets
    # source_id, so the one unique index is the dedupe for all of them.
    #
    # No backfill: nothing tracked food before.
    """
    CREATE TABLE foods (
        id           INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,
        venue        TEXT,                  -- food_venues.slug; NULL = anywhere
        serving_desc TEXT NOT NULL DEFAULT '1 serving',
        serving_g    REAL,
        nutrients    TEXT NOT NULL DEFAULT '{}',
        source       TEXT NOT NULL,         -- sr28 | usc_estimate | custom
        source_id    TEXT NOT NULL,
        confidence   REAL NOT NULL DEFAULT 1,
        detail       TEXT,                  -- JSON: portions, decomposition, note
        allergens    TEXT,
        prefs        TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE UNIQUE INDEX foods_source ON foods(source, source_id);
    CREATE INDEX foods_venue ON foods(venue);
    CREATE TABLE food_log (
        id        INTEGER PRIMARY KEY,
        day       TEXT NOT NULL,            -- day_key
        eaten_at  TEXT NOT NULL,
        meal      TEXT NOT NULL,            -- breakfast | lunch | dinner | snack
        food_id   INTEGER NOT NULL REFERENCES foods(id),
        servings  REAL NOT NULL DEFAULT 1,
        venue     TEXT,
        paid_with TEXT,                     -- dining_dollars | card | cash
        price     REAL,
        note      TEXT,
        source    TEXT NOT NULL DEFAULT 'ui'
    );
    CREATE INDEX food_log_day ON food_log(day);
    CREATE TABLE food_profile (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE food_targets (nutrient TEXT PRIMARY KEY, target REAL NOT NULL);
    CREATE TABLE food_days (
        day       TEXT PRIMARY KEY,
        closed_at TEXT NOT NULL,
        summary   TEXT
    );
    CREATE TABLE weigh_ins (day TEXT PRIMARY KEY, kg REAL NOT NULL);
    CREATE TABLE food_venues (
        slug           TEXT PRIMARY KEY,     -- hospitality.usc.edu/dining-locations/<slug>
        name           TEXT NOT NULL,
        kind           TEXT NOT NULL,        -- dining_hall | retail | restaurant
        campus         TEXT NOT NULL,        -- upc | hsc
        dining_dollars INTEGER NOT NULL,
        shifts         TEXT,                 -- JSON [[start_ms, end_ms], ...]
        shifts_day     TEXT,                 -- the day_key those shifts are for
        refreshed_at   TEXT NOT NULL
    );
    CREATE TABLE usc_menu_items (
        date       TEXT NOT NULL,            -- YYYY-MM-DD
        venue      TEXT NOT NULL,            -- food_venues.slug
        meal       TEXT NOT NULL,            -- Breakfast | Brunch | Lunch | Dinner
        station    TEXT NOT NULL,
        item       TEXT NOT NULL,
        item_key   TEXT NOT NULL,            -- food.norm_name(item)
        allergens  TEXT,
        prefs      TEXT,
        PRIMARY KEY (date, venue, meal, station, item)
    );
    CREATE INDEX usc_menu_items_key ON usc_menu_items(item_key);
    CREATE TABLE usc_menu_days (
        date       TEXT NOT NULL,
        venue      TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (date, venue)
    );
    CREATE TABLE food_estimates (
        item_key     TEXT PRIMARY KEY,
        attempted_at TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 1,
        error        TEXT
    );
    """,
    # --- Study coverage (app/study.py) ---
    #
    # One row per quiz question or flashcard answered in the console, posted
    # by StudyBlock.jsx on "Send results". Truth for "which lectures have I
    # actually drilled, and where do my misses sit" — the review log (vault
    # note) holds what was got wrong, this holds what was attempted at all.
    # `block` is a hash of the fence's source, so sending the same quiz's
    # results twice replaces rather than doubles. `doc_sha`/`page` come from
    # the item's `slide` (else `cite`) ref; NULL when it had neither.
    #
    # No backfill: results were only ever drafted into the composer.
    """
    CREATE TABLE study_attempts (
        block      TEXT NOT NULL,
        idx        INTEGER NOT NULL,
        day        TEXT NOT NULL,               -- db.day_key()
        at         TEXT NOT NULL,
        course     TEXT,                        -- the block's `course`, as written
        kind       TEXT NOT NULL,               -- choice | free | card
        question   TEXT NOT NULL,
        correct    INTEGER NOT NULL,            -- 1 | 0
        self_graded INTEGER NOT NULL DEFAULT 0, -- free response marked by the user
        doc_sha    TEXT,
        page       INTEGER,
        PRIMARY KEY (block, idx)
    );
    CREATE INDEX study_attempts_doc ON study_attempts (doc_sha, page);
    CREATE INDEX study_attempts_course ON study_attempts (course, day);
    """,
    # --- .docx extraction (providers/attachments.py) ---
    #
    # Word files used to be filed `unsupported`, and that verdict is cached:
    # extract() never retries a row, so without this the files already tried
    # (four dietitian handouts on 10-16) would stay unreadable after support
    # landed. attachment_text is a derived cache, so dropping rows is safe.
    """
    DELETE FROM attachment_text
     WHERE status = 'unsupported'
       AND attachment_id IN (
           SELECT id FROM attachments
            WHERE mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
               OR (COALESCE(mime, '') IN ('', 'application/octet-stream', 'application/zip')
                   AND lower(COALESCE(filename, '')) LIKE '%.docx'));
    """,
    # --- read receipts (providers/messages.py) ---
    #
    # chat.db stamps date_read/date_delivered onto a message row *after* it
    # arrives, in place — the ROWID never moves, so the watermark sync that
    # copied the row never saw the receipt. These are refreshed separately
    # (messages._sync_receipts) over a recent window, and the rows already
    # cached when this lands are backfilled once by the same function.
    # Outbound: when they read yours (iMessage with receipts on, else NULL
    # forever). Inbound: when you read theirs. is_read/is_delivered are kept
    # beside the timestamps because chat.db sets the flag without a date on
    # a few percent of rows.
    """
    ALTER TABLE messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN read_at TEXT;
    ALTER TABLE messages ADD COLUMN is_delivered INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN delivered_at TEXT;
    """,
    # --- scheduled email (routes/email.py, email_schedule.py) ---
    #
    # Gmail's API has no scheduled send, so the schedule lives here and the
    # message lives in Gmail as an ordinary draft until send_at, when
    # email_schedule.send_due() calls drafts.send on it. The draft is the
    # content — edit it in Gmail before then and the edit is what goes; delete
    # it and the send finds a 404 and records `cancelled`. Source of truth,
    # not a cache: a lost row is a mail that silently never goes.
    """
    CREATE TABLE scheduled_emails (
        id          INTEGER PRIMARY KEY,
        draft_id    TEXT NOT NULL,
        to_addr     TEXT NOT NULL,
        subject     TEXT,
        send_at     TEXT NOT NULL,              -- ISO UTC, so string order is time order
        status      TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|sending|sent|cancelled|failed
        created_at  TEXT NOT NULL,
        sent_at     TEXT,
        message_id  TEXT,                       -- Gmail id once sent
        thread_id   TEXT,
        error       TEXT,
        attempts    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX scheduled_emails_due ON scheduled_emails (status, send_at);
    """,
    # --- activity log + self-text routing (app/activity.py, app/selftexts.py) ---
    #
    # `activity` is every write the backend made on its own — nobody clicked
    # anything — so "what changed while I was away" has one answer and each
    # row can be taken back. `ref` is the JSON handle the undo needs (a task
    # id, a calendar event id, food_log ids); `origin` ties rows from one
    # self-text burst together so a redo can find its source. Truth, not a
    # cache: the rows are the only record of what an automation did.
    #
    # A burst is one or more texts to yourself sent close together ("photo",
    # then "for the party friday") and is routed as a unit. selftext_messages
    # is which burst each message went into, and is also the "already seen"
    # mark — a self-text with no row here hasn't been routed.
    """
    CREATE TABLE activity (
        id          INTEGER PRIMARY KEY,
        at          TEXT NOT NULL,
        source      TEXT NOT NULL,      -- selftext | finance | travel
        kind        TEXT NOT NULL,      -- task | event | food | watch | list_item | note | keep
        summary     TEXT NOT NULL,
        ref         TEXT,               -- JSON: what to undo
        origin      TEXT,               -- selftext: the burst id
        undone_at   TEXT
    );
    CREATE INDEX activity_at ON activity (at);
    CREATE TABLE selftext_bursts (
        id           INTEGER PRIMARY KEY,
        first_ts     TEXT NOT NULL,
        last_ts      TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
        attempts     INTEGER NOT NULL DEFAULT 0,
        error        TEXT,
        created_at   TEXT NOT NULL,
        processed_at TEXT
    );
    CREATE TABLE selftext_messages (
        guid      TEXT PRIMARY KEY,
        burst_id  INTEGER NOT NULL REFERENCES selftext_bursts(id) ON DELETE CASCADE
    );
    CREATE INDEX selftext_messages_burst ON selftext_messages (burst_id);
    """,
    # --- Study confidence (app/study.py) ---
    #
    # How sure the user said they were before answering a quiz question:
    # dont_know | guess | unsure | sure, NULL when they didn't say (and for
    # cards, which grade themselves). Right/wrong alone can't tell a slip
    # from a misconception; wrong-and-sure is the misconception.
    #
    # No backfill: earlier attempts were never asked.
    """
    ALTER TABLE study_attempts ADD COLUMN confidence TEXT;
    """,
    # --- Study confidence, rated late ---
    #
    # 1 when `confidence` was given after the verdict was on screen. Allowed
    # because the user asked for it, kept apart because such a rating can be
    # fitted to the verdict. No backfill: every earlier row was asked first.
    """
    ALTER TABLE study_attempts ADD COLUMN rated_after INTEGER NOT NULL DEFAULT 0;
    """,
    # --- Project folders ---
    #
    # The picker's grouping of dev projects, and only that: a project still
    # lives where it lives on disk (hardcoded paths and ~/.claude/projects
    # session history are keyed by that path), so moving one is this row. A
    # folder exists while something is in it. No row means no folder, and a
    # row for a directory that has gone is ignored.
    """
    CREATE TABLE project_folders (
        path TEXT PRIMARY KEY,
        folder TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # --- Food accuracy (§25, app/food.py) ---
    #
    # portion_conf: how sure the *amount* of one entry is, apart from how sure
    # the food's numbers are (foods.confidence). NULL = the amount was stated
    # (typed, tapped, or answered); 0.6 = guessed from a photo or a vague
    # line; 0.9 = the one portion question was answered. Portion is where
    # most calorie error comes from, so an entry's range uses both.
    # ask: the pending portion question (JSON), NULL when none or answered.
    # Backfill: entries whose note says the amount was guessed or estimated
    # (written by the console, e.g. "3 balls, guessed as half a serving")
    # were exactly that.
    # weigh_ins.at: when the weigh-in was entered, so a non-morning one can be
    # told apart. No backfill — the time was never recorded.
    """
    ALTER TABLE food_log ADD COLUMN portion_conf REAL;
    ALTER TABLE food_log ADD COLUMN ask TEXT;
    UPDATE food_log SET portion_conf = 0.6
     WHERE note LIKE '%guess%' OR note LIKE '%estimat%' OR note LIKE '%assum%';
    ALTER TABLE weigh_ins ADD COLUMN at TEXT;
    """,
    # --- Look lab (app/looks.py): what you said about each UI design variant,
    # for the redesign rounds. verdict -1 no / 0 note only / 1 yes; `against`
    # is the look it was being flipped with, `view` what was on screen.
    """
    CREATE TABLE IF NOT EXISTS look_votes (
        id INTEGER PRIMARY KEY,
        look TEXT NOT NULL,
        verdict INTEGER NOT NULL,
        note TEXT,
        against TEXT,
        view TEXT,
        at TEXT NOT NULL,
        day TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS look_votes_look ON look_votes(look);
    """,
    # --- notifications.target: what a click on the banner opens (a console
    # tab id for console-turn/console-ask; NULL just brings Kestrel forward).
    """
    ALTER TABLE notifications ADD COLUMN target TEXT;
    """,
    # --- console_work (app/console_work.py): tabs with work in flight, under
    # the server process that ran it (`boot`). A row from another boot is work
    # a quit cut off, offered back as "Resume". agents: JSON list of the
    # background agents/shells still running.
    """
    CREATE TABLE IF NOT EXISTS console_work (
        tab TEXT NOT NULL,
        boot TEXT NOT NULL,
        session_id TEXT,
        profile TEXT,
        project TEXT,
        label TEXT,
        turn INTEGER NOT NULL DEFAULT 0,
        agents TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tab, boot)
    );
    """,
    # --- delegates (app/delegates): other models sent on errands by Claude.
    # delegate_providers holds only the switch (no row = on); whether a CLI
    # is installed or signed in is read off the machine each time.
    # delegate_runs is the log: one row per agent run, Claude's fan-out
    # variants included. `batch` is also the scratch directory's name, and
    # `file` the artifact inside the variant's folder.
    """
    CREATE TABLE IF NOT EXISTS delegate_providers (
        id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS delegate_runs (
        id INTEGER PRIMARY KEY,
        batch TEXT NOT NULL,
        variant INTEGER NOT NULL DEFAULT 1,
        kind TEXT NOT NULL,                    -- delegate | fan_out
        provider TEXT NOT NULL,
        model TEXT,
        angle TEXT,
        status TEXT NOT NULL,                  -- running | ok | failed
        error TEXT,
        file TEXT,
        chars INTEGER,
        tokens_in INTEGER,
        tokens_out INTEGER,
        duration_ms INTEGER,
        redactions TEXT,
        session_id TEXT,
        started_at TEXT NOT NULL,
        day TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS delegate_runs_batch ON delegate_runs (batch, variant);
    CREATE INDEX IF NOT EXISTS delegate_runs_day ON delegate_runs (day, provider);
    """,
    # --- console_sessions (app/console_work.py → app/token_usage.py): which
    # tab and mode a Claude session ran in. The transcript knows the model,
    # the tokens and the cwd, but not whether a vault session was a general
    # or a study tab, nor the tab's title; console_work's rows go when the
    # work does, so this is the record that stays. Not derived: nothing else
    # holds it, though losing it only costs labels on the usage page.
    """
    CREATE TABLE IF NOT EXISTS console_sessions (
        session_id TEXT PRIMARY KEY,
        tab TEXT,
        profile TEXT,
        project TEXT,
        label TEXT,
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL
    );
    """,
    # --- scheduled_prompts (app/schedules.py): a prompt that opens a new tab
    # at a time, once or on repeat. `repeat`: NULL once, else 'daily' or
    # 'weekly:<0-6 csv, Mon=0>'. `next_at` is the next due instant (local
    # ISO, NULL once a one-off has run); `claimed_at` holds it while a page
    # is opening the tab, so two windows can't both run it.
    """
    CREATE TABLE IF NOT EXISTS scheduled_prompts (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'general',
        project TEXT,
        model TEXT,
        at_time TEXT NOT NULL,
        repeat TEXT,
        next_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        claimed_at TEXT,
        last_run_at TEXT,
        last_tab TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    """,
    # --- the console's held messages (app/console_queue.py, #92): typed
    # mid-turn in "queue" mode, sent as their own turn when this one ends.
    # Here and not in the page or the Link so a backend reload doesn't lose
    # them. `tab` is the page's tab id (console_link key), `payload` the
    # message as the page sent it.
    """
    CREATE TABLE IF NOT EXISTS console_queue (
        id TEXT PRIMARY KEY,
        tab TEXT NOT NULL,
        pos REAL NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS console_queue_tab ON console_queue (tab, pos);
    """,
    # --- look_themes (app/looks.py): colour themes Kestrel made when asked.
    # id is 'theme-<slug>', which is also the page's data-look value and the
    # key its votes take in look_votes. dark/light: JSON {role: '#RRGGBB'},
    # only the roles the theme moves. active: the one the window shows (at
    # most one row). Not derived: these are the user's kept colours.
    """
    CREATE TABLE IF NOT EXISTS look_themes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        note TEXT,
        dark TEXT NOT NULL DEFAULT '{}',
        light TEXT NOT NULL DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # --- the phone door (app/phone.py, #97). None of it is derived.
    # phone_state: the switch, the owner's tailnet login, a typed URL, and
    #   the server's push key pair (the private half never leaves app.db).
    # phone_devices: one row per paired phone. Only the token's SHA-256 is
    #   kept. scopes: csv of 'status', 'approve'. push: the browser's push
    #   subscription as JSON, NULL until it allows notifications.
    # phone_events: pairings, answers, pushes, refusals; the newest 500.
    """
    CREATE TABLE IF NOT EXISTS phone_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS phone_devices (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL,
        push TEXT,
        created_at TEXT NOT NULL,
        last_seen_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS phone_events (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT,
        device_id INTEGER
    );
    """,
]


def day_key(dt: datetime | None = None) -> str:
    """Local 'today' with a 4am rollover: 00:00–03:59 belongs to yesterday."""
    dt = dt or datetime.now()
    if dt.hour < DAY_ROLLOVER_HOUR:
        dt = dt - timedelta(days=1)
    return dt.date().isoformat()


def day_instant(day: str, hhmm: str) -> datetime:
    """The wall-clock instant 'HH:MM on day key `day`' — day_key() inverted.

    A day key runs 04:00 to 03:59, so 01:00 on day key D is 01:00 on the
    *calendar* day after D. Resolving it as D 01:00 instead would put a task
    filed for "tonight at 1am" twenty-three hours in the past the moment it
    was filed, and day_key(day_instant(d, t)) would stop returning d.
    """
    at = datetime.fromisoformat(f"{day}T{hhmm}")
    if at.hour < DAY_ROLLOVER_HOUR:
        at += timedelta(days=1)
    return at


BUSY_TIMEOUT_MS = 5000

# Serializes the one-time rollback->WAL conversion; see _enable_wal.
_wal_lock = threading.Lock()


def _enable_wal(con: sqlite3.Connection) -> None:
    """Put the database in WAL mode, tolerating a concurrent first open.

    Switching a database *into* WAL needs an exclusive lock, and SQLite does
    not run the busy handler for that transition — so `busy_timeout` can't
    save it, and whichever connection loses the race fails outright with
    "database is locked". It only happens on a database that isn't WAL yet
    (the mode is persistent in the file header), so it's a fresh-DB-only
    race: rare, and it lands on whichever caller happened to open second.

    Already-WAL is the overwhelmingly common case and costs one read, no
    lock, no write. Otherwise the conversion is serialized in-process and
    retried until `busy_timeout` worth of time has passed, which also covers
    the other process that may hold app.db (skills running the sqlite3 CLI).
    """
    if con.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    with _wal_lock:
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                con.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                # Someone else is converting or holds the write lock. Once
                # they finish, this same statement is a no-op that succeeds.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)


# Database files this process has already put in WAL and migrated. Journal
# mode lives in the file header and this process is the only migrator
# (CLAUDE.md: skills only ever read app.db via the sqlite3 CLI), so neither
# can change under us — but connect() re-established both on every open, at
# four extra statements a time on a call that happens thousands of times a
# day. Keyed by path so the test fixture's throwaway DBs still migrate.
_ready: set[str] = set()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(APP_DB)
    con.row_factory = sqlite3.Row
    # busy_timeout FIRST: journal_mode=WAL itself needs a write lock, so if it
    # runs before the timeout is set it fails immediately ("database is
    # locked") whenever another connection holds one (e.g. a concurrent first
    # migration). With the timeout in place it waits instead.
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if (key := str(APP_DB)) not in _ready:
        _enable_wal(con)
        _migrate(con)
        _ready.add(key)   # only once the schema is actually current
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def session():
    """Open a connection and guarantee it's closed, even on error — the
    read/query helpers below use this instead of a bare connect()/close()
    pair, which leaked the connection (and its WAL read lock) whenever the
    body raised. `with con:` inside still scopes transactions as before."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def borrow(con: sqlite3.Connection | None = None):
    """session() for functions that take an optional caller connection: yield
    `con` untouched when one was passed (the caller owns it — never closed or
    committed here), else open one and close it on the way out. Transactions
    stay the body's business either way."""
    if con is not None:
        yield con
        return
    with session() as own:
        yield own


def like_contains(text: str) -> str:
    r"""A LIKE pattern matching `text` as a literal substring. `%` and `_` are
    wildcards in LIKE, and every caller interpolates something a human (or the
    model) typed — a contact named "Jean_Luc" or a search for "50%" otherwise
    matches the wrong rows silently. Pair with ESCAPE '\'."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _schema_version(con: sqlite3.Connection) -> int:
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
    row = con.execute("SELECT v FROM schema_version").fetchone()
    return row["v"] if row else 0


# connect() runs _migrate() on every connection, and the backend opens many
# concurrently (the poller refreshes all providers in parallel threads). On a
# fresh/behind DB they would otherwise race on the same CREATE TABLE and one
# would crash with "table already exists". This process is the only migrator
# — terminal skills only ever query app.db via the sqlite3 CLI — so a plain
# in-process lock serializes them: the first thread migrates, the rest wait,
# then see the recorded fingerprints and fall through.
_migrate_lock = threading.Lock()


_SQL_COMMENT = re.compile(r"--[^\n]*")


def fingerprint(script: str) -> str:
    """Identify a migration by the SQL it runs, not by where it sits in the list.

    Position used to be the identity, and it silently stopped being unique the
    first time two branches each added a 40th migration. `schema_version` then
    said "40 applied" on both, so on either branch the count read as current
    and that branch's own 40th was skipped without a word — the DB claiming a
    schema it did not have. Counting cannot tell those two apart; only what
    they actually run can.

    Comments and whitespace are stripped first, so editing the prose inside a
    migration (these carry a lot of it) doesn't change its identity and re-run
    it. The SQL is the identity — which is another way of saying you still must
    never change the SQL of a migration that has shipped.
    """
    return hashlib.sha256(
        " ".join(_SQL_COMMENT.sub("", script).split()).encode()).hexdigest()


def _applied(con: sqlite3.Connection) -> set[str]:
    con.execute("CREATE TABLE IF NOT EXISTS schema_applied ("
                "fingerprint TEXT PRIMARY KEY,"
                " idx INTEGER NOT NULL,"
                " applied_at TEXT NOT NULL)")
    return {r["fingerprint"]
            for r in con.execute("SELECT fingerprint FROM schema_applied")}


def _adopt(con: sqlite3.Connection, prints: list[str], v: int) -> set[str] | None:
    """One-time hand-off from counting to fingerprints, for a DB that predates
    them. The first `v` migrations are what a version of `v` meant, so record
    exactly those and nothing changes on disk.

    Returns None when this checkout can't do the hand-off honestly: a DB
    counted further than this branch's list reaches was migrated by some other
    branch, and that branch's extra scripts are not here to fingerprint. Faking
    them either way corrupts the record — claim them and the branch that owns
    them re-runs them into "table already exists"; ignore them and this branch
    reuses their slot. So defer, keep the old counting behaviour for this
    process, and say so: opening the DB once from the longer branch completes
    the hand-off for good.
    """
    if v > len(prints):
        logging.getLogger(__name__).warning(
            "app.db is at schema_version %d but this checkout has %d migrations"
            " — it was migrated by another branch. Staying on version counting"
            " until the DB is opened once from that branch; new migrations"
            " added here will NOT apply in the meantime.", v, len(prints))
        return None
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO schema_applied (fingerprint, idx, applied_at)"
            " VALUES (?,?,?)",
            [(fp, i, now) for i, fp in enumerate(prints[:v], start=1)])
    return set(prints[:v])


def _migrate(con: sqlite3.Connection) -> None:
    prints = [fingerprint(s) for s in MIGRATIONS]
    if _applied(con).issuperset(prints):
        return  # already current — the hot path, no lock, full read concurrency
    with _migrate_lock:
        applied = _applied(con)   # re-read under the lock; another thread may have run it
        v = _schema_version(con)
        if not applied and v:
            adopted = _adopt(con, prints, v)
            if adopted is None:
                return
            applied = adopted
        now = datetime.now().isoformat(timespec="seconds")
        for i, (script, fp) in enumerate(zip(MIGRATIONS, prints), start=1):
            if fp in applied:
                continue
            with con:
                con.executescript(script)
                con.execute("INSERT INTO schema_applied"
                            " (fingerprint, idx, applied_at) VALUES (?,?,?)",
                            (fp, i, now))
                # Kept in step for routes/status and anything reading the old
                # number. It is now a display of how far the list has been
                # walked, not the thing decisions are made on — and it never
                # goes backwards, so a DB another branch counted higher keeps
                # its count.
                con.execute("DELETE FROM schema_version")
                con.execute("INSERT INTO schema_version (v) VALUES (?)",
                            (max(i, v),))


# ---------- tasks ----------

TASK_FIELDS = (
    "title", "due", "priority", "project", "note", "defer", "waiting_on",
    "owed_to", "parent_id", "due_time",
)


CLOSED_STATUSES = ("done", "dropped")

# Chronological ordering over the two due columns. Within one day key the
# timed tasks come first in clock order and the untimed ones last (they're due
# by end of day, not at 00:00) — and because a day key runs 04:00 to 03:59,
# an after-midnight time is the *end* of its day, not the start of it, so
# 01:00 sorts after 23:00 rather than eleven hours before it.
DUE_ORDER = (
    "COALESCE(due,'9999') ASC,"
    " CASE WHEN due_time IS NULL THEN 3 WHEN due_time < '04:00' THEN 2 ELSE 1 END ASC,"
    " COALESCE(due_time,'') ASC"
)


def split_due(value: str | None) -> tuple[str | None, str | None]:
    """Split a due value into (day key, 'HH:MM' or None).

    Accepts the bare "YYYY-MM-DD" every existing caller passes, and also
    "YYYY-MM-DDTHH:MM" / "YYYY-MM-DD HH:MM" / a trailing ":SS" — so one field
    can carry an hour without every caller (console tools, the API, skills)
    needing a second one. Raises ValueError on anything else rather than
    filing a task with a due date nobody can compare.
    """
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    day, sep, rest = raw[:10], raw[10:11], raw[11:]
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError(f"due must be YYYY-MM-DD[THH:MM], got {value!r}")
    if not sep:
        return day, None
    if sep not in ("T", " "):
        raise ValueError(f"due must be YYYY-MM-DD[THH:MM], got {value!r}")
    return day, normalize_due_time(rest)


def normalize_due_time(value: str | None) -> str | None:
    """Validate an 'HH:MM' due time, dropping any seconds. NULL/'' means the
    task is due that day with no hour named."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:5], "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"due time must be HH:MM, got {value!r}")


def _normalize_due(fields: dict, current_due: str | None = None) -> None:
    """Fold a combined "due" ("YYYY-MM-DDTHH:MM") into the two columns, in
    place. A `due` that names no hour clears `due_time` too, rather than
    leaving yesterday's 9am hanging off a task rescheduled to a plain date —
    but only when `due` is actually being set, so a PATCH that touches
    neither field leaves both alone. `current_due` is the date already on the
    row, so a PATCH setting only the hour isn't read as a homeless time."""
    if "due" in fields:
        day, hhmm = split_due(fields["due"])
        fields["due"] = day
        # An explicit due_time in the same call wins over the one parsed out
        # of `due`; passing both is how a date + time picker sends it.
        if fields.get("due_time") is None:
            fields["due_time"] = hhmm
    if "due_time" in fields:
        fields["due_time"] = normalize_due_time(fields["due_time"])
        if fields["due_time"] and not (fields["due"] if "due" in fields else current_due):
            raise ValueError("a due time needs a due date")


def _annotate(t: dict, today: str) -> dict:
    """Read-time fields the UI needs but the row doesn't store.

    `deferred` hides a task until its defer date; `due_at` is the instant it
    comes due, for the countdown. Both are derived from day_key() here rather
    than in the client, which has no business deciding what day it is (a
    browser clock disagrees with the 4am rollover for four hours a night).
    """
    t["deferred"] = bool(t.get("defer") and t["defer"] > today)
    t["due_at"] = (day_instant(t["due"], t["due_time"]).isoformat(timespec="minutes")
                   if t.get("due") and t.get("due_time") else None)
    for s in t.get("subtasks", []):
        _annotate(s, today)
    return t


def _with_subtasks(con: sqlite3.Connection, tasks: list[dict]) -> list[dict]:
    """Attach children inline under each parent (one level, by convention —
    the UI only ever renders one level deep).

    A closed (done/dropped) child drops out of a still-open parent's list: it
    has been checked off, and a top-level task that was checked off likewise
    leaves the open listing. Re-serving it is what made checking a subtask
    look like it did nothing — the row came straight back, unchecked.
    A closed parent keeps all of its children, since closing cascades and
    they are the record of what got finished."""
    ids = [t["id"] for t in tasks]
    if not ids:
        return tasks
    qmarks = ",".join("?" * len(ids))
    children: dict[int, list[dict]] = {}
    for row in con.execute(
            f"SELECT * FROM tasks WHERE parent_id IN ({qmarks}) ORDER BY id", ids):
        children.setdefault(row["parent_id"], []).append(dict(row))
    for t in tasks:
        kids = children.get(t["id"], [])
        if t["status"] not in CLOSED_STATUSES:
            kids = [k for k in kids if k["status"] not in CLOSED_STATUSES]
        t["subtasks"] = kids
    return tasks


def list_tasks(status: str | None = "open") -> list[dict]:
    """Top-level tasks only (parent_id IS NULL) — subtasks ride along nested
    under their parent via _with_subtasks, so listing them again here would
    duplicate an open subtask as its own top-level row too."""
    with session() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status=? AND parent_id IS NULL"
                f" ORDER BY {DUE_ORDER}, COALESCE(priority,9) ASC, id ASC", (status,))
        else:
            rows = con.execute("SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY id DESC")
        out = _with_subtasks(con, [dict(r) for r in rows])
    # Flag tasks deferred to a future day so the UI can hide them until then
    # (day_key authority stays server-side — clients must not date.today()).
    today = day_key()
    for t in out:
        _annotate(t, today)
    return out


def _check_parent(con: sqlite3.Connection, parent_id: int, self_id: int | None) -> None:
    """Shared by create_task/update_task: reject self-parenting, parents
    that themselves have a parent, and reparenting a task that already has
    children of its own (subtasks are one level deep, by convention —
    cheap cycle/depth guard)."""
    if parent_id == self_id:
        raise ValueError("a task cannot parent itself")
    parent = con.execute("SELECT parent_id FROM tasks WHERE id=?", (parent_id,)).fetchone()
    if parent is None:
        raise ValueError("no such parent task")
    if parent["parent_id"] is not None:
        raise ValueError("subtasks are one level deep only")
    if self_id is not None:
        child = con.execute(
            "SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1", (self_id,)).fetchone()
        if child is not None:
            raise ValueError("a task with subtasks of its own can't become a subtask")


# Callbacks run after every committed task write (create/update/status), from
# whatever thread made it. It's how the generated tasks.md stays current no
# matter who wrote — REST route, console tool, or a provider sync that opens
# a task on its own — without this module importing vault_views (which imports
# db). vault_views registers itself at import. A hook that raises is logged
# and skipped: a stale derived view must never fail the write that stales it.
_task_write_hooks: list = []


def on_task_write(fn) -> None:
    """Register `fn()` to run after each task write. Idempotent per function."""
    if fn not in _task_write_hooks:
        _task_write_hooks.append(fn)


def _task_written() -> None:
    for fn in list(_task_write_hooks):
        try:
            fn()
        except Exception:
            logging.getLogger(__name__).exception("task-write hook %r failed", fn)


def create_task(title: str, due: str | None = None, priority: int | None = None,
                project: str | None = None, source: str = "ui",
                defer: str | None = None, note: str | None = None,
                waiting_on: str | None = None, owed_to: str | None = None,
                parent_id: int | None = None, source_kind: str | None = None,
                source_ref: str | None = None, source_excerpt: str | None = None,
                due_time: str | None = None) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    # `due` may carry its own hour ("YYYY-MM-DDTHH:MM"), so callers that have
    # one string don't need to split it themselves.
    fields = {"due": due, "due_time": due_time}
    _normalize_due(fields)
    due, due_time = fields["due"], fields["due_time"]
    with session() as con:
        if parent_id is not None:
            _check_parent(con, parent_id, None)
        with con:
            cur = con.execute(
                "INSERT INTO tasks (title, due, priority, project, source, created_at,"
                " updated_at, defer, note, waiting_on, owed_to, parent_id,"
                " source_kind, source_ref, source_excerpt, due_time)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (title.strip(), due, priority, project, source, now, now, defer, note,
                 waiting_on, owed_to, parent_id, source_kind, source_ref, source_excerpt,
                 due_time))
            row = con.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()
    _task_written()
    return _annotate(dict(row), day_key())


def task_exists_for_source(source_kind: str, source_ref: str) -> bool:
    """Dedupe check for auto-created tasks (archive/ROUND34_PLAN.md: §16 check-in,
    §18 card payment) — call before create_task(). Matches regardless of
    status: a completed/dropped auto-task should not be recreated either,
    only a fresh source_ref (e.g. a new due_day) makes a new task."""
    with session() as con:
        row = con.execute(
            "SELECT 1 FROM tasks WHERE source_kind=? AND source_ref=? LIMIT 1",
            (source_kind, source_ref)).fetchone()
    return row is not None


def update_task(task_id: int, **fields) -> dict | None:
    """PATCH-style partial update over the triage-editable fields. Only keys
    the caller actually passes are touched — pass a field with value None to
    clear it, or omit it to leave it alone (widgets.py uses Pydantic's
    exclude_unset for this). Rejects parent_id pointing at a task that
    itself has a parent (one-level-deep guard) and self-parenting."""
    fields = {k: v for k, v in fields.items() if k in TASK_FIELDS}
    if not fields:
        with session() as con:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return _with_subtasks(con, [dict(row)])[0] if row else None
    with session() as con:
        if fields.get("parent_id") is not None:
            _check_parent(con, fields["parent_id"], task_id)
        if "due" in fields or "due_time" in fields:
            cur_due = con.execute("SELECT due FROM tasks WHERE id=?",
                                  (task_id,)).fetchone()
            _normalize_due(fields, cur_due["due"] if cur_due else None)
        set_clause = ", ".join(f"{k}=?" for k in fields)
        with con:
            con.execute(f"UPDATE tasks SET {set_clause} WHERE id=?",
                        (*fields.values(), task_id))
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        task = _annotate(_with_subtasks(con, [dict(row)])[0], day_key())
    _task_written()
    return task


def set_task_status(task_id: int, status: str, waiting_on: str | None = None) -> dict | None:
    """Closing a parent (done/dropped) cascades to its still-open subtasks —
    a subtask has no independent existence once its parent is finished.
    Parking a parent (waiting/someday) does NOT cascade: children stay
    attached but hidden until the parent reopens (tasks_today()/list_tasks()
    only surface parent_id IS NULL rows directly)."""
    completed = datetime.now().isoformat(timespec="seconds") if status == "done" else None
    with session() as con, con:
        if status == "open":
            # Reopening (an undo, usually) reverses the cascade too: the
            # subtasks the close took with it carry the parent's exact
            # completed_at, which a subtask finished on its own doesn't.
            prev = con.execute("SELECT status, completed_at FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
            if prev and prev["status"] in ("done", "dropped"):
                con.execute(
                    "UPDATE tasks SET status='open', completed_at=NULL WHERE parent_id=?"
                    " AND status=? AND completed_at IS ?",
                    (task_id, prev["status"], prev["completed_at"]))
        if status == "waiting" and waiting_on is not None:
            con.execute("UPDATE tasks SET status=?, completed_at=?, waiting_on=? WHERE id=?",
                        (status, completed, waiting_on, task_id))
        else:
            con.execute("UPDATE tasks SET status=?, completed_at=? WHERE id=?",
                        (status, completed, task_id))
        if status in ("done", "dropped"):
            con.execute(
                "UPDATE tasks SET status=?, completed_at=? WHERE parent_id=? AND status='open'",
                (status, completed, task_id))
        row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    _task_written()
    return dict(row)


def tasks_today() -> dict:
    """Today-view triage groups (archive/TASKS_PLAN.md). 'Available' = status='open'
    AND (defer IS NULL OR defer <= day_key()) — defer expiring needs no job,
    it's a read-time comparison (lazy-over-scheduled). Children of
    done/dropped parents and deferred/someday/fresh-waiting tasks stay
    invisible here until the full list is opened."""
    today = day_key()
    now = datetime.now().isoformat(timespec="minutes")
    with session() as con:
        available = con.execute(
            "SELECT * FROM tasks WHERE status='open' AND (defer IS NULL OR defer<=?)"
            f" AND parent_id IS NULL ORDER BY {DUE_ORDER}", (today,)).fetchall()
        available = [_annotate(dict(r), today) for r in available]
        # A task due today at an hour that has already passed is overdue, not
        # "due today" — leaving it in the today group is how a 9am deadline sat
        # quietly below a 5pm one all afternoon. An untimed task is due by end
        # of day, so it never crosses over until the day key itself rolls.
        def past(t: dict) -> bool:
            return bool(t["due"]) and (t["due"] < today
                                       or (t["due_at"] is not None and t["due_at"] <= now))
        overdue = [t for t in available if past(t)]
        due_today = [t for t in available if t["due"] == today and not past(t)]
        rest = sorted(
            (t for t in available if not t["due"] or t["due"] > today),
            key=lambda t: (t["priority"] if t["priority"] is not None else 9,
                           t["due"] or "9999", t["due_time"] or ""))
        cap = 10
        available_capped = rest[:cap]
        waiting_stale = [dict(r) for r in con.execute(
            "SELECT * FROM tasks WHERE status='waiting' AND parent_id IS NULL"
            " AND updated_at <= datetime('now','localtime','-7 days')"
            " ORDER BY updated_at ASC").fetchall()]
        counts = {row["status"]: row["n"] for row in con.execute(
            "SELECT status, COUNT(*) n FROM tasks WHERE parent_id IS NULL GROUP BY status")}
        counts.setdefault("open", 0)
        counts["deferred"] = con.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status='open' AND defer>? AND parent_id IS NULL",
            (today,)).fetchone()["n"]
        # _with_subtasks attaches "subtasks" in place, so overdue/due_today/
        # available_capped/waiting_stale (same dicts) already carry it — no
        # need to rebuild a fresh id->task map from its return value.
        shown = overdue + due_today + available_capped + waiting_stale
        _with_subtasks(con, shown)
        # waiting_stale came straight off its own query, and the subtasks just
        # attached haven't been through _annotate at all.
        for t in shown:
            _annotate(t, today)
    return {
        "overdue": overdue,
        "due_today": due_today,
        "available": available_capped,
        "waiting_stale": waiting_stale,
        # The day key the triage above was computed against. The client needs
        # it to resolve "due at 5pm" against the right date when quick-adding;
        # deciding that in the browser is what the rollover makes wrong.
        "today": today,
        "counts": {"open": counts.get("open", 0), "waiting": counts.get("waiting", 0),
                   "someday": counts.get("someday", 0), "deferred": counts["deferred"]},
    }


# ---------- habits ----------

def _created_day(habit: dict) -> str:
    """The habit's birth *day_key*, not the calendar date of its created_at
    wall-clock stamp. A habit created at 3am belongs to the previous day_key;
    comparing raw created_at dates against day keys made that habit's own
    first day look like it predated it (blank cell instead of a miss)."""
    try:
        return day_key(datetime.fromisoformat(habit["created_at"]))
    except (TypeError, ValueError):
        return str(habit.get("created_at", ""))[:10]


def list_habits(day: str | None = None) -> list[dict]:
    day = day or day_key()
    with session() as con:
        habits = [dict(r) for r in con.execute(
            "SELECT * FROM habits WHERE archived=0 ORDER BY id")]
        # One query for the day's values instead of one per habit.
        today_vals = {r["habit_id"]: r["value"] for r in con.execute(
            "SELECT habit_id, value FROM habit_checkins WHERE day=?", (day,))}
        for h in habits:
            h["today"] = today_vals.get(h["id"], 0)
            h["streak"] = _streak(con, h["id"], day)
            h["created_day"] = _created_day(h)
    return habits


def habit_history(days: int = 7) -> dict:
    """Each active habit with its check state for the last `days` day_keys
    (oldest first) — the week-grid view. Callers decide how to render a day
    the habit predates (`created_day` is returned so pre-birth days can show
    neutral, not a miss — compare against that, never raw created_at)."""
    today = day_key()
    span = [(date.fromisoformat(today) - timedelta(days=i)).isoformat()
            for i in range(days - 1, -1, -1)]
    with session() as con:
        habits = [dict(r) for r in con.execute(
            "SELECT * FROM habits WHERE archived=0 ORDER BY id")]
        qmarks = ",".join("?" * len(span))
        for h in habits:
            checks = {r["day"]: r["value"] for r in con.execute(
                f"SELECT day, value FROM habit_checkins WHERE habit_id=? AND day IN ({qmarks})",
                (h["id"], *span))}
            h["days"] = [{"day": d, "value": checks.get(d, 0)} for d in span]
            h["streak"] = _streak(con, h["id"], today)
            h["created_day"] = _created_day(h)
    return {"today": today, "span": span, "habits": habits}


def find_habit(name: str, include_archived: bool = False) -> dict | None:
    """Resolve a habit by name, case- and whitespace-insensitively.

    The UI checks a habit off by id because it renders the row it clicks; the
    console only ever has what the user said ("log my exercise"), so it needs
    the name to be the key. Kept here rather than in the caller so the check-in
    and create paths agree about when two names are the same habit — if they
    disagreed, creating "exercise" beside "Exercise" would split a streak in
    two and the check-in would keep writing to whichever one it matched.

    Matched in Python, not SQL: SQLite's LOWER() and NOCASE are ASCII-only,
    and these are user-typed names ('$50 DoorDash'). There are four rows.
    """
    key = " ".join(name.split()).casefold()
    if not key:
        return None
    sql = "SELECT * FROM habits" + ("" if include_archived else " WHERE archived=0")
    with session() as con:
        for row in con.execute(sql):
            if " ".join(row["name"].split()).casefold() == key:
                return dict(row)
    return None


def update_habit(habit_id: int, name: str | None = None,
                 archived: bool | None = None) -> dict | None:
    """Rename or archive a habit. Archiving keeps its check-in history (the
    list/grid filter on archived=0), so it's reversible and streaks aren't lost."""
    fields: dict = {}
    if name is not None:
        fields["name"] = name.strip()
    if archived is not None:
        fields["archived"] = int(archived)
    with session() as con:
        if fields:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            with con:
                con.execute(f"UPDATE habits SET {set_clause} WHERE id=?",
                            (*fields.values(), habit_id))
        row = con.execute("SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()
    return dict(row) if row else None


def delete_habit(habit_id: int) -> dict | None:
    """Remove a habit *and its check-ins*, permanently. Returns the deleted
    row, or None if there was nothing there.

    This exists alongside archiving rather than replacing it, because the two
    answer different questions. Archiving is for a habit you stopped doing —
    the history is the point, streaks stay intact, and it comes back. This is
    for a habit that should never have existed (a typo, a duplicate, an
    experiment), where keeping the row means carrying a name you don't want to
    read again.

    The check-ins go explicitly rather than by FK cascade: the column is
    declared `REFERENCES habits(id)` but `PRAGMA foreign_keys` is not on for
    every connection, so relying on the cascade would leave orphaned rows on
    some paths and not others — and orphans in habit_checkins re-appear in
    habit_history as a habit with no name.
    """
    with session() as con:
        row = con.execute("SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()
        if row is None:
            return None
        with con:
            con.execute("DELETE FROM habit_checkins WHERE habit_id=?", (habit_id,))
            con.execute("DELETE FROM habits WHERE id=?", (habit_id,))
    return dict(row)


def create_habit(name: str, cadence: str = "daily", target: float = 1,
                 unit: str | None = None) -> dict:
    with session() as con, con:
        cur = con.execute(
            "INSERT INTO habits (name, cadence, target, unit, created_at) VALUES (?,?,?,?,?)",
            (name.strip(), cadence, target, unit, datetime.now().isoformat(timespec="seconds")))
        row = con.execute("SELECT * FROM habits WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def checkin(habit_id: int, day: str | None = None, value: float = 1,
            note: str | None = None, source: str = "ui") -> None:
    """Idempotent upsert. A manual un-check (value=0) leaves a value=0 tombstone
    rather than deleting the row, so an auto pass can't resurrect a day the user
    deliberately cleared; value=0 reads as "not done" everywhere (streak filters
    it out). `source` records who wrote the row ('ui'/'console'/'auto')."""
    day = day or day_key()
    with session() as con, con:
        con.execute(
            "INSERT INTO habit_checkins (habit_id, day, value, note, source)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(habit_id, day) DO UPDATE SET"
            " value=excluded.value, note=excluded.note, source=excluded.source",
            (habit_id, day, value, note, source))


def auto_checkin(habit_name: str, day: str, value: float = 1,
                 note: str | None = None) -> bool:
    """Signal-driven check-in for the named active habit (e.g. health AZM →
    'Exercise'). Never overwrites an existing row — a person's check, un-check
    tombstone, or a prior auto row all win — so it only ever fills a day the
    user hasn't touched. Returns True iff a new row was written. A no-op if the
    habit doesn't exist (renamed/archived), keeping the signal side decoupled
    from whether the habit is present."""
    with session() as con, con:
        row = con.execute(
            "SELECT id FROM habits WHERE name=? AND archived=0", (habit_name,)
        ).fetchone()
        if row is None:
            return False
        cur = con.execute(
            "INSERT INTO habit_checkins (habit_id, day, value, note, source)"
            " VALUES (?,?,?,?,'auto')"
            " ON CONFLICT(habit_id, day) DO NOTHING",
            (row["id"], day, value, note))
        return cur.rowcount > 0


def _streak(con: sqlite3.Connection, habit_id: int, today: str) -> int:
    """Consecutive days checked, counting back from today (an un-done today
    doesn't break the streak — it survives until today is missed for good).

    Walks the (habit_id, day) index backwards and stops at the first gap, so
    it reads only the streak's own rows, not the habit's whole history."""
    expected = None
    n = 0
    for row in con.execute(
            "SELECT day FROM habit_checkins WHERE habit_id=? AND day<=? AND value>0"
            " ORDER BY day DESC",
            (habit_id, today)):
        if expected is None:
            # Anchor on the most recent check: today, or yesterday if today
            # isn't done yet. Anything older means the streak already ended.
            if row["day"] == today:
                expected = date.fromisoformat(today)
            elif row["day"] == (date.fromisoformat(today) - timedelta(days=1)).isoformat():
                expected = date.fromisoformat(today) - timedelta(days=1)
            else:
                return 0
        if row["day"] != expected.isoformat():
            break
        n += 1
        expected -= timedelta(days=1)
    return n


# ---------- small lists: debts, user lists, watchlist ----------
#
# Three tables with one shape: add a row, flip its done-flag, drop it. The
# flag column differs per table (settled / done / watched) because each reads
# naturally in its own domain, so the shared helpers take the spec rather than
# forcing a common column name. Anything domain-specific (debt totals,
# watchlist kind, which list an item is on) stays in its own function below.

_LIST_SPECS: dict[str, tuple[str, str, str]] = {
    # table         done-flag   done-timestamp  default order
    "debts":       ("settled",  "settled_at",   "settled, person COLLATE NOCASE"),
    "list_items":  ("done",     "done_at",      "done, category, name COLLATE NOCASE"),
    "watchlist":   ("watched",  "watched_at",   "watched, kind, title COLLATE NOCASE"),
    "beers":       ("settled",  "settled_at",   "settled, person COLLATE NOCASE, id"),
}


def _spec(table: str) -> tuple[str, str, str]:
    try:
        return _LIST_SPECS[table]
    except KeyError:
        raise ValueError(f"not a list table: {table}")


def list_rows(table: str, include_done: bool = False) -> list[dict]:
    """Rows for one of the small lists. Done rows are excluded by default —
    the widgets lead with what's still outstanding."""
    done_col, _, order = _spec(table)
    where = "" if include_done else f" WHERE {done_col}=0"
    with session() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM {table}{where} ORDER BY {order}")]


def set_row_done(table: str, row_id: int, done: bool) -> dict | None:
    """Flip a row's done-flag. Stamps the matching timestamp on the way in and
    clears it on undo, so a re-opened row doesn't keep a stale settled/bought
    date."""
    done_col, at_col, _ = _spec(table)
    now = datetime.now().isoformat(timespec="seconds") if done else None
    with session() as con:
        with con:
            con.execute(f"UPDATE {table} SET {done_col}=?, {at_col}=? WHERE id=?",
                        (int(done), now, row_id))
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return dict(row) if row else None


def delete_row(table: str, row_id: int) -> bool:
    _spec(table)  # reject unknown tables before interpolating one into SQL
    with session() as con, con:
        return con.execute(f"DELETE FROM {table} WHERE id=?", (row_id,)).rowcount > 0


def create_debt(person: str, amount: float, direction: str = "owed_to_me",
                reason: str | None = None, person_key: str | None = None,
                incurred_on: str | None = None) -> dict:
    if direction not in ("owed_to_me", "i_owe"):
        raise ValueError("direction must be owed_to_me or i_owe")
    if not str(person).strip():
        raise ValueError("person is required")
    now = datetime.now().isoformat(timespec="seconds")
    with session() as con, con:
        cur = con.execute(
            "INSERT INTO debts (person, person_key, direction, amount, reason,"
            " incurred_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (person.strip(), person_key, direction, float(amount), reason,
             incurred_on, now, now))
        return dict(con.execute("SELECT * FROM debts WHERE id=?", (cur.lastrowid,)).fetchone())


def update_debt(debt_id: int, **fields) -> dict | None:
    """Rewrite a debt in place: person, amount, direction, reason or
    incurred_on. Fields left as None are untouched, and "" clears reason or
    incurred_on. Returns the updated row, or None if there's no such debt.

    This exists so that correcting or merging a debt doesn't have to go
    through settling it. Settling stamps settled_at, which reads as "they
    paid". On 2026-10-11, merging nine split rows into one per person that
    way left nine payments on record that never happened."""
    sets: dict = {}
    if fields.get("person") is not None:
        if not str(fields["person"]).strip():
            raise ValueError("person can't be blank")
        sets["person"] = str(fields["person"]).strip()
    if fields.get("amount") is not None:
        if float(fields["amount"]) <= 0:
            raise ValueError("amount must be greater than zero")
        sets["amount"] = float(fields["amount"])
    if fields.get("direction") is not None:
        if fields["direction"] not in ("owed_to_me", "i_owe"):
            raise ValueError("direction must be owed_to_me or i_owe")
        sets["direction"] = fields["direction"]
    for k in ("reason", "incurred_on"):
        if fields.get(k) is not None:
            sets[k] = str(fields[k]).strip() or None
    with session() as con:
        if con.execute("SELECT 1 FROM debts WHERE id=?", (debt_id,)).fetchone() is None:
            return None
        if sets:
            sets["updated_at"] = datetime.now().isoformat(timespec="seconds")
            with con:
                con.execute(
                    f"UPDATE debts SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                    (*sets.values(), debt_id))
        return dict(con.execute("SELECT * FROM debts WHERE id=?",
                                (debt_id,)).fetchone())


def remind_debt(debt_id: int) -> dict | None:
    """Record that you've just chased this one. Returns the updated row, or
    None if there's no such debt.

    Overwrites `reminded_at` rather than appending: the nudge only ever asks
    "how long since the last ask", so a history of asks would be storage
    nothing reads. `reminded_count` keeps the part that would otherwise be
    lost — how many times you've had to ask is what eventually decides whether
    to keep asking, and it can't be rebuilt from one overwritten timestamp."""
    now = datetime.now().isoformat(timespec="seconds")
    with session() as con:
        if con.execute("SELECT 1 FROM debts WHERE id=?", (debt_id,)).fetchone() is None:
            return None
        with con:
            con.execute(
                "UPDATE debts SET reminded_at=?, reminded_count=reminded_count+1,"
                " updated_at=? WHERE id=?", (now, now, debt_id))
        return dict(con.execute("SELECT * FROM debts WHERE id=?",
                                (debt_id,)).fetchone())


def _debt_nudge(row: dict) -> dict:
    """Annotate one debt with whether it's your move, and since when.

    Three states rather than two, for the same reason heartbeat has four:
    `never` (you have not asked at all) and `overdue` (you asked and heard
    nothing back) both want your attention but are different actions, and
    collapsing them would make the card say "remind them again" about someone
    you have never once mentioned it to.

    Only `owed_to_me` is nudged. The verb here is "I asked them", which has no
    meaning pointed the other way — a debt you owe is chased by the other
    person, and inventing a nudge for it would put a button on the card that
    does nothing anyone asked for.

    Measured in day_key steps, not elapsed hours: "a week" has to mean seven
    calendar days the way the rest of this schema means them, or a reminder
    sent late at night comes due an evening early.
    """
    out = dict(row)
    out["nudge"] = None
    out["reminded_days_ago"] = None
    if row["direction"] != "owed_to_me" or row.get("settled"):
        return out

    at = row.get("reminded_at")
    if not at:
        out["nudge"] = "never"
        return out

    try:
        asked = date.fromisoformat(day_key(datetime.fromisoformat(at)))
    except (TypeError, ValueError):
        out["nudge"] = "never"      # unreadable timestamp: treat as unasked
        return out

    days = (date.fromisoformat(day_key()) - asked).days
    out["reminded_days_ago"] = days
    out["nudge"] = "overdue" if days >= DEBT_NUDGE_DAYS else "waiting"
    return out


def debt_summary() -> dict:
    """Open debts plus the two directional totals. Net is positive when more
    is owed to you than by you — the number the card leads with.

    Each debt also carries its nudge state, computed here rather than in the
    card: "a week" is a day_key question, and the browser's own midnight
    disagrees with day_key for four hours every night."""
    rows = [_debt_nudge(r) for r in list_rows("debts")]
    owed_to_me = round(sum(r["amount"] for r in rows if r["direction"] == "owed_to_me"), 2)
    i_owe = round(sum(r["amount"] for r in rows if r["direction"] == "i_owe"), 2)
    return {"debts": rows, "owed_to_me": owed_to_me, "i_owe": i_owe,
            "net": round(owed_to_me - i_owe, 2),
            "to_chase": sum(1 for r in rows if r["nudge"] in ("never", "overdue")),
            "nudge_days": DEBT_NUDGE_DAYS}


def add_beers(person: str, count: int = 1, direction: str = "owed_to_me",
              reason: str | None = None) -> list[dict]:
    """File `count` beers, one row each. A row per beer, not a count column,
    so paying one back is the same settle as any other small list and the
    record of who bought which round survives it."""
    if direction not in ("owed_to_me", "i_owe"):
        raise ValueError("direction must be owed_to_me or i_owe")
    if not str(person).strip():
        raise ValueError("person is required")
    if not 1 <= int(count) <= 24:
        raise ValueError("count must be between 1 and 24")
    now = datetime.now().isoformat(timespec="seconds")
    with session() as con, con:
        ids = [con.execute(
            "INSERT INTO beers (person, direction, reason, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (person.strip(), direction, reason, now, now)
        ).lastrowid for _ in range(int(count))]
        return [dict(r) for r in con.execute(
            f"SELECT * FROM beers WHERE id IN ({','.join('?' * len(ids))})", ids)]


def settle_beer(person: str, direction: str = "owed_to_me") -> dict | None:
    """Settle the oldest open beer between you and `person` (case-blind).
    Returns the settled row, or None when there's nothing open to settle."""
    with session() as con:
        row = con.execute(
            "SELECT id FROM beers WHERE settled=0 AND direction=?"
            " AND person=? COLLATE NOCASE ORDER BY id LIMIT 1",
            (direction, str(person).strip())).fetchone()
    return set_row_done("beers", row["id"], True) if row else None


def beer_summary() -> dict:
    """Open beers grouped per person and direction. The card shows "Mo ×2",
    not two rows, and its check settles one (the oldest) at a time.

    Kept apart from debt_summary on purpose: a beer is not $5, and folding it
    into the dollar total or the to-chase nudges would turn a running joke
    into a collections notice."""
    groups: dict[tuple[str, str], dict] = {}
    for r in list_rows("beers"):
        g = groups.setdefault((r["person"].lower(), r["direction"]), {
            "person": r["person"], "direction": r["direction"],
            "count": 0, "ids": [], "reasons": []})
        g["count"] += 1
        g["ids"].append(r["id"])
        if r["reason"] and r["reason"] not in g["reasons"]:
            g["reasons"].append(r["reason"])   # "2 for ski trip" is one reason
    beers = sorted(groups.values(), key=lambda g: (g["direction"], -g["count"],
                                                   g["person"].lower()))
    return {"beers": beers,
            "owed_to_me": sum(g["count"] for g in beers if g["direction"] == "owed_to_me"),
            "i_owe": sum(g["count"] for g in beers if g["direction"] == "i_owe")}


def all_lists(include_done: bool = False) -> list[dict]:
    """Every list with its items and its open count, in chip order.

    Two queries, not one per list: the card draws every chip's count at once,
    and these tables are small enough that shipping all of it beats a second
    round trip when the chip is clicked. `open` is counted separately from the
    items so it stays the number of things still outstanding even when the
    caller asked for the done rows too.
    """
    done_col, _, order = _spec("list_items")
    where = "" if include_done else f" WHERE {done_col}=0"
    with session() as con:
        lists = [dict(r) for r in con.execute(
            "SELECT * FROM lists ORDER BY position, id")]
        items = [dict(r) for r in con.execute(
            f"SELECT * FROM list_items{where} ORDER BY {order}")]
        open_counts = {r["list_id"]: r["n"] for r in con.execute(
            "SELECT list_id, COUNT(*) AS n FROM list_items WHERE done=0"
            " GROUP BY list_id")}
    by_list: dict[int, list[dict]] = {}
    for it in items:
        by_list.setdefault(it["list_id"], []).append(it)
    for lst in lists:
        lst["items"] = by_list.get(lst["id"], [])
        lst["open"] = open_counts.get(lst["id"], 0)
    return lists


def list_items_for(list_id: int, include_done: bool = False) -> list[dict]:
    """One list's items. `all_lists` exists alongside this rather than calling
    it per list: the card wants every list at once and that would be a query
    per chip. This is the single-list read for everything else."""
    done_col, _, order = _spec("list_items")
    where = "" if include_done else f" AND {done_col}=0"
    with session() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM list_items WHERE list_id=?{where} ORDER BY {order}",
            (list_id,))]


def project_folders() -> dict[str, str]:
    """Project path → the folder it's filed under (console_profiles)."""
    with session() as con:
        return {r["path"]: r["folder"] for r in con.execute(
            "SELECT path, folder FROM project_folders")}


def set_project_folder(path: str, folder: str | None) -> str | None:
    """File a project under `folder`, or under nothing when it's empty. An
    existing folder keeps its spelling whatever the case typed here, so
    "client projects" joins "Client Projects" instead of starting a twin.
    Returns the folder as stored."""
    now = datetime.now().isoformat(timespec="seconds")
    with session() as con, con:
        if not folder:
            con.execute("DELETE FROM project_folders WHERE path=?", (path,))
            return None
        row = con.execute("SELECT folder FROM project_folders WHERE folder=?"
                          " COLLATE NOCASE LIMIT 1", (folder,)).fetchone()
        folder = row["folder"] if row else folder
        con.execute("INSERT INTO project_folders (path, folder, updated_at)"
                    " VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET"
                    " folder=excluded.folder, updated_at=excluded.updated_at",
                    (path, folder, now))
    return folder


def find_list(name: str) -> dict | None:
    """A list by name, case-insensitively — how the console addresses one. The
    UI passes ids; a model that was told "add it to my packing list" has only
    the words."""
    with session() as con:
        row = con.execute("SELECT * FROM lists WHERE name=? COLLATE NOCASE",
                          (str(name).strip(),)).fetchone()
    return dict(row) if row else None


def create_list(name: str) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError("name is required")
    now = datetime.now().isoformat(timespec="seconds")
    with session() as con, con:
        pos = con.execute("SELECT COALESCE(MAX(position), -1) + 1"
                          " FROM lists").fetchone()[0]
        try:
            cur = con.execute(
                "INSERT INTO lists (name, position, created_at) VALUES (?,?,?)",
                (name, pos, now))
        except sqlite3.IntegrityError:
            raise ValueError(f"a list called {name!r} already exists")
        row = dict(con.execute("SELECT * FROM lists WHERE id=?",
                               (cur.lastrowid,)).fetchone())
    return row | {"items": [], "open": 0}


def rename_list(list_id: int, name: str) -> dict | None:
    name = str(name).strip()
    if not name:
        raise ValueError("name is required")
    with session() as con, con:
        try:
            con.execute("UPDATE lists SET name=? WHERE id=?", (name, list_id))
        except sqlite3.IntegrityError:
            raise ValueError(f"a list called {name!r} already exists")
        row = con.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()
    return dict(row) if row else None


def delete_list(list_id: int) -> bool:
    """Drop a list and everything on it — the items go with it by FK cascade
    (`PRAGMA foreign_keys=ON` is set per connection), so no orphan rows are
    left addressable by an id that no chip can reach."""
    with session() as con, con:
        return con.execute("DELETE FROM lists WHERE id=?",
                           (list_id,)).rowcount > 0


def get_list_item(item_id: int) -> dict | None:
    with session() as con:
        row = con.execute("SELECT * FROM list_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def add_list_item_note(item_id: int, note: str) -> dict | None:
    """Attach `note` to a list item, after any note it already has (a line
    each), so a note written at completion doesn't erase the one it was
    added with. The same text twice is kept once."""
    note = str(note or "").strip()
    with session() as con, con:
        row = con.execute("SELECT note FROM list_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return None
        have = row["note"] or ""
        if note and note not in have.splitlines():
            con.execute("UPDATE list_items SET note=? WHERE id=?",
                        (f"{have}\n{note}" if have else note, item_id))
        return dict(con.execute("SELECT * FROM list_items WHERE id=?", (item_id,)).fetchone())


def create_list_item(list_id: int, name: str, category: str | None = None,
                     note: str | None = None) -> dict:
    if not str(name).strip():
        raise ValueError("name is required")
    with session() as con, con:
        if con.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone() is None:
            raise ValueError(f"no list {list_id}")
        cur = con.execute(
            "INSERT INTO list_items (list_id, name, category, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (list_id, name.strip(), (category or None), note,
             datetime.now().isoformat(timespec="seconds")))
        return dict(con.execute("SELECT * FROM list_items WHERE id=?",
                                (cur.lastrowid,)).fetchone())


def create_watch_item(title: str, year: int | None = None, kind: str = "movie",
                      services: str | None = None, note: str | None = None) -> dict:
    if kind not in ("movie", "show"):
        raise ValueError("kind must be movie or show")
    if not str(title).strip():
        raise ValueError("title is required")
    with session() as con, con:
        cur = con.execute(
            "INSERT INTO watchlist (title, year, kind, services, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (title.strip(), year, kind, services, note,
             datetime.now().isoformat(timespec="seconds")))
        return dict(con.execute("SELECT * FROM watchlist WHERE id=?",
                                (cur.lastrowid,)).fetchone())
