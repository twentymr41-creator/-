"""
Bot configuration.
Fill in your BOT_TOKEN below (get it from @BotFather on Telegram),
OR (recommended on Railway) set it as an environment Variable instead -
that takes priority automatically.
"""

import os

# --- Required ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# Telegram numeric user ID allowed to use admin commands
# (/addcharacter, /addrarity)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8392724333"))

# --- Secondary-admin permissions (/admin, /addadmin) ---
# Each secondary admin can independently hold any combination of these -
# toggle them from /admin's "Give access" / "Take access" buttons, one
# specific command at a time. "key" is what's stored in the database (keep
# it in sync with the has_command_permission() calls in bot.py); "label" and
# "description" are only for display.
ADMIN_PERMISSIONS = [
    {"key": "ADDCHARACTER", "label": "/addcharacter", "description": "Add a new character card."},
    {"key": "REMOVECHARACTER", "label": "/removecharacter", "description": "Remove a character card."},
    {"key": "EDITCHARACTER", "label": "/editcharacter", "description": "Edit an existing character's details."},
    {"key": "ADDRARITY", "label": "/addrarity", "description": "Create or update a rarity tier."},
    {"key": "REMOVERARITY", "label": "/removerarity", "description": "Remove a rarity tier."},
    {"key": "EDITRARITY", "label": "/editrarity", "description": "Change a rarity tier's weight."},
    {"key": "NEW", "label": "/new", "description": "Publish a post to the announcement channel."},
    {"key": "ADDEVENT", "label": "/addevent", "description": "Register a new event name."},
    {"key": "REMOVEEVENT", "label": "/removeevent", "description": "Unregister an event."},
    {"key": "REVIEWSUBMISSIONS", "label": "Review /send", "description": "Approve or reject /send submissions."},
    {"key": "FORCESPAWN", "label": "/forcespawn", "description": "Instantly spawn a random character."},
    {"key": "LOCKSPAWN", "label": "/lockspawn", "description": "Stop a rarity or event from spawning."},
    {"key": "UNLOCKSPAWN", "label": "/unlockspawn", "description": "Re-allow a locked rarity or event to spawn."},
    {"key": "BAN", "label": "/ban", "description": "Ban a player from using the bot."},
    {"key": "UNBAN", "label": "/unban", "description": "Lift a player's ban."},
    {"key": "SICK", "label": "/sick out", "description": "Send the canned public burn - purely cosmetic."},
    {"key": "GIVE", "label": "/give", "description": "Give a player a card or currency."},
    {"key": "PLAYER", "label": "/player", "description": "Manage a player's cards and currency."},
    {"key": "SETPREMIUM", "label": "/setpremium", "description": "Grant a player premium."},
    {"key": "REMOVEPREMIUM", "label": "/removepremium", "description": "Revoke a player's premium."},
    {"key": "SETSELLPRICE", "label": "/setsellprice", "description": "Set the /sellbot payout for a rarity."},
    {"key": "BIN", "label": "/bin", "description": "Browse and restore recently deleted items."},
]

# Historical bundle shortcuts for /addadmin's old "artist/manager/marzieh"
# syntax, and for migrating anyone who held one of those tiers before this
# per-command system existed (see the migration in database.py's init_db).
# Each bundle lists exactly the permission keys that old tier used to imply.
_ARTIST_BUNDLE = ["ADDCHARACTER", "REMOVECHARACTER", "EDITCHARACTER", "ADDRARITY", "REMOVERARITY", "EDITRARITY"]
_MANAGER_BUNDLE = _ARTIST_BUNDLE + ["NEW", "ADDEVENT", "REMOVEEVENT"]
_MARZIEH_BUNDLE = _MANAGER_BUNDLE + [
    "REVIEWSUBMISSIONS", "FORCESPAWN", "LOCKSPAWN", "UNLOCKSPAWN", "BAN", "UNBAN", "SICK",
    "GIVE", "PLAYER", "SETPREMIUM", "REMOVEPREMIUM", "SETSELLPRICE", "BIN",
]
ADMIN_TYPE_BUNDLES = {
    "ARTIST": _ARTIST_BUNDLE,
    "MANAGER": _MANAGER_BUNDLE,
    "MARZIEH": _MARZIEH_BUNDLE,
}

