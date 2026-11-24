"""app.db — the app-owned source of truth for tasks, habits and the provider caches.

Written by both the backend and terminal skills (sqlite3 CLI), so every
connection sets `journal_mode=WAL` and `busy_timeout=5000`; skills must too.
`day_key()` is the ONLY way to compute "today" (local, 04:00 rollover).
Migrations are fingerprinted and never edited once shipped: DESIGN.md → Schema history.
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
    # v2 — OAuth tokens (secrets: never in API responses, see providers/oauth.py) + stub cache.
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
    # v3 — calendar cache (derived); calendar_days marks synced days, so no rows != never fetched.
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
    # v4 — events.calendar_name (derived; backfills on next sync).
    """
    ALTER TABLE events ADD COLUMN calendar_name TEXT;
    """,
    # v5 — health cache (derived); health_sync marks fetched days (no row = not worn, not unfetched).
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
    # v6 — email cache (metadata only, derived) + email_triage (summaries regenerable; user_action is real data).
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
    # v7 — daily brief: lazy per day_key, DB-only; the row doubles as the generation lock (app/brief.py).
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
    # v8 — finance: fin_* derived; plaid_items holds a real credential (never in responses or logs).
    # Amounts keep Plaid's sign: positive = money OUT.
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
    # v9 — weather (derived). Civil date, not day_key; sunrise/sunset are minutes since midnight; geo_cache memoizes geocoding.
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
    # v10 — tasks v2: nullable columns so v1 writers keep working; no status CHECK (skills write raw SQL); trigger keeps updated_at.
    # `source` = who created the row; source_kind/ref/excerpt = provenance (excerpt is denormalized on purpose).
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
    # v11 — task triage queue (removed in v12).
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
    # v12 — task triage queue removed.
    "DROP TABLE task_triage;",
    # v13 — screen time (dropped in v46; kept so a fresh app.db replays). Keyed by day_key.
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
    # v14 — news (derived). day = day_key of the fetch, not an article date; sources is a JSON list of domains.
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
    # v15 — fin_recurring / fin_liabilities (derived). predicted_next_date is Plaid's own; liabilities normalized to one shape.
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
    # v16 — Spotify, day_key. `plays` is NOT purely derived: the API returns only the last 50, so this is the only record.
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
    # v17 — messages (derived from chat.db, day_key, text capped at 2000) + generic sync_state watermark.
    # To force a re-sync, clear `messages` AND `sync_state`: the old watermark blocks a re-backfill.
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
    # v18 — email_extract cache + packages/trips (real state, not derived). packages.status only moves forward;
    # trips.status/gate belong to the AeroAPI poller.
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
    # v19 — people: NOT droppable (birthday/cadence/notes). person_key = normalized email or last-10 phone digits (messages.normalize_handle).
    #
    # emails.is_sent/to_addr: persisted from the existing sync; to_addr is the first recipient only.
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
    # v20 — location. location_points is NOT derived (only record); location_days is. day_key.
    # The `--` field names below are Overland's; units: providers/location_relay.py.
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
    # v21 — net-worth history: the ONLY record of balances (fin_accounts is overwritten each sync). One row per day_key per env.
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
    # v22 — people.hidden: muted senders; the row is kept so it's reversible.
    """
    ALTER TABLE people ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    """,
    # v23 — transaction logo_url/location (derived; only newly synced rows fill until a cursor reset).
    """
    ALTER TABLE fin_transactions ADD COLUMN logo_url TEXT;
    ALTER TABLE fin_transactions ADD COLUMN location TEXT;
    """,
    # v24 — chats: chat.db chat attributes (group names). Derived, full-replace each sync.
    """
    CREATE TABLE chats (
        chat_guid TEXT PRIMARY KEY,
        display_name TEXT,                     -- custom group name; NULL when unnamed
        is_group INTEGER NOT NULL DEFAULT 0,   -- chat.db style 43 = group, 45 = 1:1
        refreshed_at TEXT NOT NULL
    );
    """,
    # v25 — contact identity clusters from macOS Contacts' grouping; never merge on a shared name. Derived, full-replace.
    """
    CREATE TABLE contact_identities (
        identifier TEXT PRIMARY KEY,           -- normalized handle/email
        cluster_key TEXT NOT NULL,             -- canonical identifier for the person
        name TEXT,                             -- Contacts display name for the cluster
        refreshed_at TEXT NOT NULL
    );
    CREATE INDEX contact_identities_cluster ON contact_identities (cluster_key);
    """,
    # v26 — features day matrix (derived, providers/features.py). A source with no data is an absent row, never 0.
    """
    CREATE TABLE features (
        day TEXT NOT NULL,                     -- YYYY-MM-DD (source's own key)
        feature TEXT NOT NULL,
        value REAL NOT NULL,
        refreshed_at TEXT NOT NULL,
        PRIMARY KEY (day, feature)
    );
    """,
    # v27 — hourly heart rate (derived; the API caps rollups at 14 days). day/hour are local.
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
    # v28 — transaction website + counterparties (derived; same fill rule as v23).
    """
    ALTER TABLE fin_transactions ADD COLUMN website TEXT;
    ALTER TABLE fin_transactions ADD COLUMN counterparties TEXT;
    """,
    # v29 — checkin source. 'auto' never overwrites a person's row; un-check leaves a value=0 tombstone (reads as not done).
    """
    ALTER TABLE habit_checkins ADD COLUMN source TEXT NOT NULL DEFAULT 'ui';
    """,
    # v30 — email undo: JSON of what the action changed, replayed by POST /api/email/{id}/undo; NULL = best effort.
    """
    ALTER TABLE email_triage ADD COLUMN undo_state TEXT;
    """,
    # v31 — dismiss one waiting thread, keyed (kind, key); through_ts = the last_at dismissed, so newer activity resurfaces it.
    """
    CREATE TABLE IF NOT EXISTS waiting_dismissed (
      kind        TEXT NOT NULL,
      key         TEXT NOT NULL,
      through_ts  TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (kind, key)
    );
    """,
    # v32 — tapbacks, kept with a direction flag; excluded from volume metrics.
    """
    ALTER TABLE messages ADD COLUMN is_reaction INTEGER NOT NULL DEFAULT 0;
    """,
    # v33 — provider_health: one row per provider, overwritten each cycle; `fails` = consecutive failures.
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

    # v34 — provider_events: state changes and swallowed sub-step failures only; derived, trimmed to EVENT_LOG_KEEP.
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

    # v35 — plays.artist_key: casefolded artist name (SQLite lower() is ASCII-only); backfilled by spotify._ensure_artist_keys().
    #
    # artist_genres goes too; genres come from Last.fm (artist_tags, keyed on artist_key).
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

    # v36 — Last.fm's raw reply beside the cleaned tags, so `lastfm reclean` works offline; NULL = not refetched yet.
    """
    ALTER TABLE artist_tags ADD COLUMN raw TEXT;   -- JSON: toptags.tag verbatim
    """,

    # v37 — screen sync markers; the backfill deletes false zeros from screen_days and `features`.
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
    # v38 — attachments + attachment_text (derived; extraction is lazy). `locator` is source-shaped (local path | Gmail attachmentId);
    # nothing downloads during sync. status: empty != never tried (no row).
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
    # v39 — debts / lists / watchlist moved out of vault markdown (old files never read back); one row per debt; people.day_one/crew.
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
    # v40 — reddit (derived; drop both tables together). day = day_key of created_utc. score/num_comments NULL = unknown, never 0.
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
    # v41 — lists + list_items. shopping_items is migrated with ids kept (the console addresses items by id), then dropped.
    # lists.name is NOCASE-unique; items are not.
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
    # v42 — alarms: NOT derived; external_id is the only handle to what's in Reminders, so withdraw before DELETE.
    # The `key` 'kind:' prefix is load-bearing (alarms._alarm_key); fire_at is offset-aware ISO.
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
    # v43 — at-desk notification queue (persisted for restart-proof dedupe and to hold while away). key (app/keys.py) names the fact.
    # Queued = sent_at and expired_at both NULL.
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

    # v44 — failures: one row per fingerprint (first_at/last_at/count); derived, trimmed to FAILURE_KEEP.
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

    # v45 — usage: per-day card counts (distinct days is the honest unit); `band` separates read-at-a-glance from dead.
    # kinds: seen | open | sheet | command. Derived.
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

    # v46 — drop screen time. DESTRUCTIVE (dump: backend/data/backups/screen-tables-*.sql); IF EXISTS so fresh DBs boot.
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

    # v47 — debts.reminded_at/reminded_count. NULL = never asked; read via day_key against DEBT_NUDGE_DAYS.
    """
    ALTER TABLE debts ADD COLUMN reminded_at TEXT;
    ALTER TABLE debts ADD COLUMN reminded_count INTEGER NOT NULL DEFAULT 0;
    """,

    # v48 — office-hours proposals awaiting approval (the vault stays truth). `quote` is the verbatim email sentence;
    # rejections are kept so a message isn't re-proposed.
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
    # v49 — sleep segments + hourly steps (derived), keyed on wake-up date like health_days; timestamps local ISO with offset.
    # seq orders across the day; the health_sync marks mean 'asked, nothing there'.
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
    # v50 — basemap tiles (derived blob; no TTL, fetched_at is informational). The one table worth watching for size (~15KB/tile).
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

    # v51 — reverse-geocode cache keyed by lat/lon rounded to 4dp (~11m); derived, no TTL. Chosen names live in the vault.
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

    # v52 — place-suggestion evidence log, one row per candidate shown. NOT derived: rejections exist nowhere else.
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

    # v53 — per-visit place labels keyed by the visit's first fix (started_at); matched to any stay whose span contains it.
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

    # v54 — cite_refs: ref = sha1(kind+target) truncated (stable across sessions); INSERT OR IGNORE keeps the first target on a collision. Derived.
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
    # --- v-next: email_sender_stats (the sender prior). Derived from email_triage.user_action; rebuild() recreates it. ---
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

    # `n` decays (which action is current); `obs` doesn't (how much evidence). Don't merge them.
    """
    ALTER TABLE email_sender_stats ADD COLUMN obs INTEGER NOT NULL DEFAULT 0;
    """,
    # --- v-next: tasks.due_time. `due` stays a day key; NULL = no hour named. ---
    """
    ALTER TABLE tasks ADD COLUMN due_time TEXT;   -- 'HH:MM' local, NULL = all-day
    """,
    # --- v-next: calendars list (derived; backfills events.calendar_name). ---
    """
    CREATE TABLE calendars (
        calendar_id  TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        refreshed_at TEXT NOT NULL
    );
    """,
    # --- v-next: events.description, full text (console_preview truncates). ---
    """
    ALTER TABLE events ADD COLUMN description TEXT;
    """,
    # --- v-next: location_computed. A day computed to nothing is still computed; `points` triggers a recompute when late fixes arrive. ---
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
    # --- v-next: review labels + email_fts. label/terms are model output; user_label is real data; email_fts is derived
    # (email_search.ensure_index rebuilds when email_fts_state's signature changes). ---
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
    # email_fts without porter: prefix queries can't reach stemmed words. Derived; the cleared signature forces a rebuild.
    """
    DROP TABLE email_fts;
    CREATE VIRTUAL TABLE email_fts USING fts5(
        msg_id UNINDEXED, subject, sender, summary, terms, label,
        tokenize = 'unicode61 remove_diacritics 2');
    DELETE FROM email_fts_state;
    """,
    # --- v-next: expenses (app/expenses.py). Real data: each claim snapshots its charge (transaction_id is a soft link).
    # batch_id NULL = unsent; sent rows are frozen. expense_reviewed is per transaction, not a date watermark. ---
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
    # --- v-next: claims on pending charges. claimed_pending=1 until expenses.follow_posted moves it; posted_amount only on a sent claim that settled differently. ---
    """
    ALTER TABLE expenses ADD COLUMN claimed_pending INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE expenses ADD COLUMN posted_amount REAL;
    """,
    # --- v-next: snoozes. ask_id is the card hook's stable key; expired rows are pruned on read. ---
    """
    CREATE TABLE snoozes (
        ask_id  TEXT PRIMARY KEY,
        until   TEXT NOT NULL,
        created TEXT NOT NULL
    );
    """,
    # --- v-next: provider_health.recent, last 24 outcomes oldest first (o ok, x failed, . skipped), written by heartbeat.record(). ---
    """
    ALTER TABLE provider_health ADD COLUMN recent TEXT NOT NULL DEFAULT '';
    """,
    # --- v-next: the Brain (app/brain.py). verdict (confirmed|rejected) is NOT droppable; rebuild deletes only unruled ingest rows;
    # a rejected fact stays as a tombstone. status: active | pending | superseded (superseded_by) | rejected; prior_status restores on rejecting a replacement.
    # brain_runs is the ingest/lint ledger and watermark. ---
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
    # --- v-next: brain.source_label, fixed at ingest so pages avoid a join into a droppable cache. ---
    """
    ALTER TABLE brain_facts ADD COLUMN source_label TEXT;
    """,
    # --- office hours: action='add' (weekly block). when_str = rule, day = first date, role; prior = JSON of the entry an approved add replaced (for undo). ---
    """
    ALTER TABLE oh_proposals ADD COLUMN role TEXT;
    ALTER TABLE oh_proposals ADD COLUMN prior TEXT;
    """,
    # --- owed beers: one row per beer, kept apart from debts (a favor, not money). ---
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
    # --- USC athletics (providers/athletics.py): derived. day = Pacific civil date; start NULL when TBA. ---
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
    # --- Brightspace due dates (providers/coursework.py): derived. kind: due | available | event. ---
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
    # --- docs (app/docs.py): bytes at DATA_DIR/docs/<sha>.pdf are truth; `pages` is a derived JSON array (index 0 = page 1). ---
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
    # Food tracker (FOOD_PLAN.md). foods/food_log/food_profile/food_targets/food_days/weigh_ins are truth; the usc_*/food_venues/
    # food_estimates tables are the derived dining cache. A nutrient it doesn't know is absent, never 0. foods is keyed (source, source_id).
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
    # --- study coverage (app/study.py): one row per answered item; block = hash of the fence source, so a resend replaces. ---
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
    # --- .docx: clears cached `unsupported` verdicts (extract() never retries a row). ---
    """
    DELETE FROM attachment_text
     WHERE status = 'unsupported'
       AND attachment_id IN (
           SELECT id FROM attachments
            WHERE mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
               OR (COALESCE(mime, '') IN ('', 'application/octet-stream', 'application/zip')
                   AND lower(COALESCE(filename, '')) LIKE '%.docx'));
    """,
    # --- read receipts: chat.db stamps these in place after arrival; refreshed by messages._sync_receipts.
    # is_read/is_delivered can be set without a date. ---
    """
    ALTER TABLE messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN read_at TEXT;
    ALTER TABLE messages ADD COLUMN is_delivered INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN delivered_at TEXT;
    """,
    # --- scheduled email: the Gmail draft is the content; email_schedule.send_due() sends it at send_at. Truth, not cache. ---
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
    # --- activity log + self-texts. activity = every unprompted backend write; ref = undo handle (JSON), origin groups a burst. Truth.
    # selftext_messages = which burst a message went into, and the already-routed mark. ---
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
    # --- study confidence: dont_know | guess | unsure | sure; NULL = not said (and cards). ---
    #
    # No backfill: earlier attempts were never asked.
    """
    ALTER TABLE study_attempts ADD COLUMN confidence TEXT;
    """,
    # --- confidence_late = 1: rated after the verdict was on screen. ---
    """
    ALTER TABLE study_attempts ADD COLUMN rated_after INTEGER NOT NULL DEFAULT 0;
    """,
    # --- project folders: picker grouping only; no row = no folder; rows for missing dirs are ignored. ---
    """
    CREATE TABLE project_folders (
        path TEXT PRIMARY KEY,
        folder TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # --- food accuracy (app/food.py). portion_conf: NULL = stated, 0.6 = guessed, 0.9 = portion question answered; ask = pending question (JSON).
    # Backfill marks entries whose note says guessed/estimated. weigh_ins.at: no backfill. ---
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
    # --- console_sessions (app/console_work.py → app/token_usage.py): tab and mode of a Claude session; outlives console_work's rows. ---
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
    # --- saved Claude accounts (app/claude_accounts.py). No secret here (tokens: Keychain).
    # profile = the CLI's oauthAccount JSON; caps are percents, NULL = no ceiling.
    """
    CREATE TABLE IF NOT EXISTS claude_accounts (
        uuid TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        email TEXT,
        plan TEXT,
        profile TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        five_hour_cap REAL,
        weekly_cap REAL,
        idle_only INTEGER NOT NULL DEFAULT 0,
        active_since REAL,
        claimed_idle INTEGER NOT NULL DEFAULT 0,
        needs_login TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS claude_account_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS claude_account_events (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        text TEXT NOT NULL
    );
    """,
    # --- models (app/model_catalog.py). model_catalog and claude_model_access are derived
    # (droppable); model_defaults is the user's (keys "mode:dev", "tier:grunt", "provider:codex").
    """
    CREATE TABLE IF NOT EXISTS model_catalog (
        provider TEXT NOT NULL,
        id TEXT NOT NULL,
        family TEXT NOT NULL,
        label TEXT,
        released TEXT,
        rank INTEGER NOT NULL DEFAULT 0,
        lifecycle TEXT NOT NULL DEFAULT 'active',
        PRIMARY KEY (provider, id)
    );
    CREATE TABLE IF NOT EXISTS model_catalog_fetches (
        provider TEXT PRIMARY KEY,
        at REAL NOT NULL,
        ok INTEGER NOT NULL,
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS claude_model_access (
        account_uuid TEXT NOT NULL,
        family TEXT NOT NULL,
        ok INTEGER NOT NULL,
        reason TEXT,
        source TEXT NOT NULL,
        checked_at REAL NOT NULL,
        PRIMARY KEY (account_uuid, family)
    );
    CREATE TABLE IF NOT EXISTS model_defaults (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # --- outside providers' accounts (app/delegates/accounts.py): one row per sign-in, its token in
    # its `home`, none here. allowed_for: comma list of personal|learning|commercial.
    # delegate_runs.account is NULL before accounts or on an API key.
    """
    CREATE TABLE IF NOT EXISTS delegate_accounts (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        label TEXT NOT NULL,
        email TEXT,
        plan TEXT,
        home TEXT NOT NULL,
        allowed_for TEXT NOT NULL DEFAULT 'personal,learning,commercial',
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        monthly_credits REAL,
        credit_cap REAL,
        window_cap REAL,
        created_at TEXT NOT NULL
    );
    ALTER TABLE delegate_runs ADD COLUMN account TEXT;
    """,
    # --- model prices (app/model_prices.py): derived, droppable; $ per million tokens, long_*
    # = the tier past long_above. delegate_runs.tokens_cached is NULL when the CLI doesn't say.
    """
    CREATE TABLE IF NOT EXISTS model_prices (
        source TEXT NOT NULL,
        model TEXT NOT NULL,
        input REAL NOT NULL,
        output REAL NOT NULL,
        cache_read REAL,
        cache_write_5m REAL,
        cache_write_1h REAL,
        long_above INTEGER,
        long_input REAL,
        long_output REAL,
        long_cache_read REAL,
        long_cache_write_5m REAL,
        long_cache_write_1h REAL,
        PRIMARY KEY (source, model)
    );
    CREATE TABLE IF NOT EXISTS model_price_fetches (
        source TEXT PRIMARY KEY,
        at REAL NOT NULL,
        ok INTEGER NOT NULL,
        note TEXT,
        good_at REAL,
        models INTEGER
    );
    ALTER TABLE delegate_runs ADD COLUMN tokens_cached INTEGER;
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

    A day key runs 04:00-03:59, so 01:00 on key D is 01:00 on the calendar day after D.
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

    SQLite skips the busy handler when switching *into* WAL, so busy_timeout
    can't cover it: serialize in-process and retry for BUSY_TIMEOUT_MS.
    Already-WAL (the norm) costs one read.
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
                # Someone else is converting; once done this is a no-op.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)


# Paths already in WAL and migrated (this process is the only migrator), so
# connect() skips four statements per open. Keyed by path for test DBs.
_ready: set[str] = set()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(APP_DB)
    con.row_factory = sqlite3.Row
    # busy_timeout FIRST: journal_mode=WAL needs a write lock and fails at once without it.
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if (key := str(APP_DB)) not in _ready:
        _enable_wal(con)
        _migrate(con)
        _ready.add(key)   # only once the schema is actually current
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def session():
    """Open a connection and close it even on error. `with con:` inside scopes transactions."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def borrow(con: sqlite3.Connection | None = None):
    """session() for an optional caller connection: a passed `con` is the caller's, never closed or committed here."""
    if con is not None:
        yield con
        return
    with session() as own:
        yield own


def like_contains(text: str) -> str:
    r"""A LIKE pattern matching `text` as a literal substring (escapes `%` and `_`). Pair with ESCAPE '\'."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _schema_version(con: sqlite3.Connection) -> int:
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
    row = con.execute("SELECT v FROM schema_version").fetchone()
    return row["v"] if row else 0


# Serializes concurrent first connections (parallel poller threads) so they don't race on CREATE TABLE.
_migrate_lock = threading.Lock()


_SQL_COMMENT = re.compile(r"--[^\n]*")


def fingerprint(script: str) -> str:
    """Identify a migration by its SQL (comments and whitespace stripped), not its list position.

    Never change the SQL of a shipped migration: that changes its identity and re-runs it.
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
    """One-time hand-off from version counting to fingerprints: record the first `v` migrations.

    Returns None when the DB is counted past this checkout's list (another branch
    migrated it; its scripts aren't here to fingerprint). Opening it once from
    that branch completes the hand-off.
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
                # Display only (routes/status); never goes backwards.
                con.execute("DELETE FROM schema_version")
                con.execute("INSERT INTO schema_version (v) VALUES (?)",
                            (max(i, v),))


# ---------- tasks ----------

TASK_FIELDS = (
    "title", "due", "priority", "project", "note", "defer", "waiting_on",
    "owed_to", "parent_id", "due_time",
)


CLOSED_STATUSES = ("done", "dropped")

# Per day key: timed tasks in clock order, untimed last; times before 04:00 sort after 23:00.
DUE_ORDER = (
    "COALESCE(due,'9999') ASC,"
    " CASE WHEN due_time IS NULL THEN 3 WHEN due_time < '04:00' THEN 2 ELSE 1 END ASC,"
    " COALESCE(due_time,'') ASC"
)


def split_due(value: str | None) -> tuple[str | None, str | None]:
    """Split a due value into (day key, 'HH:MM' or None).

    Accepts "YYYY-MM-DD", "YYYY-MM-DDTHH:MM", "YYYY-MM-DD HH:MM", optional ":SS". Raises ValueError otherwise.
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
    """Validate an 'HH:MM' due time, dropping seconds. NULL/'' = no hour named."""
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
    """Fold a combined "due" into the two columns, in place.

    A `due` naming no hour clears `due_time`; a PATCH touching neither leaves both.
    `current_due` is the row's date, so an hour-only PATCH isn't a homeless time.
    """
    if "due" in fields:
        day, hhmm = split_due(fields["due"])
        fields["due"] = day
        # An explicit due_time in the same call wins over the one parsed from `due`.
        if fields.get("due_time") is None:
            fields["due_time"] = hhmm
    if "due_time" in fields:
        fields["due_time"] = normalize_due_time(fields["due_time"])
        if fields["due_time"] and not (fields["due"] if "due" in fields else current_due):
            raise ValueError("a due time needs a due date")


def _annotate(t: dict, today: str) -> dict:
    """Add read-time `deferred` and `due_at`, derived from day_key() (the browser clock disagrees with the rollover)."""
    t["deferred"] = bool(t.get("defer") and t["defer"] > today)
    t["due_at"] = (day_instant(t["due"], t["due_time"]).isoformat(timespec="minutes")
                   if t.get("due") and t.get("due_time") else None)
    for s in t.get("subtasks", []):
        _annotate(s, today)
    return t


def _with_subtasks(con: sqlite3.Connection, tasks: list[dict]) -> list[dict]:
    """Attach children under each parent (one level). A closed child drops out of an open parent's list; a closed parent keeps all."""
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
    """Top-level tasks only; subtasks ride nested via _with_subtasks."""
    with session() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status=? AND parent_id IS NULL"
                f" ORDER BY {DUE_ORDER}, COALESCE(priority,9) ASC, id ASC", (status,))
        else:
            rows = con.execute("SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY id DESC")
        out = _with_subtasks(con, [dict(r) for r in rows])
    today = day_key()
    for t in out:
        _annotate(t, today)
    return out


def _check_parent(con: sqlite3.Connection, parent_id: int, self_id: int | None) -> None:
    """Reject self-parenting, a parent that has a parent, and reparenting a task that has children (one level deep)."""
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


# Run after every committed task write, from the writer's thread (keeps tasks.md current;
# vault_views registers itself, since db can't import it). A raising hook is logged and skipped.
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
    """Dedupe check for auto-created tasks; matches any status, so a done/dropped auto-task isn't recreated."""
    with session() as con:
        row = con.execute(
            "SELECT 1 FROM tasks WHERE source_kind=? AND source_ref=? LIMIT 1",
            (source_kind, source_ref)).fetchone()
    return row is not None


def update_task(task_id: int, **fields) -> dict | None:
    """PATCH-style update: only passed keys change; None clears, omitted leaves alone."""
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
    """Closing a parent (done/dropped) cascades to its open subtasks; waiting/someday does not."""
    completed = datetime.now().isoformat(timespec="seconds") if status == "done" else None
    with session() as con, con:
        if status == "open":
            # Reopening reverses the cascade: cascaded subtasks carry the parent's exact completed_at.
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
    """Today-view triage groups. 'Available' = open and (defer IS NULL or defer <= day_key()), a read-time comparison."""
    today = day_key()
    now = datetime.now().isoformat(timespec="minutes")
    with session() as con:
        available = con.execute(
            "SELECT * FROM tasks WHERE status='open' AND (defer IS NULL OR defer<=?)"
            f" AND parent_id IS NULL ORDER BY {DUE_ORDER}", (today,)).fetchall()
        available = [_annotate(dict(r), today) for r in available]
        # A timed task past its hour is overdue; an untimed one is due by end of day.
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
        # _with_subtasks attaches in place, so the groups (same dicts) carry them.
        shown = overdue + due_today + available_capped + waiting_stale
        _with_subtasks(con, shown)
        # waiting_stale and the new subtasks haven't been through _annotate.
        for t in shown:
            _annotate(t, today)
    return {
        "overdue": overdue,
        "due_today": due_today,
        "available": available_capped,
        "waiting_stale": waiting_stale,
        # The client resolves quick-add times against this, not its own clock.
        "today": today,
        "counts": {"open": counts.get("open", 0), "waiting": counts.get("waiting", 0),
                   "someday": counts.get("someday", 0), "deferred": counts["deferred"]},
    }


# ---------- habits ----------

def _created_day(habit: dict) -> str:
    """The habit's birth day_key, not the calendar date of created_at (3am belongs to the previous key)."""
    try:
        return day_key(datetime.fromisoformat(habit["created_at"]))
    except (TypeError, ValueError):
        return str(habit.get("created_at", ""))[:10]


def list_habits(day: str | None = None) -> list[dict]:
    day = day or day_key()
    with session() as con:
        habits = [dict(r) for r in con.execute(
            "SELECT * FROM habits WHERE archived=0 ORDER BY id")]
        today_vals = {r["habit_id"]: r["value"] for r in con.execute(
            "SELECT habit_id, value FROM habit_checkins WHERE day=?", (day,))}
        for h in habits:
            h["today"] = today_vals.get(h["id"], 0)
            h["streak"] = _streak(con, h["id"], day)
            h["created_day"] = _created_day(h)
    return habits


def habit_history(days: int = 7) -> dict:
    """Active habits with check state for the last `days` day_keys, oldest first; compare against `created_day`, never raw created_at."""
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
    """Resolve a habit by name, case- and whitespace-insensitively (check-in and create must agree on identity).

    Matched in Python: SQLite's LOWER()/NOCASE are ASCII-only.
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
    """Rename or archive a habit; archiving keeps its check-ins (reversible)."""
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
    """Permanently remove a habit and its check-ins; returns the deleted row or None.

    Check-ins are deleted explicitly: foreign_keys isn't on for every connection, and orphans resurface in habit_history.
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
    """Idempotent upsert. An un-check (value=0) leaves a tombstone so an auto pass can't resurrect the day. `source`: ui/console/auto."""
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
    """Signal-driven check-in for the named active habit. Never overwrites a row (person, tombstone or auto).

    Returns True iff a row was written; a no-op if the habit is missing.
    """
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
    """Consecutive checked days back from today (an un-done today doesn't break it). Stops at the first gap."""
    expected = None
    n = 0
    for row in con.execute(
            "SELECT day FROM habit_checkins WHERE habit_id=? AND day<=? AND value>0"
            " ORDER BY day DESC",
            (habit_id, today)):
        if expected is None:
            # Anchor on today or yesterday; anything older ended the streak.
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
# One shape (add, flip a done-flag, drop); the flag column differs per table, so helpers take the spec.

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
    """Rows for one of the small lists; done rows excluded by default."""
    done_col, _, order = _spec(table)
    where = "" if include_done else f" WHERE {done_col}=0"
    with session() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM {table}{where} ORDER BY {order}")]


def set_row_done(table: str, row_id: int, done: bool) -> dict | None:
    """Flip a row's done-flag, stamping or clearing its timestamp."""
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
    """Rewrite a debt in place (None leaves a field, "" clears reason/incurred_on); None if missing.

    Don't merge debts by settling them: settled_at reads as "they paid".
    """
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
    """Record a chase; returns the row or None. Overwrites `reminded_at`; `reminded_count` keeps the tally."""
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
    """Annotate a debt with `nudge`: never | waiting | overdue (None unless an open owed_to_me debt).

    `never` and `overdue` are different actions, so they stay distinct. Measured in day_key steps, not hours.
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
    """Open debts plus directional totals (net > 0 = owed to you); each carries its nudge, computed server-side (day_key)."""
    rows = [_debt_nudge(r) for r in list_rows("debts")]
    owed_to_me = round(sum(r["amount"] for r in rows if r["direction"] == "owed_to_me"), 2)
    i_owe = round(sum(r["amount"] for r in rows if r["direction"] == "i_owe"), 2)
    return {"debts": rows, "owed_to_me": owed_to_me, "i_owe": i_owe,
            "net": round(owed_to_me - i_owe, 2),
            "to_chase": sum(1 for r in rows if r["nudge"] in ("never", "overdue")),
            "nudge_days": DEBT_NUDGE_DAYS}


def add_beers(person: str, count: int = 1, direction: str = "owed_to_me",
              reason: str | None = None) -> list[dict]:
    """File `count` beers, one row each, so settling is the same flag-flip as any small list."""
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
    """Open beers grouped per person and direction; a check settles the oldest. Kept out of debt_summary: a beer is not $5."""
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
    """Every list with its items and open count, in chip order. `open` counts outstanding items even when done rows are included."""
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
    """One list's items (all_lists is the every-chip read)."""
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
    """File a project under `folder` (empty = none); an existing folder keeps its spelling. Returns the folder as stored."""
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
    """A list by name, case-insensitively (the console has words, not ids)."""
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
    """Drop a list; its items go by FK cascade (foreign_keys=ON per connection)."""
    with session() as con, con:
        return con.execute("DELETE FROM lists WHERE id=?",
                           (list_id,)).rowcount > 0


def get_list_item(item_id: int) -> dict | None:
    with session() as con:
        row = con.execute("SELECT * FROM list_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def add_list_item_note(item_id: int, note: str) -> dict | None:
    """Append `note` as a new line after any existing note; the same text twice is kept once."""
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
