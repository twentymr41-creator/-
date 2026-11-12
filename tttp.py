"""
tttp - the artist currency.

* reward_for_rarity(): how much tttp a card of a given rarity earns its admin.
* format_info_message(): the English text behind /http.
* run_premium_loop(): background thread. At 00:00 Iran time every admin who
  earned at least config.TTTP_PREMIUM_DAILY_THRESHOLD tttp (from adding cards)
  during the day that just ended gets premium until the next 00:00 Iran time.
  Keep hitting the target and the cycle simply continues night after night.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import database as db
import economy

logger = logging.getLogger(__name__)


def reward_for_rarity(rarity_name) -> int:
    """tttp earned for adding a card of this rarity (0 for unranked / custom rarities)."""
    tier = economy.match_price_tier(rarity_name) if rarity_name else None
    return config.TTTP_RARITY_REWARDS.get(tier, 0) if tier else 0


def format_info_message() -> str:
    lines = [
        "🎨 <b>tttp — the Artist Currency</b>\n",
        "<b>What is it?</b>",
        f"tttp is a special currency for the bot's artists (admins). "
        f"<b>1 {config.TTTP_SYMBOL} = {config.TTTP_VALUE_IN_VY} {config.CURRENCY_SYMBOL}</b>, "
        f"so a card worth 200 {config.CURRENCY_SYMBOL} costs 10 {config.TTTP_SYMBOL}.\n",
        "<b>How do you earn it?</b>",
        "Every time an admin (of any type) adds a card with <code>/addcharacter</code>, "
        "or one of their <code>/send</code> submissions gets approved, they receive tttp "
        "based on the card's rarity:",
    ]
    for tier, amount in config.TTTP_RARITY_REWARDS.items():
        emoji = config.RARITY_PRICE_EMOJIS.get(tier, "")
        lines.append(f"{emoji} {tier}: <b>{amount}</b>")

    lines += [
        "",
        "<b>Where can you use it?</b>",
        f"Everywhere {config.CURRENCY_SYMBOL} works: "
        f"<code>/give [amount] {config.TTTP_SYMBOL}</code>, "
        f"<code>/vypay [amount] {config.TTTP_SYMBOL}</code>, "
        f"<code>/player</code>, <code>/inv</code> and "
        f"<code>/top {config.TTTP_SYMBOL}</code>.\n",
        "<b>How is it spent?</b>",
        f"When you buy something, your {config.TTTP_SYMBOL} is used first. "
        f"Whatever it can't cover is taken from your {config.CURRENCY_SYMBOL}. "
        f"If you have no {config.TTTP_SYMBOL}, the whole price comes out of your {config.CURRENCY_SYMBOL}. "
        f"Sellers always receive their price in {config.CURRENCY_SYMBOL}.\n",
        "<b>Daily premium</b>",
        f"Any admin who earns <b>{config.TTTP_PREMIUM_DAILY_THRESHOLD} {config.TTTP_SYMBOL}</b> "
        "from adding cards in one day (before 00:00 Iran time) automatically gets "
        "<b>24 hours of premium</b> at 00:00. Reach the target again the next day and "
        "the cycle continues; miss a day and the premium simply runs out. "
        f"Only {config.TTTP_SYMBOL} earned from cards counts toward this target — "
        f"received or gifted {config.TTTP_SYMBOL} does not.",
    ]
    return "\n".join(lines)


# ---------------- nightly premium ----------------

def _iran_now() -> datetime:
    return datetime.now(db.IRAN_TZ)


def _next_iran_midnight(now: datetime) -> datetime:
    tomorrow = (now + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=db.IRAN_TZ)


def _is_any_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID or bool(db.get_permissions(user_id))


def _send_dm(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=10)
    except Exception:
        logger.warning("Could not DM the tttp premium notice to %s", chat_id)


def run_premium_cycle():
    """
    Grants premium for the Iran day that just ended - once per day, however
    often it's called. Returns [(user_id, earned_amount)] of who was granted.
    """
    now = _iran_now()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    if db.tttp_premium_run_done(yesterday):
        return []

    expires_utc = _next_iran_midnight(now).astimezone(timezone.utc).replace(tzinfo=None)
    granted = []
    for user_id, amount in db.get_tttp_earners(yesterday, config.TTTP_PREMIUM_DAILY_THRESHOLD):
        if not _is_any_admin(user_id):  # lost their admin access since earning it
            continue
        if db.grant_premium_until(user_id, expires_utc):
            granted.append((user_id, amount))
    db.mark_tttp_premium_run(yesterday)
    return granted


def run_premium_loop():
    """Background thread: checks every few minutes (cheap - one lookup), so a
    restart around midnight can't make the bot miss a day."""
    while True:
        try:
            for user_id, amount in run_premium_cycle():
                _send_dm(
                    user_id,
                    f"⭐️ You earned <b>{amount} {config.TTTP_SYMBOL}</b> yesterday, so you now have "
                    "<b>24 hours of premium</b>, until 00:00 Iran time tonight.\n"
                    f"Earn {config.TTTP_PREMIUM_DAILY_THRESHOLD} {config.TTTP_SYMBOL} again today to keep it going!",
                )
        except Exception:
            logger.exception("tttp premium cycle failed")
        time.sleep(300)
