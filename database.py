"""
All database access for the bot lives here.
Uses SQLite (single file, no server needed - perfect for Termux).
"""

import sqlite3
import random
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import (
    DB_PATH, DEFAULT_CHARACTER_WEIGHT, FIGHTER_EVENT_NAME, ELEMENTS,
    FIGHTER_MAX_LEVEL, ARENA_BATTLE_DURATION_SECONDS, ARENA_LEAGUES,
    ARENA_MATCH_UP_CHANCE, ARENA_MATCH_DOWN_CHANCE, ARENA_NPC_OPPONENTS,
    ARENA_NPC_IDS, DAILY_TASK_BONUS, PREMIUM_DAILY_TASK_BONUS,
    PRICE_CHECK_DEDUP_WINDOW_SECONDS, ADMIN_TYPE_BUNDLES,
)


try:
    IRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:  # tz database missing on a minimal server image
    IRAN_TZ = timezone(timedelta(hours=3, minutes=30))  # Iran has no DST since 2022


def iran_today() -> str:
    """Today's date (YYYY-MM-DD) in Iran. Daily limits (captures, darts) roll over
    at 00:00 Iran time, not at the server's (UTC) midnight."""
    return datetime.now(IRAN_TZ).date().isoformat()


def get_connection():
    # The bot, the Mini App API and the background loops all share this one
    # SQLite file from different threads - wait for a busy lock instead of
    # failing straight away with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rarities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            weight INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            series TEXT NOT NULL,
            rarity_id INTEGER,
            image_file_id TEXT,
            added_by_user_id INTEGER,
            added_by_username TEXT,
            event_name TEXT,
            FOREIGN KEY (rarity_id) REFERENCES rarities(id)
        )
    """)

    # defensive migration for databases created before these columns existed
    for col_def in ("added_by_user_id INTEGER", "added_by_username TEXT", "event_name TEXT",
                     "media_type TEXT DEFAULT 'photo'",
                     # Hidden Chat-tab persona (Update 3) - never exposed to players in
                     # Market/profile/card views, only read server-side when building the
                     # AI's system prompt for that character's conversations.
                     "chat_persona TEXT", "chat_age TEXT", "chat_gender TEXT",
                     # id of this character's post in config.ARCHIVE_CHANNEL - the most
                     # recent one, whether that was the original "discovered" post or a
                     # later "updated" repost from /editcharacter. Lets /removecharacter
                     # delete the channel message when the character itself is deleted.
                     "archive_message_id INTEGER"):
        try:
            cur.execute(f"ALTER TABLE characters ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            character_id INTEGER NOT NULL,
            obtained_at TEXT,
            FOREIGN KEY (character_id) REFERENCES characters(id)
        )
    """)

    # defensive migration: per-copy Fighter progress (Update 2). Only
    # meaningful for a copy of a character that has a fighter_stats row;
    # stays NULL/default for every ordinary card.
    for col_def in ("level INTEGER DEFAULT 1", "current_attack INTEGER", "current_defense INTEGER"):
        try:
            cur.execute(f"ALTER TABLE user_characters ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            message_count INTEGER DEFAULT 0,
            distinct_senders TEXT DEFAULT '',
            pending_character_id INTEGER,
            pending_spawn_message_id INTEGER
        )
    """)

    # defensive migration: chat title, used so /stats can show which
    # groups the bot is active in by name instead of a bare numeric id.
    try:
        cur.execute("ALTER TABLE chat_state ADD COLUMN title TEXT")
    except sqlite3.OperationalError:
        pass


    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_tracker (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_sender_streak INTEGER DEFAULT 0,
            muted_until TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_last_sender (
            chat_id INTEGER PRIMARY KEY,
            last_user_id INTEGER,
            streak INTEGER DEFAULT 0,
            last_message_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_captures (
            user_id INTEGER NOT NULL,
            capture_date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, capture_date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS currency (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0
        )
    """)

    # defensive migration: lifetime earned/spent totals, used by the
    # earn_currency / spend_currency task types so unrelated income
    # (e.g. a daily bonus) can't cancel out real spending, and vice
    # versa - see _track_currency_delta.
    currency_totals_just_added = False
    for col_def in ("total_earned INTEGER DEFAULT 0", "total_spent INTEGER DEFAULT 0"):
        try:
            cur.execute(f"ALTER TABLE currency ADD COLUMN {col_def}")
            currency_totals_just_added = True
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_darts (
            user_id INTEGER NOT NULL,
            dart_date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, dart_date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS secondary_admins (
            user_id INTEGER PRIMARY KEY,
            admin_type TEXT
        )
    """)

    # defensive migration for databases created before admin_type existed
    try:
        cur.execute("ALTER TABLE secondary_admins ADD COLUMN admin_type TEXT")
    except sqlite3.OperationalError:
        pass

    # ---------------- Granular per-admin permissions (Artist/Manager/Marzieh) ----------------
    # Replaces the single admin_type slot above with a proper set: a secondary
    # admin can now hold any combination of permissions at once, toggled
    # independently from /admin's "Give access" / "Take access" buttons
    # (see grant_permission / revoke_permission further down).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_permissions (
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            PRIMARY KEY (user_id, permission)
        )
    """)

    # One-time carry-over: permissions used to be one exclusive role
    # (Artist/Manager/Marzieh); now each command is its own independent
    # permission (see config.ADMIN_PERMISSIONS). Expand whatever role
    # someone already had into every permission that role used to imply,
    # so nobody loses access on upgrade. Safe to run on every startup -
    # INSERT OR IGNORE makes it a no-op once migrated.
    cur.execute("SELECT user_id, admin_type FROM secondary_admins WHERE admin_type IS NOT NULL")
    for row in cur.fetchall():
        bundle = ADMIN_TYPE_BUNDLES.get(row["admin_type"], [row["admin_type"]])
        for permission in bundle:
            cur.execute(
                "INSERT OR IGNORE INTO admin_permissions (user_id, permission) VALUES (?, ?)",
                (row["user_id"], permission),
            )
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_filters (
            user_id INTEGER PRIMARY KEY,
            filter_type TEXT,
            filter_value TEXT
        )
    """)

    # /sort: any number of filters per player (character / series / rarity / event).
    # Values of the same type are OR-ed, different types are AND-ed - see
    # _card_passes_filters(). Replaces the old one-row-per-player user_filters.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_filter_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filter_type TEXT NOT NULL,
            filter_value TEXT NOT NULL,
            UNIQUE (user_id, filter_type, filter_value)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS locked_rarities (
            rarity_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS locked_events (
            event_name TEXT PRIMARY KEY
        )
    """)

    # Locks that only apply to the main group (the /setforcejoin group) -
    # set with "/lockspawn main ..." - on top of the global locks above.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS locked_rarities_main (
            rarity_id INTEGER PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS locked_events_main (
            event_name TEXT PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_name TEXT PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY
        )
    """)

    # The base price ranges were corrected: forget the old live prices once so /prices
    # starts from the new ranges (economy.ensure_prices_seeded re-creates the rows).
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'rarity_prices_reset_v2'")
    if not cur.fetchone():
        try:
            cur.execute("DELETE FROM rarity_market_prices")
        except sqlite3.OperationalError:
            pass  # table not created yet on a brand-new database - nothing to reset
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('rarity_prices_reset_v2')")
        conn.commit()

    # /sort filters used to live in user_filters (one per player) - copy them over once.
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'user_filter_items_v1'")
    if not cur.fetchone():
        cur.execute("""
            INSERT OR IGNORE INTO user_filter_items (user_id, filter_type, filter_value)
            SELECT user_id, filter_type, filter_value FROM user_filters
            WHERE filter_type IS NOT NULL AND filter_value IS NOT NULL AND filter_value <> ''
        """)
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('user_filter_items_v1')")
        conn.commit()

    # ---------------- /new channel publisher ----------------
    # Stores the last channel target so the owner only has to provide it
    # once. A channel target can be a public @username or a Telegram
    # channel numeric id parsed from a t.me/c/... link.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS new_channel_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            channel_target TEXT NOT NULL,
            channel_link TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS new_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_target TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            fa_text TEXT NOT NULL,
            en_text TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS new_post_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_data TEXT,
            max_uses INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES new_posts(id) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_new_post_buttons_post_id
        ON new_post_buttons(post_id)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS new_post_button_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            button_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            claimed_at TEXT NOT NULL,
            UNIQUE(button_id, user_id),
            FOREIGN KEY (button_id) REFERENCES new_post_buttons(id) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_new_post_button_claims_button_user
        ON new_post_button_claims(button_id, user_id)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bin_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            data TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_character_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            seller_username TEXT,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT,
            sold_at TEXT,
            buyer_id INTEGER,
            FOREIGN KEY (user_character_id) REFERENCES user_characters(id),
            FOREIGN KEY (character_id) REFERENCES characters(id)
        )
    """)

    # ---------------- Dynamic rarity economy (/prices) ----------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rarity_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rarity_name TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rarity_activity_log_name_time
        ON rarity_activity_log (rarity_name, created_at)
    """)

    # defensive migration: which user triggered a 'check' event, so
    # log_check_activity() can dedupe repeated /check spam from the same
    # user on the same rarity instead of letting it inflate the price.
    # NULL for 'gift'/'sold' rows and for check rows logged before this
    # column existed - both fine, they just aren't deduped retroactively.
    try:
        cur.execute("ALTER TABLE rarity_activity_log ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_rarity_activity_log_user_check
        ON rarity_activity_log (rarity_name, action, user_id, created_at)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rarity_market_prices (
            rarity_name TEXT PRIMARY KEY,
            current_min INTEGER NOT NULL,
            current_max INTEGER NOT NULL,
            last_updated_at TEXT
        )
    """)

    # ---------------- Player memories & favorite-character affinity ----------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_character_affinity (
            user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (user_id, character_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            memory_key TEXT NOT NULL,
            character_id INTEGER,
            occurred_at TEXT NOT NULL,
            UNIQUE(user_id, memory_key)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_lifetime_counters (
            user_id INTEGER NOT NULL,
            counter_key TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, counter_key)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_messages_user_character
        ON chat_messages (user_id, character_id, id)
    """)

    # Pin/block state for a chat conversation - long-press a row in the
    # Chat tab's list (see api_server.py's /api/chat/conversation/* and
    # chat_send's blocked check).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_conversation_state (
            user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, character_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_favorite_character (
            user_id INTEGER PRIMARY KEY,
            character_id INTEGER,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_users (
            user_id INTEGER PRIMARY KEY,
            granted_at TEXT,
            expires_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_theme (
            user_id INTEGER PRIMARY KEY,
            theme TEXT NOT NULL DEFAULT 'default',
            updated_at TEXT
        )
    """)

    # ---------------- Fighter / Arena (Update 2) ----------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS fighter_stats (
            character_id INTEGER PRIMARY KEY,
            element TEXT NOT NULL,
            base_attack INTEGER NOT NULL,
            base_defense INTEGER NOT NULL,
            FOREIGN KEY (character_id) REFERENCES characters(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS arena_teams (
            user_id INTEGER NOT NULL,
            team_type TEXT NOT NULL,
            slot INTEGER NOT NULL,
            user_character_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, team_type, slot)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS arena_trophies (
            user_id INTEGER PRIMARY KEY,
            trophies INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS arena_battles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attacker_id INTEGER NOT NULL,
            attacker_username TEXT,
            defender_id INTEGER NOT NULL,
            attack_power INTEGER NOT NULL,
            defense_power INTEGER NOT NULL,
            result TEXT NOT NULL,
            attacker_trophy_change INTEGER NOT NULL,
            defender_trophy_change INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            resolves_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            notified INTEGER NOT NULL DEFAULT 0
        )
    """)

    # One-time, non-destructive: register "Fighter" as a normal event so
    # it shows up in the existing rarity/event picker used by /addcharacter
    # and /send. Gated so an admin who later /removeevent's it stays removed
    # across restarts instead of it silently coming back.
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'fighter_event_seed_v1'")
    if not cur.fetchone():
        cur.execute("INSERT OR IGNORE INTO events (event_name) VALUES (?)", (FIGHTER_EVENT_NAME,))
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('fighter_event_seed_v1')")

    # ---------------- Invite / referral system ----------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            first_seen_at TEXT
        )
    """)

    # defensive migration: display-name fields, used so the leaderboard
    # (and anywhere else showing a player) can show a real name instead
    # of falling back to a bare numeric id.
    for col_def in ("username TEXT", "first_name TEXT", "last_name TEXT"):
        try:
            cur.execute(f"ALTER TABLE bot_users ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    # defensive migration: /birthday. month/day are set once (locked in
    # by the player confirming "yes, today's my birthday") and never
    # change; last_birthday_gift_year stops the same year's gift from
    # being handed out twice (once immediately on confirmation, then
    # again by the nightly loop the same day).
    for col_def in ("birthday_month INTEGER", "birthday_day INTEGER", "last_birthday_gift_year INTEGER"):
        try:
            cur.execute(f"ALTER TABLE bot_users ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    # defensive migration: started_at = when the player first ran /start. Invite
    # rewards key off THIS, not off the row merely existing: other code (the
    # interaction capture that runs before /start, Mini App login) creates the
    # bot_users row first, which made every invited friend look "not new" and
    # silently skipped the invite reward + count.
    try:
        cur.execute("ALTER TABLE bot_users ADD COLUMN started_at TEXT")
    except sqlite3.OperationalError:
        pass
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'bot_users_started_at_v1'")
    if not cur.fetchone():
        # Everyone who exists right now counts as already started.
        cur.execute("UPDATE bot_users SET started_at = COALESCE(first_seen_at, ?) WHERE started_at IS NULL",
                    (datetime.utcnow().isoformat(),))
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('bot_users_started_at_v1')")
        conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            inviter_id INTEGER PRIMARY KEY,
            invite_count INTEGER NOT NULL DEFAULT 0,
            bonus_claimed INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sell_prices (
            rarity_id INTEGER PRIMARY KEY,
            price INTEGER NOT NULL
        )
    """)

    # ---------------- Tasks & daily claim ----------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            character_id INTEGER,
            rarity_id INTEGER,
            reward_type TEXT NOT NULL,
            reward_currency INTEGER,
            reward_character_id INTEGER,
            display_text TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT,
            FOREIGN KEY (character_id) REFERENCES characters(id),
            FOREIGN KEY (rarity_id) REFERENCES rarities(id),
            FOREIGN KEY (reward_character_id) REFERENCES characters(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_task_progress (
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            baseline_value INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT,
            PRIMARY KEY (user_id, task_id),
            FOREIGN KEY (task_id) REFERENCES task_definitions(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_task_claim (
            user_id INTEGER PRIMARY KEY,
            last_claim_date TEXT
        )
    """)

    # One-time migration (only runs the moment the columns above are
    # first added): earn_currency / spend_currency tasks used to
    # baseline against raw `balance`, which unrelated income/spending
    # could cancel out. They now baseline against the lifetime
    # total_earned / total_spent counters instead (see
    # _track_currency_delta) - a different scale, so any baseline
    # already captured under the old metric would compute garbage
    # progress. Clearing unclaimed progress rows for those task types
    # makes them recapture a correct baseline the next time each
    # player opens the Tasks tab. Claimed tasks are untouched -
    # already paid out, nothing to fix. Gated on
    # currency_totals_just_added so this doesn't wipe everyone's
    # in-progress tasks on every ordinary restart.
    if currency_totals_just_added:
        cur.execute("""
            DELETE FROM user_task_progress
            WHERE claimed_at IS NULL
              AND task_id IN (
                  SELECT id FROM task_definitions
                  WHERE task_type IN ('earn_currency', 'spend_currency')
              )
        """)

    conn.commit()

    # One-time, non-destructive: seed the new events registry with every
    # distinct event name already used on existing characters, so nothing
    # that was there before gets lost and the registry starts consistent
    # with real data. Guarded by schema_migrations so it only runs once.
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'events_seed_v1'")
    if not cur.fetchone():
        cur.execute(
            "SELECT DISTINCT event_name FROM characters "
            "WHERE event_name IS NOT NULL AND event_name != ''"
        )
        for row in cur.fetchall():
            cur.execute("INSERT OR IGNORE INTO events (event_name) VALUES (?)", (row["event_name"],))
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('events_seed_v1')")
        conn.commit()

    # One-time, best-effort cleanup: bot_users used to record ANYONE seen
    # in an update (including a plain group message that wasn't even
    # directed at the bot). It now only records real interactions - see
    # _capture_user_info in bot.py - but that only affects new activity,
    # so this purges everyone already in bot_users who has no footprint
    # anywhere else that implies they ever actually used the bot (owned
    # a card, held currency, listed/bought on the Market, fought in the
    # Arena, claimed a task, etc.). Someone whose only interaction was a
    # side-effect-free command (like /help) with nothing else on record
    # is indistinguishable from a lurker and gets swept up too - there's
    # no way to tell those apart after the fact. Gated so it only ever
    # runs once.
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'prune_inactive_bot_users_v1'")
    if not cur.fetchone():
        cur.execute("""
            DELETE FROM bot_users
            WHERE user_id NOT IN (
                SELECT user_id FROM user_characters
                UNION SELECT user_id FROM currency
                UNION SELECT user_id FROM daily_captures
                UNION SELECT user_id FROM daily_darts
                UNION SELECT user_id FROM user_filters
                UNION SELECT user_id FROM arena_teams
                UNION SELECT user_id FROM arena_trophies
                UNION SELECT attacker_id FROM arena_battles
                UNION SELECT defender_id FROM arena_battles
                UNION SELECT seller_id FROM market_listings
                UNION SELECT buyer_id FROM market_listings WHERE buyer_id IS NOT NULL
                UNION SELECT inviter_id FROM referrals
                UNION SELECT user_id FROM user_task_progress
                UNION SELECT user_id FROM daily_task_claim
                UNION SELECT user_id FROM user_character_affinity
                UNION SELECT user_id FROM user_memories
                UNION SELECT user_id FROM user_lifetime_counters
                UNION SELECT user_id FROM user_favorite_character
                UNION SELECT user_id FROM secondary_admins
            )
        """)
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('prune_inactive_bot_users_v1')")
        conn.commit()

    # Drop the old single-slot /restore backup tables - fully replaced by
    # the bin_items table (30-day trash, see /bin) added below.
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = 'drop_legacy_backup_tables_v1'")
    if not cur.fetchone():
        for legacy_table in ("characters_backup", "user_characters_backup", "rarities_backup", "backup_meta"):
            cur.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        cur.execute("INSERT INTO schema_migrations (name) VALUES ('drop_legacy_backup_tables_v1')")
        conn.commit()

    # defensive migration: chat_last_sender.last_message_at (spam streak idle-reset window)
    cur.execute("PRAGMA table_info(chat_last_sender)")
    if "last_message_at" not in [c["name"] for c in cur.fetchall()]:
        cur.execute("ALTER TABLE chat_last_sender ADD COLUMN last_message_at TEXT")
        conn.commit()

    # ---------------- Force-join settings ----------------
    # The group where /send submissions are posted for review (besides the owner's DM).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS review_group_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS force_join_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            chat_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # ---------------- Personalized callback buttons ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS personalized_buttons (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
    """)
    conn.commit()

    # ---------------- Bans ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            banned_until TEXT,
            banned_at TEXT NOT NULL,
            banned_by INTEGER
        )
    """)
    conn.commit()

    # ---------------- Favorite card (/fav) ----------------
    # The card a player pinned with /fav [id]; /constellation always shows
    # this card's image instead of a random one. Separate from
    # user_favorite_character above, which is the AI-computed affinity
    # favorite used by the Chat tab / memories.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_fav_card (
            user_id INTEGER PRIMARY KEY,
            character_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()

    conn.close()


