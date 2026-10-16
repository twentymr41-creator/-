"""
Player "memories" (milestones) and favorite-character affinity.

The goal: make the bot feel like it's adapting to each player's taste.
Every time a player checks a card, or organically obtains one (claims a
spawn with /get, receives a /gift, or buys on the Market), this module:

  1. Nudges an affinity score for that specific character up a little.
     Once a night (see run_nightly_engagement_loop), whichever character
     has the highest score gets cached as that player's "favorite" -
     get_favorite_character() reads that cache. Recomputing nightly
     instead of live keeps it feeling like a stable answer ("this is
     currently my favorite") rather than flipping mid-conversation.

  2. Checks whether the acquisition just crossed a one-time milestone:
       - first-ever card of a rarity tier
       - first-ever copy of that specific character
       - a round-number cumulative count of that rarity (10th, 100th...)
       - a round-number cumulative collection size (50th card ever, ...)
     Milestones are lifetime counters - selling a card later doesn't
     undo having reached 100 Legendaries once. They're also logged to
     user_memories with a timestamp, which is what /memories reads back.

record_acquisition() returns a list of ready-to-send HTML messages,
voiced as the player's favorite character reacting to the news. The
caller (bot.py for /get and /gift, api_server.py for Market buys) is
responsible for actually delivering them (DM, since this is meant to
feel like a personal note, not a group announcement).

run_nightly_engagement_loop() is meant to run once, forever, in a
background thread (see bot.py) - each pass it recomputes every player's
cached favorite and checks whether today is anyone's randomly-assigned
monthly gift day.
"""

import calendar
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

import config
import database as db
import economy

logger = logging.getLogger(__name__)

# Cumulative-count milestones checked both per-rarity and for the whole
# collection. Kept short on purpose - frequent enough to feel earned,
# not so frequent it starts to feel like a notification farm.
COUNT_MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

AFFINITY_CHECK = 1
AFFINITY_GET = 3
AFFINITY_GIFT_RECEIVED = 2
AFFINITY_MARKET_BUY = 3


def _ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _humanize_elapsed(seconds: float) -> str:
    if seconds < 3600:
        return "less than an hour"
    days = seconds / 86400
    if days < 1:
        hours = int(seconds / 3600)
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    if days < 30:
        d = int(days)
        return f"about {d} day{'s' if d != 1 else ''}"
    if days < 365:
        months = int(days / 30)
        return f"about {months} month{'s' if months != 1 else ''}"
    years = int(days / 365)
    return f"about {years} year{'s' if years != 1 else ''}"


def _format_date(iso_string: str) -> str:
    try:
        return datetime.fromisoformat(iso_string).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_string or "—"


def record_check(user_id: int, character_id: int):
    """Call from /check - a small affinity nudge just for looking a
    character up, no milestone logic involved."""
    db.bump_affinity(user_id, character_id, AFFINITY_CHECK)


def get_favorite_character(user_id: int):
    """The player's cached favorite (refreshed nightly - see
    run_nightly_engagement_loop). Falls back to a live calculation for a
    brand-new player whose first night hasn't happened yet, so the very
    first congratulation message still has someone to speak in."""
    cached = db.get_cached_favorite_character(user_id)
    if cached:
        return cached
    return db.get_top_affinity_character(user_id)


def _voice_line(user_id: int, tone: str) -> str:
    favorite = get_favorite_character(user_id)
    if favorite:
        name = favorite["name"]
        if tone == "congrats":
            return f"💬 <b>{name}</b> congratulated you on the progress!"
        if tone == "excited":
            return f"🌟 <b>{name}</b> is excited for what's ahead!"
        if tone == "proud":
            return f"💬 <b>{name}</b> is proud of how far you've come!"
    return "✨ A new chapter in your collection begins!"