# Channel every newly added character card gets posted to (bot must be admin there)
ARCHIVE_CHANNEL = "@gettersArchivum"

# --- Spawn settings ---
MESSAGES_NEEDED_TO_SPAWN = 100      # messages required before a spawn

# --- Anti-spam settings ---
MAX_CONSECUTIVE_MESSAGES = 10       # more than this in a row = spam
SPAM_PUNISHMENT_MINUTES = 15        # minutes banned from claiming + excluded from spawn counter
SPAM_STREAK_RESET_MINUTES = 15      # a gap this long (or more) between messages starts the streak count over from 0

# User IDs that are never muted for spam - they still get a friendly
# heads-up if they'd otherwise have tripped the spam threshold.
SPAM_MUTE_EXEMPT_USER_IDS = {8235775970}

# --- Daily capture limit ---
DAILY_CAPTURE_LIMIT = 30            # max characters a player can claim per day (resets at local midnight)
PREMIUM_DAILY_CAPTURE_LIMIT = 50    # same, for premium players

# --- Dart game ---
DAILY_DART_LIMIT = 5                # max dart throws per player per day (resets at local midnight)
PREMIUM_DAILY_DART_LIMIT = 10       # same, for premium players
DART_REWARD_BULLSEYE = 15           # dead center (dice value 6)
DART_REWARD_RING_TWO = 10           # second red ring (dice value 5)
DART_REWARD_RING_THREE = 5          # third/outer ring (dice value 3 or 4)
DART_REWARD_MISS = 0                # missed the board entirely (dice value 1 or 2)

# --- Mini App daily bonus (see /api/tasks/daily-claim) ---
DAILY_TASK_BONUS = 15               # flat currency, once per calendar day (UTC)
PREMIUM_DAILY_TASK_BONUS = 50       # same, for premium players

# --- Currency ---
# Unit symbol appended after every currency amount shown to players.
CURRENCY_SYMBOL = "VɎ"

# --- Mini App / Market API ---
# The Waifu Market Mini App's deployed URL (e.g. https://waifu-market.vercel.app),
# once you have one. Locks down the API's CORS to just that origin instead
# of allowing any website to call it. Leave unset while testing locally.
MINI_APP_URL = os.environ.get("MINI_APP_URL", "")

# The Mini App's direct link inside Telegram (BotFather -> your bot -> Mini App).
# /miniapp, /market and the Mini App buttons of /new posts open the app through it -
# Telegram only allows the normal "web app" button in PRIVATE chats, and only when
# MINI_APP_URL is set. Override with the MINI_APP_DIRECT_LINK Variable if the link
# ever changes (e.g. https://t.me/YourBotUsername/appname).
MINI_APP_DIRECT_LINK = os.environ.get("MINI_APP_DIRECT_LINK", "https://t.me/Character_getter_bot?startapp")

# --- Database ---
# On Railway, set the DB_PATH Variable to a path inside your mounted
# Volume (e.g. /data/bot_database.db) so data survives restarts.
DB_PATH = os.environ.get("DB_PATH", "bot_database.db")

# Default weight used for a character that has no rarity assigned yet
DEFAULT_CHARACTER_WEIGHT = 10

# --- Fighter / Arena (Update 2) ---
# The event name that flags a character as a Fighter (Arena-eligible).
# Auto-registered as a normal event on first startup - see init_db().
FIGHTER_EVENT_NAME = "Fighter"

# key -> {label shown on buttons/cards, attack upgrade multiplier, defense upgrade multiplier}
ELEMENTS = {
    "fire":  {"label": "🔥 Fire",  "emoji": "🔥", "attack_mult": 1.20, "defense_mult": 1.08},
    "water": {"label": "💧 Water", "emoji": "💧", "attack_mult": 1.12, "defense_mult": 1.12},
    "wind":  {"label": "🌪 Wind",  "emoji": "🌪", "attack_mult": 1.17, "defense_mult": 1.10},
    "dark":  {"label": "🌑 Dark",  "emoji": "🌑", "attack_mult": 1.25, "defense_mult": 1.05},
    "light": {"label": "☀ Light", "emoji": "☀", "attack_mult": 1.08, "defense_mult": 1.20},
}

