"""
HTTP API for the Waifu Market Mini App.

Runs inside the SAME process as the bot (started as a background thread
from bot.py's main()), so it reads/writes the exact same SQLite file as
the bot - no second Railway service, no shared volume, no sync issues.

Endpoints:
  GET  /api/market/listings          - all active listings (public, no auth)
  GET  /api/market/balance           - the caller's VɎ balance (auth required)
  POST /api/market/buy               - buy a listing (auth required)
  GET  /api/market/image/{file_id}   - proxies a character's Telegram photo,
                                        so the browser never needs the bot token

Auth: every authenticated request must include the raw string Telegram
gives the Mini App at launch (`window.Telegram.WebApp.initData`) in an
`X-Telegram-Init-Data` header. That string is signed by Telegram using
the bot token, so verifying it here is how we know which real Telegram
user is asking - the browser can't forge it without the token.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import chat_ai
import database as db
import memories

logger = logging.getLogger(__name__)

api_app = FastAPI(title="Waifu Market API")

# Only this Telegram user id is allowed to create new tasks from the
# Mini App's Task tab.
TASK_ADMIN_ID = config.ADMIN_ID

# The Mini App is served from a different domain (Vercel/Netlify/etc.),
# so the browser needs CORS allowed explicitly. Once you have the Mini
# App's real URL, set MINI_APP_URL in your environment so this locks
# down to just that origin instead of "*".
def _cors_origin(url: str) -> str:
    """CORS wants a bare origin (https://host), not a full URL - a trailing
    slash or path in MINI_APP_URL would otherwise make every request fail."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url.rstrip("/")


api_app.add_middleware(
    CORSMiddleware,
    allow_origins=[_cors_origin(config.MINI_APP_URL)] if config.MINI_APP_URL else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def verify_init_data(init_data: str, max_age_seconds: int = 86400) -> dict:
    """
    Validates a Telegram WebApp initData string against the bot token,
    following Telegram's documented check, and returns the parsed user
    dict. Raises HTTPException(401) on anything invalid or missing -
    never trust a user id sent any other way.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram auth")

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed Telegram auth")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > max_age_seconds:
        raise HTTPException(status_code=401, detail="Expired auth - reopen the Mini App")

    user = json.loads(parsed.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="No user in auth data")
    return user


def current_user(x_telegram_init_data: str = Header(default=None)):
    user = verify_init_data(x_telegram_init_data)
    # Every authenticated Mini App request carries the caller's live
    # Telegram profile - piggyback on that to keep the leaderboard's
    # stored display names fresh (see database.upsert_user_profile).
    try:
        db.upsert_user_profile(
            user["id"],
            username=user.get("username"),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
        )
    except Exception:
        pass
    return user


@api_app.get("/api/market/listings")
def list_listings():
    rows = db.get_active_listings()
    return [dict(row) for row in rows]


@api_app.get("/api/market/balance")
def get_balance(user: dict = Depends(current_user)):
    return {"balance": db.get_currency(user["id"])}


@api_app.get("/api/market/profile")
def get_profile(user: dict = Depends(current_user)):
    """
    Powers the Mini App's Home tab: display name, balance, total card
    count, and every character the player owns (duplicates collapsed
    into a single entry with a quantity, so a triple-owned card shows
    once with a x3 badge instead of three times).
    """
    inventory = db.get_user_inventory(user["id"])

    display_name = user.get("first_name") or user.get("username") or f"Player {user['id']}"
    if user.get("last_name"):
        display_name = f"{display_name} {user['last_name']}"

    grouped = {}
    order = []
    for row in inventory:
        character_id = row["id"]
        if character_id not in grouped:
            grouped[character_id] = {
                "id": character_id,
                "name": row["name"],
                "series": row["series"],
                "rarity_name": row["rarity_name"],
                "image_file_id": row["image_file_id"],
                "media_type": row["media_type"],
                "quantity": 0,
            }
            order.append(character_id)
        grouped[character_id]["quantity"] += 1

    return {
        "name": display_name,
        "balance": db.get_currency(user["id"]),
        "card_count": len(inventory),
        "cards": [grouped[cid] for cid in order],
        "is_premium": db.is_premium(user["id"]),
        "theme": db.get_user_theme(user["id"]),
    }


# ---------------- Home tab sort/filter ----------------
# Same user_filters row /sort (Telegram command) reads and writes - so
# setting it here changes what /constellation shows too, and vice versa.
# db.get_user_inventory() (used by /api/market/profile above) already
# applies whichever filter is active, so these two endpoints are all
# that's needed - no change to how the Home tab's card list is fetched.

@api_app.get("/api/profile/filter")
def get_profile_filter(user: dict = Depends(current_user)):
    """filter_type/filter_value = the first active filter (what the picker shows);
    `filters` = every filter the player stacked with /sort in the bot."""
    filters = [
        {"filter_type": f["filter_type"], "filter_value": f["filter_value"]}
        for f in db.get_user_filters(user["id"])
    ]
    if not filters:
        return {"filter_type": None, "filter_value": None, "filters": []}
    return {**filters[0], "filters": filters}


@api_app.get("/api/profile/filter-options")
def get_profile_filter_options(user: dict = Depends(current_user)):
    """Only the character/series names this player actually owns (not
    every character in the game) - picking one always has a result. Uses
    the UNFILTERED inventory so switching filters doesn't shrink the
    picker choices to whatever the previous filter left visible."""
    inventory = db.get_user_inventory(user["id"], apply_filter=False)
    characters = sorted({row["name"] for row in inventory})
    series = sorted({row["series"] for row in inventory})
    rarities = [r["name"] for r in db.list_rarities()]
    return {"characters": characters, "series": series, "rarities": rarities}


class ProfileFilterRequest(BaseModel):
    filter_type: Optional[str] = None
    filter_value: Optional[str] = None


@api_app.post("/api/profile/filter")
def set_profile_filter(payload: ProfileFilterRequest, user: dict = Depends(current_user)):
    if not payload.filter_type or not payload.filter_value:
        db.clear_user_filter(user["id"])
        return {"ok": True, "filter_type": None, "filter_value": None}

    if payload.filter_type not in db.FILTER_TYPES:
        raise HTTPException(status_code=400, detail="invalid_filter_type")

    db.set_user_filter(user["id"], payload.filter_type, payload.filter_value)
    return {"ok": True, "filter_type": payload.filter_type, "filter_value": payload.filter_value}


class ThemeRequest(BaseModel):
    theme: str


@api_app.post("/api/settings/theme")
def set_theme(payload: ThemeRequest, user: dict = Depends(current_user)):
    theme = payload.theme
    if theme != "default" and theme not in config.PREMIUM_THEMES:
        raise HTTPException(status_code=400, detail="unknown_theme")
    if theme in config.PREMIUM_THEMES and not db.is_premium(user["id"]):
        raise HTTPException(status_code=403, detail="premium_required")
    db.set_user_theme(user["id"], theme)
    return {"ok": True, "theme": theme}


class BuyRequest(BaseModel):
    listing_id: int


@api_app.post("/api/market/buy")
async def buy(payload: BuyRequest, user: dict = Depends(current_user)):
    username = user.get("username") or user.get("first_name")
    result = db.buy_listing(payload.listing_id, user["id"], username)
    if not result["ok"]:
        status = 409 if result["reason"] in ("not_available", "own_listing") else 400
        raise HTTPException(status_code=status, detail=result["reason"])

    character = db.get_character(result["character_id"])
    if character:
        for msg in memories.record_acquisition(user["id"], character, "market_buy"):
            await _send_telegram_message(user["id"], msg)

    return result


class SellToBotRequest(BaseModel):
    character_id: int


@api_app.get("/api/market/sell-prices")
def sell_prices():
    """Powers the Sell tab's per-card price preview - rarity -> price, public (no auth needed to just look)."""
    rows = db.get_all_sell_prices()
    return [{"rarity_id": row["rarity_id"], "rarity_name": row["name"], "price": row["price"] or 0} for row in rows]


@api_app.post("/api/market/sell")
def sell_to_bot(payload: SellToBotRequest, user: dict = Depends(current_user)):
    character = db.get_character(payload.character_id)
    if not character:
        raise HTTPException(status_code=404, detail="character_not_found")

    price = db.get_sell_price(character["rarity_id"])
    if price <= 0:
        raise HTTPException(status_code=400, detail="no_sell_price_set")

    success = db.sell_character_to_bot(user["id"], payload.character_id)
    if not success:
        raise HTTPException(status_code=409, detail="not_owned_or_listed")

    new_balance = db.add_currency(user["id"], price)
    return {"ok": True, "price": price, "new_balance": new_balance}


# ---------------- Chat tab ----------------
# The player picks one of THEIR OWN characters (deduplicated - 3 copies
# of the same character still shows up once) and chats with it.
# Character replies currently come from chat_ai.generate_reply(), a
# placeholder - see that module's docstring for how the real AI bot
# will plug in later without changing anything here.

@api_app.get("/api/chat/characters")
def chat_characters(user: dict = Depends(current_user)):
    return db.list_chat_characters(user["id"])


@api_app.get("/api/chat/messages/{character_id}")
def chat_messages(character_id: int, user: dict = Depends(current_user)):
    if not db.user_owns_character(user["id"], character_id):
        raise HTTPException(status_code=404, detail="character_not_owned")
    rows = db.get_chat_messages(user["id"], character_id)
    return [dict(row) for row in rows]


class ChatSendRequest(BaseModel):
    character_id: int
    text: str


@api_app.post("/api/chat/messages")
async def chat_send(payload: ChatSendRequest, user: dict = Depends(current_user)):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty_message")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="message_too_long")

    character = db.get_character(payload.character_id)
    if not character or not db.user_owns_character(user["id"], payload.character_id):
        raise HTTPException(status_code=404, detail="character_not_owned")
    if db.is_chat_blocked(user["id"], payload.character_id):
        raise HTTPException(status_code=403, detail="conversation_blocked")

    user_message = db.add_chat_message(user["id"], payload.character_id, "user", text)

    history = db.get_chat_messages(user["id"], payload.character_id)
    reply_text = await chat_ai.generate_reply(dict(character), [dict(row) for row in history], text)
    character_message = db.add_chat_message(user["id"], payload.character_id, "character", reply_text)

    return {"ok": True, "user_message": user_message, "reply": character_message}


# ---------------- Chat tab: pin / block / delete (long-press menu) ----------------

class ChatConversationAction(BaseModel):
    character_id: int


@api_app.post("/api/chat/conversation/pin")
def chat_pin(payload: ChatConversationAction, user: dict = Depends(current_user)):
    db.set_chat_pinned(user["id"], payload.character_id, True)
    return {"ok": True}


@api_app.post("/api/chat/conversation/unpin")
def chat_unpin(payload: ChatConversationAction, user: dict = Depends(current_user)):
    db.set_chat_pinned(user["id"], payload.character_id, False)
    return {"ok": True}


@api_app.post("/api/chat/conversation/block")
def chat_block(payload: ChatConversationAction, user: dict = Depends(current_user)):
    db.set_chat_blocked(user["id"], payload.character_id, True)
    return {"ok": True}


@api_app.post("/api/chat/conversation/unblock")
def chat_unblock(payload: ChatConversationAction, user: dict = Depends(current_user)):
    db.set_chat_blocked(user["id"], payload.character_id, False)
    return {"ok": True}


@api_app.post("/api/chat/conversation/delete")
def chat_delete(payload: ChatConversationAction, user: dict = Depends(current_user)):
    db.delete_chat_conversation(user["id"], payload.character_id)
    return {"ok": True}


# ---------------- Leaderboards ----------------

@api_app.get("/api/leaderboard/constellations")
def leaderboard_constellations():
    rows = db.get_top_series(limit=50)
    return [{"series": row["series"], "character_count": row["character_count"]} for row in rows]


@api_app.get("/api/leaderboard/constellations/{series}")
def leaderboard_constellation_detail(series: str):
    rows = db.get_series_characters(series)
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "series": row["series"],
            "rarity_name": row["rarity_name"],
            "image_file_id": row["image_file_id"],
            "media_type": row["media_type"],
        }
        for row in rows
    ]


