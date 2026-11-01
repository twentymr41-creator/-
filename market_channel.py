"""
Mirrors the Waifu Market into a public Telegram channel (config.MARKET_CHANNEL).

* /sell  -> a card post (photo + details + seller + price) with a "Buy" button
* bought -> the card gets a green, tilted SOLD ink-stamp, the buyer's name is
            added to the caption and the Buy button disappears
* pulled -> (/cancelsell, cancel button, card deleted) the card gets a red,
            tilted EXPIRED stamp and the Buy button disappears

Everything here talks to the raw Bot API with a *synchronous* httpx client, so it
can be used from bot.py (via asyncio.to_thread) AND from api_server.py's thread
(Mini App purchases) without needing the other thread's event loop / Bot object.
Every public function is best-effort: it logs and returns False/None on failure and
never raises, so a channel hiccup can never break a sale.

Names are always read from the person's live Telegram account (see live_name()).
"""
import html
import io
import logging
import os
import random

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import config
import database as db

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

STAMP_GREEN = (22, 150, 60)
STAMP_RED = (200, 30, 40)


# ---------------------------------------------------------------- Bot API

def _api(method: str, data: dict = None, files: dict = None):
    """Calls a Bot API method; returns the `result` or None on any failure."""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}",
            data=data or {}, files=files, timeout=_TIMEOUT,
        )
        body = r.json()
        if not body.get("ok"):
            logger.warning("Market channel: %s failed: %s", method, body.get("description"))
            return None
        return body["result"]
    except Exception:
        logger.exception("Market channel: %s crashed", method)
        return None


def _download_file(file_id: str):
    info = _api("getFile", {"file_id": file_id})
    if not info or not info.get("file_path"):
        return None
    try:
        r = httpx.get(
            f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{info['file_path']}",
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception("Market channel: could not download card image")
        return None


# ---------------------------------------------------------------- names

def live_name(user_id: int, tg_user=None) -> str:
    """The person's CURRENT name, straight from their Telegram account.
    `tg_user` is the telegram.User object when the caller already has one
    (message / button press) - that is the freshest source. Otherwise the Bot API
    is asked. db.get_display_name (stored copy) is only the last resort."""
    if tg_user is not None:
        full = " ".join(p for p in (getattr(tg_user, "first_name", None),
                                    getattr(tg_user, "last_name", None)) if p)
        if full:
            return full
    return fetch_live_name(user_id) or db.get_display_name(user_id)


def fetch_live_name(user_id: int, timeout: float = 10.0):
    """Asks Telegram (getChat) for the account's current name. None when Telegram
    won't say (the person never started the bot, blocked it, ...)."""
    try:
        r = httpx.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/getChat",
                       data={"chat_id": user_id}, timeout=timeout)
        body = r.json()
        if not body.get("ok"):
            return None
        chat = body["result"]
        full = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
        if full:
            if chat.get("first_name"):
                try:  # keep the stored copy in step with the account
                    db.upsert_user_profile(user_id, username=chat.get("username"),
                                           first_name=chat.get("first_name"), last_name=chat.get("last_name"))
                except Exception:
                    pass
            return full
        return f"@{chat['username']}" if chat.get("username") else None
    except Exception:
        return None


def _mention(user_id: int, name: str) -> str:
    star = " ⭐️" if user_id and db.is_premium(user_id) else ""
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>{star}'


# ---------------------------------------------------------------- caption

def _card_lines(character, rarity_name):
    lines = [
        f"✦ <b>{html.escape(character['name'])}</b>",
        "",
        f"✦ Series: {html.escape(character['series'] or '—')}",
        f"✦ Rarity: {html.escape(rarity_name or 'Unranked')}",
        f"✦ ID: <code>{character['id']}</code>",
    ]
    if character["event_name"]:
        lines.append(f"✦ Event: {html.escape(character['event_name'])}")
    return lines