# ---------------- Personalized callback buttons ----------------

def set_personalized_button_owner(chat_id: int, message_id: int, owner_user_id: int):
    conn = get_connection()
    conn.execute(
        """INSERT INTO personalized_buttons (chat_id, message_id, owner_user_id, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, message_id) DO UPDATE SET
               owner_user_id=excluded.owner_user_id, created_at=excluded.created_at""",
        (chat_id, message_id, owner_user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def prune_personalized_buttons(max_age_days: int = 30):
    """Forget button owners of messages older than `max_age_days` - nobody
    presses buttons that old, and the table would otherwise grow forever."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    conn = get_connection()
    conn.execute("DELETE FROM personalized_buttons WHERE created_at < ?", (cutoff,))
    conn.commit()
    conn.close()


def get_personalized_button_owner(chat_id: int, message_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT owner_user_id FROM personalized_buttons WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    conn.close()
    return int(row["owner_user_id"]) if row else None


# ---------------- Force-join settings ----------------

def get_force_join_settings():
    conn = get_connection()
    row = conn.execute("SELECT * FROM force_join_settings WHERE id = 1").fetchone()
    conn.close()
    return row


def set_force_join_settings(chat_id: int, invite_link: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO force_join_settings (id, chat_id, invite_link, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id, invite_link = excluded.invite_link, updated_at = excluded.updated_at",
        (chat_id, invite_link, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------------- Submission review group ----------------

def get_review_group():
    """The group /send submissions are also posted to, or None."""
    conn = get_connection()
    row = conn.execute("SELECT chat_id, chat_title FROM review_group_settings WHERE id = 1").fetchone()
    conn.close()
    return row


def set_review_group(chat_id: int, chat_title: str = None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO review_group_settings (id, chat_id, chat_title, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id, chat_title = excluded.chat_title, "
        "updated_at = excluded.updated_at",
        (chat_id, chat_title, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def clear_review_group():
    conn = get_connection()
    conn.execute("DELETE FROM review_group_settings WHERE id = 1")
    conn.commit()
    conn.close()


# ---------------- Rarities ----------------

def add_rarity(name: str, weight: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rarities (name, weight) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET weight = excluded.weight",
        (name, weight),
    )
    conn.commit()
    conn.close()


def get_rarity_by_name(name: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rarities WHERE LOWER(name) = LOWER(?)", (name,))
    row = cur.fetchone()
    conn.close()
    return row


def get_rarity_by_id(rarity_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rarities WHERE id = ?", (rarity_id,))
    row = cur.fetchone()
    conn.close()
    return row


def list_rarities():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rarities ORDER BY weight DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_total_character_count() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM characters")
    total = cur.fetchone()["c"]
    conn.close()
    return total


def get_card_counts_by_rarity():
    """How many cards exist in the bot for each rarity, highest weight first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.name AS label, COUNT(c.id) AS count
        FROM rarities r
        LEFT JOIN characters c ON c.rarity_id = r.id
        GROUP BY r.id
        ORDER BY r.weight DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_card_counts_by_character():
    """How many cards exist in the bot for each character name, most first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT name AS label, COUNT(*) AS count
        FROM characters
        GROUP BY name
        ORDER BY count DESC, name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_card_counts_by_series():
    """How many cards exist in the bot for each series, most first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT series AS label, COUNT(*) AS count
        FROM characters
        GROUP BY series
        ORDER BY count DESC, series ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_card_counts_by_event():
    """How many cards exist in the bot for each event, most first. Cards
    with no event_name are grouped together under 'No Event' rather than
    dropped, so the totals here still add up to the full character count."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(event_name, 'No Event') AS label, COUNT(*) AS count
        FROM characters
        GROUP BY label
        ORDER BY (label = 'No Event') ASC, count DESC, label ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------- Characters ----------------

def _next_available_character_id(cur) -> int:
    """Smallest positive integer not currently used as a character ID -
    keeps IDs gap-free after deletions (e.g. deleting #3 out of 1-5 means
    the next added character becomes #3 again, not #6)."""
    cur.execute("SELECT id FROM characters ORDER BY id")
    existing_ids = {row["id"] for row in cur.fetchall()}
    candidate = 1
    while candidate in existing_ids:
        candidate += 1
    return candidate


def add_character(name: str, series: str, image_file_id: str, rarity_name: str = None,
                   added_by_user_id: int = None, added_by_username: str = None, event_name: str = None,
                   media_type: str = "photo"):
    rarity_id = None
    if rarity_name:
        rarity = get_rarity_by_name(rarity_name)
        if rarity:
            rarity_id = rarity["id"]

    # An event name only sticks if it's registered (see /addevent). An
    # unregistered name is dropped rather than rejecting the character.
    if event_name:
        canonical = get_event_by_name(event_name)
        event_name = canonical if canonical else None

    conn = get_connection()
    cur = conn.cursor()
    next_id = _next_available_character_id(cur)
    cur.execute(
        "INSERT INTO characters (id, name, series, rarity_id, image_file_id, added_by_user_id, added_by_username, event_name, media_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (next_id, name, series, rarity_id, image_file_id, added_by_user_id, added_by_username, event_name, media_type or "photo"),
    )
    conn.commit()
    char_id = next_id
    conn.close()
    return char_id


def get_character(character_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.*, rarities.name AS rarity_name, rarities.weight AS rarity_weight
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE characters.id = ?
    """, (character_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_archive_message_id(character_id: int, message_id: int):
    """Remembers the id of this character's latest post in the archive
    channel (see config.ARCHIVE_CHANNEL) - overwritten each time
    /editcharacter reposts an updated version, so it always points at
    whatever's currently the newest/most accurate post. /removecharacter
    uses this to delete that message when the character itself goes."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE characters SET archive_message_id = ? WHERE id = ?", (message_id, character_id))
    conn.commit()
    conn.close()


def pick_random_character(chat_id: int = None):
    """
    Two-stage weighted pick:
      1) Pick a rarity TIER using rarity weight (a tier's total odds don't
         change no matter how many characters are in it).
      2) Pick uniformly among the characters within that tier.
    Characters with no rarity assigned form their own "Unranked" tier,
    using DEFAULT_CHARACTER_WEIGHT as that tier's overall weight.
    Locked rarities and locked events are excluded entirely - the global locks
    everywhere, and the "main" locks too when `chat_id` is the main group.
    """
    is_main_group = chat_id is not None and chat_id == get_main_chat_id()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.id, characters.rarity_id, characters.event_name,
               COALESCE(rarities.weight, ?) AS tier_weight
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
    """, (DEFAULT_CHARACTER_WEIGHT,))
    rows = cur.fetchall()

    cur.execute("SELECT rarity_id FROM locked_rarities")
    locked_rarity_ids = {r["rarity_id"] for r in cur.fetchall()}

    cur.execute("SELECT event_name FROM locked_events")
    locked_events = {r["event_name"].lower() for r in cur.fetchall()}

    if is_main_group:
        cur.execute("SELECT rarity_id FROM locked_rarities_main")
        locked_rarity_ids |= {r["rarity_id"] for r in cur.fetchall()}
        cur.execute("SELECT event_name FROM locked_events_main")
        locked_events |= {r["event_name"].lower() for r in cur.fetchall()}

    conn.close()

    if not rows:
        return None

    tiers = {}
    for r in rows:
        if r["rarity_id"] is not None and r["rarity_id"] in locked_rarity_ids:
            continue
        if r["event_name"] and r["event_name"].lower() in locked_events:
            continue
        key = r["rarity_id"]  # None = unranked tier
        if key not in tiers:
            tiers[key] = {"weight": r["tier_weight"], "ids": []}
        tiers[key]["ids"].append(r["id"])

    if not tiers:
        return None

    tier_keys = list(tiers.keys())
    tier_weights = [tiers[k]["weight"] for k in tier_keys]
    chosen_tier = random.choices(tier_keys, weights=tier_weights, k=1)[0]

    chosen_id = random.choice(tiers[chosen_tier]["ids"])
    return get_character(chosen_id)


# ---------------- Spawn locks (rarities & events) ----------------

def get_main_chat_id():
    """Chat id of the main group (the one set with /setforcejoin), or None."""
    settings = get_force_join_settings()
    return settings["chat_id"] if settings else None


def lock_rarity(rarity_id: int, main: bool = False):
    table = "locked_rarities_main" if main else "locked_rarities"
    conn = get_connection()
    conn.execute(f"INSERT OR IGNORE INTO {table} (rarity_id) VALUES (?)", (rarity_id,))
    conn.commit()
    conn.close()


def unlock_rarity(rarity_id: int, main: bool = False):
    table = "locked_rarities_main" if main else "locked_rarities"
    conn = get_connection()
    conn.execute(f"DELETE FROM {table} WHERE rarity_id = ?", (rarity_id,))
    conn.commit()
    conn.close()


def lock_event(event_name: str, main: bool = False):
    table = "locked_events_main" if main else "locked_events"
    conn = get_connection()
    conn.execute(f"INSERT OR IGNORE INTO {table} (event_name) VALUES (?)", (event_name,))
    conn.commit()
    conn.close()


def unlock_event(event_name: str, main: bool = False):
    table = "locked_events_main" if main else "locked_events"
    conn = get_connection()
    conn.execute(f"DELETE FROM {table} WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    conn.commit()
    conn.close()


def is_rarity_locked(rarity_id: int, main: bool = False) -> bool:
    table = "locked_rarities_main" if main else "locked_rarities"
    conn = get_connection()
    row = conn.execute(f"SELECT 1 FROM {table} WHERE rarity_id = ?", (rarity_id,)).fetchone()
    conn.close()
    return row is not None


def is_event_locked(event_name: str, main: bool = False) -> bool:
    table = "locked_events_main" if main else "locked_events"
    conn = get_connection()
    row = conn.execute(f"SELECT 1 FROM {table} WHERE LOWER(event_name) = LOWER(?)", (event_name,)).fetchone()
    conn.close()
    return row is not None


# ---------------- Events registry ----------------
# Only names registered here are valid event names. Characters submitted
# or added with an event name that isn't registered simply get no event
# (see add_character / update_character below), instead of failing.

def add_event(event_name: str) -> bool:
    """Registers a new event name. Returns False if it already exists."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    if cur.fetchone():
        conn.close()
        return False
    cur.execute("INSERT INTO events (event_name) VALUES (?)", (event_name,))
    conn.commit()
    conn.close()
    return True


def remove_event(event_name: str) -> bool:
    """Unregisters an event (also unlocks it, if it was locked). The
    event name is moved into the bin so it can be restored within 30 days."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT event_name FROM events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    _add_to_bin(cur, "event", row["event_name"], {"event_name": row["event_name"]})

    cur.execute("DELETE FROM events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    cur.execute("DELETE FROM locked_events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    cur.execute("DELETE FROM locked_events_main WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    conn.commit()
    conn.close()
    return True


def event_exists(event_name: str) -> bool:
    if not event_name:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_event_by_name(event_name: str):
    """Returns the canonically-cased event name as registered, or None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT event_name FROM events WHERE LOWER(event_name) = LOWER(?)", (event_name,))
    row = cur.fetchone()
    conn.close()
    return row["event_name"] if row else None


def get_all_events():
    """All registered events with their locked status, for /spawnstatus.
    `locked` = locked everywhere; `locked_main` = locked in the main group only."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT event_name FROM events ORDER BY event_name COLLATE NOCASE")
    names = [r["event_name"] for r in cur.fetchall()]
    cur.execute("SELECT event_name FROM locked_events")
    locked = {r["event_name"].lower() for r in cur.fetchall()}
    cur.execute("SELECT event_name FROM locked_events_main")
    locked_main = {r["event_name"].lower() for r in cur.fetchall()}
    conn.close()
    return [
        {"name": n, "locked": n.lower() in locked, "locked_main": n.lower() in locked_main}
        for n in names
    ]


def count_owners(character_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM user_characters WHERE character_id = ?", (character_id,))
    row = cur.fetchone()
    conn.close()
    return row["c"]


def get_random_owner_names(character_id: int, limit: int = 5):
    """Up to `limit` distinct owners of this character, picked at random.
    Shows their real @username when we know one (from bot_users, kept
    fresh on every interaction), falling back to get_display_name only
    for owners with no username set - puts a few real names behind
    /check's 'Claimed by N Keepers' count."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM user_characters WHERE character_id = ?", (character_id,))
    owner_ids = [row["user_id"] for row in cur.fetchall()]
    if not owner_ids:
        conn.close()
        return []

    sample = random.sample(owner_ids, min(limit, len(owner_ids)))
    names = []
    for uid in sample:
        cur.execute("SELECT username FROM bot_users WHERE user_id = ?", (uid,))
        row = cur.fetchone()
        if row and row["username"]:
            names.append(f"@{row['username']}")
        else:
            names.append(get_display_name(uid))
    conn.close()
    return names


def get_random_owner_ids(character_id: int, limit: int = 5):
    """Up to `limit` distinct owners (user ids) of this character, picked at random."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM user_characters WHERE character_id = ?", (character_id,))
    owner_ids = [row["user_id"] for row in cur.fetchall()]
    conn.close()
    return random.sample(owner_ids, min(limit, len(owner_ids)))


def count_user_copies(user_id: int, character_id: int) -> int:
    """How many copies of this exact card the player owns right now."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM user_characters WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    row = cur.fetchone()
    conn.close()
    return row["c"]


def _init_fighter_fields_if_applicable(cur, user_character_id: int, character_id: int):
    """
    If character_id is a registered Fighter, stamps the freshly-inserted
    user_characters row with level 1 and its base attack/defense so it's
    battle-ready immediately. No-op for ordinary (non-Fighter) characters.
    """
    cur.execute("SELECT base_attack, base_defense FROM fighter_stats WHERE character_id = ?", (character_id,))
    stats = cur.fetchone()
    if stats:
        cur.execute(
            "UPDATE user_characters SET level = 1, current_attack = ?, current_defense = ? WHERE id = ?",
            (stats["base_attack"], stats["base_defense"], user_character_id),
        )


def _remove_from_arena_teams(cur, user_character_id: int):
    """Un-slots a specific owned copy from any Attack/Defense team it's
    on - called whenever that copy stops belonging to its current owner
    (sold, gifted, sold on market) so a team never points at a card the
    player no longer has."""
    cur.execute("DELETE FROM arena_teams WHERE user_character_id = ?", (user_character_id,))


def give_character_to_user(user_id: int, username: str, character_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_characters (user_id, username, character_id, obtained_at) VALUES (?, ?, ?, ?)",
        (user_id, username, character_id, datetime.utcnow().isoformat()),
    )
    _init_fighter_fields_if_applicable(cur, cur.lastrowid, character_id)
    conn.commit()
    conn.close()


def get_user_inventory(user_id: int, apply_filter: bool = True):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT characters.id, characters.name, characters.series, characters.image_file_id,
               characters.media_type, characters.event_name,
               characters.added_by_user_id, characters.added_by_username,
               rarities.name AS rarity_name, user_characters.obtained_at
        FROM user_characters
        JOIN characters ON user_characters.character_id = characters.id
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE user_characters.user_id = ?
        ORDER BY user_characters.obtained_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()

    if apply_filter:
        grouped = {}
        for f in get_user_filters(user_id):
            grouped.setdefault(f["filter_type"], []).append(f["filter_value"])
        if grouped:
            rows = [row for row in rows if _card_passes_filters(row, grouped)]
    return rows


# ---------------- Chat tab ----------------
# Powers the Mini App's Chat tab: a player picks one of THEIR OWN
# characters (deduplicated - owning 3 copies of the same character
# still only shows up once) and exchanges messages with it. Messages
# are stored per (user_id, character_id) pair, independent of which
# specific copy (user_characters.id) they own or how many.

def list_chat_characters(user_id: int):
    """Every character this player owns, deduplicated by character,
    each annotated with its most recent chat message (if any) so the
    picker list can show a preview/sort by recency."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.id, characters.name, characters.series, characters.image_file_id,
               characters.media_type, rarities.name AS rarity_name,
               MIN(user_characters.obtained_at) AS obtained_at
        FROM user_characters
        JOIN characters ON user_characters.character_id = characters.id
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE user_characters.user_id = ?
        GROUP BY characters.id
    """, (user_id,))
    characters = cur.fetchall()

    cur.execute("""
        SELECT character_id, content, sender, created_at
        FROM chat_messages
        WHERE user_id = ? AND id IN (
            SELECT MAX(id) FROM chat_messages WHERE user_id = ? GROUP BY character_id
        )
    """, (user_id, user_id))
    last_message_by_character = {row["character_id"]: row for row in cur.fetchall()}

    cur.execute("""
        SELECT character_id, pinned, blocked FROM chat_conversation_state WHERE user_id = ?
    """, (user_id,))
    state_by_character = {row["character_id"]: row for row in cur.fetchall()}
    conn.close()

    results = []
    for row in characters:
        last = last_message_by_character.get(row["id"])
        state = state_by_character.get(row["id"])
        results.append({
            "id": row["id"],
            "name": row["name"],
            "series": row["series"],
            "image_file_id": row["image_file_id"],
            "media_type": row["media_type"],
            "rarity_name": row["rarity_name"],
            "last_message": last["content"] if last else None,
            "last_message_sender": last["sender"] if last else None,
            "last_message_at": last["created_at"] if last else row["obtained_at"],
            "pinned": bool(state["pinned"]) if state else False,
            "blocked": bool(state["blocked"]) if state else False,
        })
    # Pinned conversations first, then most-recently-active; characters
    # never chatted with yet fall back to when they were obtained.
    results.sort(key=lambda c: c["last_message_at"] or "", reverse=True)
    results.sort(key=lambda c: c["pinned"], reverse=True)
    return results


def set_chat_pinned(user_id: int, character_id: int, pinned: bool) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chat_conversation_state (user_id, character_id, pinned, blocked)
        VALUES (?, ?, ?, 0)
        ON CONFLICT (user_id, character_id) DO UPDATE SET pinned = excluded.pinned
    """, (user_id, character_id, 1 if pinned else 0))
    conn.commit()
    conn.close()


def set_chat_blocked(user_id: int, character_id: int, blocked: bool) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chat_conversation_state (user_id, character_id, pinned, blocked)
        VALUES (?, ?, 0, ?)
        ON CONFLICT (user_id, character_id) DO UPDATE SET blocked = excluded.blocked
    """, (user_id, character_id, 1 if blocked else 0))
    conn.commit()
    conn.close()


def is_chat_blocked(user_id: int, character_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT blocked FROM chat_conversation_state WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row["blocked"]) if row else False


def delete_chat_conversation(user_id: int, character_id: int) -> None:
    """Wipes message history AND pin/block state for this pairing -
    a fresh start, not just a hidden/archived conversation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_messages WHERE user_id = ? AND character_id = ?", (user_id, character_id))
    cur.execute(
        "DELETE FROM chat_conversation_state WHERE user_id = ? AND character_id = ?", (user_id, character_id)
    )
    conn.commit()
    conn.close()


# user_owns_character() already exists above (see Gifting section) - reused
# as-is for the Chat tab's ownership check, no need to redefine it here.


def get_chat_messages(user_id: int, character_id: int, limit: int = 100):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, sender, content, created_at
        FROM chat_messages
        WHERE user_id = ? AND character_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, character_id, limit))
    rows = cur.fetchall()
    conn.close()
    return list(reversed(rows))  # oldest first, for a natural chat scroll


def add_chat_message(user_id: int, character_id: int, sender: str, content: str):
    """sender is 'user' or 'character'."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO chat_messages (user_id, character_id, sender, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, character_id, sender, content, now),
    )
    conn.commit()
    message_id = cur.lastrowid
    conn.close()
    return {"id": message_id, "sender": sender, "content": content, "created_at": now}