@api_app.get("/api/leaderboard/richest")
def leaderboard_richest():
    rows = db.get_richest_users(limit=50)
    result = []
    for row in rows:
        result.append({
            "user_id": row["user_id"],
            "display_name": db.get_display_name(row["user_id"]),
            "balance": row["balance"],
        })
    return result


@api_app.get("/api/leaderboard/collectors")
def leaderboard_collectors():
    rows = db.get_top_collectors(limit=50)
    result = []
    for row in rows:
        result.append({
            "user_id": row["user_id"],
            "display_name": db.get_display_name(row["user_id"]),
            "card_count": row["card_count"],
        })
    return result


@api_app.get("/api/leaderboard/profile/{user_id}")
def leaderboard_player_profile(user_id: int):
    """
    Powers the Mini App's leaderboard - tapping a player's name opens
    this. Public (no auth) since it only exposes what's already shown
    on the leaderboard itself: display name, balance, card count.
    """
    inventory = db.get_user_inventory(user_id)
    return {
        "user_id": user_id,
        "display_name": db.get_display_name(user_id),
        "balance": db.get_currency(user_id),
        "card_count": len(inventory),
    }


# ---------------- Tasks ----------------

def _serialize_task_entry(entry: dict) -> dict:
    task = entry["task"]
    return {
        "id": task["id"],
        "task_type": task["task_type"],
        "display_text": task["display_text"],
        "target": entry["target"],
        "progress": entry["progress"],
        "done": entry["done"],
        "reward_type": task["reward_type"],
        "reward_currency": task["reward_currency"],
        "reward_character_id": task["reward_character_id"],
    }