def record_acquisition(user_id: int, character, source: str) -> list:
    """Call every time a player ORGANICALLY obtains a card - claiming a
    spawn (source="get"), receiving a /gift (source="gift_received"), or
    buying on the Market (source="market_buy"). Bumps that character's
    affinity and checks every milestone type. Returns a list of
    HTML-formatted messages for whatever milestone(s) just fired (often
    none, occasionally more than one - e.g. a rarity-count milestone and
    an overall-collection milestone landing on the same card)."""
    affinity_points = {
        "get": AFFINITY_GET,
        "gift_received": AFFINITY_GIFT_RECEIVED,
        "market_buy": AFFINITY_MARKET_BUY,
        "monthly_gift": AFFINITY_GET,
        "trade": AFFINITY_GET,
    }.get(source, 1)
    db.bump_affinity(user_id, character["id"], affinity_points)

    messages = []
    rarity_name = character["rarity_name"] or "Unranked"
    character_name = character["name"]

    # ---- First time ever getting THIS specific character ----
    if db.record_memory_if_new(user_id, f"first_character_{character['id']}", character["id"]):
        messages.append(
            f"✨ You got your first <b>{character_name}</b>! This one's going in the memory book. 💫"
        )

    # ---- First-ever card of this rarity ----
    first_rarity_key = f"first_rarity_{rarity_name}"
    is_first_of_rarity = db.record_memory_if_new(user_id, first_rarity_key, character["id"])
    if is_first_of_rarity:
        messages.append(
            f"💫 <b>{rarity_name}</b> milestone!\n"
            f"You just got your very first {rarity_name} card: <b>{character_name}</b>.\n\n"
            f"{_voice_line(user_id, 'excited')}"
        )

    # ---- Cumulative count-of-this-rarity milestone ----
    rarity_total = db.increment_lifetime_counter(user_id, f"rarity_total:{rarity_name}")
    if not is_first_of_rarity and rarity_total in COUNT_MILESTONES:
        db.record_memory_if_new(user_id, f"rarity_count_{rarity_name}_{rarity_total}", character["id"])
        since_text = ""
        first_memory = db.get_memory(user_id, first_rarity_key)
        if first_memory:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(first_memory["occurred_at"])).total_seconds()
            since_text = f"It's been {_humanize_elapsed(elapsed)} since your first {rarity_name} card, and now "
        messages.append(
            f"🎉 {rarity_total}{_ordinal_suffix(rarity_total)} <b>{rarity_name}</b> card milestone!\n"
            f"{since_text}you just added your {rarity_total}{_ordinal_suffix(rarity_total)}: <b>{character_name}</b>.\n\n"
            f"{_voice_line(user_id, 'congrats')}"
        )

    # ---- Overall collection-size milestone ----
    collection_total = db.increment_lifetime_counter(user_id, "collection_total")
    if collection_total in COUNT_MILESTONES:
        db.record_memory_if_new(user_id, f"collection_count_{collection_total}", character["id"])
        messages.append(
            f"🌌 Constellation milestone!\n"
            f"You've now collected {collection_total} cards total.\n\n"
            f"{_voice_line(user_id, 'proud')}"
        )

    return messages


# ---------------- /memories ----------------

_MEMORY_PATTERNS = [
    (re.compile(r"^first_character_(\d+)$"), "character"),
    (re.compile(r"^first_rarity_(.+)$"), "first_rarity"),
    (re.compile(r"^rarity_count_(.+)_(\d+)$"), "rarity_count"),
    (re.compile(r"^collection_count_(\d+)$"), "collection_count"),
    (re.compile(r"^monthly_gift_(\d{4})-(\d{2})$"), "monthly_gift"),
]


def _format_memory_line(row) -> str:
    key = row["memory_key"]
    date_str = _format_date(row["occurred_at"])

    for pattern, kind in _MEMORY_PATTERNS:
        m = pattern.match(key)
        if not m:
            continue
        if kind == "character":
            character = db.get_character(int(m.group(1)))
            name = character["name"] if character else "a character"
            return f"🌟 First <b>{name}</b> — {date_str}"
        if kind == "first_rarity":
            return f"💫 First <b>{m.group(1)}</b> card — {date_str}"
        if kind == "rarity_count":
            rarity, n = m.group(1), int(m.group(2))
            return f"🎉 {n}{_ordinal_suffix(n)} <b>{rarity}</b> card — {date_str}"
        if kind == "collection_count":
            n = int(m.group(1))
            return f"🌌 {n} cards collected — {date_str}"
        if kind == "monthly_gift":
            month_name = calendar.month_name[int(m.group(2))]
            return f"🎁 {month_name} monthly gift — {date_str}"

    return f"• {key} — {date_str}"


def format_memories_text(user_id: int) -> str:
    rows = db.list_user_memories(user_id, limit=30)
    if not rows:
        return "📖 No memories yet - your milestones will show up here as you play!"

    lines = ["📖 <b>Your Memories</b>\n"]
    lines.extend(_format_memory_line(row) for row in rows)
    if len(rows) == 30:
        lines.append("\n(showing your 30 most recent)")
    return "\n".join(lines)


# ---------------- Nightly engagement job ----------------

def _send_telegram_message_sync(chat_id: int, text: str):
    """A plain synchronous DM, used from the background thread this job
    runs in (mirrors api_server.py's async _send_telegram_message, which
    can't be used here since there's no event loop on this thread)."""
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.URLError:
        logger.exception("Failed to send nightly engagement message to %s", chat_id)


def recompute_all_favorites():
    """Refreshes the cached favorite character for every player who has
    any affinity history. Run once a night."""
    now = datetime.utcnow().isoformat()
    for user_id in db.list_users_with_affinity():
        top = db.get_top_affinity_character(user_id)
        if top:
            db.set_cached_favorite_character(user_id, top["character_id"], now)


def _assigned_gift_day(user_id: int, year: int, month: int, days_in_month: int) -> int:
    """A stable (not re-randomized on every run) day of the month for this
    player, derived from their id and the current year/month - so the
    check below lands on exactly one day per month per player without
    needing to store a schedule anywhere."""
    digest = hashlib.sha256(f"{user_id}-{year}-{month}".encode()).hexdigest()
    return 1 + (int(digest, 16) % days_in_month)