# ---------------- Sort / filter preference ----------------

FILTER_TYPES = ("character", "series", "rarity", "event")
_FILTER_ORDER_SQL = (
    "CASE filter_type WHEN 'character' THEN 0 WHEN 'series' THEN 1 WHEN 'rarity' THEN 2 ELSE 3 END, id"
)


def _word_set(text) -> set:
    return set(re.findall(r"\w+", _normalize_search_text(text)))


def text_matches_words(query: str, full_text: str) -> bool:
    """True if every word of `query` is a word of `full_text` (case-insensitive, styled
    Unicode folded): "Yae" and "Yae Miko" both match the character "Yae Miko", and
    "Miko" matches it too. A query with no letters/digits never matches."""
    words = _word_set(query)
    return bool(words) and words <= _word_set(full_text)


def _card_passes_filters(row, grouped: dict) -> bool:
    """`grouped` maps filter type -> [values]. A card passes when, for EVERY type
    present, it matches AT LEAST ONE of that type's values."""
    for ftype, values in grouped.items():
        if ftype == "character":
            ok = any(text_matches_words(v, row["name"]) for v in values)
        elif ftype == "series":
            ok = any(text_matches_words(v, row["series"]) for v in values)
        elif ftype == "rarity":
            card_rarity = _normalize_search_text(row["rarity_name"])
            ok = bool(card_rarity) and any(_normalize_search_text(v) == card_rarity for v in values)
        elif ftype == "event":
            card_event = _normalize_search_text(row["event_name"])
            ok = bool(card_event) and any(_normalize_search_text(v) == card_event for v in values)
        else:
            ok = True  # unknown type: ignore rather than hide everything
        if not ok:
            return False
    return True