@api_app.get("/api/tasks")
def get_tasks(user: dict = Depends(current_user)):
    user_id = user["id"]
    is_admin = user_id == TASK_ADMIN_ID

    entries = db.get_active_tasks_for_user(user_id)
    daily_claimed = db.has_claimed_daily_task_bonus_today(user_id)
    daily_reward = config.PREMIUM_DAILY_TASK_BONUS if db.is_premium(user_id) else config.DAILY_TASK_BONUS

    return {
        "is_admin": is_admin,
        "daily_claimed": daily_claimed,
        "daily_reward": daily_reward,
        "tasks": [_serialize_task_entry(entry) for entry in entries],
    }


@api_app.post("/api/tasks/daily-claim")
def daily_claim(user: dict = Depends(current_user)):
    new_balance = db.claim_daily_task_bonus(user["id"])
    if new_balance is None:
        raise HTTPException(status_code=409, detail="already_claimed_today")
    return {"ok": True, "new_balance": new_balance}


class TaskClaimRequest(BaseModel):
    task_id: int


@api_app.post("/api/tasks/claim")
def task_claim(payload: TaskClaimRequest, user: dict = Depends(current_user)):
    username = user.get("username") or user.get("first_name")
    result = db.claim_task(user["id"], payload.task_id, username=username)
    if not result["ok"]:
        status = 409 if result["reason"] in ("already_claimed", "not_complete") else 404
        raise HTTPException(status_code=status, detail=result["reason"])
    return result