FIGHTER_MAX_LEVEL = 15

# How long an Arena battle takes to resolve after Fight is pressed.
ARENA_BATTLE_DURATION_SECONDS = 3 * 60

# Chance the matchmaker looks one league UP or DOWN instead of the
# player's own league (each checked independently; the remainder stays
# in-league). No league beyond +-1 is ever selected.
ARENA_MATCH_UP_CHANCE = 0.10
ARENA_MATCH_DOWN_CHANCE = 0.10

# Ordered lowest -> highest. "min" is the trophy count where the league
# begins. victory/defeat are how many trophies a player in THIS league
# gains on a win / loses on a loss (each side of a battle is scored
# against their OWN league, not the opponent's).
ARENA_LEAGUES = [
    {"key": "novice",     "name": "Novice",          "emoji": "🌱", "min": 0,    "victory": 30, "defeat": -10},
    {"key": "waifu_fan",  "name": "Waifu Fan",       "emoji": "🌸", "min": 300,  "victory": 28, "defeat": -12},
    {"key": "collector",  "name": "Collector",       "emoji": "💖", "min": 700,  "victory": 26, "defeat": -14},
    {"key": "elite",      "name": "Elite Collector", "emoji": "✨", "min": 1200, "victory": 24, "defeat": -16},
    {"key": "diamond",    "name": "Diamond Heart",   "emoji": "💎", "min": 1800, "victory": 22, "defeat": -18},
    {"key": "lord",       "name": "Waifu Lord",      "emoji": "👑", "min": 2600, "victory": 20, "defeat": -20},
    {"key": "emperor",    "name": "Waifu Emperor",   "emoji": "🌟", "min": 3600, "victory": 18, "defeat": -22},
    {"key": "legend",     "name": "Eternal Legend",  "emoji": "🔥", "min": 5000, "victory": 15, "defeat": -25},
]

# Fallback opponents used only when find_arena_opponent() finds literally
# no other player who owns a Fighter card yet - keeps the Arena playable
# for the very first players instead of /fight always failing with
# "no_opponent". Negative ids so they can never collide with a real
# Telegram user id. power_multiplier scales the NPC's defense_power off
# whatever the challenger's own attack_power is for that fight (not off
# a fixed number), so the match stays roughly fair at any stage.
ARENA_NPC_OPPONENTS = [
    {"id": -1, "name": "🎯 Training Dummy",  "power_multiplier": 0.6},
    {"id": -2, "name": "🎲 Random Waifu",    "power_multiplier": 0.9},
    {"id": -3, "name": "🤖 NPC Collector",   "power_multiplier": 1.0},
    {"id": -4, "name": "🛡️ Arena Guardian", "power_multiplier": 1.3},
]
ARENA_NPC_IDS = {npc["id"] for npc in ARENA_NPC_OPPONENTS}

# ==================== Dynamic rarity economy (/prices) ====================
# "Day one" price range for each rarity tier, in CURRENCY_SYMBOL. The live
# price /prices shows drifts away from these over time based on real
# activity (see economy.py) but is always kept within
# PRICE_FLOOR_MULTIPLIER..PRICE_CEILING_MULTIPLIER of these numbers.
# Listed from the cheapest to the priciest tier - /prices shows them in this
# order and /rarities uses it (reversed) to put the rarest tier first.
RARITY_BASE_PRICES = {
    "Common":    (10, 15),
    "Rare":      (12, 20),
    "Mystic":    (20, 24),
    "Legendary": (50, 80),
    "Elysian":   (120, 250),
    "Prismatic": (400, 700),
    "Nocturne":  (1500, 1700),
    "Ethereal":  (2000, 2500),
    "Sovereign": (3000, 4000),
    "Aevoria":   (7000, 9000),
    "Celestial": (10000, 12000),
    "Singular":  (17000, 20000),
    "Omnara":    (20000, 30000),
}

RARITY_PRICE_EMOJIS = {
    "Common": "⚪", "Rare": "🟠", "Mystic": "🟢", "Legendary": "🟡",
    "Elysian": "🪻", "Prismatic": "💎", "Nocturne": "🌙", "Ethereal": "🫧",
    "Sovereign": "👑", "Aevoria": "🪽", "Celestial": "🌌", "Singular": "🪐", "Omnara": "💫",
}