def check_monthly_gifts() -> list:
    """Checks whether today is any player's randomly-assigned monthly
    gift day, and gives out a random card from MONTHLY_GIFT_RARITY_TIERS
    to each one who's due. Returns a list of (user_id, message) pairs
    for the caller to actually send. Run once a night."""
    today = datetime.utcnow().date()
    year, month, day = today.year, today.month, today.day
    days_in_month = calendar.monthrange(year, month)[1]
    year_month_key = f"{year}-{month:02d}"

    all_rarities = db.list_rarities()
    eligible_rarity_ids = [
        r["id"] for r in all_rarities if economy.match_price_tier(r["name"]) in config.MONTHLY_GIFT_RARITY_TIERS
    ]

    results = []
    for row in db.list_bot_users():
        user_id = row["user_id"]
        if db.get_lifetime_counter(user_id, "collection_total") <= 0:
            continue  # only players who've actually collected something
        if _assigned_gift_day(user_id, year, month, days_in_month) != day:
            continue
        if not db.record_memory_if_new(user_id, f"monthly_gift_{year_month_key}", None):
            continue  # already gifted this month (also guards re-running same day)

        # If their favorite character happens to sit in one of the
        # eligible tiers, gift that specific character instead of a
        # random one - otherwise fall back to a random pick.
        character = None
        favorite = get_favorite_character(user_id)
        if favorite and economy.match_price_tier(favorite["rarity_name"]) in config.MONTHLY_GIFT_RARITY_TIERS:
            character = db.get_character(favorite["character_id"])
        if not character:
            character = db.get_random_character_in_rarity_ids(eligible_rarity_ids)
        if not character:
            continue

        username = row["username"] or row["first_name"]
        db.give_character_to_user(user_id, username, character["id"])
        is_favorite_pick = bool(favorite) and character["id"] == favorite["character_id"]
        if is_favorite_pick:
            message = (
                f"🎁 <b>Monthly Gift!</b>\n\n"
                f"They noticed you love <b>{character['name']}</b> - here's another one, straight from "
                f"the <b>{character['rarity_name']}</b> tier: ({character['series']}).\n\nEnjoy! 💫"
            )
        else:
            message = (
                f"🎁 <b>Monthly Gift!</b>\n\n"
                f"A surprise <b>{character['rarity_name']}</b> card just for you: "
                f"<b>{character['name']}</b> ({character['series']}).\n\nEnjoy! 💫"
            )
        results.append((user_id, message))
        # A monthly gift still counts as a real acquisition - it can
        # itself complete a rarity/collection milestone (e.g. their
        # first-ever Sovereign card), so run it through the same checks.
        for extra_message in record_acquisition(user_id, character, "monthly_gift"):
            results.append((user_id, extra_message))

    return results


def check_birthday_gifts() -> list:
    """Checks whether today is any confirmed birthday, and gives out a
    random config.BIRTHDAY_GIFT_RARITY_NAME card to each player who's due
    (i.e. hasn't already been gifted this calendar year - covers both the
    very first year, where /birthday itself already gave the immediate
    gift and stamped last_birthday_gift_year, and every year after that,
    which is entirely this function's job). Returns (user_id, message)
    pairs for the caller to send, same shape as check_monthly_gifts()."""
    today = datetime.utcnow().date()
    year, month, day = today.year, today.month, today.day

    rarity = db.get_rarity_by_name(config.BIRTHDAY_GIFT_RARITY_NAME)
    if not rarity:
        return []

    results = []
    for row in db.list_users_with_birthday_today(month, day):
        user_id = row["user_id"]
        if row["last_birthday_gift_year"] == year:
            continue  # already gifted this year

        character = db.get_random_character_in_rarity_ids([rarity["id"]])
        if not character:
            continue

        username = row["username"] or row["first_name"]
        db.give_character_to_user(user_id, username, character["id"])
        db.mark_birthday_gifted(user_id, year)

        message = (
            f"🎂 <b>Happy Birthday!</b>\n\n"
            f"Your yearly <b>{config.BIRTHDAY_GIFT_RARITY_NAME}</b> gift: "
            f"<b>{character['name']}</b> ({character['series']}).\n\nHave a great one! 🎉"
        )
        results.append((user_id, message))
        for extra_message in record_acquisition(user_id, character, "birthday_gift"):
            results.append((user_id, extra_message))

    return results


def run_nightly_engagement_loop():
    """Runs forever in a background thread: once roughly every 24 hours,
    refresh cached favorites and check for monthly/birthday gifts."""
    while True:
        try:
            recompute_all_favorites()
        except Exception:
            logger.exception("Nightly favorite recompute failed")

        try:
            for user_id, message in check_monthly_gifts():
                _send_telegram_message_sync(user_id, message)
        except Exception:
            logger.exception("Monthly gift check failed")

        try:
            for user_id, message in check_birthday_gifts():
                _send_telegram_message_sync(user_id, message)
        except Exception:
            logger.exception("Birthday gift check failed")

        time.sleep(config.NIGHTLY_ENGAGEMENT_INTERVAL_SECONDS)