class TaskCreateRequest(BaseModel):
    task_type: str
    target_count: int
    display_text: str
    reward_type: str
    character_id: int | None = None
    rarity_id: int | None = None
    reward_currency: int | None = None
    reward_character_id: int | None = None


@api_app.post("/api/tasks/admin/add")
def task_admin_add(payload: TaskCreateRequest, user: dict = Depends(current_user)):
    if user["id"] != TASK_ADMIN_ID:
        raise HTTPException(status_code=403, detail="not_authorized")

    valid_types = {
        "own_cards", "rarity_cards", "character_cards",
        "buy_market", "sell_market", "earn_currency", "spend_currency",
    }
    if payload.task_type not in valid_types:
        raise HTTPException(status_code=400, detail="invalid_task_type")
    if payload.reward_type not in ("currency", "card"):
        raise HTTPException(status_code=400, detail="invalid_reward_type")
    if payload.reward_type == "currency" and not payload.reward_currency:
        raise HTTPException(status_code=400, detail="missing_reward_currency")
    if payload.reward_type == "card" and not payload.reward_character_id:
        raise HTTPException(status_code=400, detail="missing_reward_character_id")
    if payload.task_type == "character_cards" and not payload.character_id:
        raise HTTPException(status_code=400, detail="missing_character_id")
    if payload.task_type == "rarity_cards" and not payload.rarity_id:
        raise HTTPException(status_code=400, detail="missing_rarity_id")

    task_id = db.add_task_definition(
        task_type=payload.task_type,
        target_count=payload.target_count,
        reward_type=payload.reward_type,
        display_text=payload.display_text,
        created_by=user["id"],
        character_id=payload.character_id,
        rarity_id=payload.rarity_id,
        reward_currency=payload.reward_currency,
        reward_character_id=payload.reward_character_id,
    )
    return {"ok": True, "task_id": task_id}