# How often (seconds) the live prices above get recalculated.
PRICE_UPDATE_INTERVAL_SECONDS = 60 * 60

# Weight each economic signal gets when computing an hourly price move -
# higher weight means that activity pushes the price harder.
PRICE_GIFT_WEIGHT = 1.0          # a card of that rarity being /gift-ed
PRICE_CHECK_WEIGHT = 0.3         # a card of that rarity being looked up with /check
PRICE_MARKET_SOLD_WEIGHT = 2.0   # a card of that rarity actually selling on /market (strongest signal)

# A single user spamming /check on the same rarity over and over only
# counts once toward its price within this window - otherwise /check
# activity would be a free, repeatable lever to pump a rarity's price.
PRICE_CHECK_DEDUP_WINDOW_SECONDS = 60 * 60  # 1 hour

# Scarcity: a tier with fewer total cards than this "typical" count gets
# its price pushed up harder by the same amount of activity; more cards
# than this dampens the push. This is the "fewer cards = pricier" rule.
PRICE_SCARCITY_REFERENCE_COUNT = 20
PRICE_SCARCITY_MIN_MULTIPLIER = 0.5
PRICE_SCARCITY_MAX_MULTIPLIER = 3.0

# Converts the combined activity score into a percentage price change,
# capped so no tier can swing too hard in a single hour.
PRICE_SENSITIVITY = 0.01
PRICE_MAX_HOURLY_CHANGE_PCT = 0.15   # max 15% up or down per hourly tick
PRICE_IDLE_DECAY_PCT = 0.02          # drifts 2%/hour back toward base with zero activity

# Live price can never drift further than this multiple of the base price,
# in either direction - keeps the economy from running away permanently.
PRICE_FLOOR_MULTIPLIER = 0.5
PRICE_CEILING_MULTIPLIER = 2.0

# Event price-boost tiers, shown by the /prices command's "event bonuses"
# button. Informational only for now - not yet applied to any actual sale.
EVENT_PRICE_TIERS = [
    {
        "label": "🌟 S+ Tier",
        "range": "+80%-120%",
        "events": ["🌌𝗔𝘀𝘁𝗿𝗮𝗹𝗶𝘀🌌", "🪽𝗦𝗲𝗿𝗮𝗽𝗵𝗶𝗺🪽", "☢️𝗔𝗽𝗼𝗰𝗮𝗹𝘆𝗽𝘀𝗲☢️", "🏛️𝗢𝗹𝘆𝗺𝗽𝘂𝘀🏛️", "🌊𝗔𝘁𝗹𝗮𝗻𝘁𝗶𝘀🌊", "🧷𝗙𝗮𝗸𝗲 𝗖𝗼𝗹𝗹𝗮𝗴𝗲🧷"],
    },
    {
        "label": "⭐ S Tier",
        "range": "+50%-80%",
        "events": ["🦇𝗩𝗮𝗺𝗽𝘆𝗿𝗶𝗮🦇", "🔥𝗜𝗻𝗳𝗲𝗿𝗻𝗼🔥", "🦊𝗞𝗶𝘁𝘀𝘂𝗻𝗲🦊", "🥷𝗦𝗵𝗶𝗻𝗼𝗯𝗶🥷", "🏴‍☠️𝗣𝗶𝗿𝗮𝘁𝗲𝘀🏴‍☠️", "🎭𝗠𝗮𝘀𝗾𝘂𝗲𝗿𝗮𝗱𝗲🎭", "🌫𝗗𝗿𝘂𝗸𝗮𝗲🌫", "🍃𝗡𝗮𝘁𝘂𝗿𝗲🍃"],
    },
    {
        "label": "💎 A Tier",
        "range": "+30%-50%",
        "events": ["💘𝗔𝗺𝗼𝗿𝗶𝗮💘", "🕯️𝗣𝗵𝗮𝗻𝘁𝗼𝗺🕯️", "⚔️𝗦𝗮𝗺𝘂𝗿𝗮𝗶⚔️", "💿𝗡𝗲𝗼𝗻💿", "🎃𝗛𝗮𝗹𝗹𝗼𝘄🎃", "🎄𝗘𝘃𝗲𝗿𝗴𝗿𝗲𝗲𝗻🎄", "📸𝗗𝗼𝗹𝗰𝗲 & 𝗚𝗮𝗯𝗯𝗮𝗻𝗮📸"],
    },
    {
        "label": "🟢 B Tier",
        "range": "+15%-30%",
        "events": ["🍽️𝗚𝗮𝘀𝘁𝗿𝗶𝗮🍽️", "🌸𝗛𝗮𝗻𝗮𝗺𝗶🌸", "🃏𝗝𝗲𝘀𝘁𝗲𝗿🃏", "🐰𝗕𝘂𝗻𝗻𝘆🐰", "☀️𝗖𝗼𝘸𝗯𝗼𝘆☀️", "🍾𝗕𝗮𝗿𝘁𝗲𝗻𝗱𝗲𝗿🍾", "🪭𝗣𝗮𝗽𝗮𝗿𝗮𝘇𝘇𝗶🪭"],
    },
    {
        "label": "⚪ C Tier",
        "range": "+5%-15%",
        "events": ["🎮𝗚𝗮𝗺𝗲𝗿🎮", "📚𝗦𝗰𝗵𝗼𝗼𝗹📚", "🏓𝗦𝗽𝗼𝗿𝘁🏓", "😭𝗖𝗿𝘆😭", "🛡𝗙𝗶𝗴𝗵𝘁𝗲𝗿🛡"],
    },
]

