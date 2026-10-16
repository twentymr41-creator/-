"""
Dynamic rarity economy for /prices.

Every rarity tier has a "base" price range (config.RARITY_BASE_PRICES) and
a live price range that drifts away from it over time based on real
in-game activity:
    - how many cards of that rarity got /gift-ed to someone
    - how many times cards of that rarity got looked up with /check
    - how many cards of that rarity actually sold on the /market
    - how many cards of that rarity exist in total (fewer cards = pricier)

run_price_update_cycle() recomputes every tier's live price and is meant
to be called once at startup and then every config.PRICE_UPDATE_INTERVAL_SECONDS
- see run_price_update_loop(), which bot.py runs in a background thread.
"""

import logging
import re
import time
from datetime import datetime, timedelta

import config
import database as db

logger = logging.getLogger(__name__)

# Precomputed: "commonly typed variant" -> canonical tier name, so whatever
# raw text the owner actually used in /addrarity (with or without an emoji
# prefix, any case) still maps to the right row in config.RARITY_BASE_PRICES.
def _normalize(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]", "", name.lower())


_NORMALIZED_TIERS = {_normalize(name): name for name in config.RARITY_BASE_PRICES}


def match_price_tier(raw_rarity_name: str):
    """Maps a raw rarities.name value to one of the canonical economy tiers
    in config.RARITY_BASE_PRICES, or None if it's a custom rarity the pricing
    system doesn't manage."""
    return _NORMALIZED_TIERS.get(_normalize(raw_rarity_name))


def ensure_prices_seeded():
    """Gives every canonical tier a starting row (at its base price) the
    first time it's ever seen - safe to call every cycle. Returns the set
    of tier names that were freshly seeded just now (so the caller can
    skip applying a price adjustment to them on this same pass)."""
    existing = db.get_rarity_market_prices()
    now = datetime.utcnow().isoformat()
    newly_seeded = set()
    for name, (base_min, base_max) in config.RARITY_BASE_PRICES.items():
        if name not in existing:
            db.set_rarity_market_price(name, base_min, base_max, now)
            newly_seeded.add(name)
    return newly_seeded


def _drift_toward(current: float, base: float, pct: float) -> float:
    """Moves `current` about `pct` of itself closer to `base` (at least 1 unit, so small
    prices don't get stuck on rounding) without ever overshooting it."""
    step = max(1, round(current * pct))
    if current > base:
        return max(base, current - step)
    if current < base:
        return min(base, current + step)
    return current


def compute_price_update(current_min, current_max, base_min, base_max, counts, total_cards):
    """Returns the next (min, max) for one tier.

    activity = weighted sum of gifts/checks/sales this cycle - more
    activity pushes the price up. scarcity_mult amplifies that push when
    the tier has fewer cards than PRICE_SCARCITY_REFERENCE_COUNT (and
    dampens it when there are more). With zero activity, the price decays
    a little back toward its base instead of staying frozen. The result
    is always clamped to a floor/ceiling multiple of the base price so
    the economy can't run away permanently.
    """
    gift_pts = counts.get("gift", 0) * config.PRICE_GIFT_WEIGHT
    check_pts = counts.get("check", 0) * config.PRICE_CHECK_WEIGHT
    sold_pts = counts.get("sold", 0) * config.PRICE_MARKET_SOLD_WEIGHT
    activity = gift_pts + check_pts + sold_pts

    if total_cards > 0:
        scarcity_mult = config.PRICE_SCARCITY_REFERENCE_COUNT / total_cards
        scarcity_mult = max(config.PRICE_SCARCITY_MIN_MULTIPLIER,
                             min(config.PRICE_SCARCITY_MAX_MULTIPLIER, scarcity_mult))
    else:
        scarcity_mult = 1.0

    if activity <= 0:
        # Nothing happened: drift back TOWARD the base price - never past it. (This used
        # to always subtract 2%, so an idle tier sank hourly all the way to half its
        # base price and /prices never showed the real ranges.)
        new_min = _drift_toward(current_min, base_min, config.PRICE_IDLE_DECAY_PCT)
        new_max = _drift_toward(current_max, base_max, config.PRICE_IDLE_DECAY_PCT)
    else:
        pct_change = activity * scarcity_mult * config.PRICE_SENSITIVITY
        pct_change = max(-config.PRICE_MAX_HOURLY_CHANGE_PCT,
                          min(config.PRICE_MAX_HOURLY_CHANGE_PCT, pct_change))
        new_min = current_min * (1 + pct_change)
        new_max = current_max * (1 + pct_change)

    floor_min = base_min * config.PRICE_FLOOR_MULTIPLIER
    floor_max = base_max * config.PRICE_FLOOR_MULTIPLIER
    ceil_min = base_min * config.PRICE_CEILING_MULTIPLIER
    ceil_max = base_max * config.PRICE_CEILING_MULTIPLIER

    new_min = max(floor_min, min(ceil_min, new_min))
    new_max = max(floor_max, min(ceil_max, new_max))

    if new_max <= new_min:
        new_max = new_min + max(1, base_max - base_min)

    return round(new_min), round(new_max)