@api_app.get("/api/tasks/admin/characters")
def task_admin_characters(q: str = "", user: dict = Depends(current_user)):
    """Powers the admin Add Task form's character picker (search-as-you-type)."""
    if user["id"] != TASK_ADMIN_ID:
        raise HTTPException(status_code=403, detail="not_authorized")
    rows = db.search_characters_for_picker(q)
    return [
        {"id": row["id"], "name": row["name"], "series": row["series"], "rarity_name": row["rarity_name"]}
        for row in rows
    ]


@api_app.get("/api/tasks/admin/rarities")
def task_admin_rarities(user: dict = Depends(current_user)):
    if user["id"] != TASK_ADMIN_ID:
        raise HTTPException(status_code=403, detail="not_authorized")
    rows = db.list_rarities()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


# ---------------- Fighter / Arena (Update 2) ----------------

def _serialize_fighter_card(row) -> dict:
    element = config.ELEMENTS.get(row["element"], {})
    return {
        "user_character_id": row["user_character_id"],
        "character_id": row["character_id"],
        "name": row["name"],
        "series": row["series"],
        "image_file_id": row["image_file_id"],
        "media_type": row["media_type"],
        "element": row["element"],
        "element_label": element.get("label", row["element"]),
        "level": row["level"] or 1,
        "max_level": config.FIGHTER_MAX_LEVEL,
        "current_attack": row["current_attack"] or 0,
        "current_defense": row["current_defense"] or 0,
    }


def _serialize_battle(row) -> dict:
    return {
        "id": row["id"],
        "attacker_id": row["attacker_id"],
        "attacker_username": row["attacker_username"],
        "defender_id": row["defender_id"],
        "attack_power": row["attack_power"],
        "defense_power": row["defense_power"],
        "result": row["result"],
        "attacker_trophy_change": row["attacker_trophy_change"],
        "defender_trophy_change": row["defender_trophy_change"],
        "started_at": row["started_at"],
        "resolves_at": row["resolves_at"],
        "resolved": bool(row["resolved"]),
    }