# ==================== Player memories & nightly engagement ====================
# How often (seconds) the nightly job runs. It handles two things each
# pass: recomputing every player's cached "favorite character" (see
# memories.get_favorite_character), and checking whether today is
# anyone's randomly-assigned monthly gift day.
NIGHTLY_ENGAGEMENT_INTERVAL_SECONDS = 24 * 60 * 60

# Rarity tier the monthly gift draws from. One random card from this
# tier is given to every player once a month, on a random day that's
# stable per-player (see memories._assigned_gift_day) so it's not the
# same day for everyone. Previously also included Elysian/Prismatic/
# Sovereign - narrowed to just Nocturne now that Sovereign is reserved
# for the /birthday gift instead.
MONTHLY_GIFT_RARITY_TIERS = ["Nocturne"]

# Rarity given out by /birthday - once immediately on confirmation, then
# automatically every year afterward on the same date.
BIRTHDAY_GIFT_RARITY_NAME = "Sovereign"

# ==================== Mini App premium themes ====================
# Theme ids the Mini App accepts from /api/settings/theme. "default" is
# always allowed for everyone; every other id requires premium.
PREMIUM_THEMES = ["seraphim", "tenebris"]

# ==================== Chat tab AI (Update 3) ====================
# The Chat tab's per-character replies go through any OpenAI Chat
# Completions-compatible service (OpenRouter, OpenAI, Groq, DeepSeek,
# a local Ollama, ...) - same approach as the standalone AI bot this
# was adapted from (see chat_ai.py). Only AI_API_KEY is required to
# turn the feature on; everything else has a sane default.
# Set AI_API_KEY as an environment variable (e.g. in Railway's Variables
# tab) - never hardcode a real key here, it would end up in git history.
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_API_BASE_URL = os.environ.get("AI_API_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "openrouter/free")
# Auto-swapped in, once, if AI_MODEL ever 404s (free OpenRouter models get
# retired without notice) - see ai_client.py.
AI_FALLBACK_MODEL = os.environ.get("AI_FALLBACK_MODEL", "openrouter/free")
AI_TEMPERATURE = float(os.environ.get("AI_TEMPERATURE", "0.9"))
AI_REQUEST_TIMEOUT = float(os.environ.get("AI_REQUEST_TIMEOUT", "60"))
# How many of the most recent chat_messages rows (player + character
# combined) get sent as context on every reply. Keeps prompts (and
# cost) bounded on long-running conversations.
AI_MAX_HISTORY_MESSAGES = int(os.environ.get("AI_MAX_HISTORY_MESSAGES", "20"))