def get_user_filters(user_id: int):
    """Every active /sort filter of this player: rows with id, filter_type, filter_value."""
    conn = get_connection()
    rows = conn.execute(
        f"SELECT id, filter_type, filter_value FROM user_filter_items WHERE user_id = ? ORDER BY {_FILTER_ORDER_SQL}",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def _find_filter_id(cur, user_id: int, filter_type: str, filter_value: str):
    wanted = _normalize_search_text(filter_value)
    cur.execute(
        "SELECT id, filter_value FROM user_filter_items WHERE user_id = ? AND filter_type = ?",
        (user_id, filter_type),
    )
    for row in cur.fetchall():
        if _normalize_search_text(row["filter_value"]) == wanted:
            return row["id"]
    return None


def add_user_filter(user_id: int, filter_type: str, filter_value: str) -> bool:
    """Adds one filter. Returns False if the player already has that exact filter."""
    filter_value = filter_value.strip()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if _find_filter_id(cur, user_id, filter_type, filter_value) is not None:
            return False
        cur.execute(
            "INSERT INTO user_filter_items (user_id, filter_type, filter_value) VALUES (?, ?, ?)",
            (user_id, filter_type, filter_value),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def toggle_user_filter(user_id: int, filter_type: str, filter_value: str) -> bool:
    """Adds the filter if the player doesn't have it, removes it if they do.
    Returns True when it is active afterwards."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        existing_id = _find_filter_id(cur, user_id, filter_type, filter_value)
        if existing_id is not None:
            cur.execute("DELETE FROM user_filter_items WHERE id = ?", (existing_id,))
            conn.commit()
            return False
        cur.execute(
            "INSERT INTO user_filter_items (user_id, filter_type, filter_value) VALUES (?, ?, ?)",
            (user_id, filter_type, filter_value.strip()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_user_filter(user_id: int, filter_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM user_filter_items WHERE id = ? AND user_id = ?", (filter_id, user_id))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


def clear_user_filter(user_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM user_filter_items WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# --- single-filter API kept for the Mini App (/api/profile/filter) ---

def get_user_filter(user_id: int):
    """The player's first filter (or None). Only the Mini App's single-filter picker uses this."""
    rows = get_user_filters(user_id)
    return rows[0] if rows else None


def set_user_filter(user_id: int, filter_type: str, filter_value: str):
    """Replaces ALL of the player's filters with this one (the Mini App's single-filter picker)."""
    clear_user_filter(user_id)
    add_user_filter(user_id, filter_type, filter_value)


def get_distinct_character_names():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT name FROM characters ORDER BY name")
    rows = [r["name"] for r in cur.fetchall()]
    conn.close()
    return rows


def get_distinct_series():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT series FROM characters ORDER BY series")
    rows = [r["series"] for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------- Search ----------------

def _normalize_search_text(text) -> str:
    """Lowercased, NFKC-normalized form of a string, used for search
    matching. NFKC folds stylized Unicode letters (e.g. the bold-font
    event names like '🛡𝗙𝗶𝗴𝗵𝘁𝗲𝗿🛡') back to plain ASCII, so a search for
    plain "fighter" still matches the styled name. Emoji are untouched
    by NFKC, so this also naturally handles matching a rarity/event by
    its leading emoji (it's just a substring of the stored name)."""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).lower()


def search_characters(query: str):
    """
    Matches characters by name (full or partial/single word), series,
    rarity (full name or its emoji), or event (full name - styled or
    plain - or its emoji).

    Multiple filters can be combined with '|', in any order, and all
    must match (AND) - e.g. "Ada | 👑 | 🛡" and "🛡 | Ada | 👑" both mean
    "name contains Ada AND rarity is 👑Sovereign AND event is 🛡Fighter🛡".
    A query with no '|' is a single filter that matches any field.
    """
    parts = [p.strip() for p in query.split("|") if p.strip()]
    if not parts:
        return []

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.*, rarities.name AS rarity_name, rarities.weight AS rarity_weight
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
    """)
    rows = cur.fetchall()
    conn.close()

    results = []
    for row in rows:
        searchable_fields = [
            _normalize_search_text(row["name"]),
            _normalize_search_text(row["series"]),
            _normalize_search_text(row["rarity_name"]),
            _normalize_search_text(row["event_name"]),
        ]
        if all(
            any(_normalize_search_text(part) in field for field in searchable_fields if field)
            for part in parts
        ):
            results.append(row)

    results.sort(key=lambda r: r["name"])
    return results


def get_all_characters():
    """Return every character in the database, ordered by name.
    Includes all fields needed by build_card_caption."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.id, characters.name, characters.series,
               characters.image_file_id, characters.media_type,
               characters.event_name, characters.added_by_user_id,
               characters.added_by_username,
               rarities.name AS rarity_name
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        ORDER BY characters.name
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------- Rarity stats ----------------

def count_characters_by_rarity(rarity_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM characters WHERE rarity_id = ?", (rarity_id,))
    row = cur.fetchone()
    conn.close()
    return row["c"]


def count_user_owned_by_rarity(user_id: int, rarity_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT characters.id) AS c
        FROM user_characters
        JOIN characters ON user_characters.character_id = characters.id
        WHERE user_characters.user_id = ? AND characters.rarity_id = ?
    """, (user_id, rarity_id))
    row = cur.fetchone()
    conn.close()
    return row["c"]


# ---------------- Dynamic rarity economy (/prices) ----------------

def log_rarity_activity(rarity_name: str, action: str):
    """Records one economic-activity event (a /check view - 'check', a
    player /gift - 'gift', or a Market sale - 'sold') for a rarity's raw
    name exactly as stored in the rarities table. The hourly job in
    economy.py reads these back to decide whether that tier's live price
    should rise or fall."""
    if not rarity_name:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rarity_activity_log (rarity_name, action, created_at) VALUES (?, ?, ?)",
        (rarity_name, action, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def log_check_activity(rarity_name: str, user_id: int):
    """Same economic signal as log_rarity_activity(rarity_name, "check"),
    but only counts once per user per rarity within
    config.PRICE_CHECK_DEDUP_WINDOW_SECONDS. Without this, a player could
    spam /check on their own rarity of choice to push its live price up
    for free - each repeat call inside the window is now a no-op."""
    if not rarity_name:
        return
    conn = get_connection()
    cur = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(seconds=PRICE_CHECK_DEDUP_WINDOW_SECONDS)).isoformat()
    cur.execute("""
        SELECT 1 FROM rarity_activity_log
        WHERE rarity_name = ? AND action = 'check' AND user_id = ? AND created_at >= ?
        LIMIT 1
    """, (rarity_name, user_id, cutoff))
    if cur.fetchone():
        conn.close()
        return
    cur.execute(
        "INSERT INTO rarity_activity_log (rarity_name, action, user_id, created_at) VALUES (?, ?, ?, ?)",
        (rarity_name, "check", user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def _log_rarity_activity_for_character(cur, character_id: int, action: str, now: str):
    """Same as log_rarity_activity, but reuses an existing cursor so the
    log entry commits atomically with whatever operation triggered it
    (used inside gift_character and buy_listing)."""
    cur.execute("""
        SELECT rarities.name AS rarity_name FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE characters.id = ?
    """, (character_id,))
    row = cur.fetchone()
    if row and row["rarity_name"]:
        cur.execute(
            "INSERT INTO rarity_activity_log (rarity_name, action, created_at) VALUES (?, ?, ?)",
            (row["rarity_name"], action, now),
        )


def get_rarity_activity_counts(rarity_name: str, since_iso: str) -> dict:
    """How many 'gift', 'check', and 'sold' events a rarity's raw name has
    logged since a given timestamp."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT action, COUNT(*) AS c FROM rarity_activity_log
        WHERE rarity_name = ? AND created_at >= ?
        GROUP BY action
    """, (rarity_name, since_iso))
    counts = {"gift": 0, "check": 0, "sold": 0}
    for row in cur.fetchall():
        counts[row["action"]] = row["c"]
    conn.close()
    return counts


def prune_old_rarity_activity_log(before_iso: str):
    """Storage housekeeping - activity older than every rolling window the
    economy job looks at is never read again, so it's safe to drop."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM rarity_activity_log WHERE created_at < ?", (before_iso,))
    conn.commit()
    conn.close()


def get_rarity_market_prices() -> dict:
    """Every canonical rarity tier's current live price range and when it
    was last recalculated, keyed by tier name."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT rarity_name, current_min, current_max, last_updated_at FROM rarity_market_prices")
    rows = cur.fetchall()
    conn.close()
    return {row["rarity_name"]: dict(row) for row in rows}


def set_rarity_market_price(rarity_name: str, current_min: int, current_max: int, updated_at: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO rarity_market_prices (rarity_name, current_min, current_max, last_updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(rarity_name) DO UPDATE SET
            current_min = excluded.current_min,
            current_max = excluded.current_max,
            last_updated_at = excluded.last_updated_at
    """, (rarity_name, current_min, current_max, updated_at))
    conn.commit()
    conn.close()


# ---------------- Player memories & favorite-character affinity ----------------

def bump_affinity(user_id: int, character_id: int, amount: int):
    """Nudges how strongly a player seems to gravitate toward a specific
    character - see memories.py for what earns points and by how much.
    get_top_affinity_character() is what the game treats as their
    'favorite'."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO user_character_affinity (user_id, character_id, score, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, character_id) DO UPDATE SET
            score = score + excluded.score,
            updated_at = excluded.updated_at
    """, (user_id, character_id, amount, now))
    conn.commit()
    conn.close()


def get_top_affinity_character(user_id: int):
    """The character a player has shown the most affinity for (checked,
    claimed, received, or bought the most), or None if they have no
    affinity history yet."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT uca.character_id, uca.score, c.name, c.series, c.image_file_id, c.media_type,
               rarities.name AS rarity_name
        FROM user_character_affinity uca
        JOIN characters c ON c.id = uca.character_id
        LEFT JOIN rarities ON c.rarity_id = rarities.id
        WHERE uca.user_id = ?
        ORDER BY uca.score DESC, uca.updated_at DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def record_memory_if_new(user_id: int, memory_key: str, character_id: int = None) -> bool:
    """Records a one-time milestone the first time it happens for this
    player. Returns True if it just fired for the first time, False if
    this player already has it (so the caller only reacts once)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO user_memories (user_id, memory_key, character_id, occurred_at) VALUES (?, ?, ?, ?)",
            (user_id, memory_key, character_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        fired = True
    except sqlite3.IntegrityError:
        fired = False
    conn.close()
    return fired


def get_memory(user_id: int, memory_key: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM user_memories WHERE user_id = ? AND memory_key = ?", (user_id, memory_key)
    )
    row = cur.fetchone()
    conn.close()
    return row


def increment_lifetime_counter(user_id: int, counter_key: str, amount: int = 1) -> int:
    """A counter that only ever goes up, even if the cards behind it are
    later sold or gifted away - milestones are about what a player has
    achieved, not what they currently hold. Returns the new total."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_lifetime_counters (user_id, counter_key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, counter_key) DO UPDATE SET value = value + excluded.value
    """, (user_id, counter_key, amount))
    conn.commit()
    cur.execute(
        "SELECT value FROM user_lifetime_counters WHERE user_id = ? AND counter_key = ?",
        (user_id, counter_key),
    )
    value = cur.fetchone()["value"]
    conn.close()
    return value


def get_lifetime_counter(user_id: int, counter_key: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM user_lifetime_counters WHERE user_id = ? AND counter_key = ?",
        (user_id, counter_key),
    )
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else 0


def list_users_with_affinity():
    """Every user_id that has at least one affinity record - the pool the
    nightly favorite-recompute job iterates over."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM user_character_affinity")
    rows = cur.fetchall()
    conn.close()
    return [row["user_id"] for row in rows]


def set_cached_favorite_character(user_id: int, character_id: int, updated_at: str):
    """Stores the 'official' favorite character the nightly job computed -
    see get_cached_favorite_character. Recomputed nightly rather than on
    every single interaction so a player's favorite feels stable (like a
    person's current best friend) instead of flipping mid-conversation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_favorite_character (user_id, character_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            character_id = excluded.character_id,
            updated_at = excluded.updated_at
    """, (user_id, character_id, updated_at))
    conn.commit()
    conn.close()


def get_cached_favorite_character(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id AS character_id, c.name, c.series, c.image_file_id, c.media_type,
               rarities.name AS rarity_name, ufc.updated_at
        FROM user_favorite_character ufc
        JOIN characters c ON c.id = ufc.character_id
        LEFT JOIN rarities ON c.rarity_id = rarities.id
        WHERE ufc.user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_random_character_in_rarity_ids(rarity_ids: list):
    """A random character among a set of rarity ids - used for the
    monthly gift, which picks uniformly among every character across
    several rarity tiers rather than one tier at a time."""
    if not rarity_ids:
        return None
    conn = get_connection()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(rarity_ids))
    cur.execute(f"SELECT id FROM characters WHERE rarity_id IN ({placeholders})", rarity_ids)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    chosen_id = random.choice(rows)["id"]
    return get_character(chosen_id)


def list_user_memories(user_id: int, limit: int = 30):
    """A player's milestones, most recent first - powers /memories."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT memory_key, character_id, occurred_at FROM user_memories
        WHERE user_id = ?
        ORDER BY occurred_at DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------- Premium ----------------

def set_premium(user_id: int, days: int = None):
    """Grants premium. days=None means it never expires until explicitly
    removed; otherwise it lapses on its own after that many days."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow()
    expires_at = (now + timedelta(days=days)).isoformat() if days else None
    cur.execute("""
        INSERT INTO premium_users (user_id, granted_at, expires_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET granted_at = excluded.granted_at, expires_at = excluded.expires_at
    """, (user_id, now.isoformat(), expires_at))
    conn.commit()
    conn.close()


def remove_premium(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_premium_status(user_id: int):
    """The premium_users row if this player currently has active premium,
    else None. An expired row is deleted the moment it's noticed here,
    so it stops counting from that point on without needing a separate
    cleanup job."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM premium_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    if row["expires_at"] and datetime.utcnow() > datetime.fromisoformat(row["expires_at"]):
        cur.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return None
    conn.close()
    return row


def is_premium(user_id: int) -> bool:
    return get_premium_status(user_id) is not None


def get_random_character_in_rarity_excluding(rarity_id: int, exclude_character_id: int):
    """A random OTHER character of the same rarity - used by /trade so a
    trade never just hands back the exact card you gave up."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM characters WHERE rarity_id = ? AND id != ?", (rarity_id, exclude_character_id)
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    chosen_id = random.choice(rows)["id"]
    return get_character(chosen_id)


# ---------------- Mini App theme ----------------

def get_user_theme(user_id: int) -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT theme FROM user_theme WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["theme"] if row else "default"


def set_user_theme(user_id: int, theme: str):
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO user_theme (user_id, theme, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET theme = excluded.theme, updated_at = excluded.updated_at
    """, (user_id, theme, now))
    conn.commit()
    conn.close()


def get_username_for_user(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT username FROM user_characters WHERE user_id = ? ORDER BY obtained_at DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row["username"] if row else None


# ---------------- Chat spawn state ----------------

def _get_chat_state(cur, chat_id):
    cur.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO chat_state (chat_id) VALUES (?)", (chat_id,))
        cur.execute("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
    return row


def upsert_chat_title(chat_id: int, title: str):
    """Keeps a group's display title fresh for /stats, the same way
    upsert_user_profile keeps a player's display name fresh. Called on
    every incoming group/supergroup update - see _capture_user_info."""
    if not title:
        return
    conn = get_connection()
    cur = conn.cursor()
    _get_chat_state(cur, chat_id)
    cur.execute("UPDATE chat_state SET title = ? WHERE chat_id = ?", (title, chat_id))
    conn.commit()
    conn.close()


def register_message_for_spawn(chat_id: int, user_id: int):
    """
    Call for every non-spam-muted message. Returns True if this message
    pushed the chat over the spawn threshold (message_count and distinct
    senders requirement met), False otherwise.
    """
    conn = get_connection()
    cur = conn.cursor()
    state = _get_chat_state(cur, chat_id)

    senders = set(state["distinct_senders"].split(",")) if state["distinct_senders"] else set()
    senders.add(str(user_id))

    new_count = state["message_count"] + 1

    cur.execute(
        "UPDATE chat_state SET message_count = ?, distinct_senders = ? WHERE chat_id = ?",
        (new_count, ",".join(senders), chat_id),
    )
    conn.commit()
    conn.close()
    # Threshold check (against config values) happens in bot.py;
    # here we just return the raw counters.
    return new_count, len(senders)


def reset_spawn_counter(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat_state SET message_count = 0, distinct_senders = '' WHERE chat_id = ?",
        (chat_id,),
    )
    conn.commit()
    conn.close()


def set_pending_spawn(chat_id: int, character_id: int, message_id: int):
    conn = get_connection()
    cur = conn.cursor()
    _get_chat_state(cur, chat_id)
    cur.execute(
        "UPDATE chat_state SET pending_character_id = ?, pending_spawn_message_id = ? WHERE chat_id = ?",
        (character_id, message_id, chat_id),
    )
    conn.commit()
    conn.close()


def clear_pending_spawn(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat_state SET pending_character_id = NULL, pending_spawn_message_id = NULL WHERE chat_id = ?",
        (chat_id,),
    )
    conn.commit()
    conn.close()


def get_pending_spawn(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    row = _get_chat_state(cur, chat_id)
    conn.commit()
    conn.close()
    return row["pending_character_id"]


def claim_pending_spawn(chat_id: int, character_id: int, user_id: int, username: str) -> bool:
    """
    Atomically claims a spawn: gives the character to user_id AND clears
    the chat's pending spawn in a single transaction, but only if
    character_id still matches what's actually pending for that chat.

    This replaces the old get_pending_spawn -> give_character_to_user ->
    clear_pending_spawn sequence used by /get, which was three separate
    connections/commits - if two /get requests for the same spawn landed
    close enough together, both could pass the "is there a pending spawn"
    check before either cleared it, handing the same card out twice.

    Here, the UPDATE's WHERE clause re-checks pending_character_id in the
    same statement that clears it: SQLite serializes writers, so only one
    of two racing callers can ever see rowcount == 1 for the same spawn.
    The other gets rowcount == 0 back (returned as False) and knows
    someone beat them to it - nothing else in this function runs for them.

    Returns True if this call won the claim, False if the spawn was
    already gone (claimed by someone else, or changed/cleared) by the
    time this ran.
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "UPDATE chat_state SET pending_character_id = NULL, pending_spawn_message_id = NULL "
            "WHERE chat_id = ? AND pending_character_id = ?",
            (chat_id, character_id),
        )
        if cur.rowcount == 0:
            cur.execute("ROLLBACK")
            return False

        cur.execute(
            "INSERT INTO user_characters (user_id, username, character_id, obtained_at) VALUES (?, ?, ?, ?)",
            (user_id, username, character_id, datetime.utcnow().isoformat()),
        )
        _init_fighter_fields_if_applicable(cur, cur.lastrowid, character_id)
        cur.execute("COMMIT")
        return True
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------------- Anti-spam ----------------

def register_message_for_spam(
    chat_id: int, user_id: int, max_consecutive: int, punishment_minutes: int,
    streak_reset_minutes: int = None, exempt_from_mute: bool = False,
):
    """
    Tracks consecutive messages from the same user in a chat. If the same
    user is still the last sender but streak_reset_minutes (or more) have
    passed since their last message, the streak starts over from 1 instead
    of continuing to climb.

    exempt_from_mute=True skips actually muting the user once they cross
    the threshold - the streak still resets to 0 so it can trip again
    later, but no entry is written to spam_tracker.

    Returns (just_muted, streak, would_have_muted).
    """
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow()

    cur.execute("SELECT * FROM chat_last_sender WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()

    idle_too_long = False
    if row is not None and row["last_message_at"]:
        gap_minutes = (now - datetime.fromisoformat(row["last_message_at"])).total_seconds() / 60
        idle_too_long = streak_reset_minutes is not None and gap_minutes >= streak_reset_minutes

    if row is None:
        cur.execute(
            "INSERT INTO chat_last_sender (chat_id, last_user_id, streak, last_message_at) VALUES (?, ?, 1, ?)",
            (chat_id, user_id, now.isoformat()),
        )
        streak = 1
    elif row["last_user_id"] == user_id and not idle_too_long:
        streak = row["streak"] + 1
        cur.execute(
            "UPDATE chat_last_sender SET streak = ?, last_message_at = ? WHERE chat_id = ?",
            (streak, now.isoformat(), chat_id),
        )
    else:
        streak = 1
        cur.execute(
            "UPDATE chat_last_sender SET last_user_id = ?, streak = 1, last_message_at = ? WHERE chat_id = ?",
            (user_id, now.isoformat(), chat_id),
        )

    just_muted = False
    would_have_muted = False
    if streak > max_consecutive:
        would_have_muted = True
        if exempt_from_mute:
            cur.execute("UPDATE chat_last_sender SET streak = 0 WHERE chat_id = ?", (chat_id,))
        else:
            muted_until = (now + timedelta(minutes=punishment_minutes)).isoformat()
            cur.execute("""
                INSERT INTO spam_tracker (chat_id, user_id, last_sender_streak, muted_until)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET muted_until = excluded.muted_until
            """, (chat_id, user_id, muted_until))
            cur.execute(
                "UPDATE chat_last_sender SET streak = 0 WHERE chat_id = ?",
                (chat_id,),
            )
            just_muted = True

    conn.commit()
    conn.close()
    return just_muted, streak, would_have_muted


def is_user_muted(chat_id: int, user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT muted_until FROM spam_tracker WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    if row is None or row["muted_until"] is None:
        return False
    muted_until = datetime.fromisoformat(row["muted_until"])
    return datetime.utcnow() < muted_until


# ---------------- Daily capture limit ----------------

def get_daily_capture_count(user_id: int):
    today = iran_today()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT count FROM daily_captures WHERE user_id = ? AND capture_date = ?",
        (user_id, today),
    )
    row = cur.fetchone()
    conn.close()
    return row["count"] if row else 0


def increment_daily_capture(user_id: int):
    today = iran_today()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO daily_captures (user_id, capture_date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, capture_date) DO UPDATE SET count = count + 1
    """, (user_id, today))
    conn.commit()
    cur.execute(
        "SELECT count FROM daily_captures WHERE user_id = ? AND capture_date = ?",
        (user_id, today),
    )
    row = cur.fetchone()
    conn.close()
    return row["count"]


# ---------------- Currency ----------------

def _track_currency_delta(cur, user_id: int, delta: int):
    """
    Records a balance change in the user's lifetime earned/spent
    totals (separately from `balance`, which nets out over time and
    can't tell "earned 15 then spent 10" apart from "never touched
    it"). earn_currency / spend_currency tasks read these totals
    instead of the raw balance, so unrelated income landing between a
    task's baseline and now (a daily bonus, a card sale) can't cancel
    out real spending progress, and unrelated spending can't cancel
    out real earning progress.
    """
    if delta > 0:
        cur.execute("""
            INSERT INTO currency (user_id, balance, total_earned) VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_earned = total_earned + excluded.total_earned
        """, (user_id, delta))
    elif delta < 0:
        cur.execute("""
            INSERT INTO currency (user_id, balance, total_spent) VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_spent = total_spent + excluded.total_spent
        """, (user_id, -delta))


def get_currency(user_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["balance"] if row else 0


def add_currency(user_id: int, amount: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO currency (user_id, balance)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
    """, (user_id, amount))
    _track_currency_delta(cur, user_id, amount)
    conn.commit()
    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["balance"]


# ---------------- Invite / referral system ----------------
# Reward doubles each successful invite (5, 10, 20, 40, 80, 160), capping
# at 160 - every invite after that still pays 160, just without doubling
# further. The first invite that reaches the 160 cap also earns a one-time
# +40 bonus and a random 🌙Nocturne card (see record_referral).
INVITE_REWARD_START = 5
INVITE_REWARD_CAP = 160
INVITE_FIRST_CAP_BONUS = 40


def register_bot_user_if_new(user_id: int, username: str = None, first_name: str = None, last_name: str = None) -> bool:
    """
    Records that this user has started the bot, and stores their current
    display info (see get_display_name). Returns True only the first
    time this is ever called for that user (a genuinely new player) -
    False if they'd already started the bot before, so re-running /start
    (e.g. via someone else's invite link) never counts as a new referral.
    "Started" is bot_users.started_at, which only this function sets - the
    row itself may already exist (upsert_user_profile creates it).
    """
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT started_at FROM bot_users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        is_new = row is None or row["started_at"] is None

        cur.execute("""
            INSERT INTO bot_users (user_id, first_seen_at, started_at, username, first_name, last_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                started_at = COALESCE(bot_users.started_at, excluded.started_at),
                username = COALESCE(excluded.username, bot_users.username),
                first_name = COALESCE(excluded.first_name, bot_users.first_name),
                last_name = COALESCE(excluded.last_name, bot_users.last_name)
        """, (user_id, now, now, username, first_name, last_name))
        conn.commit()
        return is_new
    finally:
        conn.close()


def upsert_user_profile(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    """
    Refreshes a user's known display info (see get_display_name) without
    affecting the "new user" bookkeeping register_bot_user_if_new does
    for the invite system. Safe to call on every interaction - existing
    non-null fields are only overwritten when a fresher value is given.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bot_users (user_id, first_seen_at, username, first_name, last_name)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = COALESCE(excluded.username, bot_users.username),
            first_name = COALESCE(excluded.first_name, bot_users.first_name),
            last_name = COALESCE(excluded.last_name, bot_users.last_name)
    """, (user_id, datetime.utcnow().isoformat(), username, first_name, last_name))
    conn.commit()
    conn.close()


# ---------------- /birthday ----------------

def get_user_birthday(user_id: int):
    """Returns (month, day) if this user has confirmed a birthday before,
    otherwise None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT birthday_month, birthday_day FROM bot_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row and row["birthday_month"] and row["birthday_day"]:
        return (row["birthday_month"], row["birthday_day"])
    return None


def set_user_birthday(user_id: int, month: int, day: int) -> bool:
    """
    Locks in a player's birthday - only if they don't already have one
    set (a birthday is permanent once confirmed, so this can't be used
    to re-roll it). Returns True if it was set just now, False if they
    already had one (in which case nothing here changed).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT birthday_month, birthday_day FROM bot_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row and row["birthday_month"] and row["birthday_day"]:
        conn.close()
        return False

    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO bot_users (user_id, first_seen_at, birthday_month, birthday_day)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            birthday_month = excluded.birthday_month,
            birthday_day = excluded.birthday_day
    """, (user_id, now, month, day))
    conn.commit()
    conn.close()
    return True


def mark_birthday_gifted(user_id: int, year: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE bot_users SET last_birthday_gift_year = ? WHERE user_id = ?", (year, user_id))
    conn.commit()
    conn.close()


def list_users_with_birthday_today(month: int, day: int):
    """Everyone whose locked-in birthday is this month/day, regardless of
    whether they've already been gifted this year - the caller (the
    nightly loop) filters that out using last_birthday_gift_year, same as
    the /birthday command's own immediate-gift path does."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_birthday_gift_year
        FROM bot_users
        WHERE birthday_month = ? AND birthday_day = ?
    """, (month, day))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_display_name(user_id: int) -> str:
    """
    Best available human-readable name for a user: their first name
    (+ last name) if we've ever seen them interact with the bot or Mini
    App, otherwise their @username, otherwise a numbered fallback for a
    player we truly have no info on yet. Arena NPC ids (negative,
    never real Telegram users) short-circuit to their scripted name.
    """
    if user_id in ARENA_NPC_IDS:
        return next(n["name"] for n in ARENA_NPC_OPPONENTS if n["id"] == user_id)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT username, first_name, last_name FROM bot_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if row and row["first_name"]:
        name = row["first_name"]
        if row["last_name"]:
            name = f"{name} {row['last_name']}"
        return name
    if row and row["username"]:
        return f"@{row['username']}"

    # Players seen before this table existed may still have a username
    # captured from a past card pickup - fall back to that.
    legacy_username = get_username_for_user(user_id)
    if legacy_username:
        return f"@{legacy_username}"

    return f"Player {user_id}"


def record_referral(inviter_id: int):
    """
    Bumps the inviter's invite count by one and returns
    (invite_count, reward, bonus_triggered) for the invite that just
    happened. `bonus_triggered` is True only the very first time the
    reward reaches the 160 cap (i.e. once per inviter, ever).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO referrals (inviter_id, invite_count, bonus_claimed)
        VALUES (?, 1, 0)
        ON CONFLICT(inviter_id) DO UPDATE SET invite_count = invite_count + 1
    """, (inviter_id,))
    conn.commit()

    cur.execute("SELECT invite_count, bonus_claimed FROM referrals WHERE inviter_id = ?", (inviter_id,))
    row = cur.fetchone()
    count = row["invite_count"]

    reward = INVITE_REWARD_START * (2 ** (count - 1))
    bonus_triggered = False
    if reward >= INVITE_REWARD_CAP:
        reward = INVITE_REWARD_CAP
        if not row["bonus_claimed"]:
            bonus_triggered = True
            cur.execute("UPDATE referrals SET bonus_claimed = 1 WHERE inviter_id = ?", (inviter_id,))
            conn.commit()

    conn.close()
    return count, reward, bonus_triggered


def get_invite_count(user_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT invite_count FROM referrals WHERE inviter_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["invite_count"] if row else 0


def get_random_character_by_rarity_substring(rarity_substring: str):
    """
    Returns a random character row whose rarity name contains
    `rarity_substring` (case-insensitive), or None if no such rarity is
    configured or it has no characters in it yet.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM rarities WHERE LOWER(name) LIKE LOWER(?)", (f"%{rarity_substring}%",))
    rarity_row = cur.fetchone()
    if not rarity_row:
        conn.close()
        return None
    cur.execute("SELECT id FROM characters WHERE rarity_id = ?", (rarity_row["id"],))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    chosen_id = random.choice(rows)["id"]
    return get_character(chosen_id)


def transfer_currency(sender_id: int, receiver_id: int, amount: int):
    """
    Atomically moves `amount` currency from sender to receiver.
    Returns the sender's remaining balance on success, or None if the
    sender doesn't have enough currency (nothing is changed in that case).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (sender_id,))
        row = cur.fetchone()
        sender_balance = row["balance"] if row else 0

        if sender_balance < amount:
            return None

        cur.execute("""
            INSERT INTO currency (user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance - excluded.balance
        """, (sender_id, amount))
        _track_currency_delta(cur, sender_id, -amount)

        cur.execute("""
            INSERT INTO currency (user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
        """, (receiver_id, amount))
        _track_currency_delta(cur, receiver_id, amount)

        conn.commit()

        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (sender_id,))
        return cur.fetchone()["balance"]
    finally:
        conn.close()


# ---------------- Daily dart limit ----------------

def get_daily_dart_count(user_id: int) -> int:
    today = iran_today()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT count FROM daily_darts WHERE user_id = ? AND dart_date = ?",
        (user_id, today),
    )
    row = cur.fetchone()
    conn.close()
    return row["count"] if row else 0


def increment_daily_dart(user_id: int) -> int:
    today = iran_today()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO daily_darts (user_id, dart_date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, dart_date) DO UPDATE SET count = count + 1
    """, (user_id, today))
    conn.commit()
    cur.execute(
        "SELECT count FROM daily_darts WHERE user_id = ? AND dart_date = ?",
        (user_id, today),
    )
    row = cur.fetchone()
    conn.close()
    return row["count"]

# ---------------- Granular per-admin permissions (Artist / Manager / Marzieh) ----------------
# A secondary admin can independently hold any combination of these (unlike
# the old single admin_type slot) - see config.ADMIN_PERMISSIONS for the
# canonical list with display labels, and bot.py's /admin command for the
# give/take-access UI that calls grant_permission / revoke_permission below.

def grant_permission(user_id: int, permission: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO admin_permissions (user_id, permission) VALUES (?, ?)",
        (user_id, permission.upper()),
    )
    conn.commit()
    conn.close()


def revoke_permission(user_id: int, permission: str) -> bool:
    """Removes one permission from one admin. Returns False if they didn't have it."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM admin_permissions WHERE user_id = ? AND permission = ?",
        (user_id, permission.upper()),
    )
    if not cur.fetchone():
        conn.close()
        return False
    cur.execute(
        "DELETE FROM admin_permissions WHERE user_id = ? AND permission = ?",
        (user_id, permission.upper()),
    )
    conn.commit()
    conn.close()
    return True


def has_permission(user_id: int, permission: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM admin_permissions WHERE user_id = ? AND permission = ?",
        (user_id, permission.upper()),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_permissions(user_id: int) -> list:
    """Every permission this user currently holds, e.g. ["ARTIST", "MARZIEH"]."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT permission FROM admin_permissions WHERE user_id = ? ORDER BY permission", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row["permission"] for row in rows]


def get_all_admins() -> list:
    """Every user_id that currently holds at least one permission - the list /admin shows."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM admin_permissions")
    rows = cur.fetchall()
    conn.close()
    return [row["user_id"] for row in rows]


def get_admin_type(user_id: int):
    """Deprecated - a user can now hold multiple permissions at once, so a
    single type no longer fully describes their access. Kept only in case
    something outside this file still calls it; returns the first permission
    found (if any). Prefer get_permissions() or has_permission()."""
    perms = get_permissions(user_id)
    return perms[0] if perms else None


def remove_admin(user_id: int) -> bool:
    """Revokes every permission this user holds at once. Returns False if they had none."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admin_permissions WHERE user_id = ?", (user_id,))
    if not cur.fetchone():
        conn.close()
        return False
    cur.execute("DELETE FROM admin_permissions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def remove_all_admins() -> int:
    """Revokes every secondary admin's permissions at once. Returns how many admins were removed."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) AS c FROM admin_permissions")
    count = cur.fetchone()["c"]
    cur.execute("DELETE FROM admin_permissions")
    conn.commit()
    conn.close()
    return count


# ---------------- /player (owner account management) ----------------

def get_user_id_by_username(username: str):
    """
    Resolves a bare username (no leading @) to a user_id. Only finds
    players who have actually interacted with the bot before (their
    username is recorded in bot_users, or - for older records predating
    that table - on a user_characters row), since Telegram gives bots no
    other way to look up a user by username.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM bot_users WHERE LOWER(username) = LOWER(?)", (username,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT user_id FROM user_characters WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
    conn.close()
    return row["user_id"] if row else None


def admin_remove_character_from_user(user_id: int, character_id: int) -> bool:
    """
    Owner override: removes ONE owned, not-currently-listed copy of
    character_id from the given user's inventory. Returns True on
    success, False if they have no free copy to remove.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT uc.id FROM user_characters uc
        WHERE uc.user_id = ? AND uc.character_id = ?
          AND uc.id NOT IN (
              SELECT user_character_id FROM market_listings WHERE status = 'active'
          )
        LIMIT 1
    """, (user_id, character_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    _remove_from_arena_teams(cur, row["id"])
    cur.execute("DELETE FROM user_characters WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


# ---------------- Bans ----------------

def ban_user(user_id: int, days: int = None, banned_by: int = None):
    """Bans a user. days=None means permanent."""
    banned_until = (datetime.utcnow() + timedelta(days=days)).isoformat() if days else None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bans (user_id, banned_until, banned_at, banned_by) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            banned_until = excluded.banned_until,
            banned_at = excluded.banned_at,
            banned_by = excluded.banned_by
    """, (user_id, banned_until, datetime.utcnow().isoformat(), banned_by))
    conn.commit()
    conn.close()
    return banned_until


def unban_user(user_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,))
    if not cur.fetchone():
        conn.close()
        return False
    cur.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def get_ban(user_id: int):
    """
    Returns the active ban row for this user, or None if they aren't
    banned. A permanent ban has banned_until = NULL. An expired timed
    ban is cleaned up automatically and None is returned.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bans WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row and row["banned_until"]:
        if datetime.fromisoformat(row["banned_until"]) <= datetime.utcnow():
            cur.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return None
    conn.close()
    return row


def is_banned(user_id: int) -> bool:
    return get_ban(user_id) is not None



# ---------------- Bin (30-day trash for deleted characters/rarities/events) ----------------

BIN_RETENTION_DAYS = 30


def _purge_expired_bin(cur):
    cur.execute("DELETE FROM bin_items WHERE expires_at <= ?", (datetime.utcnow().isoformat(),))


def _add_to_bin(cur, kind: str, label: str, data: dict):
    now = datetime.utcnow()
    cur.execute("""
        INSERT INTO bin_items (kind, label, data, deleted_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
    """, (kind, label, json.dumps(data), now.isoformat(),
          (now + timedelta(days=BIN_RETENTION_DAYS)).isoformat()))


def _capture_character_payload(cur, character_id: int) -> dict:
    """Snapshots a character row plus everything needed to fully restore
    it later: who owns it, and its Fighter stats (if any)."""
    cur.execute("SELECT * FROM characters WHERE id = ?", (character_id,))
    character = dict(cur.fetchone())
    cur.execute("SELECT * FROM user_characters WHERE character_id = ?", (character_id,))
    owners = [dict(row) for row in cur.fetchall()]
    cur.execute("SELECT * FROM fighter_stats WHERE character_id = ?", (character_id,))
    fighter_row = cur.fetchone()
    fighter_stats = dict(fighter_row) if fighter_row else None
    return {"character": character, "owners": owners, "fighter_stats": fighter_stats}


def delete_character(character_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM characters WHERE id = ?", (character_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    payload = _capture_character_payload(cur, character_id)
    _add_to_bin(cur, "character", f"{row['name']} (#{character_id})", payload)

    cur.execute("DELETE FROM user_characters WHERE character_id = ?", (character_id,))
    # Fighter stats are keyed by character_id, and IDs get reused after a
    # delete (see _next_available_character_id) - without this, the next
    # character added at this same ID would silently inherit the deleted
    # character's element/attack/defense.
    cur.execute("DELETE FROM fighter_stats WHERE character_id = ?", (character_id,))
    cur.execute("DELETE FROM characters WHERE id = ?", (character_id,))
    conn.commit()
    conn.close()
    return True


def wipe_all_characters():
    """Moves every character (with its owners and Fighter stats) into the
    bin as its own entry, then clears the live tables. Each one can be
    restored individually, or all at once, from /bin."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM characters")
    rows = cur.fetchall()
    for row in rows:
        payload = _capture_character_payload(cur, row["id"])
        _add_to_bin(cur, "character", f"{row['name']} (#{row['id']})", payload)

    cur.execute("DELETE FROM user_characters")
    cur.execute("DELETE FROM fighter_stats")
    cur.execute("DELETE FROM characters")
    conn.commit()
    conn.close()


def delete_rarity(name: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rarities WHERE LOWER(name) = LOWER(?)", (name,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    _add_to_bin(cur, "rarity", row["name"], {"rarity": dict(row)})

    cur.execute("UPDATE characters SET rarity_id = NULL WHERE rarity_id = ?", (row["id"],))
    cur.execute("DELETE FROM rarities WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


def wipe_all_rarities():
    """Moves every rarity into the bin as its own entry, then clears the
    table. Characters that had a wiped rarity simply show as Unranked
    until that specific rarity is restored from /bin."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rarities")
    rows = cur.fetchall()
    for row in rows:
        _add_to_bin(cur, "rarity", row["name"], {"rarity": dict(row)})

    cur.execute("UPDATE characters SET rarity_id = NULL")
    cur.execute("DELETE FROM rarities")
    conn.commit()
    conn.close()


def wipe_all_events():
    """Moves every registered event into the bin as its own entry, then
    clears the registry (also unlocking all of them)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT event_name FROM events")
    rows = cur.fetchall()
    for row in rows:
        _add_to_bin(cur, "event", row["event_name"], {"event_name": row["event_name"]})

    cur.execute("DELETE FROM events")
    cur.execute("DELETE FROM locked_events")
    cur.execute("DELETE FROM locked_events_main")
    conn.commit()
    conn.close()


def get_bin_items(kind: str):
    """Active (non-expired) bin entries of one kind, newest first."""
    conn = get_connection()
    cur = conn.cursor()
    _purge_expired_bin(cur)
    conn.commit()
    cur.execute("SELECT * FROM bin_items WHERE kind = ? ORDER BY id DESC", (kind,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bin_counts():
    """{'character': n, 'rarity': n, 'event': n} for the /bin main menu."""
    conn = get_connection()
    cur = conn.cursor()
    _purge_expired_bin(cur)
    conn.commit()
    cur.execute("SELECT kind, COUNT(*) AS c FROM bin_items GROUP BY kind")
    counts = {"character": 0, "rarity": 0, "event": 0}
    for row in cur.fetchall():
        counts[row["kind"]] = row["c"]
    conn.close()
    return counts


def _restore_bin_row(cur, row) -> bool:
    """Re-inserts whatever a single bin row describes. Returns True on
    success. Assumes the caller commits/closes the connection."""
    kind = row["kind"]
    data = json.loads(row["data"])

    if kind == "character":
        character = data["character"]
        columns = list(character.keys())
        placeholders = ", ".join("?" for _ in columns)
        try:
            cur.execute(
                f"INSERT INTO characters ({', '.join(columns)}) VALUES ({placeholders})",
                [character[c] for c in columns],
            )
            char_id = character["id"]
        except sqlite3.IntegrityError:
            # That ID has since been reused by a different character -
            # restore this one under a fresh ID instead of overwriting it.
            columns_no_id = [c for c in columns if c != "id"]
            placeholders = ", ".join("?" for _ in columns_no_id)
            cur.execute(
                f"INSERT INTO characters ({', '.join(columns_no_id)}) VALUES ({placeholders})",
                [character[c] for c in columns_no_id],
            )
            char_id = cur.lastrowid

        for owner in data.get("owners", []):
            cur.execute(
                "INSERT INTO user_characters (user_id, username, character_id, obtained_at) "
                "VALUES (?, ?, ?, ?)",
                (owner["user_id"], owner["username"], char_id, owner["obtained_at"]),
            )

        fighter_stats = data.get("fighter_stats")
        if fighter_stats:
            cur.execute(
                "INSERT OR IGNORE INTO fighter_stats (character_id, element, base_attack, base_defense) "
                "VALUES (?, ?, ?, ?)",
                (char_id, fighter_stats["element"], fighter_stats["base_attack"], fighter_stats["base_defense"]),
            )
        return True

    if kind == "rarity":
        rarity = data["rarity"]
        cur.execute("SELECT 1 FROM rarities WHERE LOWER(name) = LOWER(?)", (rarity["name"],))
        if cur.fetchone():
            return False  # a rarity with this name already exists again
        try:
            cur.execute(
                "INSERT INTO rarities (id, name, weight) VALUES (?, ?, ?)",
                (rarity["id"], rarity["name"], rarity["weight"]),
            )
        except sqlite3.IntegrityError:
            cur.execute(
                "INSERT INTO rarities (name, weight) VALUES (?, ?)",
                (rarity["name"], rarity["weight"]),
            )
        return True

    if kind == "event":
        cur.execute("INSERT OR IGNORE INTO events (event_name) VALUES (?)", (data["event_name"],))
        return True

    return False


def restore_bin_item(item_id: int):
    """Restores one bin entry by ID. Returns its label on success, or
    None if it wasn't found (already expired/restored)."""
    conn = get_connection()
    cur = conn.cursor()
    _purge_expired_bin(cur)
    cur.execute("SELECT * FROM bin_items WHERE id = ?", (item_id,))
    row = cur.fetchone()
    if not row:
        conn.commit()
        conn.close()
        return None

    _restore_bin_row(cur, row)
    cur.execute("DELETE FROM bin_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return row["label"]


def restore_all_bin_items(kind: str) -> int:
    """Restores every active bin entry of one kind. Returns how many were restored."""
    conn = get_connection()
    cur = conn.cursor()
    _purge_expired_bin(cur)
    cur.execute("SELECT * FROM bin_items WHERE kind = ? ORDER BY id ASC", (kind,))
    rows = cur.fetchall()
    restored = 0
    for row in rows:
        if _restore_bin_row(cur, row):
            restored += 1
        cur.execute("DELETE FROM bin_items WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return restored


# ---------------- /new channel publisher ----------------

def get_new_channel_setting():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM new_channel_settings WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row


def set_new_channel_setting(channel_target: str, channel_link: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO new_channel_settings (id, channel_target, channel_link, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            channel_target = excluded.channel_target,
            channel_link = excluded.channel_link,
            updated_at = excluded.updated_at
    """, (channel_target, channel_link, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def create_new_post(channel_target: str, message_id: int, fa_text: str, en_text: str, created_by: int, buttons=None):
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO new_posts (channel_target, message_id, fa_text, en_text, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (channel_target, message_id, fa_text, en_text, created_by, now))
    post_id = cur.lastrowid
    for button in buttons or []:
        cur.execute("""
            INSERT INTO new_post_buttons
                (post_id, label, action_type, action_data, max_uses, uses, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
        """, (
            post_id, button["label"], button["action_type"], button.get("action_data"),
            button.get("max_uses"), now,
        ))
    conn.commit()
    conn.close()
    return post_id


def get_new_post(post_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM new_posts WHERE id = ?", (post_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_new_post_buttons(post_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM new_post_buttons WHERE post_id = ? ORDER BY id", (post_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_new_post_button(button_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM new_post_buttons WHERE id = ?", (button_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_new_post_message_id(post_id: int, message_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE new_posts SET message_id = ? WHERE id = ?", (message_id, post_id))
    conn.commit()
    conn.close()


def delete_new_post(post_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM new_post_buttons WHERE post_id = ?", (post_id,))
    cur.execute("DELETE FROM new_posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def consume_new_post_button(button_id: int):
    """Atomically consumes one global use of a /new custom button.
    Returns the button row after consumption, or None when exhausted/not found."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("""
            UPDATE new_post_buttons
            SET uses = uses + 1
            WHERE id = ? AND (max_uses IS NULL OR uses < max_uses)
        """, (button_id,))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        cur.execute("SELECT * FROM new_post_buttons WHERE id = ?", (button_id,))
        row = cur.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def grant_character_copy(user_id: int, username: str, character_id: int) -> bool:
    """Give a player one fresh copy of a character, without requiring a
    source owner. Used by /new's public claim-card buttons."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM characters WHERE id = ?", (character_id,))
    if not cur.fetchone():
        conn.close()
        return False
    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO user_characters (user_id, username, character_id, obtained_at) VALUES (?, ?, ?, ?)",
        (user_id, username, character_id, now),
    )
    _init_fighter_fields_if_applicable(cur, cur.lastrowid, character_id)
    conn.commit()
    conn.close()
    return True



def claim_new_card_button(button_id: int, user_id: int, username: str):
    """Atomically allow one user to claim a card-button reward only once."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT * FROM new_post_buttons WHERE id = ?", (button_id,))
        button = cur.fetchone()
        if not button or button["action_type"] != "card":
            conn.rollback()
            return {"status": "invalid"}

        try:
            character_id = int(button["action_data"])
        except (TypeError, ValueError):
            conn.rollback()
            return {"status": "invalid"}

        cur.execute("SELECT id FROM characters WHERE id = ?", (character_id,))
        if not cur.fetchone():
            conn.rollback()
            return {"status": "invalid"}

        cur.execute(
            "SELECT 1 FROM new_post_button_claims WHERE button_id = ? AND user_id = ?",
            (button_id, user_id),
        )
        if cur.fetchone():
            conn.rollback()
            return {"status": "already_claimed"}

        cur.execute("""
            UPDATE new_post_buttons
            SET uses = uses + 1
            WHERE id = ? AND (max_uses IS NULL OR uses < max_uses)
        """, (button_id,))
        if cur.rowcount != 1:
            conn.rollback()
            return {"status": "exhausted"}

        cur.execute(
            "INSERT INTO new_post_button_claims (button_id, user_id, claimed_at) VALUES (?, ?, ?)",
            (button_id, user_id, datetime.utcnow().isoformat()),
        )
        cur.execute(
            "INSERT INTO user_characters (user_id, username, character_id, obtained_at) VALUES (?, ?, ?, ?)",
            (user_id, username, character_id, datetime.utcnow().isoformat()),
        )
        _init_fighter_fields_if_applicable(cur, cur.lastrowid, character_id)
        conn.commit()
        return {"status": "ok", "character_id": character_id}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"status": "already_claimed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_new_vy_button(button_id: int, user_id: int, amount: int):
    """Atomically allow one user to claim a VɎ-button reward only once."""
    if amount <= 0:
        return {"status": "invalid"}
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT action_type FROM new_post_buttons WHERE id = ?", (button_id,))
        button = cur.fetchone()
        if not button or button["action_type"] != "vy":
            conn.rollback()
            return {"status": "invalid"}

        cur.execute(
            "SELECT 1 FROM new_post_button_claims WHERE button_id = ? AND user_id = ?",
            (button_id, user_id),
        )
        if cur.fetchone():
            conn.rollback()
            return {"status": "already_claimed"}

        cur.execute("""
            UPDATE new_post_buttons
            SET uses = uses + 1
            WHERE id = ? AND (max_uses IS NULL OR uses < max_uses)
        """, (button_id,))
        if cur.rowcount != 1:
            conn.rollback()
            return {"status": "exhausted"}

        cur.execute(
            "INSERT INTO new_post_button_claims (button_id, user_id, claimed_at) VALUES (?, ?, ?)",
            (button_id, user_id, datetime.utcnow().isoformat()),
        )
        cur.execute("""
            INSERT INTO currency (user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
        """, (user_id, amount))
        _track_currency_delta(cur, user_id, amount)
        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.commit()
        return {"status": "ok", "balance": row["balance"]}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"status": "already_claimed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------- Spawn lock status ----------------

def get_locked_rarity_ids(main: bool = False):
    """Rarity ids locked everywhere - or, with main=True, locked in the main group only."""
    table = "locked_rarities_main" if main else "locked_rarities"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT rarity_id FROM {table}")
    ids = {r["rarity_id"] for r in cur.fetchall()}
    conn.close()
    return ids


def get_locked_events():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT event_name FROM locked_events")
    names = [r["event_name"] for r in cur.fetchall()]
    conn.close()
    return names


# ---------------- Edit character ----------------

def update_character(character_id: int, name: str, series: str, rarity_name: str = None, event_name: str = None) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM characters WHERE id = ?", (character_id,))
    if not cur.fetchone():
        conn.close()
        return False

    rarity_id = None
    if rarity_name:
        rarity = get_rarity_by_name(rarity_name)
        if rarity:
            rarity_id = rarity["id"]

    # Same rule as add_character: an unregistered event name is dropped.
    if event_name:
        canonical = get_event_by_name(event_name)
        event_name = canonical if canonical else None

    cur.execute(
        "UPDATE characters SET name = ?, series = ?, rarity_id = ?, event_name = ? WHERE id = ?",
        (name, series, rarity_id, event_name, character_id),
    )
    conn.commit()
    conn.close()
    return True


def set_character_persona(character_id: int, persona: str, age: str = None, gender: str = None) -> bool:
    """Sets the HIDDEN Chat-tab persona for a character (see chat_ai.py) -
    never surfaced in the Market/profile/card views, only read when
    building that character's AI system prompt."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM characters WHERE id = ?", (character_id,))
    if not cur.fetchone():
        conn.close()
        return False

    cur.execute(
        "UPDATE characters SET chat_persona = ?, chat_age = ?, chat_gender = ? WHERE id = ?",
        (persona, age, gender, character_id),
    )
    conn.commit()
    conn.close()
    return True


# ---------------- Favorite card (/fav) ----------------

def set_favorite_card(user_id: int, character_id: int):
    """Pins `character_id` as this player's favorite card (one per player,
    replaces any previous choice)."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO user_fav_card (user_id, character_id, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               character_id = excluded.character_id,
               updated_at = excluded.updated_at""",
        (user_id, character_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_favorite_card(user_id: int):
    """The player's pinned favorite card as a character row (same shape
    get_user_inventory returns), or None if they haven't set one, the
    character no longer exists, or they don't own a copy anymore (gifted,
    sold, traded away...). Callers fall back to a random card then; the
    pin comes back on its own if they get the card again."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT characters.id, characters.name, characters.series, characters.image_file_id,
               characters.media_type, characters.event_name,
               rarities.name AS rarity_name
        FROM user_fav_card
        JOIN characters ON characters.id = user_fav_card.character_id
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE user_fav_card.user_id = ?
          AND EXISTS (
              SELECT 1 FROM user_characters
              WHERE user_characters.user_id = user_fav_card.user_id
                AND user_characters.character_id = user_fav_card.character_id
          )
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    return row


# ---------------- Gifting ----------------

def user_owns_character(user_id: int, character_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_characters WHERE user_id = ? AND character_id = ? LIMIT 1",
        (user_id, character_id),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_bot_stats():
    """Owner-only /stats: total distinct users the bot has ever seen
    (bot_users, refreshed on every incoming update - see upsert_user_profile),
    and total groups the bot has been active in (one chat_state row per
    group/supergroup that has ever sent a spawn-countable message)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM bot_users")
    user_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM chat_state")
    group_count = cur.fetchone()["c"]
    conn.close()
    return user_count, group_count


def list_bot_users():
    """Every user the bot has ever seen, most recently first-seen last.
    Used by the /stats 'Users' button."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_name, first_seen_at
        FROM bot_users
        ORDER BY first_seen_at
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def remove_chat_state(chat_id: int):
    """Drops a chat's row entirely - used when /stats discovers the bot
    is no longer a member (left or kicked), so a dead group stops
    cluttering the 'currently active' groups list."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def list_active_groups():
    """Every group the bot has been active in, with its title (when known)
    and how many spawn-countable messages it's logged. Used by the
    /stats 'Groups' button."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT chat_id, title, message_count
        FROM chat_state
        ORDER BY message_count DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def gift_character(giver_id: int, recipient_id: int, recipient_username: str, character_id: int) -> bool:
    """Moves exactly ONE copy of a character from giver to recipient.
    Updates the existing row in place (rather than delete+reinsert) so
    a Fighter copy's level/attack/defense progress carries over to the
    recipient instead of resetting - and un-slots it from the giver's
    Arena team, since they no longer own it."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
    cur.execute(
        "SELECT id FROM user_characters WHERE user_id = ? AND character_id = ? LIMIT 1",
        (giver_id, character_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    now = datetime.utcnow().isoformat()
    _remove_from_arena_teams(cur, row["id"])
    cur.execute(
        "UPDATE user_characters SET user_id = ?, username = ?, obtained_at = ? WHERE id = ?",
        (recipient_id, recipient_username, now, row["id"]),
    )
    _log_rarity_activity_for_character(cur, character_id, "gift", now)
    conn.commit()
    conn.close()
    return True


# ---------------- Sell to bot ----------------
# Unranked characters (rarity_id IS NULL) are priced under the sentinel
# rarity_id 0, since no real rarity ever gets id 0 (AUTOINCREMENT starts
# at 1) - this keeps a single, ordinary-looking table instead of dealing
# with NULL primary keys.
UNRANKED_SELL_KEY = 0


def set_sell_price(rarity_id, price: int):
    """rarity_id may be None to set the price for Unranked characters."""
    key = rarity_id if rarity_id is not None else UNRANKED_SELL_KEY
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sell_prices (rarity_id, price) VALUES (?, ?)
        ON CONFLICT(rarity_id) DO UPDATE SET price = excluded.price
    """, (key, price))
    conn.commit()
    conn.close()


def get_sell_price(rarity_id) -> int:
    """rarity_id may be None (Unranked). Returns 0 if no price is set yet."""
    key = rarity_id if rarity_id is not None else UNRANKED_SELL_KEY
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT price FROM sell_prices WHERE rarity_id = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["price"] if row else 0


def get_all_sell_prices():
    """
    Returns one entry per rarity (plus Unranked), each with the rarity's
    id (0 = Unranked), name, and its currently configured sell price
    (None if never set).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM rarities ORDER BY weight DESC")
    rarities = cur.fetchall()
    cur.execute("SELECT rarity_id, price FROM sell_prices")
    price_map = {r["rarity_id"]: r["price"] for r in cur.fetchall()}
    conn.close()

    result = [{"rarity_id": r["id"], "name": r["name"], "price": price_map.get(r["id"])} for r in rarities]
    result.append({"rarity_id": UNRANKED_SELL_KEY, "name": "Unranked", "price": price_map.get(UNRANKED_SELL_KEY)})
    return result


def sell_character_to_bot(user_id: int, character_id: int) -> bool:
    """
    Removes ONE owned, not-currently-listed copy of character_id from the
    user's inventory (selling a copy that's actively listed on the market
    would double-dip). Returns True on success, False if the user has no
    free copy to sell.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
    cur.execute("""
        SELECT uc.id FROM user_characters uc
        WHERE uc.user_id = ? AND uc.character_id = ?
          AND uc.id NOT IN (
              SELECT user_character_id FROM market_listings WHERE status = 'active'
          )
        LIMIT 1
    """, (user_id, character_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    _remove_from_arena_teams(cur, row["id"])
    cur.execute("DELETE FROM user_characters WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


# ---------------- Market (Mini App) ----------------

def create_listing(user_id: int, username: str, character_id: int, price: int):
    """
    Lists ONE owned, not-already-listed copy of character_id for sale.
    Returns the new listing's id, or None if the user has no free copy
    to sell (either they don't own it, or every copy they own is already
    listed).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("""
            SELECT uc.id FROM user_characters uc
            WHERE uc.user_id = ? AND uc.character_id = ?
              AND uc.id NOT IN (
                  SELECT user_character_id FROM market_listings WHERE status = 'active'
              )
            LIMIT 1
        """, (user_id, character_id))
        row = cur.fetchone()
        if not row:
            return None

        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO market_listings "
            "(user_character_id, character_id, seller_id, seller_username, price, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (row["id"], character_id, user_id, username, price, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_active_listings():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT market_listings.id AS listing_id, market_listings.price,
               market_listings.seller_id, market_listings.seller_username,
               market_listings.created_at,
               characters.id AS character_id, characters.name, characters.series,
               characters.image_file_id, characters.media_type, characters.event_name,
               rarities.name AS rarity_name
        FROM market_listings
        JOIN characters ON market_listings.character_id = characters.id
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE market_listings.status = 'active'
        ORDER BY market_listings.created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_listings(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT market_listings.id AS listing_id, market_listings.price, market_listings.created_at,
               characters.id AS character_id, characters.name, characters.series,
               rarities.name AS rarity_name
        FROM market_listings
        JOIN characters ON market_listings.character_id = characters.id
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE market_listings.seller_id = ? AND market_listings.status = 'active'
        ORDER BY market_listings.created_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_listing_by_id(listing_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM market_listings WHERE id = ?", (listing_id,))
    row = cur.fetchone()
    conn.close()
    return row


def cancel_listing(listing_id: int, user_id: int) -> bool:
    """Only the seller can cancel, and only while it's still active."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
    cur.execute("SELECT seller_id, status FROM market_listings WHERE id = ?", (listing_id,))
    row = cur.fetchone()
    if not row or row["status"] != "active" or row["seller_id"] != user_id:
        conn.close()
        return False

    cur.execute("UPDATE market_listings SET status = 'cancelled' WHERE id = ?", (listing_id,))
    conn.commit()
    conn.close()
    return True


def buy_listing(listing_id: int, buyer_id: int, buyer_username: str):
    """
    Buys an active listing: moves currency buyer -> seller, moves the
    specific card copy buyer's way, and marks the listing sold - all in
    one transaction, so a crash mid-purchase can't charge someone
    without handing over the card (or vice versa).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT * FROM market_listings WHERE id = ?", (listing_id,))
        listing = cur.fetchone()
        if not listing or listing["status"] != "active":
            return {"ok": False, "reason": "not_available"}

        seller_id = listing["seller_id"]
        price = listing["price"]

        if buyer_id == seller_id:
            return {"ok": False, "reason": "own_listing"}

        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (buyer_id,))
        row = cur.fetchone()
        buyer_balance = row["balance"] if row else 0
        if buyer_balance < price:
            return {"ok": False, "reason": "insufficient_funds"}

        cur.execute("""
            INSERT INTO currency (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance - excluded.balance
        """, (buyer_id, price))
        _track_currency_delta(cur, buyer_id, -price)
        cur.execute("""
            INSERT INTO currency (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
        """, (seller_id, price))
        _track_currency_delta(cur, seller_id, price)

        now = datetime.utcnow().isoformat()
        _remove_from_arena_teams(cur, listing["user_character_id"])
        cur.execute(
            "UPDATE user_characters SET user_id = ?, username = ?, obtained_at = ? WHERE id = ?",
            (buyer_id, buyer_username, now, listing["user_character_id"]),
        )
        cur.execute(
            "UPDATE market_listings SET status = 'sold', buyer_id = ?, sold_at = ? WHERE id = ?",
            (buyer_id, now, listing_id),
        )
        _log_rarity_activity_for_character(cur, listing["character_id"], "sold", now)

        conn.commit()
        return {
            "ok": True,
            "character_id": listing["character_id"],
            "price": price,
            "seller_id": seller_id,
        }
    finally:
        conn.close()


# ---------------- Leaderboards ----------------

def get_top_series(limit: int = 50):
    """
    Top `limit` series ranked by how many distinct characters exist in
    that series (not how many copies players own - this is about the
    size of the collectible set, e.g. for the Mini App's Constellation
    leaderboard).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT series, COUNT(*) AS character_count
        FROM characters
        GROUP BY series
        ORDER BY character_count DESC, series ASC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_series_characters(series: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT characters.id, characters.name, characters.series, characters.image_file_id,
               characters.media_type, rarities.name AS rarity_name
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE characters.series = ?
        ORDER BY characters.name ASC
    """, (series,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_richest_users(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, balance
        FROM currency
        WHERE balance > 0
        ORDER BY balance DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_richest_rank(user_id: int):
    """1-based rank of this user on the richest-players leaderboard (same
    ordering as get_richest_users, the one behind the Mini App's leaderboard).
    Returns None if the player has no balance yet or a balance of 0, since
    those aren't on the leaderboard at all."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row or row["balance"] <= 0:
        conn.close()
        return None
    cur.execute("SELECT COUNT(*) AS c FROM currency WHERE balance > ?", (row["balance"],))
    higher = cur.fetchone()["c"]
    conn.close()
    return higher + 1


def get_top_collectors(limit: int = 50):
    """
    Top `limit` players ranked by how many cards they personally own
    (COUNT(*) over user_characters, so duplicates count - this is about
    each player's own collection size, e.g. for the Mini App's
    leaderboard "Collection" tab, as opposed to get_top_series which
    ranks the series themselves by how many characters exist in them).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, COUNT(*) AS card_count
        FROM user_characters
        GROUP BY user_id
        ORDER BY card_count DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------- Tasks ----------------

def _task_current_value(cur, user_id: int, task: sqlite3.Row) -> int:
    """
    Returns the player's CURRENT raw count for whatever a task's type
    measures (not yet adjusted for the task's baseline). Progress is
    always (current - baseline), computed by the caller.
    """
    task_type = task["task_type"]

    if task_type == "own_cards":
        cur.execute("SELECT COUNT(*) AS c FROM user_characters WHERE user_id = ?", (user_id,))
        return cur.fetchone()["c"]

    if task_type == "rarity_cards":
        cur.execute("""
            SELECT COUNT(*) AS c FROM user_characters
            JOIN characters ON user_characters.character_id = characters.id
            WHERE user_characters.user_id = ? AND characters.rarity_id = ?
        """, (user_id, task["rarity_id"]))
        return cur.fetchone()["c"]

    if task_type == "character_cards":
        cur.execute("""
            SELECT COUNT(*) AS c FROM user_characters
            WHERE user_id = ? AND character_id = ?
        """, (user_id, task["character_id"]))
        return cur.fetchone()["c"]

    if task_type == "buy_market":
        cur.execute("""
            SELECT COUNT(*) AS c FROM market_listings
            WHERE buyer_id = ? AND status = 'sold'
        """, (user_id,))
        return cur.fetchone()["c"]

    if task_type == "sell_market":
        cur.execute("""
            SELECT COUNT(*) AS c FROM market_listings
            WHERE seller_id = ? AND status = 'sold'
        """, (user_id,))
        return cur.fetchone()["c"]

    if task_type in ("earn_currency", "spend_currency"):
        column = "total_earned" if task_type == "earn_currency" else "total_spent"
        cur.execute(f"SELECT {column} FROM currency WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[column] if row else 0

    return 0


def _ensure_progress_row(cur, user_id: int, task: sqlite3.Row):
    """
    Lazily creates the user's progress row for a task the first time
    they see it, snapshotting their current value as the baseline so
    they only get credit for progress made AFTER the task appeared on
    their board (not credit for things they already had).
    """
    cur.execute(
        "SELECT * FROM user_task_progress WHERE user_id = ? AND task_id = ?",
        (user_id, task["id"]),
    )
    row = cur.fetchone()
    if row:
        return row

    baseline = _task_current_value(cur, user_id, task)
    cur.execute(
        "INSERT INTO user_task_progress (user_id, task_id, baseline_value) VALUES (?, ?, ?)",
        (user_id, task["id"], baseline),
    )
    cur.execute(
        "SELECT * FROM user_task_progress WHERE user_id = ? AND task_id = ?",
        (user_id, task["id"]),
    )
    return cur.fetchone()


def get_active_tasks_for_user(user_id: int, limit: int = 3):
    """
    A player's current board: the oldest `limit` tasks they haven't
    claimed yet, in creation order. When a player claims one, it drops
    out of this query and the next-oldest unclaimed task takes its
    place - independently per player, so everyone can complete the
    same tasks without racing each other.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT task_definitions.* FROM task_definitions
        WHERE task_definitions.id NOT IN (
            SELECT task_id FROM user_task_progress
            WHERE user_id = ? AND claimed_at IS NOT NULL
        )
        ORDER BY task_definitions.id ASC
        LIMIT ?
    """, (user_id, limit))
    tasks = cur.fetchall()

    result = []
    for task in tasks:
        progress_row = _ensure_progress_row(cur, user_id, task)
        current = _task_current_value(cur, user_id, task)
        progress = max(0, current - progress_row["baseline_value"])

        result.append({
            "task": task,
            "progress": min(progress, task["target_count"]),
            "target": task["target_count"],
            "done": progress >= task["target_count"],
        })

    conn.commit()
    conn.close()
    return result


def add_task_definition(task_type: str, target_count: int, reward_type: str,
                         display_text: str, created_by: int,
                         character_id: int = None, rarity_id: int = None,
                         reward_currency: int = None, reward_character_id: int = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO task_definitions
            (task_type, target_count, character_id, rarity_id, reward_type,
             reward_currency, reward_character_id, display_text, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task_type, target_count, character_id, rarity_id, reward_type,
        reward_currency, reward_character_id, display_text, created_by,
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def claim_task(user_id: int, task_id: int, username: str = None):
    """
    Verifies the task is actually complete server-side (never trusts
    the client), pays out the reward, and marks it claimed for this
    player. Returns a dict describing what happened.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT * FROM task_definitions WHERE id = ?", (task_id,))
        task = cur.fetchone()
        if not task:
            return {"ok": False, "reason": "not_found"}

        progress_row = _ensure_progress_row(cur, user_id, task)
        if progress_row["claimed_at"]:
            return {"ok": False, "reason": "already_claimed"}

        current = _task_current_value(cur, user_id, task)
        progress = max(0, current - progress_row["baseline_value"])

        if progress < task["target_count"]:
            return {"ok": False, "reason": "not_complete"}

        cur.execute(
            "UPDATE user_task_progress SET claimed_at = ? WHERE user_id = ? AND task_id = ?",
            (datetime.utcnow().isoformat(), user_id, task_id),
        )

        if task["reward_type"] == "currency":
            cur.execute("""
                INSERT INTO currency (user_id, balance) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
            """, (user_id, task["reward_currency"]))
            _track_currency_delta(cur, user_id, task["reward_currency"])
        elif task["reward_type"] == "card":
            cur.execute(
                "INSERT INTO user_characters (user_id, username, character_id, obtained_at) VALUES (?, ?, ?, ?)",
                (user_id, username, task["reward_character_id"], datetime.utcnow().isoformat()),
            )
            _init_fighter_fields_if_applicable(cur, cur.lastrowid, task["reward_character_id"])

        conn.commit()
        return {
            "ok": True,
            "reward_type": task["reward_type"],
            "reward_currency": task["reward_currency"],
            "reward_character_id": task["reward_character_id"],
        }
    finally:
        conn.close()


def claim_daily_task_bonus(user_id: int):
    """
    A flat daily bonus (config.DAILY_TASK_BONUS, more for premium - see
    PREMIUM_DAILY_TASK_BONUS), once per calendar day (UTC). Returns the
    new balance on success, or None if already claimed today.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        today = datetime.utcnow().date().isoformat()
        cur.execute("SELECT last_claim_date FROM daily_task_claim WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row["last_claim_date"] == today:
            return None

        amount = PREMIUM_DAILY_TASK_BONUS if is_premium(user_id) else DAILY_TASK_BONUS

        cur.execute("""
            INSERT INTO daily_task_claim (user_id, last_claim_date) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_claim_date = excluded.last_claim_date
        """, (user_id, today))
        cur.execute("""
            INSERT INTO currency (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
        """, (user_id, amount))
        _track_currency_delta(cur, user_id, amount)
        conn.commit()

        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
        return cur.fetchone()["balance"]
    finally:
        conn.close()


def search_characters_for_picker(query: str = "", limit: int = 30):
    """
    Character search for the admin Task creation form: returns id +
    rarity alongside the name so the admin can tell apart characters
    that share a name but were added at a different rarity/event.
    """
    conn = get_connection()
    cur = conn.cursor()
    like = f"%{query}%"
    cur.execute("""
        SELECT characters.id, characters.name, characters.series,
               rarities.name AS rarity_name
        FROM characters
        LEFT JOIN rarities ON characters.rarity_id = rarities.id
        WHERE characters.name LIKE ? OR characters.series LIKE ?
        ORDER BY characters.name ASC
        LIMIT ?
    """, (like, like, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def has_claimed_daily_task_bonus_today(user_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    today = datetime.utcnow().date().isoformat()
    cur.execute("SELECT last_claim_date FROM daily_task_claim WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row["last_claim_date"] == today)


# ==================== Fighter / Arena (Update 2) ====================

# ---------------- Fighter stats (per character definition) ----------------

def set_fighter_stats(character_id: int, element: str, base_attack: int, base_defense: int):
    """Registers (or overwrites) a character's Fighter definition. Also
    backfills current_attack/current_defense for any copies already
    owned that haven't been upgraded yet, so re-editing a freshly-added
    Fighter's stats doesn't leave existing copies stuck at 0."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO fighter_stats (character_id, element, base_attack, base_defense)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(character_id) DO UPDATE SET
            element = excluded.element,
            base_attack = excluded.base_attack,
            base_defense = excluded.base_defense
    """, (character_id, element, base_attack, base_defense))
    cur.execute("""
        UPDATE user_characters SET current_attack = ?, current_defense = ?
        WHERE character_id = ? AND (level IS NULL OR level = 1)
          AND current_attack IS NULL
    """, (base_attack, base_defense, character_id))
    conn.commit()
    conn.close()


def get_fighter_stats(character_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM fighter_stats WHERE character_id = ?", (character_id,))
    row = cur.fetchone()
    conn.close()
    return row


def is_fighter_character(character_id: int) -> bool:
    return get_fighter_stats(character_id) is not None


# ---------------- Owned Fighter cards & upgrades ----------------

def get_user_fighter_cards(user_id: int):
    """Every Fighter copy this user owns - powers Card Management and
    the Attack/Defense team pickers in the Mini App's Arena tab."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT uc.id AS user_character_id, uc.level, uc.current_attack, uc.current_defense,
               c.id AS character_id, c.name, c.series, c.image_file_id, c.media_type,
               fs.element, fs.base_attack, fs.base_defense
        FROM user_characters uc
        JOIN characters c ON c.id = uc.character_id
        JOIN fighter_stats fs ON fs.character_id = c.id
        WHERE uc.user_id = ?
        ORDER BY uc.id
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_user_fighter_card(user_id: int, user_character_id: int):
    """A single owned Fighter copy, only if it belongs to user_id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT uc.id AS user_character_id, uc.level, uc.current_attack, uc.current_defense,
               c.id AS character_id, c.name, c.series, c.image_file_id, c.media_type,
               fs.element, fs.base_attack, fs.base_defense
        FROM user_characters uc
        JOIN characters c ON c.id = uc.character_id
        JOIN fighter_stats fs ON fs.character_id = c.id
        WHERE uc.id = ? AND uc.user_id = ?
    """, (user_character_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row


def upgrade_fighter_card(user_id: int, user_character_id: int, stat: str):
    """
    Card Management -> upgrade one owned Fighter copy's Attack OR
    Defense by its element's multiplier, and bumps its level by 1 (cap
    FIGHTER_MAX_LEVEL). Returns {"ok": True, ...new values} or
    {"ok": False, "reason": ...}.
    """
    if stat not in ("attack", "defense"):
        return {"ok": False, "reason": "invalid_stat"}

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("""
            SELECT uc.level, uc.current_attack, uc.current_defense, fs.element
            FROM user_characters uc
            JOIN fighter_stats fs ON fs.character_id = uc.character_id
            WHERE uc.id = ? AND uc.user_id = ?
        """, (user_character_id, user_id))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}

        level = row["level"] or 1
        if level >= FIGHTER_MAX_LEVEL:
            return {"ok": False, "reason": "max_level"}

        element = ELEMENTS.get(row["element"])
        if not element:
            return {"ok": False, "reason": "invalid_element"}

        if stat == "attack":
            new_value = round((row["current_attack"] or 0) * element["attack_mult"])
            cur.execute(
                "UPDATE user_characters SET current_attack = ?, level = level + 1 WHERE id = ?",
                (new_value, user_character_id),
            )
        else:
            new_value = round((row["current_defense"] or 0) * element["defense_mult"])
            cur.execute(
                "UPDATE user_characters SET current_defense = ?, level = level + 1 WHERE id = ?",
                (new_value, user_character_id),
            )

        conn.commit()
        cur.execute(
            "SELECT level, current_attack, current_defense FROM user_characters WHERE id = ?",
            (user_character_id,),
        )
        updated = cur.fetchone()
        return {
            "ok": True,
            "stat": stat,
            "level": updated["level"],
            "current_attack": updated["current_attack"],
            "current_defense": updated["current_defense"],
        }
    finally:
        conn.close()


# ---------------- Arena teams ----------------

def get_arena_team(user_id: int, team_type: str):
    """team_type: 'attack' or 'defense'. Ordered by slot (1..3)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT at.slot, uc.id AS user_character_id, uc.level, uc.current_attack, uc.current_defense,
               c.id AS character_id, c.name, c.series, c.image_file_id, c.media_type, fs.element
        FROM arena_teams at
        JOIN user_characters uc ON uc.id = at.user_character_id
        JOIN characters c ON c.id = uc.character_id
        JOIN fighter_stats fs ON fs.character_id = c.id
        WHERE at.user_id = ? AND at.team_type = ?
        ORDER BY at.slot
    """, (user_id, team_type))
    rows = cur.fetchall()
    conn.close()
    return rows


def set_arena_team(user_id: int, team_type: str, user_character_ids: list):
    """
    Replaces the player's saved Attack or Defense team with up to 3
    owned Fighter copies. Validates every id actually belongs to this
    user and is a Fighter card before touching anything.
    """
    if team_type not in ("attack", "defense"):
        return {"ok": False, "reason": "invalid_team_type"}

    ids = list(dict.fromkeys(user_character_ids))[:3]  # de-dupe, cap at 3, same card can't fill two slots

    conn = get_connection()
    cur = conn.cursor()
    try:
        for ucid in ids:
            cur.execute("""
                SELECT 1 FROM user_characters uc
                JOIN fighter_stats fs ON fs.character_id = uc.character_id
                WHERE uc.id = ? AND uc.user_id = ?
            """, (ucid, user_id))
            if not cur.fetchone():
                return {"ok": False, "reason": "invalid_card"}

        cur.execute("DELETE FROM arena_teams WHERE user_id = ? AND team_type = ?", (user_id, team_type))
        for slot, ucid in enumerate(ids, start=1):
            cur.execute(
                "INSERT INTO arena_teams (user_id, team_type, slot, user_character_id) VALUES (?, ?, ?, ?)",
                (user_id, team_type, slot, ucid),
            )
        conn.commit()
        return {"ok": True, "team_type": team_type, "size": len(ids)}
    finally:
        conn.close()


# ---------------- Trophies & leagues ----------------

def get_trophies(user_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT trophies FROM arena_trophies WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["trophies"] if row else 0


def _league_index_for_trophies(trophies: int) -> int:
    idx = 0
    for i, entry in enumerate(ARENA_LEAGUES):
        if trophies >= entry["min"]:
            idx = i
        else:
            break
    return idx


def get_league_for_trophies(trophies: int) -> dict:
    return ARENA_LEAGUES[_league_index_for_trophies(trophies)]


def _apply_trophy_change(cur, user_id: int, delta: int):
    cur.execute("SELECT trophies FROM arena_trophies WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    current = row["trophies"] if row else 0
    new_value = max(0, current + delta)
    cur.execute("""
        INSERT INTO arena_trophies (user_id, trophies) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET trophies = excluded.trophies
    """, (user_id, new_value))


# ---------------- Matchmaking & battles ----------------

def _fighter_owner_pool(cur, exclude_user_id: int):
    """Every distinct user_id (other than exclude_user_id) who owns at
    least one Fighter card - only these can ever be picked as an
    opponent."""
    cur.execute("""
        SELECT DISTINCT uc.user_id FROM user_characters uc
        JOIN fighter_stats fs ON fs.character_id = uc.character_id
        WHERE uc.user_id != ?
    """, (exclude_user_id,))
    return [r["user_id"] for r in cur.fetchall()]


def find_arena_opponent(user_id: int):
    """
    Picks a random opponent for user_id. Most of the time stays in the
    player's own league; small independent chances to reach one league
    up or one league down instead - never further than that. Falls back
    to the player's own league, then to anyone with Fighter cards, if
    the preferred league has nobody in it. If literally nobody else owns
    a Fighter card yet, hands back a scripted NPC id instead of None, so
    the Arena isn't a dead feature for the first players in a fresh bot.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        candidates = _fighter_owner_pool(cur, user_id)
        if not candidates:
            return random.choice(ARENA_NPC_OPPONENTS)["id"]

        my_league_idx = _league_index_for_trophies(get_trophies(user_id))
        max_idx = len(ARENA_LEAGUES) - 1

        roll = random.random()
        if roll < ARENA_MATCH_UP_CHANCE and my_league_idx < max_idx:
            target_idx = my_league_idx + 1
        elif roll < ARENA_MATCH_UP_CHANCE + ARENA_MATCH_DOWN_CHANCE and my_league_idx > 0:
            target_idx = my_league_idx - 1
        else:
            target_idx = my_league_idx

        by_league = {}
        for cand in candidates:
            idx = _league_index_for_trophies(get_trophies(cand))
            by_league.setdefault(idx, []).append(cand)

        pool = by_league.get(target_idx) or by_league.get(my_league_idx) or candidates
        return random.choice(pool)
    finally:
        conn.close()


def get_pending_battle(user_id: int):
    """The caller's own in-progress (unresolved) battle, if they started
    one recently - so the Mini App can show a live countdown instead of
    letting them start a second fight on top of it."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM arena_battles WHERE attacker_id = ? AND resolved = 0
        ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_battle(battle_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM arena_battles WHERE id = ?", (battle_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_recent_battles(user_id: int, limit: int = 10):
    """Resolved battles this user was on either side of, most recent first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM arena_battles
        WHERE (attacker_id = ? OR defender_id = ?) AND resolved = 1
        ORDER BY id DESC LIMIT ?
    """, (user_id, user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def start_arena_battle(attacker_id: int, attacker_username: str = None):
    """
    Fight button: finds an opponent, computes both teams' total power
    right now (deterministic - no interaction happens during the 3
    minutes), and stores a battle that a background job resolves once
    ARENA_BATTLE_DURATION_SECONDS has passed. Returns {"ok": True, ...}
    or {"ok": False, "reason": ...}.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT 1 FROM arena_battles WHERE attacker_id = ? AND resolved = 0", (attacker_id,))
        if cur.fetchone():
            return {"ok": False, "reason": "battle_in_progress"}

        cur.execute("""
            SELECT uc.current_attack FROM arena_teams at
            JOIN user_characters uc ON uc.id = at.user_character_id
            WHERE at.user_id = ? AND at.team_type = 'attack'
            ORDER BY at.slot
        """, (attacker_id,))
        attack_rows = cur.fetchall()
        if len(attack_rows) < 3:
            return {"ok": False, "reason": "attack_team_incomplete"}
        attack_power = sum(r["current_attack"] or 0 for r in attack_rows)

        defender_id = find_arena_opponent(attacker_id)
        if defender_id is None:
            return {"ok": False, "reason": "no_opponent"}

        is_npc = defender_id in ARENA_NPC_IDS

        if is_npc:
            # No real account behind this id - nothing to look up in
            # arena_teams/user_characters. Scale its defense straight off
            # the challenger's own attack_power so the fight stays roughly
            # fair whether this is someone's first battle or their 500th.
            npc = next(n for n in ARENA_NPC_OPPONENTS if n["id"] == defender_id)
            defense_power = round(attack_power * npc["power_multiplier"])
        else:
            cur.execute("""
                SELECT uc.current_defense FROM arena_teams at
                JOIN user_characters uc ON uc.id = at.user_character_id
                WHERE at.user_id = ? AND at.team_type = 'defense'
                ORDER BY at.slot
            """, (defender_id,))
            defense_rows = cur.fetchall()

            if len(defense_rows) < 3:
                # No saved Defense Team: auto-pick 3 random Fighter cards from
                # the defender's own collection, for this battle only - never
                # persisted as their real team.
                cur.execute("""
                    SELECT uc.current_defense FROM user_characters uc
                    JOIN fighter_stats fs ON fs.character_id = uc.character_id
                    WHERE uc.user_id = ?
                """, (defender_id,))
                owned = cur.fetchall()
                if len(owned) < 3:
                    return {"ok": False, "reason": "opponent_no_defense"}
                defense_rows = random.sample(owned, 3)

            defense_power = sum(r["current_defense"] or 0 for r in defense_rows)

        if attack_power > defense_power:
            result = "attacker"
        elif defense_power > attack_power:
            result = "defender"
        else:
            result = "draw"

        attacker_league = get_league_for_trophies(get_trophies(attacker_id))

        if is_npc:
            # NPCs don't hold real trophies - the challenger still gains/
            # loses based on their own league, the NPC side just isn't
            # persisted anywhere.
            def_change = 0
            atk_change = attacker_league["victory"] if result == "attacker" \
                else (attacker_league["defeat"] if result == "defender" else 0)
        else:
            defender_league = get_league_for_trophies(get_trophies(defender_id))
            if result == "attacker":
                atk_change, def_change = attacker_league["victory"], defender_league["defeat"]
            elif result == "defender":
                atk_change, def_change = attacker_league["defeat"], defender_league["victory"]
            else:
                atk_change, def_change = 0, 0

        now = datetime.utcnow()
        resolves_at = now + timedelta(seconds=ARENA_BATTLE_DURATION_SECONDS)
        cur.execute("""
            INSERT INTO arena_battles
                (attacker_id, attacker_username, defender_id, attack_power, defense_power, result,
                 attacker_trophy_change, defender_trophy_change, started_at, resolves_at, resolved, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        """, (attacker_id, attacker_username, defender_id, attack_power, defense_power, result,
              atk_change, def_change, now.isoformat(), resolves_at.isoformat()))
        conn.commit()

        return {
            "ok": True,
            "battle_id": cur.lastrowid,
            "defender_id": defender_id,
            "attack_power": attack_power,
            "defense_power": defense_power,
            "result": result,
            "attacker_trophy_change": atk_change,
            "defender_trophy_change": def_change,
            "started_at": now.isoformat(),
            "resolves_at": resolves_at.isoformat(),
        }
    finally:
        conn.close()


def resolve_arena_battle(battle_id: int):
    """Applies the stored trophy changes and marks the battle resolved.
    Idempotent - safe to call more than once (e.g. after a restart)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # take the write lock BEFORE reading, so concurrent requests can't interleave
        cur.execute("SELECT * FROM arena_battles WHERE id = ?", (battle_id,))
        battle = cur.fetchone()
        if not battle or battle["resolved"]:
            return None

        _apply_trophy_change(cur, battle["attacker_id"], battle["attacker_trophy_change"])
        if battle["defender_id"] not in ARENA_NPC_IDS:
            _apply_trophy_change(cur, battle["defender_id"], battle["defender_trophy_change"])
        cur.execute("UPDATE arena_battles SET resolved = 1 WHERE id = ?", (battle_id,))
        conn.commit()

        cur.execute("SELECT * FROM arena_battles WHERE id = ?", (battle_id,))
        return cur.fetchone()
    finally:
        conn.close()


def mark_battle_notified(battle_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE arena_battles SET notified = 1 WHERE id = ?", (battle_id,))
    conn.commit()
    conn.close()


def get_overdue_unresolved_battles():
    """Battles whose resolves_at has already passed but never got
    resolved (e.g. the process restarted mid-wait) - picked up once at
    API startup so nobody's fight is silently lost."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("SELECT * FROM arena_battles WHERE resolved = 0 AND resolves_at <= ?", (now,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_unresolved_battles():
    """Every battle still waiting to resolve, overdue or not - used at
    API startup to re-schedule each one's remaining wait time so a
    restart doesn't lose the timer."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM arena_battles WHERE resolved = 0")
    rows = cur.fetchall()
    conn.close()
    return rows