def _caption(listing, character, rarity_name, seller_name, state, buyer_id=None, buyer_name=None):
    lines = _card_lines(character, rarity_name)
    lines += ["", "──────── ✦ ────────", ""]
    lines.append(f"💰 Price: <b>{listing['price']} {config.CURRENCY_SYMBOL}</b>")
    lines.append(f"👤 Seller: {_mention(listing['seller_id'], seller_name)}")
    if state == "sold":
        lines.append(f"🛒 Buyer: {_mention(buyer_id, buyer_name)}")
        lines += ["", "✅ <b>SOLD</b>"]
    elif state == "expired":
        lines += ["", "⛔ <b>EXPIRED</b> — removed from the shop"]
    return "\n".join(lines)


# ---------------------------------------------------------------- the ink stamp

_FONT_CANDIDATES = [
    os.environ.get("STAMP_FONT", ""),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "stamp.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


def _font(size: int):
    for path in _FONT_CANDIDATES:
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _stamp_layer(img_w: int, img_h: int, text: str, color, angle: float) -> Image.Image:
    """An RGBA layer holding one rubber-stamp impression: double rounded border,
    bold text, uneven ink (speckle + faded patches), rotated by `angle`."""
    target_w = img_w * 0.78                       # stamp covers ~3/4 of the card width
    size = max(24, int(min(img_w, img_h) * 0.22))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for _ in range(12):                            # shrink the font until the stamp fits
        font = _font(size)
        l, t, r, b = probe.textbbox((0, 0), text, font=font)
        pad = int(size * 0.42)
        if (r - l) + 2 * pad <= target_w:
            break
        size = int(size * 0.9)
    tw, th = r - l, b - t
    border = max(4, int(size * 0.11))
    sw, sh = tw + 2 * pad, th + 2 * pad
    margin = border * 3

    mask = Image.new("L", (sw + 2 * margin, sh + 2 * margin), 0)
    d = ImageDraw.Draw(mask)
    radius = int(size * 0.28)
    d.rounded_rectangle((margin, margin, margin + sw, margin + sh), radius=radius, outline=255, width=border)
    gap = int(border * 1.9)
    d.rounded_rectangle((margin + gap, margin + gap, margin + sw - gap, margin + sh - gap),
                        radius=max(2, radius - gap // 2), outline=255, width=max(2, border // 3))
    d.text((margin + pad - l, margin + pad - t), text, font=font, fill=255)

    # uneven ink: fine speckle + a few larger faded patches, like a real impression
    speckle = Image.effect_noise(mask.size, 70).point(lambda v: 255 if v > 90 else 120)
    patches = Image.effect_noise((max(2, mask.size[0] // 14), max(2, mask.size[1] // 14)), 40)
    patches = patches.resize(mask.size, Image.BICUBIC).point(lambda v: 255 if v > 105 else 205)
    mask = ImageChops.multiply(ImageChops.multiply(mask, speckle), patches)
    mask = mask.filter(ImageFilter.GaussianBlur(0.8)).point(lambda v: int(v * 0.94))

    layer = Image.new("RGBA", mask.size, tuple(color) + (0,))
    layer.putalpha(mask)
    return layer.rotate(angle, resample=Image.BICUBIC, expand=True)


def stamp_image(image_bytes: bytes, kind: str) -> bytes:
    """kind: 'sold' (green SOLD) or 'expired' (red EXPIRED). Returns JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    text, color = ("SOLD", STAMP_GREEN) if kind == "sold" else ("EXPIRED", STAMP_RED)
    angle = random.uniform(13, 22) * random.choice((1, -1))     # always crooked, never perfectly level
    layer = _stamp_layer(w, h, text, color, angle)

    cx = w // 2 + random.randint(-w // 40, w // 40)
    cy = int(h * 0.52) + random.randint(-h // 40, h // 40)
    base = img.convert("RGBA")
    base.alpha_composite(layer, (max(0, cx - layer.width // 2), max(0, cy - layer.height // 2))) \
        if layer.width <= w and layer.height <= h else \
        base.alpha_composite(layer.resize((w, int(layer.height * w / layer.width))).crop((0, 0, w, min(h, int(layer.height * w / layer.width)))), (0, 0))
    out = io.BytesIO()
    base.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


# ---------------------------------------------------------------- public API

def _load(listing_id: int):
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        return None, None, None
    character = db.get_character(listing["character_id"])
    rarity_name = None
    if character:
        try:
            rarity_name = character["rarity_name"]
        except (IndexError, KeyError):
            rarity_name = None
    return listing, character, rarity_name


def post_listing(listing_id: int, seller_tg_user=None) -> bool:
    """Announces a fresh /sell listing in the shop channel with a Buy button."""
    channel = getattr(config, "MARKET_CHANNEL", "")
    if not channel:
        return False
    listing, character, rarity_name = _load(listing_id)
    if not listing or not character:
        return False

    seller_name = live_name(listing["seller_id"], seller_tg_user)
    caption = _caption(listing, character, rarity_name, seller_name, "active")
    markup = '{"inline_keyboard": [[{"text": "🛒 Buy — %s %s", "callback_data": "chbuy:%s"}]]}' % (
        listing["price"], config.CURRENCY_SYMBOL, listing_id)

    is_video = (character["media_type"] or "photo") == "video"
    method, field = ("sendVideo", "video") if is_video else ("sendPhoto", "photo")
    msg = _api(method, {
        "chat_id": channel, field: character["image_file_id"], "caption": caption,
        "parse_mode": "HTML", "reply_markup": markup,
    })
    if not msg:
        return False
    db.set_listing_channel_post(listing_id, str(msg["chat"]["id"]), msg["message_id"],
                                "video" if is_video else "photo")
    return True


def _finish(listing_id: int, state: str, buyer_id=None, buyer_tg_user=None) -> bool:
    listing, character, rarity_name = _load(listing_id)
    if not listing or not listing["channel_message_id"]:
        return False                                   # never posted (older listing / channel was down)
    chat_id, msg_id = listing["channel_chat_id"], listing["channel_message_id"]

    seller_name = live_name(listing["seller_id"])
    buyer_name = live_name(buyer_id, buyer_tg_user) if buyer_id else None
    if character:
        caption = _caption(listing, character, rarity_name, seller_name, state, buyer_id, buyer_name)
    else:                                              # card already deleted from the bot
        caption = (f"💰 Price: <b>{listing['price']} {config.CURRENCY_SYMBOL}</b>\n"
                   f"👤 Seller: {_mention(listing['seller_id'], seller_name)}\n\n⛔ <b>EXPIRED</b>")
    no_buttons = '{"inline_keyboard": []}'

    original = None
    if character and listing["channel_media_type"] != "video":
        original = _download_file(character["image_file_id"])

    if original:
        try:
            stamped = stamp_image(original, state)
        except Exception:
            logger.exception("Market channel: stamping failed for listing %s", listing_id)
            stamped = None
        if stamped:
            import json
            media = json.dumps({"type": "photo", "media": "attach://card", "caption": caption, "parse_mode": "HTML"})
            res = _api("editMessageMedia",
                       {"chat_id": chat_id, "message_id": msg_id, "media": media, "reply_markup": no_buttons},
                       files={"card": ("card.jpg", stamped, "image/jpeg")})
            if res:
                return True

    # videos / failed stamping: still fix the caption and drop the button
    return bool(_api("editMessageCaption", {
        "chat_id": chat_id, "message_id": msg_id, "caption": caption,
        "parse_mode": "HTML", "reply_markup": no_buttons,
    }))


def mark_sold(listing_id: int, buyer_id: int, buyer_tg_user=None) -> bool:
    try:
        return _finish(listing_id, "sold", buyer_id, buyer_tg_user)
    except Exception:
        logger.exception("Market channel: mark_sold crashed for listing %s", listing_id)
        return False


def mark_expired(listing_id: int) -> bool:
    try:
        listing = db.get_listing_by_id(listing_id)
        if listing and listing["status"] == "sold":
            return False                               # a sold card never turns into "expired"
        return _finish(listing_id, "expired")
    except Exception:
        logger.exception("Market channel: mark_expired crashed for listing %s", listing_id)
        return False