def run_price_update_cycle():
    """One full recalculation pass over every canonical rarity tier."""
    newly_seeded = ensure_prices_seeded()

    prices = db.get_rarity_market_prices()
    all_rarities = db.list_rarities()
    now_dt = datetime.utcnow()
    now_iso = now_dt.isoformat()

    for canonical_name, (base_min, base_max) in config.RARITY_BASE_PRICES.items():
        if canonical_name in newly_seeded:
            # Just created this pass at the base price - nothing has had a
            # chance to happen yet, so there's nothing to adjust for.
            continue

        row = prices.get(canonical_name)
        current_min = row["current_min"] if row else base_min
        current_max = row["current_max"] if row else base_max
        last_updated = row["last_updated_at"] if row and row["last_updated_at"] else now_iso

        # Every raw rarity row (however the owner actually named/emoji'd
        # it) that maps to this canonical tier.
        matching = [r for r in all_rarities if match_price_tier(r["name"]) == canonical_name]

        total_cards = 0
        counts = {"gift": 0, "check": 0, "sold": 0}
        for r in matching:
            total_cards += db.count_characters_by_rarity(r["id"])
            raw_counts = db.get_rarity_activity_counts(r["name"], last_updated)
            for key in counts:
                counts[key] += raw_counts.get(key, 0)

        new_min, new_max = compute_price_update(
            current_min, current_max, base_min, base_max, counts, total_cards
        )
        db.set_rarity_market_price(canonical_name, new_min, new_max, now_iso)

    # Housekeeping: nothing older than 2 days is ever read by the windows above.
    cutoff = (now_dt - timedelta(days=2)).isoformat()
    db.prune_old_rarity_activity_log(cutoff)


def run_price_update_loop():
    """Runs forever in a background thread: recompute prices once at
    startup, then every config.PRICE_UPDATE_INTERVAL_SECONDS."""
    while True:
        try:
            run_price_update_cycle()
        except Exception:
            logger.exception("Rarity price update cycle failed")
        time.sleep(config.PRICE_UPDATE_INTERVAL_SECONDS)


def _format_timestamp(iso_string: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return iso_string or "—"


def format_prices_message() -> str:
    prices = db.get_rarity_market_prices()
    lines = ["💰 <b>Rarity Prices</b>\n"]
    last_updates = []

    for name, (base_min, base_max) in config.RARITY_BASE_PRICES.items():
        emoji = config.RARITY_PRICE_EMOJIS.get(name, "")
        row = prices.get(name)
        if row:
            price_min, price_max = row["current_min"], row["current_max"]
            if row.get("last_updated_at"):
                last_updates.append(row["last_updated_at"])
        else:
            price_min, price_max = base_min, base_max
        lines.append(f"{emoji} <b>{name}</b>\n{price_min:,} – {price_max:,} {config.CURRENCY_SYMBOL}\n")

    if last_updates:
        lines.append(f"🕒 Last updated: {_format_timestamp(max(last_updates))}")
    else:
        lines.append("🕒 Prices haven't been recalculated yet.")

    return "\n".join(lines).strip()


def format_event_tiers_message() -> str:
    lines = ["🎉 <b>Event Price Bonuses</b>\n"]
    for tier in config.EVENT_PRICE_TIERS:
        lines.append(f"{tier['label']}")
        lines.append(tier["range"])
        lines.append(", ".join(tier["events"]))
        lines.append("")
    return "\n".join(lines).strip()