async def _send_telegram_message(chat_id: int, text: str):
    """Fire-and-forget DM via the raw Bot API - mirrors get_image()'s use
    of httpx below, so this file never needs a live PTB Application/Bot
    instance from the other thread/event loop bot.py runs in."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception:
        logger.exception("Failed to send Arena battle notification to %s", chat_id)


def _battle_result_text(battle, for_user_id: int) -> str:
    is_attacker = for_user_id == battle["attacker_id"]
    my_power = battle["attack_power"] if is_attacker else battle["defense_power"]
    enemy_power = battle["defense_power"] if is_attacker else battle["attack_power"]
    my_change = battle["attacker_trophy_change"] if is_attacker else battle["defender_trophy_change"]

    if battle["result"] == "draw":
        headline, change_line = "🤝 Draw", "0 Trophies"
    elif (battle["result"] == "attacker") == is_attacker:
        headline, change_line = "🏆 Victory", f"+{my_change} Trophies"
    else:
        headline, change_line = "💀 Defeat", f"{my_change} Trophies"

    label_a, label_b = ("Your Attack Power", "Enemy Defense Power") if is_attacker \
        else ("Your Defense Power", "Enemy Attack Power")

    return (
        "⚔ Arena Battle Finished\n\n"
        f"{label_a}: {my_power}\n"
        f"{label_b}: {enemy_power}\n\n"
        f"{headline}\n"
        f"{change_line}"
    )


async def _resolve_and_notify(battle_id: int, delay_seconds: float):
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    battle = db.resolve_arena_battle(battle_id)
    if battle is None:
        # already resolved (e.g. a reconciled duplicate schedule) - nothing to do
        return

    await _send_telegram_message(battle["attacker_id"], _battle_result_text(battle, battle["attacker_id"]))
    if battle["defender_id"] not in config.ARENA_NPC_IDS:
        await _send_telegram_message(battle["defender_id"], _battle_result_text(battle, battle["defender_id"]))
    db.mark_battle_notified(battle_id)


@api_app.on_event("startup")
async def _reschedule_pending_arena_battles():
    """
    Picks back up every Arena battle that was still waiting to resolve
    when the process last stopped, so a restart during someone's 3
    minute fight never loses their trophies or their result DM.
    """
    for battle in db.get_all_unresolved_battles():
        resolves_at = datetime.fromisoformat(battle["resolves_at"])
        remaining = (resolves_at - datetime.utcnow()).total_seconds()
        asyncio.create_task(_resolve_and_notify(battle["id"], max(0, remaining)))


@api_app.get("/api/arena/leagues")
def arena_leagues():
    """Public - powers the League table shown in the Arena tab."""
    return config.ARENA_LEAGUES


@api_app.get("/api/arena/status")
def arena_status(user: dict = Depends(current_user)):
    trophies = db.get_trophies(user["id"])
    league = db.get_league_for_trophies(trophies)
    pending = db.get_pending_battle(user["id"])
    return {
        "trophies": trophies,
        "league": league,
        "pending_battle": _serialize_battle(pending) if pending else None,
    }


@api_app.get("/api/arena/cards")
def arena_cards(user: dict = Depends(current_user)):
    """Every Fighter copy the caller owns - powers Card Management and
    the Attack/Defense team pickers."""
    rows = db.get_user_fighter_cards(user["id"])
    return [_serialize_fighter_card(r) for r in rows]


@api_app.get("/api/arena/teams")
def arena_teams(user: dict = Depends(current_user)):
    attack = db.get_arena_team(user["id"], "attack")
    defense = db.get_arena_team(user["id"], "defense")
    return {
        "attack": [_serialize_fighter_card(r) for r in attack],
        "defense": [_serialize_fighter_card(r) for r in defense],
    }


class SetTeamRequest(BaseModel):
    team_type: str
    user_character_ids: list[int]


@api_app.post("/api/arena/teams/set")
def arena_teams_set(payload: SetTeamRequest, user: dict = Depends(current_user)):
    if payload.team_type not in ("attack", "defense"):
        raise HTTPException(status_code=400, detail="invalid_team_type")
    if len(payload.user_character_ids) > 3:
        raise HTTPException(status_code=400, detail="too_many_cards")

    result = db.set_arena_team(user["id"], payload.team_type, payload.user_character_ids)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    return result


class UpgradeRequest(BaseModel):
    user_character_id: int
    stat: str


@api_app.post("/api/arena/upgrade")
def arena_upgrade(payload: UpgradeRequest, user: dict = Depends(current_user)):
    if payload.stat not in ("attack", "defense"):
        raise HTTPException(status_code=400, detail="invalid_stat")

    result = db.upgrade_fighter_card(user["id"], payload.user_character_id, payload.stat)
    if not result["ok"]:
        status = 404 if result["reason"] == "not_found" else 400
        raise HTTPException(status_code=status, detail=result["reason"])
    return result


@api_app.post("/api/arena/fight")
def arena_fight(user: dict = Depends(current_user)):
    """
    Starts a battle: opponent + result are decided immediately, but the
    trophy payout and result DM only land ARENA_BATTLE_DURATION_SECONDS
    later - the Mini App should poll GET /api/arena/battle/{id} (or
    /api/arena/status for pending_battle) to show the countdown/result.
    """
    username = user.get("username") or user.get("first_name")
    result = db.start_arena_battle(user["id"], username)
    if not result["ok"]:
        status = 409 if result["reason"] == "battle_in_progress" else 400
        raise HTTPException(status_code=status, detail=result["reason"])

    delay = config.ARENA_BATTLE_DURATION_SECONDS
    asyncio.create_task(_resolve_and_notify(result["battle_id"], delay))
    return result


@api_app.get("/api/arena/battle/{battle_id}")
def arena_battle_detail(battle_id: int, user: dict = Depends(current_user)):
    battle = db.get_battle(battle_id)
    if not battle:
        raise HTTPException(status_code=404, detail="not_found")
    if user["id"] not in (battle["attacker_id"], battle["defender_id"]):
        raise HTTPException(status_code=403, detail="not_authorized")
    return _serialize_battle(battle)


@api_app.get("/api/arena/history")
def arena_history(user: dict = Depends(current_user)):
    rows = db.get_recent_battles(user["id"])
    return [_serialize_battle(r) for r in rows]


# ---------------- Rider mini-game ----------------
# The game itself runs entirely client-side (Canvas + physics), so what it
# reports isn't provably honest. Rather than trust it outright, every claim
# is clamped two ways:
#   1. distance can never exceed how far the bike could plausibly have
#      traveled - even riding flat-out with boost the whole time - in the
#      real wall-clock time that's actually passed since this player's
#      last claim.
#   2. money can never exceed what that (now-clamped) distance could ever
#      pay out at the top speed tier (3 currency units per 100m - see
#      speedRewardTier in GameCanvas.jsx), regardless of what the client
#      claims it earned.
# Together these close off the easy "replay a bigger number" abuse case
# without needing a server-authoritative physics simulation.
#
# RIDER_MAX_SPEED_MPS mirrors the client's physics tuning in
# GameCanvas.jsx: PX_PER_METER=24, MAX_SPEED=31 game-units/tick at
# TICKS_PER_SECOND=60, boosted by BOOST_MAX_SPEED's *1.2 multiplier. Keep
# these two files in sync if that client-side tuning ever changes.
RIDER_MAX_SPEED_MPS = (31 * 1.2 * 60) / 24  # ~93 m/s, boosted top speed
RIDER_REWARD_PER_100M_MAX = 3  # top speed tier - see speedRewardTier in GameCanvas.jsx
RIDER_MAX_RUN_SECONDS = 3600  # 1 hour sanity ceiling, regardless of claimed elapsed time

# In-memory only (resets on restart) - same low-stakes tradeoff as the
# submission/task stores elsewhere in this file.
_rider_last_claim_at = {}


class RiderRunRequest(BaseModel):
    distance_meters: int
    money: int


@api_app.post("/api/game/rider-finish")
def rider_finish(payload: RiderRunRequest, user: dict = Depends(current_user)):
    now = time.time()
    last = _rider_last_claim_at.get(user["id"])
    elapsed_seconds = (now - last) if last else RIDER_MAX_RUN_SECONDS
    elapsed_seconds = min(elapsed_seconds, RIDER_MAX_RUN_SECONDS)
    max_plausible_distance = elapsed_seconds * RIDER_MAX_SPEED_MPS

    distance_meters = max(0, min(int(payload.distance_meters), int(max_plausible_distance)))
    max_plausible_money = (distance_meters // 100) * RIDER_REWARD_PER_100M_MAX
    money = max(0, min(int(payload.money), max_plausible_money))

    _rider_last_claim_at[user["id"]] = now

    new_balance = db.get_currency(user["id"])
    if money > 0:
        new_balance = db.add_currency(user["id"], money)

    return {"ok": True, "distance_meters": distance_meters, "reward": money, "new_balance": new_balance}


# In-memory only (resets on restart) - avoids re-calling Telegram's
# getFile for the same character on every single card render.
_file_path_cache = {}


@api_app.get("/api/market/image/{file_id}")
async def get_image(file_id: str):
    file_path = _file_path_cache.get(file_id)

    async with httpx.AsyncClient() as client:
        if not file_path:
            resp = await client.get(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            data = resp.json()
            if not data.get("ok"):
                raise HTTPException(status_code=404, detail="Image not found")
            file_path = data["result"]["file_path"]
            _file_path_cache[file_id] = file_path

        file_resp = await client.get(f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_path}")

    if file_resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Image not found")

    return Response(
        content=file_resp.content,
        media_type=file_resp.headers.get("content-type", "image/jpeg"),
    )
