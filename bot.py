"""
Main Telegram bot: character spawn & collection game.

Setup:
    pip install python-telegram-bot --upgrade
    (fill BOT_TOKEN in config.py)
    python bot.py

Last synced: 2026-09-13 - repo/Railway wiring check.
"""

import asyncio
from contextvars import ContextVar
import html
import logging
import math
import os
import random
import re
import tarfile
import threading
import unicodedata
import uuid
from urllib.parse import urlparse
from datetime import datetime

from telegram import (
    Update,
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    LinkPreviewOptions,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    TypeHandler,
    ContextTypes,
    filters,
)

import config
import database as db
import economy
import memories

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

FORCE_JOIN_LINK = "https://t.me/+itkOif9ZlFJkYTc0"
FORCE_JOIN_SETUP_COMMAND = "setforcejoin"

# In-memory store for /send submissions awaiting the owner's decision
# (add directly, or pick a rarity first). Key -> submission details.
# Cleared automatically once handled; lost on restart, which just means
# an unreviewed submission has to be re-sent - no persistent state needed.
PENDING_SUBMISSIONS = {}

# In-memory store for the button-based rarity/event picker that runs right
# after a photo is sent to /addcharacter or /send (before the character is
# actually added, or forwarded to the owner for /send). Key -> pending details.
# Same lifetime story as PENDING_SUBMISSIONS: lost on restart, no big deal.
PENDING_ADD_FLOW = {}

# Owner IDs currently expected to send a .tar.gz backup document after
# running /fileup. Cleared once the document arrives (or on restart -
# they'd just have to run /fileup again).
PENDING_FILE_RESTORE = set()

# In-memory wizard state for /new. The published posts themselves are
# persisted in SQLite; only the admin's unfinished composition lives here.
PENDING_NEW_FLOWS = {}

# The user and chat of the update being processed right now. Every bot message
# that carries personal buttons is bound to this user (see below).
_CURRENT_UPDATE_USER_ID = ContextVar("current_update_user_id", default=None)
_CURRENT_UPDATE_CHAT_ID = ContextVar("current_update_chat_id", default=None)
_PERSONAL_BUTTON_GUARD_INSTALLED = False

# ---- Personal buttons -------------------------------------------------------
# Rule: a button can only be pressed by the user whose action made the bot post
# it. Exactly two kinds of buttons are exempt:
#   * PUBLIC: the buttons of already-published /new posts - meant for everyone.
#   * SELF-GUARDED: buttons whose own handler already checks the user (the
#     force-join check carries the user id in its callback_data; the /send review
#     buttons are checked by role - reviewers other than the sender must be able to press them).
# Everything else is bound to its owner automatically - see
# _install_personal_button_guard() and _block_unowned_callbacks().
PUBLIC_CALLBACK_PREFIXES = ("new:lang:", "new:use:")
SELF_GUARDED_CALLBACK_PREFIXES = ("forcejoin:check:", "submit:")
_EXEMPT_CALLBACK_PREFIXES = PUBLIC_CALLBACK_PREFIXES + SELF_GUARDED_CALLBACK_PREFIXES


# Every command this bot handles (filled in by main() from the registered handlers).
KNOWN_COMMANDS = set()


def _our_command(message, bot_username: str):
    """The command name (lowercase, no slash) if `message` is a command meant for THIS
    bot, else None. Other bots' commands - "/play@OtherBot", or a plain "/daily" that only
    some other bot in the group understands - must not trigger anything here (join
    prompts, ban notices, user tracking)."""
    if message is None:
        return None
    text = message.text or message.caption or ""
    if not text.startswith("/"):
        return None
    name, _, target = text.split(maxsplit=1)[0][1:].partition("@")
    if target and target.lower() != (bot_username or "").lower():
        return None  # explicitly addressed to a different bot
    name = name.lower()
    return name if name in KNOWN_COMMANDS else None


def _markup_has_personal_buttons(reply_markup) -> bool:
    """True if the keyboard has at least one callback button that must be personal."""
    if not reply_markup or not getattr(reply_markup, "inline_keyboard", None):
        return False
    return any(
        getattr(button, "callback_data", None) is not None
        and not str(button.callback_data).startswith(_EXEMPT_CALLBACK_PREFIXES)
        for row in reply_markup.inline_keyboard
        for button in row
    )


def _install_personal_button_guard():
    """Remember, for every message the bot sends with personal buttons, which
    user's action triggered it. Only messages posted into the SAME chat as the
    triggering update are bound: anything the bot sends elsewhere (a /send
    submission forwarded to the owner, a post published to a channel, a DM)
    is meant for someone else and stays unbound."""
    global _PERSONAL_BUTTON_GUARD_INSTALLED
    if _PERSONAL_BUTTON_GUARD_INSTALLED:
        return

    async def _send_and_record(original, bot_self, *args, **kwargs):
        reply_markup = kwargs.get("reply_markup")
        result = await original(bot_self, *args, **kwargs)
        owner_id = _CURRENT_UPDATE_USER_ID.get()
        if owner_id and result is not None and _markup_has_personal_buttons(reply_markup):
            chat_id = getattr(getattr(result, "chat", None), "id", None)
            message_id = getattr(result, "message_id", None)
            if chat_id is not None and message_id is not None and chat_id == _CURRENT_UPDATE_CHAT_ID.get():
                try:
                    db.set_personalized_button_owner(chat_id, message_id, owner_id)
                except Exception:
                    logger.exception("Failed to save personalized button owner")
        return result

    for method_name in ("send_message", "send_photo", "send_video", "send_animation", "send_document"):
        original = getattr(Bot, method_name)

        async def wrapped(bot_self, *args, _original=original, **kwargs):
            return await _send_and_record(_original, bot_self, *args, **kwargs)

        setattr(Bot, method_name, wrapped)

    _PERSONAL_BUTTON_GUARD_INSTALLED = True


async def _ignore_edited_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Editing an old message must not re-run a command (e.g. /dart) or count as a
    new group message for the spam/spawn counters - nothing in this bot needs edits."""
    if update.edited_message is not None or update.edited_channel_post is not None:
        raise ApplicationHandlerStop


async def _bind_update_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First handler to run for every update: records who/where it came from so
    the messages the bot sends while handling it can be bound to that user."""
    user = update.effective_user
    chat = update.effective_chat
    _CURRENT_UPDATE_USER_ID.set(user.id if user else None)
    _CURRENT_UPDATE_CHAT_ID.set(chat.id if chat else None)


async def _block_unowned_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let a callback button through only for the user it was made for."""
    query = update.callback_query
    if query is None or query.from_user is None or query.message is None:
        return
    if (query.data or "").startswith(_EXEMPT_CALLBACK_PREFIXES):
        return

    owner_id = db.get_personalized_button_owner(query.message.chat.id, query.message.message_id)
    if owner_id is None:
        # Messages sent before this guard existed (or in other chats) have no owner - leave them alone.
        return

    if query.from_user.id != owner_id:
        await query.answer("⛔ This button belongs to another user.", show_alert=True)
        raise ApplicationHandlerStop


async def _consume_buttons(query) -> bool:
    """Strips a confirm/cancel keyboard BEFORE acting on the tap. If the buttons
    are already gone (a double tap, or two taps queued up), this returns False and
    the caller must do nothing - otherwise one card could be sold/gifted twice."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        return True
    except TelegramError:
        return False


def _mention(user_id: int, name: str = None) -> str:
    """Clickable user name for HTML messages, safe against names containing < > &."""
    return f'<a href="tg://user?id={user_id}">{html.escape(name or "Player")}</a>'


# ---------------- Helpers ----------------

def is_admin(user_id: int) -> bool:
    """The owner - full access to everything."""
    return user_id == config.ADMIN_ID


def has_command_permission(user_id: int, permission: str) -> bool:
    """Owner always passes; anyone else needs this specific permission
    (see config.ADMIN_PERMISSIONS) granted via /admin or /addadmin."""
    return is_admin(user_id) or db.has_permission(user_id, permission)


def _volume_dir() -> str:
    """Directory holding the persistent DB file - the Railway volume mount
    (e.g. /data) when DB_PATH is set to a path inside it, or the current
    working directory for local/dev runs where DB_PATH has no folder."""
    return os.path.dirname(os.path.abspath(config.DB_PATH)) or "."


def format_display_name(user_id: int, name: str) -> str:
    """Appends the premium star to a display name wherever a username
    shows up publicly (Market listings, /check's "discovered by", ...)."""
    if user_id and db.is_premium(user_id):
        return f"{name} ⭐️"
    return name


async def send_character_result(context: ContextTypes.DEFAULT_TYPE, chat_id: int, character, caption: str, reply_to_message_id: int = None, reply_markup=None):
    """Sends a character's photo (or video, for video cards) with `caption`
    as the caption - the standard way to confirm any command that performs
    an action on a single card (gift, sellbot, trade, /player add/remove,
    /give, /removecharacter, /sell, ...), so the result shows the card
    itself instead of a plain text line."""
    kwargs = {"caption": caption, "parse_mode": ParseMode.HTML}
    if reply_to_message_id:
        kwargs["reply_to_message_id"] = reply_to_message_id
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    file_id = character["image_file_id"]
    if (character["media_type"] or "photo") == "video":
        await context.bot.send_video(chat_id=chat_id, video=file_id, **kwargs)
    else:
        await context.bot.send_photo(chat_id=chat_id, photo=file_id, **kwargs)


async def _force_join_required(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True when the user is not currently a member of the required group."""
    user = update.effective_user
    if user is None or user.is_bot:
        return False

    settings = db.get_force_join_settings()
    if not settings:
        # The owner must run /setforcejoin once inside the target group so the
        # bot can learn the group's numeric chat ID.
        return False

    try:
        member = await context.bot.get_chat_member(settings["chat_id"], user.id)
        return not _is_group_member(member)
    except Exception:
        logger.exception("Force-join membership check failed for user %s", user.id)
        # Fail closed: if the bot cannot verify membership, do not grant access.
        return True


def _is_group_member(member) -> bool:
    """True if this ChatMember is actually inside the group right now.
    A 'restricted' user can be either still in the group (is_member=True)
    or already gone (is_member=False), so that flag has to be checked."""
    if member.status in ("creator", "administrator", "member"):
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


def _force_join_markup(settings=None, user_id=None):
    invite_link = (settings["invite_link"] if settings else FORCE_JOIN_LINK) or FORCE_JOIN_LINK
    target_user_id = int(user_id or 0)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join the group", url=invite_link)],
        [InlineKeyboardButton("✅ I joined — Check", callback_data=f"forcejoin:check:{target_user_id}")],
    ])


async def _block_non_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block bot commands/buttons until the user is currently in the required group."""
    user = update.effective_user
    if user is None or user.is_bot:
        return

    # Force-join must only run for commands and callback buttons.
    # Normal messages must never receive a membership warning.
    # Only OUR commands: another bot's "/command@OtherBot" (or a command this bot
    # doesn't have) must not get a membership warning from us.
    message = update.effective_message
    command = _our_command(message, context.bot.username)
    is_command = command is not None
    is_callback = update.callback_query is not None

    # The setup command and membership-check button must always be allowed through.
    if command == "setforcejoin":
        return
    if update.callback_query and (update.callback_query.data or "").startswith("forcejoin:check:"):
        return

    if not (is_command or is_callback):
        return

    if not await _force_join_required(update, context):
        return

    settings = db.get_force_join_settings()
    notice = (
        "🔒 <b>Group membership required</b>\n\n"
        "Please join the required group first, then tap <b>I joined — Check</b>."
    )
    try:
        if update.callback_query:
            await update.callback_query.answer("🔒 Please join the required group first.", show_alert=True)
            # For buttons tapped in a channel post, never post the join prompt
            # back into the channel. Send it privately to the clicking user.
            if update.callback_query.message and update.callback_query.message.chat.type == "channel":
                await context.bot.send_message(
                    chat_id=user.id,
                    text=notice,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_force_join_markup(settings, user.id),
                )
            else:
                await update.callback_query.message.reply_text(
                    notice, parse_mode=ParseMode.HTML, reply_markup=_force_join_markup(settings, user.id)
                )
        elif update.effective_message:
            await update.effective_message.reply_text(
                notice, parse_mode=ParseMode.HTML, reply_markup=_force_join_markup(settings, user.id)
            )
    except Exception:
        logger.exception("Failed to send force-join prompt to user %s", user.id)
    raise ApplicationHandlerStop


async def set_force_join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register the group where membership is required. Owner-only setup."""
    user = update.effective_user
    chat = update.effective_chat
    if not is_admin(user.id):
        await update.effective_message.reply_text("⛔ Only the owner can configure the required group.")
        return
    if chat is None or chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "⚠️ Run /setforcejoin inside the required group."
        )
        return

    try:
        me = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat.id, me.id)
        if bot_member.status not in ("administrator", "creator"):
            await update.effective_message.reply_text(
                "⚠️ Please make the bot an administrator in this group, then run /setforcejoin again."
            )
            return
    except Exception:
        await update.effective_message.reply_text(
            "⚠️ I couldn't verify my admin status in this group. Please make me an administrator and try again."
        )
        return

    # Generate a fresh primary invite link owned by this bot. Telegram
    # guarantees this link is permanent unless it is revoked, and generating
    # a new primary link also replaces the bot's previous primary link.
    try:
        invite_link = await context.bot.export_chat_invite_link(chat.id)
    except Exception:
        logger.exception("Failed to create permanent force-join invite link for chat %s", chat.id)
        await update.effective_message.reply_text(
            "⚠️ I couldn't create a permanent invite link. Please make sure I am an administrator "
            "with permission to invite users, then try again."
        )
        return

    db.set_force_join_settings(chat.id, invite_link)
    await update.effective_message.reply_text(
        "✅ Force-join group configured.\n\n"
        "A permanent invite link was created for this group.\n"
        "From now on, users must be members of this group to use the bot, "
        "and leaving the group will remove their access immediately."
    )


async def force_join_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'I joined - Check' button. Personal: only the user the prompt was
    made for can press it. On success the prompt message itself is edited
    (no new message); on failure a popup tells them and the button stays."""
    query = update.callback_query
    user = query.from_user

    try:
        target_id = int(query.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("⚠️ Invalid button.", show_alert=True)
        return

    if target_id == 0:
        # Prompts sent by older versions weren't bound to anyone. They were
        # sent as a reply to the user's command, so its author is the owner.
        replied = getattr(query.message, "reply_to_message", None)
        replied_user = replied.from_user if replied else None
        target_id = replied_user.id if (replied_user and not replied_user.is_bot) else user.id

    if user.id != target_id:
        await query.answer("⛔ This button belongs to another user.", show_alert=True)
        return

    settings = db.get_force_join_settings()
    if not settings:
        await query.answer(
            "⚠️ The required group has not been configured yet. Please contact the owner.",
            show_alert=True,
        )
        return

    try:
        member = await context.bot.get_chat_member(settings["chat_id"], user.id)
        joined = _is_group_member(member)
    except Exception:
        logger.exception("Force-join re-check failed for user %s", user.id)
        joined = False

    if not joined:
        await query.answer(
            "❌ I still can't see you in the required group. Join it first, then check again.",
            show_alert=True,
        )
        return

    await query.answer()
    confirmed_text = "✅ Membership confirmed. You can use the bot now."
    try:
        # Edit the prompt in place (this also removes its buttons).
        await query.edit_message_text(confirmed_text)
    except Exception:
        logger.exception("Could not edit force-join prompt for user %s", user.id)
        if query.message is not None:
            await query.message.reply_text(confirmed_text)


async def _capture_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs early, ahead of the normal command handlers, for every incoming update.
    Only counts someone as a "real" user of the bot - and refreshes their
    display name - when they've actually used it: ran a command, tapped
    a button, or messaged the bot privately. A plain, non-command message
    in a group the bot happens to be in does NOT count (that's just
    someone talking in a group, not using the bot) - see /stats. Mini App
    usage is captured separately in api_server.py's current_user(), since
    it never comes through here.
    """
    user = update.effective_user
    if user is None or user.is_bot:
        return

    message = update.effective_message
    is_command = _our_command(message, context.bot.username) is not None
    is_private_message = bool(message and update.effective_chat and update.effective_chat.type == "private")
    is_button_tap = update.callback_query is not None

    if not (is_command or is_private_message or is_button_tap):
        return

    try:
        db.upsert_user_profile(
            user.id, username=user.username, first_name=user.first_name, last_name=user.last_name
        )
    except Exception:
        logger.exception("Failed to refresh display info for user %s", user.id)

    chat = update.effective_chat
    if chat is not None and chat.type in ("group", "supergroup"):
        try:
            db.upsert_chat_title(chat.id, chat.title)
        except Exception:
            logger.exception("Failed to refresh title for chat %s", chat.id)


def name_matches(guess: str, full_name: str) -> bool:
    """
    Flexible match for /get [name]:
    - exact full name match
    - matches just one word of the name (first OR last name)
    - if the character card holds multiple characters (name separated by
      '&', '/', ',' or ' and '), matches any of those sub-names, or any
      single word within them
    """
    guess = guess.strip().lower()
    if not guess:
        return False

    if guess == full_name.strip().lower():
        return True

    sub_names = re.split(r"\s*[&/,]\s*|\s+and\s+", full_name, flags=re.IGNORECASE)
    for sub in sub_names:
        sub = sub.strip()
        if not sub:
            continue
        if guess == sub.lower():
            return True
        for word in sub.split():
            if guess == word.lower():
                return True

    return False


async def _send_character_media(bot, chat_id, character, caption=None, parse_mode=None, reply_markup=None):
    """Sends a character's card as whichever media type it was added with
    (photo or video) - every place that shows a character's image goes
    through this instead of assuming send_photo."""
    kwargs = {"caption": caption, "parse_mode": parse_mode, "reply_markup": reply_markup}
    if character["media_type"] == "video":
        return await bot.send_video(chat_id=chat_id, video=character["image_file_id"], **kwargs)
    return await bot.send_photo(chat_id=chat_id, photo=character["image_file_id"], **kwargs)


async def try_spawn(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    character = db.pick_random_character(chat_id)
    if character is None:
        return False  # no characters added yet, nothing to spawn

    rarity_name = character["rarity_name"]
    prefix = rarity_name[0] if rarity_name else "✨"

    text = (
        f"{prefix}𝛢 𝛈ew 𝝇elestial relic\n"
        "awaits its keeper!\n"
        "Use <b>/get [Name]</b> to make it part of your constellation ✨️"
    )

    if character["image_file_id"]:
        msg = await _send_character_media(
            context.bot, chat_id, character, caption=text, parse_mode=ParseMode.HTML
        )
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )

    db.set_pending_spawn(chat_id, character["id"], msg.message_id)
    db.reset_spawn_counter(chat_id)
    return True


# ---------------- Message handler (counts messages + spam) ----------------

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    exempt = user_id in config.SPAM_MUTE_EXEMPT_USER_IDS

    just_muted, streak, would_have_muted = db.register_message_for_spam(
        chat_id, user_id,
        config.MAX_CONSECUTIVE_MESSAGES,
        config.SPAM_PUNISHMENT_MINUTES,
        streak_reset_minutes=config.SPAM_STREAK_RESET_MINUTES,
        exempt_from_mute=exempt,
    )
    logger.info(f"[SPAM-DEBUG] chat={chat_id} user={user_id} streak={streak} just_muted={just_muted}")

    if just_muted:
        await update.message.reply_text(
            "✦ 𝛦𝛘𝝇essive 𝛢𝝇tivit𝛄 𝐷etected❗️\n"
            f"⏳ 𝛶𝛐𝛖'𝛎e 𝛃ee𝛈 m𝛖𝛕ed f𝛐r {config.SPAM_PUNISHMENT_MINUTES} minutes due 𝛕𝛐 "
            "𝛠𝛘𝝇essive 𝛼𝝇tivity ."
        )
        return

    if exempt and would_have_muted:
        await update.message.reply_text(
            "😅 You're too pretty to be ignored, but please don't spam."
        )
        return

    if db.is_user_muted(chat_id, user_id):
        # message doesn't count toward the spawn counter while muted
        return

    count, distinct = db.register_message_for_spawn(chat_id, user_id)

    if count >= config.MESSAGES_NEEDED_TO_SPAWN:
        # if a previous card was never claimed, this new spawn replaces it -
        # the old one stops being claimable since /get only checks the
        # current pending character.
        await try_spawn(chat_id, context)


# ---------------- /get ----------------


async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if db.is_user_muted(chat_id, user.id):
        await update.message.reply_text(
            "🚫 You're temporarily muted from claiming characters due to spam. Try again later!"
        )
        return

    capture_limit = config.PREMIUM_DAILY_CAPTURE_LIMIT if db.is_premium(user.id) else config.DAILY_CAPTURE_LIMIT
    if db.get_daily_capture_count(user.id) >= capture_limit:
        await update.message.reply_text(
            f"😴 You've reached your daily capture limit ({capture_limit}/{capture_limit}). "
            f"Your limit resets at midnight Iran time! 🌙"
        )
        return

    pending_id = db.get_pending_spawn(chat_id)
    if not pending_id:
        await update.message.reply_text("🎇𝛵here's 𝛈𝛐 𝝇haracter to get right now.")
        return

    if not context.args:
        await update.message.reply_text("✏️ Usage: <code>/get [character name]</code>", parse_mode=ParseMode.HTML)
        return

    guess = " ".join(context.args).strip()
    character = db.get_character(pending_id)
    if not character:
        # The spawned character was deleted while it was waiting to be claimed.
        db.clear_pending_spawn(chat_id)
        await update.message.reply_text("🎇𝛵here's 𝛈𝛐 𝝇haracter to get right now.")
        return

    if not name_matches(guess, character["name"]):
        await update.message.reply_text("❌ Wrong name, try again!")
        return

    claimed = db.claim_pending_spawn(chat_id, character["id"], user.id, user.username or user.first_name)
    if not claimed:
        # Someone else's /get won the race for this exact spawn between our
        # read above and the atomic claim - nothing was given to us.
        await update.message.reply_text("💨 Too slow! Someone already got that one.")
        return

    daily_count = db.increment_daily_capture(user.id)

    milestone_messages = memories.record_acquisition(user.id, character, "get")
    for msg in milestone_messages:
        try:
            await context.bot.send_message(chat_id=user.id, text=msg, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to DM milestone message to %s", user.id)

    rarity_text = character["rarity_name"] if character["rarity_name"] else "Unranked"
    claimer_name = _mention(user.id, user.first_name)

    # The claim above already added this copy, so 2+ means they had it before.
    copies = db.count_user_copies(user.id, character["id"])
    count_suffix = f" x{copies}" if copies >= 2 else ""

    text = (
        f"✨ <b>{claimer_name}</b> has got a celestial relic!\n\n"
        f"𝛮ame: <b>{character['name']}</b>{count_suffix}\n"
        f"𝛢nime: {character['series']}\n"
        f"𝛪𝐷: #{character['id']}\n"
        f"R𝛼rity: {rarity_text}\n\n"
        f"🌌 A new relic now shines among your constellations\n"
        f"Daily capture: {daily_count}/{capture_limit}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🌌 See constellation",
            switch_inline_query_current_chat=f"constellation:{user.id}",
        )]
    ])

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ---------------- Constellation (collection) ----------------

CONSTELLATION_PAGE_SIZE = 8


def _rarity_rank_map() -> dict:
    """{rarity name: position} with the rarest tier at 0 (same order as /rarities)."""
    return {r["name"]: i for i, r in enumerate(sorted(db.list_rarities(), key=_rarity_list_order))}


def build_constellation_page(owner_id: int, owner_display_name: str, page: int):
    """Returns (text, keyboard) for one page of the constellation menu."""
    items = db.get_user_inventory(owner_id)

    # dedupe duplicate copies into counts, grouped by series in first-seen order
    counts = {}
    grouped = {}
    for item in items:
        cid = item["id"]
        counts[cid] = counts.get(cid, 0) + 1
        series_list = grouped.setdefault(item["series"], {})
        series_list.setdefault(cid, item)

    # Rarest first. Cards stay grouped by series (one header per series); inside a
    # series the rarest card comes first, and the series holding the rarest card
    # comes first. Python's sort is stable, so ties keep their "newest first" order.
    rank = _rarity_rank_map()
    unranked = len(rank)

    def _card_rank(card):
        return rank.get(card["rarity_name"], unranked)

    ordered_series = sorted(
        grouped.items(),
        key=lambda kv: min(_card_rank(c) for c in kv[1].values()),
    )

    flat_entries = []  # (series, item) pairs, series-contiguous
    for series, chars_by_id in ordered_series:
        for item in sorted(chars_by_id.values(), key=_card_rank):
            flat_entries.append((series, item))

    unique_count = len(flat_entries)
    total_cards = len(items)

    page_size = CONSTELLATION_PAGE_SIZE
    total_pages = max(1, (unique_count + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_entries = flat_entries[start:start + page_size]

    active_filters = db.get_user_filters(owner_id)
    if active_filters:
        kinds = [k for k in db.FILTER_TYPES if any(f["filter_type"] == k for f in active_filters)]
        search_line = ", ".join(_SORT_KIND_INFO[k][1] for k in kinds)
        sort_line = " • ".join(
            ", ".join(html.escape(f["filter_value"]) for f in active_filters if f["filter_type"] == k)
            for k in kinds
        )
    else:
        search_line = "None"
        sort_line = "None"

    lines = [
        "╭━━━「 Constellation 」━━━╮",
        "",
        f"✦ {owner_display_name}'s collection",
        f"└ Total: {total_cards} • Unique: {unique_count}",
        "",
        f"⌕ Search › {search_line}",
        f"↳ Sort › {sort_line}",
        "",
        "╰━━━━━━━━━━━━━━━━━╯",
        "",
    ]

    last_series = None
    for series, item in page_entries:
        if series != last_series:
            if last_series is not None:
                lines.append("──────✧──────")  # between two series
            lines.append(f"◈ <b>{html.escape(series)}</b>")
            last_series = series
        rarity_char = item["rarity_name"][0] if item["rarity_name"] else "?"
        event_tag = f" [{item['event_name'][0]}]" if item["event_name"] else ""
        count = counts[item["id"]]
        suffix = f" x{count}" if count > 1 else ""
        lines.append(f"▸   {rarity_char}〔{item['id']}〕 {html.escape(item['name'])}{event_tag}{suffix}")
    lines.append("━━━━━━━━━━━━━━━━━")

    text = "\n".join(lines)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"conspage:{owner_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"conspage:{owner_id}:{page + 1}"))

    keyboard_rows = []
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([InlineKeyboardButton(
        "See constellation", switch_inline_query_current_chat=f"constellation:{owner_id}"
    )])

    return text, InlineKeyboardMarkup(keyboard_rows)


async def send_constellation_summary(context: ContextTypes.DEFAULT_TYPE, target_chat_id: int,
                                       owner_id: int, owner_display_name: str, page: int = 0):
    """Sends the player's /fav card (or a random owned character if they haven't
    set one) + the constellation menu (page 1 by default), with buttons to page
    through it and open the full photo gallery."""
    items = db.get_user_inventory(owner_id)
    if not items:
        await context.bot.send_message(chat_id=target_chat_id, text="🌌 𝛵h𝛊s 𝝇𝛐𝛈stell𝛼𝛕𝛊𝛐𝛈 is 𝛠𐌼𝛒𝛕𝛄!")
        return

    text, keyboard = build_constellation_page(owner_id, owner_display_name, page)

    favorite = db.get_favorite_card(owner_id)
    featured_item = favorite if (favorite and favorite["image_file_id"]) else random.choice(items)
    if featured_item["image_file_id"]:
        await _send_character_media(
            context.bot, target_chat_id, featured_item,
            caption=text[:1024], parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def constellation_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    _, owner_id_str, page_str = query.data.split(":")
    owner_id = int(owner_id_str)
    page = int(page_str)

    if query.from_user.id != owner_id:
        await query.answer("⛔ This button belongs to another user.", show_alert=True)
        return
    await query.answer()

    try:
        chat = await context.bot.get_chat(owner_id)
        display_name = _mention(owner_id, chat.first_name)
    except Exception:
        stored_name = db.get_username_for_user(owner_id)
        display_name = stored_name or "Player"

    text, keyboard = build_constellation_page(owner_id, display_name, page)

    try:
        # Text-only menu -> edit the text; any media card (photo OR video) -> edit the caption.
        if getattr(query.message, "text", None):
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await query.edit_message_caption(caption=text[:1024], parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        pass  # e.g. "message not modified" when re-clicking the same page


async def constellation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone command to view your own constellation, posted right in the chat it was called from."""
    user = update.effective_user
    display_name = _mention(user.id, user.first_name)
    chat_id = update.effective_chat.id

    await send_constellation_summary(context, chat_id, user.id, display_name)


# ---------------- /fav (favorite card) ----------------

async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fav [ID] - shows the card and asks OK / Cancel before pinning it as your
    favorite (/constellation then always shows that card's picture)."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/fav [ID]</code>\n"
            "Pick one of your cards - it becomes the picture shown on your /constellation.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        char_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    user = update.effective_user

    if not db.user_owns_character(user.id, char_id):
        await update.message.reply_text("❓ You don't own that card.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    caption = (
        f"❤️ Make <b>{html.escape(character['name'])}</b> (#{char_id}) your favorite card?\n"
        "It will be the picture on your /constellation."
    )
    await _send_confirmation(
        update, context, character, caption, f"fav:ok:{user.id}:{char_id}", f"fav:cancel:{user.id}"
    )


async def _edit_text_or_caption(query, text: str, reply_markup=None):
    """Rewrites a bot message whether it is a plain text message or a photo/video card
    (where the words live in the caption). Without `reply_markup` the buttons go away."""
    if getattr(query.message, "text", None):
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def _send_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, character, text: str,
                             ok_data: str, cancel_data: str):
    """The "are you sure?" step every card/currency action goes through: the card's picture
    (when there is a card) with the question as its caption and ✅ OK / ❌ Cancel under it.
    Nothing happens until OK is pressed - by the same person only."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ OK", callback_data=ok_data),
        InlineKeyboardButton("❌ Cancel", callback_data=cancel_data),
    ]])
    if character and character["image_file_id"]:
        await send_character_result(
            context, update.effective_chat.id, character, text,
            reply_to_message_id=update.message.message_id, reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def fav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action, owner_id = parts[1], int(parts[2])

    if query.from_user.id != owner_id:
        await query.answer("⛔ This button belongs to another user.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Cancelled - your favorite card stays as it was.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    char_id = int(parts[3])
    character = db.get_character(char_id)
    if not character or not db.user_owns_character(owner_id, char_id):
        await query.answer()
        await _edit_text_or_caption(query, "❓ You no longer own that card.")
        return

    db.set_favorite_card(owner_id, char_id)
    await query.answer("❤️ Saved!")
    await _edit_text_or_caption(
        query,
        f"❤️ <b>{html.escape(character['name'])}</b> (#{char_id}) is now your favorite card.\n"
        "It will be the picture on your /constellation from now on.",
    )


# ---------------- /miniapp ----------------

MINIAPP_CARD_TEXT = (
    "───────── ✦ ─────────\n"
    "\n"
    "    𝗪𝗔𝗜𝗙𝗨 𝗠𝗔𝗥𝗞𝗘𝗧\n"
    "\n"
    "Your collection awaits.\n"
    "Collect, trade & discover new characters.\n"
    "\n"
    "───────── ✦ ─────────"
)
MINIAPP_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "miniapp.png")
_MINIAPP_PHOTO_FILE_ID = None  # cached after the first upload so the PNG isn't re-uploaded every time


def _miniapp_direct_link(bot_username: str = None) -> str:
    """The Mini App's direct t.me link: config.MINI_APP_DIRECT_LINK, else the bot's
    main Mini App link (https://t.me/<bot>?startapp). "" if neither is known."""
    link = (getattr(config, "MINI_APP_DIRECT_LINK", "") or "").strip()
    if link:
        return link
    return f"https://t.me/{bot_username}?startapp" if bot_username else ""


def _miniapp_launch_button(label: str, chat_type: str, bot_username: str = None):
    """A button that opens the Mini App, or None if there is no way to open it.
    In a PRIVATE chat with config.MINI_APP_URL set this is a real web_app button.
    Telegram doesn't allow those anywhere else, and they need the web URL, so in every
    other case the button is the Mini App's direct link - which also hands the Mini
    App its login data, exactly like the web_app button does."""
    url = (config.MINI_APP_URL or "").strip()
    if url and chat_type == "private":
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    direct_link = _miniapp_direct_link(bot_username)
    return InlineKeyboardButton(label, url=direct_link) if direct_link else None


async def miniapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _MINIAPP_PHOTO_FILE_ID

    button = _miniapp_launch_button("🌙 Open the Mini App", update.effective_chat.type, context.bot.username)
    if button is None:
        await update.message.reply_text("⚠️ The Mini App isn't available yet.")
        return
    keyboard = InlineKeyboardMarkup([[button]])

    if _MINIAPP_PHOTO_FILE_ID:
        try:
            await update.message.reply_photo(
                photo=_MINIAPP_PHOTO_FILE_ID, caption=MINIAPP_CARD_TEXT, reply_markup=keyboard
            )
            return
        except Exception:
            logger.exception("Cached /miniapp photo failed, re-uploading it")
            _MINIAPP_PHOTO_FILE_ID = None

    try:
        with open(MINIAPP_IMAGE_PATH, "rb") as photo:
            sent = await update.message.reply_photo(
                photo=photo, caption=MINIAPP_CARD_TEXT, reply_markup=keyboard
            )
    except FileNotFoundError:
        logger.warning("Mini App image not found at %s - sending text only", MINIAPP_IMAGE_PATH)
        await update.message.reply_text(MINIAPP_CARD_TEXT, reply_markup=keyboard)
        return

    if sent.photo:
        _MINIAPP_PHOTO_FILE_ID = sent.photo[-1].file_id


# ---------------- Dart game ----------------

def dart_reward(dice_value: int) -> int:
    if dice_value == 6:
        return config.DART_REWARD_BULLSEYE
    if dice_value == 5:
        return config.DART_REWARD_RING_TWO
    if dice_value in (3, 4):
        return config.DART_REWARD_RING_THREE
    return config.DART_REWARD_MISS  # 1 or 2 - missed the board


async def dart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    dart_limit = config.PREMIUM_DAILY_DART_LIMIT if db.is_premium(user.id) else config.DAILY_DART_LIMIT
    used_today = db.get_daily_dart_count(user.id)
    if used_today >= dart_limit:
        await update.message.reply_text(
            f"🎯 You're out of darts for today ({dart_limit}/{dart_limit}). "
            f"They come back at midnight Iran time!"
        )
        return

    dart_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎯")
    # Count the throw right away, then reveal the result in the background:
    # sleeping inside the handler would hold up every other update for 4 seconds.
    used_today = db.increment_daily_dart(user.id)
    context.application.create_task(
        _reveal_dart_result(context.bot, chat_id, update.message.message_id, user, dart_msg.dice.value, dart_limit - used_today)
    )


async def _reveal_dart_result(bot, chat_id: int, reply_to_message_id: int, user, dice_value: int, remaining: int):
    try:
        await asyncio.sleep(4)  # let the throw animation play out before revealing the result

        reward = dart_reward(dice_value)
        name_link = _mention(user.id, user.first_name)

        if reward > 0:
            balance = db.add_currency(user.id, reward)
            text = (
                f"{name_link}\n\n"
                f"🎉 𝐘𝐨𝐮 𝐰𝐨𝐧!\n"
                f"𝐀𝐦𝐨𝐮𝐧𝐭: {reward} {config.CURRENCY_SYMBOL}\n\n"
                f"🎯 𝐃𝐚𝐫𝐭𝐬 𝐥𝐞𝐟𝐭 𝐭𝐨𝐝𝐚𝐲: {remaining}\n"
                f"💰 𝐁𝐚𝐥𝐚𝐧𝐜𝐞: {balance} {config.CURRENCY_SYMBOL}"
            )
        else:
            balance = db.get_currency(user.id)
            text = (
                f"{name_link}\n\n"
                f"😔 𝐌𝐢𝐬𝐬𝐞𝐝 𝐭𝐡𝐞 𝐛𝐨𝐚𝐫𝐝!\n\n"
                f"🎯 𝐃𝐚𝐫𝐭𝐬 𝐥𝐞𝐟𝐭 𝐭𝐨𝐝𝐚𝐲: {remaining}\n"
                f"💰 𝐁𝐚𝐥𝐚𝐧𝐜𝐞: {balance} {config.CURRENCY_SYMBOL}"
            )

        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id, allow_sending_without_reply=True,
        )
    except Exception:
        logger.exception("Failed to reveal the /dart result for user %s", user.id)


INVENTORY_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "inventory.jpg")


async def inventory_currency_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = db.get_currency(user.id)
    card_count = len(db.get_user_inventory(user.id, apply_filter=False))
    rank = db.get_richest_rank(user.id)
    rank_text = f"#{rank}" if rank else "—"
    display_name = db.get_display_name(user.id)

    lines = [
        f"🎒 {_bold_sans('INVENTORY')} ",
        "",
        f"       👤 {html.escape(display_name)}",
        f"       🏆 {_bold_sans('Rank')}  {rank_text}",
        "",
        f"       💰 {_bold_sans(f'{balance:,}')} {config.CURRENCY_SYMBOL}",
        f"          {_bold_sans('BALANCE')}",
        "",
        f"       🎴 {_bold_sans(f'{card_count:,}')}",
        f"          {_bold_sans('CARDS')}",
    ]
    text = "\n".join(lines)

    try:
        with open(INVENTORY_IMAGE_PATH, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=text)
    except FileNotFoundError:
        await update.message.reply_text(text)


# ---------------- /vypay ----------------

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Reply to the person you want to pay, using <code>/vypay [amount]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/vypay [amount]</code> (as a reply to the recipient)",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Amount must be a number.")
        return

    if amount <= 0:
        await update.message.reply_text("⚠️ Amount must be greater than zero.")
        return

    sender = update.effective_user
    recipient = update.message.reply_to_message.from_user

    if recipient.id == sender.id:
        await update.message.reply_text("😅 You can't pay yourself!")
        return

    if recipient.is_bot:
        await update.message.reply_text("🤖 You can't pay a bot.")
        return

    if db.get_currency(sender.id) < amount:
        await update.message.reply_text("ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴇɴᴏᴜɢʜ VɎ")
        return

    await _send_confirmation(
        update, context, None,
        f"💸 Send <b>{amount} {config.CURRENCY_SYMBOL}</b> to {_mention(recipient.id, recipient.first_name)}?",
        f"pay:ok:{sender.id}:{recipient.id}:{amount}", f"pay:cancel:{sender.id}",
    )


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OK / Cancel under the /vypay confirmation. Only OK moves any currency."""
    query = update.callback_query
    parts = query.data.split(":")
    action, sender_id = parts[1], int(parts[2])

    if query.from_user.id != sender_id:
        await query.answer("Only the sender can confirm this.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Payment cancelled.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    recipient_id, amount = int(parts[3]), int(parts[4])
    remaining = db.transfer_currency(sender_id, recipient_id, amount)
    await query.answer()

    if remaining is None:
        await _edit_text_or_caption(query, "ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴇɴᴏᴜɢʜ VɎ")
        return

    try:
        recipient_chat = await context.bot.get_chat(recipient_id)
        recipient_name = _mention(recipient_id, recipient_chat.first_name)
    except Exception:
        recipient_name = _mention(recipient_id, None)

    await _edit_text_or_caption(
        query,
        "‌-------------💎ᴛʀᴀɴꜱꜰᴇʀ ᴄᴏᴍᴘʟᴇᴛᴇᴅ💎-------------\n\n"
        f"👝ʀᴇᴄᴇɪᴠᴇʀ:{recipient_name}\n"
        f"💲ᴀᴍᴏᴜɴᴛ:{amount} {config.CURRENCY_SYMBOL}\n"
        f"💰ʟᴇꜰᴛ:{remaining} {config.CURRENCY_SYMBOL}",
    )


# ---------------- Owner tools: /stats & /give ----------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    user_count, group_count = db.get_bot_stats()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👤 Users", callback_data="stats:users"),
        InlineKeyboardButton("👥 Groups", callback_data="stats:groups"),
    ]])
    await update.message.reply_text(
        "📊 <b>Bot Stats</b>\n\n"
        f"👤 Users: <b>{user_count}</b>\n"
        f"👥 Active groups: <b>{group_count}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


def _group_link(chat):
    """A link people can use to open this group, or None: t.me/<username> for public
    groups, otherwise the group's invite link - which Telegram only reveals to the bot
    when it is an admin of that group."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}"
    return getattr(chat, "invite_link", None) or None


def _chunk_lines(text: str, limit: int = 3800):
    """Splits `text` into pieces under Telegram's length cap, only ever between entries
    (cutting inside a line could cut through an HTML tag and make Telegram reject it).
    An indented line belongs to the entry above it, so a group's link stays under its name."""
    entries = []
    for line in text.split("\n"):
        if line.startswith(" ") and entries:
            entries[-1] += "\n" + line
        else:
            entries.append(line)

    chunks, current = [], ""
    for entry in entries:
        if current and len(current) + len(entry) + 1 > limit:
            chunks.append(current)
            current = entry
        else:
            current = f"{current}\n{entry}" if current else entry
    if current:
        chunks.append(current)
    return chunks


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Only the bot owner can view this.", show_alert=True)
        return

    kind = query.data.split(":")[1]
    await query.answer()

    MAX_LISTED = 100  # keeps a single reply well under Telegram's message-length cap

    if kind == "users":
        rows = db.list_bot_users()
        if not rows:
            await query.message.reply_text("👤 No users recorded yet.")
            return
        lines = []
        for row in rows[:MAX_LISTED]:
            name = f"@{row['username']}" if row["username"] else (row["first_name"] or "—")
            lines.append(f"• <code>{row['user_id']}</code> — {html.escape(name)}")
        text = f"👤 <b>Users ({len(rows)})</b>\n\n" + "\n".join(lines)
        if len(rows) > MAX_LISTED:
            text += f"\n\n… and {len(rows) - MAX_LISTED} more."
    else:
        rows = db.list_active_groups()
        if not rows:
            await query.message.reply_text("👥 No groups recorded yet.")
            return
        lines = []
        for row in rows[:MAX_LISTED]:
            # Confirm the bot is still actually in this chat before listing
            # it - a group it was removed from shouldn't show up as "active".
            try:
                member = await context.bot.get_chat_member(row["chat_id"], context.bot.id)
                if member.status in ("left", "kicked"):
                    db.remove_chat_state(row["chat_id"])
                    continue
                chat = await context.bot.get_chat(row["chat_id"])
                title = chat.title or row["title"]
                if title and title != row["title"]:
                    db.upsert_chat_title(row["chat_id"], title)
            except Exception:
                # No longer reachable (removed, chat deleted, etc.) - drop it.
                db.remove_chat_state(row["chat_id"])
                continue
            link = _group_link(chat)
            lines.append(
                f"• <code>{row['chat_id']}</code> — {html.escape(title or '(unknown title)')}\n"
                + (f"   🔗 {html.escape(link)}" if link else "   🔒 No link available")
            )

        if not lines:
            await query.message.reply_text("👥 The bot isn't currently active in any group.")
            return

        text = f"👥 <b>Groups ({len(lines)})</b>\n\n" + "\n".join(lines)
        if len(rows) > MAX_LISTED:
            text += f"\n\n… and {len(rows) - MAX_LISTED} more not checked."

    # Split between lines if the list exceeds Telegram's 4096-char cap. Link previews are
    # off: with many links in one message Telegram would preview only the first one.
    for chunk in _chunk_lines(text):
        await query.message.reply_text(
            chunk, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )


async def give_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "GIVE"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Reply to the person you want to give to, using:\n"
            "<code>/give [card ID]</code> for a card\n"
            "<code>/give [amount] vy</code> for currency",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/give [ID]</code> or <code>/give [amount] vy</code> (as a reply to the recipient)",
            parse_mode=ParseMode.HTML,
        )
        return

    recipient = update.message.reply_to_message.from_user

    if recipient.is_bot:
        await update.message.reply_text("🤖 You can't give to a bot.")
        return

    try:
        value = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ The first argument must be a number.")
        return

    if value <= 0:
        await update.message.reply_text("⚠️ The amount/ID must be greater than zero.")
        return

    is_currency = len(context.args) > 1 and context.args[1].lower() == "vy"
    recipient_name = _mention(recipient.id, recipient.first_name)

    if is_currency:
        new_balance = db.add_currency(recipient.id, value)
        await update.message.reply_text(
            f"💎 Gave <b>{value} {config.CURRENCY_SYMBOL}</b> to {recipient_name}.\n"
            f"💰 Their new balance: <b>{new_balance} {config.CURRENCY_SYMBOL}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    char_id = value
    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    recipient_username = recipient.username or recipient.first_name
    db.give_character_to_user(recipient.id, recipient_username, char_id)
    await send_character_result(
        context, update.effective_chat.id, character,
        f"🎁 Gave <b>{character['name']}</b> (#{char_id}) to {recipient_name}.",
        reply_to_message_id=update.message.message_id,
    )


# ---------------- /top (public leaderboards) ----------------

def _top_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💎 VɎ", callback_data="top:vy"),
            InlineKeyboardButton("🎴 Collection", callback_data="top:collection"),
        ]
    ])


def _format_top_name(user_id: int) -> str:
    """Best available, HTML-safe player name for a leaderboard row."""
    name = html.escape(db.get_display_name(user_id))
    if len(name) > 28:
        name = name[:25] + "..."
    return name


def _build_top_text(mode: str) -> str:
    if mode == "vy":
        rows = db.get_richest_users(limit=10)
        title = "💎 𝗧𝗢𝗣 𝟭𝟬 — 𝗩Ɏ"
        if not rows:
            return f"╔══════════════════════════╗\n║     {title}     ║\n╚══════════════════════════╝\n\nNo players have VɎ yet."
        lines = [f"╔══════════════════════════╗", f"║     {title}     ║", "╚══════════════════════════╝", ""]
        for rank, row in enumerate(rows, 1):
            lines.append(f"{rank:>2}. {_format_top_name(row['user_id'])} — <b>{row['balance']:,} VɎ</b>")
        return "\n".join(lines)

    rows = db.get_top_collectors(limit=10)
    title = "🎴 𝗧𝗢𝗣 𝟭𝟬 — 𝗖𝗢𝗟𝗟𝗘𝗖𝗧𝗜𝗢𝗡"
    if not rows:
        return f"╔══════════════════════════╗\n║ {title} ║\n╚══════════════════════════╝\n\nNo cards have been collected yet."
    lines = ["╔══════════════════════════╗", f"║ {title} ║", "╚══════════════════════════╝", ""]
    for rank, row in enumerate(rows, 1):
        count = row["card_count"]
        card_word = "card" if count == 1 else "cards"
        lines.append(f"{rank:>2}. {_format_top_name(row['user_id'])} — <b>{count:,} {card_word}</b>")
    return "\n".join(lines)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.args[0].lower() if context.args else None

    if mode not in (None, "vy", "collection"):
        await update.message.reply_text(
            "⚠️ Usage: <code>/top</code>, <code>/top vy</code> or <code>/top collection</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if mode is None:
        await update.message.reply_text(
            "🏆 <b>Choose a leaderboard:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_top_keyboard(),
        )
        return

    await update.message.reply_text(
        _build_top_text(mode),
        parse_mode=ParseMode.HTML,
        reply_markup=_top_keyboard(),
    )


async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    mode = query.data.split(":", 1)[1]
    if mode not in ("vy", "collection"):
        return

    await query.edit_message_text(
        _build_top_text(mode),
        parse_mode=ParseMode.HTML,
        reply_markup=_top_keyboard(),
    )


# ---------------- /player (owner: manage a player's account) ----------------

def _player_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add character", callback_data="padmin:add_char")],
        [InlineKeyboardButton("➖ Remove character", callback_data="padmin:remove_char")],
        [InlineKeyboardButton(f"💰 Give {config.CURRENCY_SYMBOL}", callback_data="padmin:give_money")],
        [InlineKeyboardButton(f"💸 Take {config.CURRENCY_SYMBOL}", callback_data="padmin:take_money")],
        [InlineKeyboardButton("✅ Done", callback_data="padmin:done")],
    ])


async def player_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "PLAYER"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    # /player can now take either a numeric Telegram ID or a username
    # directly. Keeping the old prompt when no argument is supplied means
    # the interactive flow still works exactly as before.
    if context.args:
        value = context.args[0].strip()
        target_id = None

        if value.lstrip("+").isdigit():
            try:
                target_id = int(value)
            except ValueError:
                target_id = None
        else:
            target_id = db.get_user_id_by_username(value.lstrip("@"))

        if not target_id:
            label = value if value.startswith("@") else f"@{value}"
            await update.message.reply_text(
                f"❓ No player found for <code>{label}</code>.\n"
                "Use a valid Telegram numeric ID or username.\n"
                "The player must have interacted with the bot before when using a username.",
                parse_mode=ParseMode.HTML,
            )
            return

        context.user_data.pop("player_awaiting_username", None)
        context.user_data.pop("player_awaiting_action", None)
        context.user_data["player_target_id"] = target_id
        display_name = db.get_display_name(target_id)
        await update.message.reply_text(
            f"👤 Managing <b>{html.escape(display_name)}</b> (<code>{target_id}</code>). What would you like to do?",
            parse_mode=ParseMode.HTML,
            reply_markup=_player_menu_keyboard(),
        )
        return

    context.user_data["player_awaiting_username"] = True
    context.user_data.pop("player_target_id", None)
    context.user_data.pop("player_awaiting_action", None)
    await update.message.reply_text("✏️ Send the player's @username or numeric Telegram ID:")


async def player_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    if not has_command_permission(uid, "PLAYER"):
        await query.edit_message_text("⛔ You're not allowed to use this.")
        return

    target_id = context.user_data.get("player_target_id")
    if not target_id:
        await query.edit_message_text("⚠️ Session expired - send /player again.")
        return

    action = query.data.split(":", 1)[1]

    if action == "done":
        context.user_data.pop("player_target_id", None)
        context.user_data.pop("player_awaiting_action", None)
        await query.edit_message_text("✅ Done managing that player's account.")
        return

    prompts = {
        "add_char": "✏️ Send the character ID to add to their collection:",
        "remove_char": "✏️ Send the character ID to remove from their collection:",
        "give_money": f"✏️ Send the amount of {config.CURRENCY_SYMBOL} to give them:",
        "take_money": f"✏️ Send the amount of {config.CURRENCY_SYMBOL} to take from them:",
    }
    context.user_data["player_awaiting_action"] = action
    await query.edit_message_text(prompts[action])


async def capture_player_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in the same group as capture_sort_input - only acts when the
    owner has an active /player prompt waiting for a typed value."""
    text = update.message.text.strip() if update.message.text else ""
    if not text:
        return

    if context.user_data.get("player_awaiting_username"):
        value = text.strip()
        target_id = None

        if value.lstrip("+").isdigit():
            try:
                target_id = int(value)
            except ValueError:
                target_id = None
        else:
            target_id = db.get_user_id_by_username(value.lstrip("@"))

        if not target_id:
            await update.message.reply_text(
                "❓ No player found. Send a valid numeric Telegram ID or @username. "
                "For usernames, the player needs to have interacted with the bot at least once."
            )
            return

        context.user_data.pop("player_awaiting_username", None)
        context.user_data["player_target_id"] = target_id
        display_name = db.get_display_name(target_id)
        await update.message.reply_text(
            f"👤 Managing <b>{html.escape(display_name)}</b> (<code>{target_id}</code>). What would you like to do?",
            parse_mode=ParseMode.HTML,
            reply_markup=_player_menu_keyboard(),
        )
        return

    action = context.user_data.get("player_awaiting_action")
    if not action:
        return

    target_id = context.user_data.get("player_target_id")
    if not target_id:
        context.user_data.pop("player_awaiting_action", None)
        await update.message.reply_text("⚠️ Session expired - send /player again.")
        return

    if action in ("add_char", "remove_char"):
        try:
            char_id = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a whole number ID.")
            return

        character = db.get_character(char_id)
        if not character:
            await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
            return

        if action == "add_char":
            target_username = db.get_username_for_user(target_id)
            db.give_character_to_user(target_id, target_username, char_id)
            result_text = f"✅ Added <b>{character['name']}</b> (#{char_id}) to their collection."
        else:
            removed = db.admin_remove_character_from_user(target_id, char_id)
            if not removed:
                await update.message.reply_text(
                    f"❓ They don't own a free (unlisted) copy of <b>{character['name']}</b> (#{char_id}).",
                    parse_mode=ParseMode.HTML,
                )
                return
            result_text = f"✅ Removed <b>{character['name']}</b> (#{char_id}) from their collection."

    else:  # give_money / take_money
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a whole number amount.")
            return
        if amount <= 0:
            await update.message.reply_text("⚠️ The amount must be greater than zero.")
            return

        if action == "give_money":
            new_balance = db.add_currency(target_id, amount)
            result_text = f"✅ Gave {amount} {config.CURRENCY_SYMBOL}. New balance: <b>{new_balance} {config.CURRENCY_SYMBOL}</b>."
        else:
            current = db.get_currency(target_id)
            taken = min(amount, current)
            new_balance = db.add_currency(target_id, -taken)
            result_text = f"✅ Took {taken} {config.CURRENCY_SYMBOL}. New balance: <b>{new_balance} {config.CURRENCY_SYMBOL}</b>."

    is_character_action = action in ("add_char", "remove_char")

    context.user_data.pop("player_awaiting_action", None)
    display_name = db.get_display_name(target_id)

    if is_character_action:
        await send_character_result(context, update.effective_chat.id, character, result_text)
        await update.message.reply_text(
            f"👤 Still managing <b>{html.escape(display_name)}</b>. What next?",
            parse_mode=ParseMode.HTML,
            reply_markup=_player_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"{result_text}\n\n👤 Still managing <b>{html.escape(display_name)}</b>. What next?",
            parse_mode=ParseMode.HTML,
            reply_markup=_player_menu_keyboard(),
        )


# ---------------- /ban ----------------

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "BAN"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    days = None
    target_id = None
    target_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.first_name
        if context.args:
            try:
                days = int(context.args[0])
            except ValueError:
                await update.message.reply_text("⚠️ The number of days must be a whole number.")
                return
    else:
        if not context.args:
            await update.message.reply_text(
                "⚠️ Usage: reply to the player with <code>/ban [days]</code>, or "
                "<code>/ban [user ID] [days]</code>. Leave days out for a permanent ban.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ User ID must be a number.")
            return
        if len(context.args) > 1:
            try:
                days = int(context.args[1])
            except ValueError:
                await update.message.reply_text("⚠️ The number of days must be a whole number.")
                return
        target_name = db.get_display_name(target_id)

    if target_id == update.effective_user.id:
        await update.message.reply_text("😅 You can't ban yourself.")
        return
    if days is not None and days <= 0:
        await update.message.reply_text("⚠️ The number of days must be greater than zero.")
        return

    db.ban_user(target_id, days=days, banned_by=update.effective_user.id)

    if days:
        await update.message.reply_text(f"🚫 {target_name} has been banned for {days} day(s).")
    else:
        await update.message.reply_text(f"🚫 {target_name} has been permanently banned.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "UNBAN"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ User ID must be a number.")
            return
    else:
        await update.message.reply_text(
            "⚠️ Usage: reply to the player with <code>/unban</code>, or <code>/unban [user ID]</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if db.unban_user(target_id):
        await update.message.reply_text("✅ Player unbanned.")
    else:
        await update.message.reply_text("❓ That player isn't banned.")


async def sick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A canned public "burn" with zero real effect - no ban, no mute, just
    the one fixed line below every time. Gated like /ban since it's a
    moderation-flavored joke, not something every player should be able to
    fire at each other."""
    uid = update.effective_user.id
    if not has_command_permission(uid, "SICK"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not update.message.reply_to_message or not context.args or context.args[0].lower() != "out":
        await update.message.reply_text(
            "⚠️ Usage: reply to the player with <code>/sick out</code>.", parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text("This user is nothing anymore and has completely gotten lost for good.")


async def _block_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs ahead of every other handler (see main()). Silently drops any
    update from a banned player - except the owner, who can never lock
    themselves out - so a ban actually stops them from using the bot at
    all, not just from specific commands.
    """
    user = update.effective_user
    if user is None or user.is_bot or is_admin(user.id):
        return

    ban = db.get_ban(user.id)
    if not ban:
        return

    message = update.effective_message
    is_command = _our_command(message, context.bot.username) is not None
    is_private = update.effective_chat is not None and update.effective_chat.type == "private"
    if message is not None and (is_command or is_private):
        # Only answer when the banned user actually tried to use the bot; their
        # ordinary chatter in a group is dropped silently instead of getting a reply each time.
        if ban["banned_until"]:
            until = datetime.fromisoformat(ban["banned_until"]).strftime("%Y-%m-%d %H:%M UTC")
            text = f"🚫 You're banned until {until}."
        else:
            text = "🚫 You're permanently banned."
        try:
            await message.reply_text(text)
        except Exception:
            pass
    elif update.callback_query is not None:
        try:
            await update.callback_query.answer("🚫 You're banned.", show_alert=True)
        except Exception:
            pass

    raise ApplicationHandlerStop


# ---------------- Owner tools: /artiststats ----------------

def _build_artist_stats():
    """
    Groups every character actually in the database (i.e. already approved
    and added - an unapproved /send submission never makes it into the
    characters table, so it's automatically excluded here) by whoever added
    it (added_by_user_id/added_by_username, set for both /addcharacter and
    an approved /send). Returns an ordered dict:
        {user_id: {"username": str, "total": int, "rarities": {rarity_name: count}}}
    sorted by total cards added, descending. Characters added with no
    recorded artist (added_by_user_id is NULL) are skipped entirely - there's
    no one to attribute them to.
    """
    artists = {}
    for character in db.get_all_characters():
        artist_id = character["added_by_user_id"]
        if not artist_id:
            continue
        rarity_name = character["rarity_name"] or "Unranked"

        entry = artists.get(artist_id)
        if entry is None:
            entry = {
                "username": character["added_by_username"] or "Unknown",
                "total": 0,
                "rarities": {},
            }
            artists[artist_id] = entry

        entry["total"] += 1
        entry["rarities"][rarity_name] = entry["rarities"].get(rarity_name, 0) + 1

    return dict(sorted(artists.items(), key=lambda item: item[1]["total"], reverse=True))


def _artist_mention(user_id: int, username: str) -> str:
    return _mention(user_id, format_display_name(user_id, username))


async def artist_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    artists = _build_artist_stats()
    if not artists:
        await update.message.reply_text("📭 No one has added any cards yet.")
        return

    lines = [_artist_mention(uid, entry["username"]) for uid, entry in artists.items()]
    text = f"🖌 <b>Artists ({len(artists)})</b>\n\n" + "\n".join(f"• {line}" for line in lines)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Cards per artist", callback_data="artiststats:byartist"),
        InlineKeyboardButton("💎 Rarities per artist", callback_data="artiststats:byrarity"),
    ]])

    for i in range(0, len(text), 4000):
        await update.message.reply_text(
            text[i:i + 4000],
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard if i + 4000 >= len(text) else None,
        )


async def artist_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Only the bot owner can view this.", show_alert=True)
        return

    kind = query.data.split(":")[1]
    await query.answer()

    artists = _build_artist_stats()
    if not artists:
        await query.message.reply_text("📭 No one has added any cards yet.")
        return

    if kind == "byartist":
        lines = [
            f"• {_artist_mention(uid, entry['username'])} — <b>{entry['total']}</b>"
            for uid, entry in artists.items()
        ]
        text = "📊 <b>Cards added per artist</b>\n\n" + "\n".join(lines)
    else:
        blocks = []
        for uid, entry in artists.items():
            rarity_lines = [
                f"    ◦ {rarity_name}: <b>{count}</b>"
                for rarity_name, count in sorted(
                    entry["rarities"].items(), key=lambda kv: kv[1], reverse=True
                )
            ]
            blocks.append(f"• {_artist_mention(uid, entry['username'])}\n" + "\n".join(rarity_lines))
        text = "💎 <b>Rarities added per artist</b>\n\n" + "\n\n".join(blocks)

    for i in range(0, len(text), 4000):
        await query.message.reply_text(text[i:i + 4000], parse_mode=ParseMode.HTML)


# ---------------- /character (owner: card count breakdowns) ----------------

async def character_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    total = db.get_total_character_count()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 Rarity", callback_data="charstats:rarity"),
        InlineKeyboardButton("🧑 Character", callback_data="charstats:character"),
        InlineKeyboardButton("🎬 Serie", callback_data="charstats:series"),
        InlineKeyboardButton("🎉 Event", callback_data="charstats:event"),
    ]])
    await update.message.reply_text(
        f"🗂 <b>{total}</b> card(s) total in the bot.\nPick a breakdown:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def character_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Only the bot owner can view this.", show_alert=True)
        return
    await query.answer()

    kind = query.data.split(":", 1)[1]
    if kind == "rarity":
        rows = db.get_card_counts_by_rarity()
        title = "💎 Cards per rarity"
    elif kind == "character":
        rows = db.get_card_counts_by_character()
        title = "🧑 Cards per character"
    elif kind == "series":
        rows = db.get_card_counts_by_series()
        title = "🎬 Cards per series"
    else:
        rows = db.get_card_counts_by_event()
        title = "🎉 Cards per event"

    total = db.get_total_character_count()
    if not rows:
        await query.message.reply_text("📭 No cards in the bot yet.")
        return

    lines = [f"• {row['label']}: <b>{row['count']}</b>" for row in rows]
    text = f"{title}\n🗂 Total cards: <b>{total}</b>\n\n" + "\n".join(lines)

    for i in range(0, len(text), 4000):
        await query.message.reply_text(text[i:i + 4000], parse_mode=ParseMode.HTML)




async def set_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "SETPREMIUM"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Reply to the person you want to grant premium to, using:\n"
            "<code>/setpremium</code> for permanent premium\n"
            "<code>/setpremium [days]</code> for a limited time (e.g. <code>/setpremium 30</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    recipient = update.message.reply_to_message.from_user
    if recipient.is_bot:
        await update.message.reply_text("🤖 A bot can't be premium.")
        return

    days = None
    if context.args:
        try:
            days = int(context.args[0])
            if days <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Days must be a positive whole number.")
            return

    db.set_premium(recipient.id, days)
    recipient_name = _mention(recipient.id, recipient.first_name)
    duration_text = f"for {days} day{'s' if days != 1 else ''}" if days else "permanently"
    await update.message.reply_text(
        f"⭐️ {recipient_name} is now premium ({duration_text}).", parse_mode=ParseMode.HTML
    )


async def remove_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "REMOVEPREMIUM"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ Reply to the person you want to revoke premium from with <code>/removepremium</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    recipient = update.message.reply_to_message.from_user
    db.remove_premium(recipient.id)
    recipient_name = _mention(recipient.id, recipient.first_name)
    await update.message.reply_text(f"◽ {recipient_name} is no longer premium.", parse_mode=ParseMode.HTML)


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    status = db.get_premium_status(user.id)

    if not status:
        await update.message.reply_text(
            "◽ You're not premium yet.\n\n"
            "⭐️ <b>Premium perks:</b>\n"
            f"• Daily capture limit: {config.PREMIUM_DAILY_CAPTURE_LIMIT} (instead of {config.DAILY_CAPTURE_LIMIT})\n"
            f"• Daily darts: {config.PREMIUM_DAILY_DART_LIMIT} (instead of {config.DAILY_DART_LIMIT})\n"
            f"• Mini App daily bonus: {config.PREMIUM_DAILY_TASK_BONUS} {config.CURRENCY_SYMBOL} "
            f"(instead of {config.DAILY_TASK_BONUS})\n"
            "• A ⭐️ next to your name in the Market and on cards you discovered\n"
            "• Access to <code>/trade</code> - trade a card in for a random one of the same rarity\n"
            "• 2 exclusive Mini App themes",
            parse_mode=ParseMode.HTML,
        )
        return

    if status["expires_at"]:
        expires_text = f"until {status['expires_at'][:10]}"
    else:
        expires_text = "forever (no expiry)"

    await update.message.reply_text(
        f"⭐️ You're premium, {expires_text}.\n\n"
        f"• Daily capture limit: {config.PREMIUM_DAILY_CAPTURE_LIMIT}\n"
        f"• Daily darts: {config.PREMIUM_DAILY_DART_LIMIT}\n"
        f"• Mini App daily bonus: {config.PREMIUM_DAILY_TASK_BONUS} {config.CURRENCY_SYMBOL}\n"
        "• ⭐️ badge next to your name\n"
        "• <code>/trade</code> access\n"
        "• 2 exclusive Mini App themes",
        parse_mode=ParseMode.HTML,
    )


# ---------------- /trade (premium only) ----------------

async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not db.is_premium(user.id):
        await update.message.reply_text(
            "⭐️ <code>/trade</code> is a premium perk. Check <code>/premium</code> for details.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/trade [ID]</code>\n"
            "Trades one copy of that card in for a random OTHER character of the same rarity.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        char_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    if not db.user_owns_character(user.id, char_id):
        await update.message.reply_text("❓ You don't own that card.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    if not character["rarity_id"]:
        await update.message.reply_text("❓ That card has no rarity set, so it can't be traded.")
        return

    rarity_display = character["rarity_name"] or "Unranked"
    await _send_confirmation(
        update, context, character,
        f"🔄 Trade <b>{html.escape(character['name'])}</b> (#{char_id}, {html.escape(rarity_display)}) for a random "
        f"<b>{html.escape(rarity_display)}</b> character? This can't be undone.",
        f"trade:confirm:{user.id}:{char_id}", f"trade:cancel:{user.id}",
    )


async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    trader_id = int(parts[2])

    if query.from_user.id != trader_id:
        await query.answer("Only the person who started this trade can confirm it.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Trade cancelled.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    if not db.is_premium(trader_id):
        await query.answer()
        await _edit_text_or_caption(query, "⭐️ Premium is required for /trade.")
        return

    char_id = int(parts[3])
    character = db.get_character(char_id)
    if not character:
        await query.answer()
        await _edit_text_or_caption(query, f"❓ No character found with ID #{char_id}.")
        return

    replacement = db.get_random_character_in_rarity_excluding(character["rarity_id"], char_id)
    if not replacement:
        await query.answer()
        await _edit_text_or_caption(query, 
            "❓ There's no other character in that rarity to trade for right now."
        )
        return

    success = db.sell_character_to_bot(trader_id, char_id)
    await query.answer()
    if not success:
        await _edit_text_or_caption(query, "❓ You no longer own a free copy of that card (maybe it's listed on the market).")
        return

    username = query.from_user.username or query.from_user.first_name
    db.give_character_to_user(trader_id, username, replacement["id"])

    rarity_display = replacement["rarity_name"] or "Unranked"
    await query.delete_message()
    await send_character_result(
        context, query.message.chat.id, replacement,
        f"🔄 Traded <b>{character['name']}</b> for <b>{replacement['name']}</b> ({rarity_display})!",
    )

    milestone_messages = memories.record_acquisition(trader_id, replacement, "trade")
    for msg in milestone_messages:
        try:
            await context.bot.send_message(chat_id=trader_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to DM milestone message to %s", trader_id)


# ---------------- /sell (Mini App market) ----------------

async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: <code>/sell [ID] [price]</code>\n"
            "Example: <code>/sell 42 250</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        char_id = int(context.args[0])
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ ID and price must both be numbers.")
        return

    if price <= 0:
        await update.message.reply_text("⚠️ Price must be greater than zero.")
        return

    user = update.effective_user

    if not db.user_owns_character(user.id, char_id):
        await update.message.reply_text("❓ You don't own that card.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    await _send_confirmation(
        update, context, character,
        f"🛍 List <b>{html.escape(character['name'])}</b> (#{char_id}) on the Waifu Market "
        f"for {price} {config.CURRENCY_SYMBOL}?",
        f"sell:ok:{user.id}:{char_id}:{price}", f"sell:cancel:{user.id}",
    )


async def sell_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OK / Cancel under the /sell confirmation. Only OK actually creates the listing."""
    query = update.callback_query
    parts = query.data.split(":")
    action, seller_id = parts[1], int(parts[2])

    if query.from_user.id != seller_id:
        await query.answer("Only the seller can confirm this.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Cancelled - nothing was listed.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    char_id, price = int(parts[3]), int(parts[4])
    character = db.get_character(char_id)
    if not character or not db.user_owns_character(seller_id, char_id):
        await query.answer()
        await _edit_text_or_caption(query, "❓ You no longer own that card.")
        return

    seller = query.from_user
    listing_id = db.create_listing(seller_id, seller.username or seller.first_name, char_id, price)
    await query.answer()

    if listing_id is None:
        await _edit_text_or_caption(
            query,
            "❓ Every copy of that card you own is already listed. "
            "Use <code>/cancelsell [listing ID]</code> to pull one back first.",
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel listing", callback_data=f"cancelsell:{listing_id}:{seller_id}"),
    ]])
    await _edit_text_or_caption(
        query,
        f"🛍 Listed <b>{html.escape(character['name'])}</b> (#{char_id}) for {price} {config.CURRENCY_SYMBOL} "
        f"on the Waifu Market.\nListing ID: <code>{listing_id}</code>",
        reply_markup=keyboard,
    )


async def cancelsell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/cancelsell [listing ID]</code>", parse_mode=ParseMode.HTML,
        )
        return

    try:
        listing_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Listing ID must be a number.")
        return

    listing = db.get_listing_by_id(listing_id)
    success = db.cancel_listing(listing_id, update.effective_user.id)
    if success:
        character = db.get_character(listing["character_id"]) if listing else None
        text = "✅ Listing cancelled - the card is back in your inventory only."
        if character:
            await send_character_result(
                context, update.effective_chat.id, character, text,
                reply_to_message_id=update.message.message_id,
            )
        else:
            await update.message.reply_text(text)
    else:
        await update.message.reply_text("❓ No active listing with that ID belongs to you.")


async def cancelsell_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    listing_id = int(parts[1])
    seller_id = int(parts[2])

    if query.from_user.id != seller_id:
        await query.answer("Only the seller can cancel this listing.", show_alert=True)
        return

    success = db.cancel_listing(listing_id, seller_id)
    await query.answer()
    if success:
        # This button is attached to the photo /sell posted, so the
        # message has a caption, not plain text - it must be edited as one.
        await query.edit_message_caption("✅ Listing cancelled - the card is back in your inventory only.")
    else:
        await query.edit_message_caption("❓ That listing is no longer active (already sold or cancelled).")


# ---------------- /sellbot (sell a card directly to the bot) ----------------

async def sellbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/sellbot [ID]</code>\nExample: <code>/sellbot 42</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        char_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    user = update.effective_user

    if not db.user_owns_character(user.id, char_id):
        await update.message.reply_text("❓ You don't own that card.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    price = db.get_sell_price(character["rarity_id"])
    rarity_display = character["rarity_name"] if character["rarity_name"] else "Unranked"

    if price <= 0:
        await update.message.reply_text(
            f"❓ No sell price is set for the <b>{rarity_display}</b> rarity yet - ask the owner or a C admin to set one.",
            parse_mode=ParseMode.HTML,
        )
        return

    await _send_confirmation(
        update, context, character,
        f"🏪 Sell <b>{html.escape(character['name'])}</b> (#{char_id}, {html.escape(rarity_display)}) to the bot "
        f"for {price} {config.CURRENCY_SYMBOL}?",
        f"sellbot:confirm:{user.id}:{char_id}", f"sellbot:cancel:{user.id}",
    )


async def sellbot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    seller_id = int(parts[2])

    if query.from_user.id != seller_id:
        await query.answer("Only the seller can confirm this.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Sale cancelled.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    char_id = int(parts[3])
    character = db.get_character(char_id)
    if not character:
        await query.answer()
        await _edit_text_or_caption(query, f"❓ No character found with ID #{char_id}.")
        return

    price = db.get_sell_price(character["rarity_id"])
    success = db.sell_character_to_bot(seller_id, char_id)
    await query.answer()

    if not success:
        await _edit_text_or_caption(query, "❓ You no longer own a free copy of that card (maybe it's listed on the market).")
        return

    new_balance = db.add_currency(seller_id, price)
    await query.delete_message()
    await send_character_result(
        context, query.message.chat.id, character,
        f"🏪 Sold <b>{character['name']}</b> (#{char_id}) for {price} {config.CURRENCY_SYMBOL}.\n"
        f"💰 New balance: {new_balance} {config.CURRENCY_SYMBOL}",
    )


# ---------------- Admin (owner + C): /setsellprice ----------------

async def set_sell_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "SETSELLPRICE"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if len(context.args) < 2:
        prices = db.get_all_sell_prices()
        lines = ["⚠️ Usage: <code>/setsellprice [rarity name] [amount]</code>",
                 "Example: <code>/setsellprice Legendary 100</code>",
                 "",
                 "Current sell prices:"]
        for p in prices:
            price_display = f"{p['price']} {config.CURRENCY_SYMBOL}" if p["price"] is not None else "not set"
            lines.append(f"• {p['name']}: {price_display}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    *rarity_parts, amount_str = context.args
    rarity_input = " ".join(rarity_parts).strip()

    try:
        amount = int(amount_str)
    except ValueError:
        await update.message.reply_text("⚠️ Amount must be a number.")
        return

    if amount < 0:
        await update.message.reply_text("⚠️ Amount can't be negative.")
        return

    if rarity_input.lower() == "unranked":
        db.set_sell_price(None, amount)
        await update.message.reply_text(f"✅ Sell price for <b>Unranked</b> set to {amount} {config.CURRENCY_SYMBOL}.", parse_mode=ParseMode.HTML)
        return

    rarity = db.get_rarity_by_name(rarity_input)
    if not rarity:
        await update.message.reply_text(f"❓ No rarity named \"{rarity_input}\" found. Use /rarities to see valid names, or \"Unranked\".")
        return

    db.set_sell_price(rarity["id"], amount)
    await update.message.reply_text(
        f"✅ Sell price for <b>{rarity['name']}</b> set to {amount} {config.CURRENCY_SYMBOL}.",
        parse_mode=ParseMode.HTML,
    )


# ---------------- /market (browse active Mini App listings) ----------------

MARKET_PAGE_SIZE = 8


def build_market_page(page: int, chat_type: str = "private", bot_username: str = None):
    """Returns (text, keyboard) for one page of currently active /sell listings."""
    listings = db.get_active_listings()

    total = len(listings)
    page_size = MARKET_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_listings = listings[start:start + page_size]

    lines = [
        "╭━━━「 🛍 Waifu Market 」━━━╮",
        "",
        f"✦ Active listings: {total}",
        "",
        "╰━━━━━━━━━━━━━━━━━╯",
        "",
    ]

    if not page_listings:
        lines.append("Nobody's selling anything right now.")
    else:
        for row in page_listings:
            rarity_text = row["rarity_name"] if row["rarity_name"] else "Unranked"
            seller = f"@{row['seller_username']}" if row["seller_username"] else f"Player {row['seller_id']}"
            seller = format_display_name(row["seller_id"], seller)
            lines.append(
                f"🔹 <b>{row['name']}</b> (#{row['character_id']}) _ {rarity_text}\n"
                f"   💰 {row['price']} {config.CURRENCY_SYMBOL} • 👤 {seller} • Listing <code>{row['listing_id']}</code>"
            )
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("Open the Mini App to buy a listing.")

    text = "\n".join(lines)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"marketpage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"marketpage:{page + 1}"))

    keyboard_rows = []
    if nav_row:
        keyboard_rows.append(nav_row)
    open_button = _miniapp_launch_button("🛍 Open Market", chat_type, bot_username)
    if open_button:
        keyboard_rows.append([open_button])

    return text, InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = build_market_page(0, update.effective_chat.type, context.bot.username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def market_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    text, keyboard = build_market_page(page, query.message.chat.type, context.bot.username)

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        pass  # e.g. "message not modified" when re-clicking the same page


# ---------------- /gift ----------------

async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Reply to the person you want to gift, using <code>/gift [ID]</code>", parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/gift [ID]</code> (as a reply to the recipient)", parse_mode=ParseMode.HTML)
        return

    try:
        char_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    giver = update.effective_user
    recipient = update.message.reply_to_message.from_user

    if recipient.id == giver.id:
        await update.message.reply_text("😅 You can't gift a card to yourself!")
        return

    if recipient.is_bot:
        await update.message.reply_text("🤖 Bots can't collect cards - gift it to a real player!")
        return

    if not db.user_owns_character(giver.id, char_id):
        await update.message.reply_text("❓ You don't own that card.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    recipient_name = _mention(recipient.id, recipient.first_name)
    await _send_confirmation(
        update, context, character,
        f"🎁 Gift <b>{html.escape(character['name'])}</b> (#{char_id}) to {recipient_name}?",
        f"gift:confirm:{giver.id}:{recipient.id}:{char_id}", f"gift:cancel:{giver.id}",
    )


async def gift_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    giver_id = int(parts[2])

    if query.from_user.id != giver_id:
        await query.answer("Only the gifter can confirm this.", show_alert=True)
        return

    if action == "cancel":
        await query.answer()
        await _edit_text_or_caption(query, "❌ Gift cancelled.")
        return

    if not await _consume_buttons(query):  # already handled (double tap)
        await query.answer()
        return

    recipient_id = int(parts[3])
    char_id = int(parts[4])

    try:
        recipient_chat = await context.bot.get_chat(recipient_id)
        recipient_username = recipient_chat.username or recipient_chat.first_name
        recipient_name = _mention(recipient_id, recipient_chat.first_name)
    except Exception:
        recipient_username = None
        recipient_name = "them"

    character = db.get_character(char_id)
    success = db.gift_character(giver_id, recipient_id, recipient_username, char_id)
    await query.answer()
    if success:
        await query.delete_message()
        if character:
            await send_character_result(
                context, query.message.chat.id, character,
                f"🎁 <b>{character['name']}</b> gifted to {recipient_name}!",
            )
            milestone_messages = memories.record_acquisition(recipient_id, character, "gift_received")
            for msg in milestone_messages:
                try:
                    await context.bot.send_message(chat_id=recipient_id, text=msg, parse_mode=ParseMode.HTML)
                except Exception:
                    logger.exception("Failed to DM milestone message to %s", recipient_id)
    else:
        await _edit_text_or_caption(query, "❓ You no longer own that card.")




async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/search [name / rarity / event]</code>\n"
            "Combine filters with <code>|</code>, in any order - "
            "e.g. <code>/search Ada | 👑 | 🛡</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    query = " ".join(context.args).strip()
    results = db.search_characters(query)

    if not results:
        await update.message.reply_text(f"🔍 No results found for \"{query}\".")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 View search results", switch_inline_query_current_chat=query)]
    ])
    await update.message.reply_text(
        "Click the button below to see the search results:", reply_markup=keyboard
    )


# ---------------- /sort ----------------
# A player can stack any number of filters: characters, series, rarities, events.
# Values of the SAME kind are OR-ed ("Yae" or "Law"), different kinds are AND-ed
# ("...and Legendary or Prismatic ...and Fighter"). See db._card_passes_filters().

_SORT_KIND_INFO = {
    "character": ("🧑", "Character"),
    "series": ("🎬", "Series"),
    "rarity": ("💎", "Rarity"),
    "event": ("🎉", "Event"),
}


def _short_label(text: str, limit: int = 32) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sort_filters_text(filters) -> str:
    """Multi-line summary of the active filters (HTML)."""
    grouped = {}
    for f in filters:
        grouped.setdefault(f["filter_type"], []).append(f["filter_value"])
    lines = []
    for kind in db.FILTER_TYPES:
        if kind in grouped:
            emoji, label = _SORT_KIND_INFO[kind]
            lines.append(f"{emoji} <b>{label}:</b> " + ", ".join(html.escape(v) for v in grouped[kind]))
    return "\n".join(lines)


def _sort_menu(user_id: int):
    """(text, keyboard) of the main /sort menu for this player."""
    filters = db.get_user_filters(user_id)
    lines = ["🗂 <b>Sort your constellation</b>", ""]
    if filters:
        lines.append(_sort_filters_text(filters))
        lines += [
            "",
            "A card is shown when it matches <i>at least one</i> value of <i>every</i> group above.",
        ]
    else:
        lines.append("No filters yet - your constellation shows everything.")
    lines += ["", "Add as many filters as you like:"]

    rows = [
        [InlineKeyboardButton("🧑 Character", callback_data="sortmenu:character"),
         InlineKeyboardButton("🎬 Series", callback_data="sortmenu:series")],
        [InlineKeyboardButton("💎 Rarity", callback_data="sortmenu:rarity"),
         InlineKeyboardButton("🎉 Event", callback_data="sortmenu:event")],
    ]
    for f in filters:
        emoji, _ = _SORT_KIND_INFO.get(f["filter_type"], ("•", ""))
        rows.append([InlineKeyboardButton(
            f"❌ {emoji} {_short_label(f['filter_value'])}", callback_data=f"sortdel:{f['id']}"
        )])
    if filters:
        rows.append([InlineKeyboardButton("🗑️ Clear all filters", callback_data="sortmenu:clear")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _sort_picker_options(kind: str):
    """[(value, callback_data)] for the rarity / event pickers."""
    if kind == "rarity":
        return [(r["name"], f"sortval:rarity:{r['id']}") for r in db.list_rarities()]
    return [(e["name"], f"sortval:event:{i}") for i, e in enumerate(db.get_all_events())]


def _sort_picker(user_id: int, kind: str):
    """(text, keyboard) of the rarity / event multi-select. Tap = toggle."""
    options = _sort_picker_options(kind)
    emoji, label = _SORT_KIND_INFO[kind]
    if not options:
        text = f"❓ No {label.lower()}s have been created yet."
        rows = []
    else:
        active = {_norm(f["filter_value"]) for f in db.get_user_filters(user_id) if f["filter_type"] == kind}
        text = f"{emoji} <b>Pick {label.lower()}s</b>\nTap to turn each one on or off - you can pick several."
        rows = [
            [InlineKeyboardButton(
                ("✅ " if _norm(name) in active else "") + _short_label(name, 40), callback_data=data
            )]
            for name, data in options
        ]
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="sortmenu:main")])
    return text, InlineKeyboardMarkup(rows)


def _norm(text: str) -> str:
    return db._normalize_search_text(text)


async def sort_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_filter_type", None)
    text, keyboard = _sort_menu(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def _show_sort_menu(query, user_id: int):
    text, keyboard = _sort_menu(user_id)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except TelegramError:
        pass  # "message is not modified" when nothing changed


async def sort_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action != "character" and action != "series":
        context.user_data.pop("awaiting_filter_type", None)

    if action == "main":
        await _show_sort_menu(query, user_id)
        return

    if action == "clear":
        db.clear_user_filter(user_id)
        await _show_sort_menu(query, user_id)
        return

    if action in ("rarity", "event"):
        text, keyboard = _sort_picker(user_id, action)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if action in ("character", "series"):
        context.user_data["awaiting_filter_type"] = {"kind": action, "chat_id": query.message.chat.id}
        example = "Yae" if action == "character" else "Genshin"
        await query.edit_message_text(
            f"✏️ Type the {action} name - one word is enough (for example <code>{example}</code>).\n"
            "To add several at once, separate them with <code>|</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="sortmenu:main")]]),
        )


async def sort_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A rarity / event button in the picker: turns that filter on or off."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    _, kind, value = query.data.split(":", 2)
    if kind not in ("rarity", "event"):
        return

    if value.isdigit():
        # Buttons carry an id / list position (callback_data is capped at 64 bytes).
        options = _sort_picker_options(kind)
        if kind == "rarity":
            name = next((n for n, data in options if data == query.data), None)
        else:
            name = options[int(value)][0] if int(value) < len(options) else None
        if name is None:
            await query.answer("❓ That option no longer exists.", show_alert=True)
            return
    else:
        name = value  # older messages carried the rarity name itself

    db.toggle_user_filter(user_id, kind, name)
    text, keyboard = _sort_picker(user_id, kind)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except TelegramError:
        pass


async def sort_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The ❌ button next to one active filter."""
    query = update.callback_query
    await query.answer()
    try:
        filter_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    db.remove_user_filter(query.from_user.id, filter_id)
    await _show_sort_menu(query, query.from_user.id)


async def capture_sort_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in its own handler group so it never interferes with the
    spawn/spam message counter - only acts when the user has an active
    /sort or /editrarity prompt waiting for a typed value."""
    value = update.message.text.strip() if update.message.text else ""
    if not value:
        return

    editrarity_target = context.user_data.get("awaiting_editrarity")
    if editrarity_target:
        try:
            new_weight = int(value)
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a whole number.")
            return
        db.add_rarity(editrarity_target, new_weight)
        context.user_data.pop("awaiting_editrarity", None)
        await update.message.reply_text(f"✅ {editrarity_target} weight updated to {new_weight}.")
        return

    awaiting = context.user_data.get("awaiting_filter_type")
    if not awaiting or awaiting.get("chat_id") != update.effective_chat.id:
        return  # not waiting for a /sort value from this person in this chat

    kind = awaiting["kind"]
    known_names = db.get_distinct_character_names() if kind == "character" else db.get_distinct_series()

    added, already, not_found = [], [], []
    for part in re.split(r"[|\n]", value):
        part = part.strip()
        if not part:
            continue
        # One word of a name is enough: "Yae" finds "Yae Miko".
        if not any(db.text_matches_words(part, name) for name in known_names):
            not_found.append(part)
        elif db.add_user_filter(update.effective_user.id, kind, part):
            added.append(part)
        else:
            already.append(part)

    if not added and not already:
        shown = ", ".join(html.escape(n) for n in not_found)
        await update.message.reply_text(
            f"❓ Nothing matches {shown}. Try another {kind} name, or tap Back to cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data.pop("awaiting_filter_type", None)
    notes = []
    if added:
        notes.append("✅ Added: " + ", ".join(html.escape(n) for n in added))
    if already:
        notes.append("ℹ️ Already active: " + ", ".join(html.escape(n) for n in already))
    if not_found:
        notes.append("❓ No match for: " + ", ".join(html.escape(n) for n in not_found))

    text, keyboard = _sort_menu(update.effective_user.id)
    await update.message.reply_text(
        "\n".join(notes) + "\n\n" + text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def constellation_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Powers the inline gallery:
    - '🌌 See constellation' button -> '@yourbot constellation:<owner_id>'
      (the player's WHOLE collection - /sort filters only affect the /constellation
      list, never this gallery; each card shows once, with xN in its caption)
    - '@yourbot gallery:...'        -> every card in the game
    - '@yourbot <anything else>'    -> character search (name / rarity / event,
      combine with '|'); no '@yourbot' text at all -> every card in the game
    Returns a scrollable native Telegram photo gallery - nothing gets posted in
    the chat itself unless the person taps a specific photo to send it.
    """
    query_text = (update.inline_query.query or "").strip()

    owner_id = None
    counts = {}
    if query_text.startswith("constellation:"):
        try:
            owner_id = int(query_text.split(":", 1)[1])
        except ValueError:
            owner_id = update.inline_query.from_user.id
        all_copies = db.get_user_inventory(owner_id, apply_filter=False)
        items = []
        for item in all_copies:
            if item["id"] not in counts:
                items.append(item)
            counts[item["id"]] = counts.get(item["id"], 0) + 1
    elif query_text.startswith("gallery:") or not query_text:
        # No extra text (just opening the inline keyboard) - show the full game
        # gallery, not only the cards this person happens to own.
        items = db.get_all_characters()
    else:
        if query_text.lower().startswith("search:"):  # old "View search results" buttons
            query_text = query_text.split(":", 1)[1].strip()
        items = db.search_characters(query_text)

    offset = update.inline_query.offset
    start = int(offset) if offset else 0
    chunk = items[start:start + CONSTELLATION_PAGE_SIZE]

    owner_name = db.get_display_name(owner_id) if owner_id is not None else None

    results = []
    for i, item in enumerate(chunk):
        if not item["image_file_id"]:
            continue
        result_id = f"{item['id']}_{start + i}"
        if owner_id is not None:
            caption = build_inline_collection_caption(
                item, owner_id, owner_name, counts.get(item["id"], 1)
            )
        else:
            caption = build_inline_share_caption(item)
        if item["media_type"] == "video":
            results.append(InlineQueryResultCachedVideo(
                id=result_id,
                video_file_id=item["image_file_id"],
                title=item["name"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            ))
        else:
            results.append(InlineQueryResultCachedPhoto(
                id=result_id,
                photo_file_id=item["image_file_id"],
                title=item["name"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            ))

    next_offset = str(start + CONSTELLATION_PAGE_SIZE) if start + CONSTELLATION_PAGE_SIZE < len(items) else ""

    await update.inline_query.answer(results, cache_time=1, next_offset=next_offset, is_personal=True)


def _is_fighter_event_name(event_name: str) -> bool:
    """
    Matches the Fighter event regardless of how it's stylized - the
    original spec used bold Unicode letters (e.g. "🛡𝗙𝗶𝗴𝗵𝘁𝗲𝗿🛡"), which
    are entirely different code points from plain ASCII "Fighter" and
    won't match with a plain .lower() comparison. NFKC normalization
    folds styled Unicode letters back to their plain ASCII equivalents,
    and stripping everything but letters drops any surrounding emoji/
    symbols, so any spelling/styling of "Fighter" matches.
    """
    if not event_name:
        return False
    normalized = unicodedata.normalize("NFKC", event_name).strip().lower()
    letters_only = re.sub(r"[^a-z]", "", normalized)
    return letters_only == config.FIGHTER_EVENT_NAME.lower()


def _is_aevoria_rarity(rarity_name: str) -> bool:
    """Same NFKC-fold-then-strip trick as _is_fighter_event_name, so this
    matches the 🪽Aevoria rarity regardless of emoji/spacing around it."""
    if not rarity_name:
        return False
    normalized = unicodedata.normalize("NFKC", rarity_name).strip().lower()
    letters_only = re.sub(r"[^a-z]", "", normalized)
    return letters_only == "aevoria"


# ---------------- Shared: button-based rarity/event picker ----------------
# Used by both /addcharacter (adds immediately) and /send (forwards to the
# owner for review). After the photo + "Name | Series" caption comes in, we
# walk the sender through two button menus - rarity, then event - skipping
# any step that has nothing to pick from, before finalizing.

def _grid_rows(buttons, columns=3):
    """Lays a flat list of InlineKeyboardButtons out into rows of `columns`
    buttons each (last row may be shorter). Used to turn long one-per-row
    picker lists (rarity, event) into a compact grid."""
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def _rarity_picker_keyboard(pending_id: str):
    rarities = db.list_rarities()
    if not rarities:
        return None
    buttons = [
        InlineKeyboardButton(r["name"], callback_data=f"addflow:rarity:{pending_id}:{r['id']}")
        for r in rarities
    ]
    rows = _grid_rows(buttons)
    rows.append([InlineKeyboardButton("🚫 Unranked (no rarity)", callback_data=f"addflow:rarity:{pending_id}:none")])
    return InlineKeyboardMarkup(rows)


def _event_picker_keyboard(pending_id: str, pending: dict):
    events = db.get_all_events()
    if not events:
        return None
    # Event names can contain characters unsafe for callback_data, so we
    # reference them by index into this cached list instead of by name.
    pending["_event_options"] = [e["name"] for e in events]
    buttons = [
        InlineKeyboardButton(e["name"], callback_data=f"addflow:event:{pending_id}:{i}")
        for i, e in enumerate(events)
    ]
    rows = _grid_rows(buttons)
    rows.append([InlineKeyboardButton("🚫 No event", callback_data=f"addflow:event:{pending_id}:none")])
    return InlineKeyboardMarkup(rows)


def _element_picker_keyboard(pending_id: str, pending: dict):
    """Step shown only when the chosen event is Fighter (Update 2) - lets
    the sender pick one of config.ELEMENTS by button, same indexed-option
    trick as the event picker above."""
    keys = list(config.ELEMENTS.keys())
    pending["_element_options"] = keys
    rows = [
        [InlineKeyboardButton(config.ELEMENTS[key]["label"], callback_data=f"addflow:fighterel:{pending_id}:{i}")]
        for i, key in enumerate(keys)
    ]
    return InlineKeyboardMarkup(rows)


async def _advance_add_flow(pending_id: str, context: ContextTypes.DEFAULT_TYPE,
                             message=None, query=None):
    """Shows the next relevant step (rarity, then event), or finalizes once
    both are resolved. `message` drives the very first prompt (a reply to
    the photo); every step after that arrives via `query` and edits that
    same prompt in place, so the chat doesn't fill up with extra messages."""
    pending = PENDING_ADD_FLOW[pending_id]

    async def respond(text, keyboard=None):
        if query is not None:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    if not pending["rarity_done"]:
        keyboard = _rarity_picker_keyboard(pending_id)
        if keyboard is None:
            pending["rarity_done"] = True
        else:
            await respond(f"🏷 Choose a rarity for <b>{pending['name']}</b>:", keyboard)
            return

    if not pending["event_done"]:
        keyboard = _event_picker_keyboard(pending_id, pending)
        if keyboard is None:
            pending["event_done"] = True
        else:
            await respond(f"🎉 Choose an event for <b>{pending['name']}</b> (or skip):", keyboard)
            return

    # ---- Fighter branch (Update 2) ----
    # Entered exactly once, right after the event resolves. Only triggers
    # when the chosen event is the Fighter event; every other character
    # skips straight to finalize, unaffected.
    if "fighter_stage" not in pending:
        is_fighter = _is_fighter_event_name(pending["event_name"])
        pending["fighter_stage"] = "element" if is_fighter else "done"

    if pending["fighter_stage"] == "element":
        keyboard = _element_picker_keyboard(pending_id, pending)
        await respond(f"⚔ Choose an <b>Element</b> for <b>{pending['name']}</b>:", keyboard)
        return

    if pending["fighter_stage"] == "await_attack":
        await respond(f"⚔ Send the <b>Base Attack Power</b> for <b>{pending['name']}</b> (a whole number):")
        return

    if pending["fighter_stage"] == "await_defense":
        await respond(f"🛡 Send the <b>Base Defense Power</b> for <b>{pending['name']}</b> (a whole number):")
        return

    await _finalize_add_flow(pending_id, context, respond)


async def _finalize_add_flow(pending_id: str, context: ContextTypes.DEFAULT_TYPE, respond):
    pending = PENDING_ADD_FLOW.pop(pending_id)
    name, series = pending["name"], pending["series"]
    rarity_name, event_name = pending["rarity_name"], pending["event_name"]
    file_id = pending["file_id"]
    media_type = pending.get("media_type") or "photo"

    fighter_element = pending.get("fighter_element")
    fighter_attack = pending.get("fighter_attack")
    fighter_defense = pending.get("fighter_defense")

    if pending["kind"] == "addcharacter":
        event_was_dropped = bool(event_name) and not db.event_exists(event_name)
        char_id = db.add_character(
            name, series, file_id, rarity_name,
            added_by_user_id=pending["user_id"], added_by_username=pending["username"],
            event_name=event_name, media_type=media_type,
        )
        if fighter_element and fighter_attack is not None and fighter_defense is not None:
            db.set_fighter_stats(char_id, fighter_element, fighter_attack, fighter_defense)

        rarity_display = rarity_name if rarity_name else "Unranked"
        result = f"✅ Added <b>{name}</b> ({series}) as #{char_id}\nRarity: {rarity_display}"
        if event_name:
            result += f"\nEvent: {event_name}"
        if fighter_element:
            elem = config.ELEMENTS.get(fighter_element, {})
            result += (
                f"\n\n🛡 <b>Fighter</b>\nElement: {elem.get('label', fighter_element)}"
                f"\nBase Attack: {fighter_attack}\nBase Defense: {fighter_defense}"
            )
        if event_was_dropped:
            result += f"\n⚠️ Event \"{pending['event_name']}\" isn't registered - added without an event."
        await respond(result)

        character = db.get_character(char_id)
        caption_text = build_channel_announcement(character)
        try:
            sent = await _send_character_media(
                context.bot, config.ARCHIVE_CHANNEL, character,
                caption=caption_text, parse_mode=ParseMode.HTML,
            )
            db.set_archive_message_id(char_id, sent.message_id)
        except Exception:
            logger.exception("Failed to post new character to archive channel")

    else:  # "send" - forward to the owner for review, same as before
        submission_id = uuid.uuid4().hex[:8]
        PENDING_SUBMISSIONS[submission_id] = {
            "name": name,
            "series": series,
            "rarity_name": rarity_name,
            "event_name": event_name,
            "file_id": file_id,
            "media_type": media_type,
            "sender_user_id": pending["user_id"],
            "sender_username": pending["username"],
            "fighter_element": fighter_element,
            "fighter_attack": fighter_attack,
            "fighter_defense": fighter_defense,
            "messages": [],  # (chat_id, message_id) of every review panel, so all can be closed together
        }

        sender_name = _mention(pending["user_id"], pending["first_name"])
        summary = f"{html.escape(name)} | {html.escape(series)}"  # user-typed text inside an HTML caption
        if rarity_name:
            summary += f" | {html.escape(rarity_name)}"
        if event_name:
            summary += f" | {html.escape(event_name)}"
        if fighter_element:
            elem = config.ELEMENTS.get(fighter_element, {})
            summary += f"\n🛡 {elem.get('label', fighter_element)} | ATK {fighter_attack} | DEF {fighter_defense}"

        delivered = await _deliver_submission(
            context, submission_id,
            media_type=media_type, file_id=file_id,
            caption=f"📥 Character submission from {sender_name}:\n\n{summary}",
        )
        if not delivered:
            PENDING_SUBMISSIONS.pop(submission_id, None)
            await respond("⚠️ I couldn't deliver your character for review right now. Please try again later.")
            return
        await respond("✅ Your character will be added soon.")


async def add_flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    stage, pending_id, raw_value = parts[1], parts[2], parts[3]

    pending = PENDING_ADD_FLOW.get(pending_id)
    if pending is None:
        await query.answer("⚠️ This selection has expired (bot restarted?) - please resend the photo.", show_alert=True)
        return

    user = query.from_user
    if user.id != pending["user_id"] and not is_admin(user.id):
        await query.answer("⛔ Only the person who sent this can pick.", show_alert=True)
        return

    if stage == "rarity":
        if raw_value == "none":
            pending["rarity_name"] = None
        else:
            rarity_row = db.get_rarity_by_id(int(raw_value))
            pending["rarity_name"] = rarity_row["name"] if rarity_row else None
        pending["rarity_done"] = True
        await query.answer()
        await _advance_add_flow(pending_id, context, query=query)
        return

    if stage == "event":
        if raw_value == "none":
            pending["event_name"] = None
        else:
            options = pending.get("_event_options", [])
            idx = int(raw_value)
            pending["event_name"] = options[idx] if idx < len(options) else None
        pending["event_done"] = True
        await query.answer()
        await _advance_add_flow(pending_id, context, query=query)
        return

    if stage == "fighterel":
        options = pending.get("_element_options", [])
        idx = int(raw_value)
        element_key = options[idx] if idx < len(options) else options[0]
        pending["fighter_element"] = element_key
        pending["fighter_stage"] = "await_attack"
        # The next two answers arrive as plain text messages, not button
        # taps - flag this user so capture_fighter_stat_input picks them up.
        context.user_data["awaiting_fighter_pending_id"] = pending_id
        await query.answer()
        await _advance_add_flow(pending_id, context, query=query)
        return

    await query.answer()


async def capture_fighter_stat_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in its own handler group so it doesn't interfere with /sort
    input or the group spawn counter - only acts when this user has an
    active Fighter add-flow waiting on a typed Attack/Defense number."""
    pending_id = context.user_data.get("awaiting_fighter_pending_id")
    if not pending_id:
        return

    pending = PENDING_ADD_FLOW.get(pending_id)
    if pending is None:
        context.user_data.pop("awaiting_fighter_pending_id", None)
        return

    raw_value = update.message.text.strip() if update.message.text else ""
    try:
        number = int(raw_value)
        if number < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please send a whole positive number.")
        return

    if pending["fighter_stage"] == "await_attack":
        pending["fighter_attack"] = number
        pending["fighter_stage"] = "await_defense"
        await _advance_add_flow(pending_id, context, message=update.message)
        return

    if pending["fighter_stage"] == "await_defense":
        pending["fighter_defense"] = number
        pending["fighter_stage"] = "done"
        context.user_data.pop("awaiting_fighter_pending_id", None)
        await _advance_add_flow(pending_id, context, message=update.message)
        return


# ---------------- Admin: /addcharacter ----------------

def _extract_media(message):
    """Returns (file_id, media_type) from a message's photo or video, or
    (None, None) if it has neither. Used by /addcharacter and /send so
    both accept either a photo or a video as a character's card."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    return None, None


async def add_character_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "ADDCHARACTER"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    file_id, media_type = _extract_media(update.message)
    if not file_id:
        await update.message.reply_text(
            "📸 Please send a photo or video with this caption format:\n"
            "<code>/addcharacter Name | Series</code>\n"
            "You'll then pick a rarity and event with buttons "
            "(or add them as extra <code>| Rarity | Event</code> parts to skip the buttons).",
            parse_mode=ParseMode.HTML,
        )
        return

    caption = update.message.caption or ""
    # strip the command itself out of the caption
    caption_body = caption.split(None, 1)[1] if " " in caption else ""
    parts = [p.strip() for p in caption_body.split("|")]

    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "⚠️ Format error. Use:\n"
            "<code>/addcharacter Name | Series</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name, series = parts[0], parts[1]
    preset_rarity_name = parts[2] if len(parts) > 2 and parts[2] else None
    preset_event_name = parts[3] if len(parts) > 3 and parts[3] else None

    artist_username = user.username or user.first_name

    pending_id = uuid.uuid4().hex[:8]
    PENDING_ADD_FLOW[pending_id] = {
        "kind": "addcharacter",
        "name": name,
        "series": series,
        "file_id": file_id,
        "media_type": media_type,
        "user_id": user.id,
        "username": artist_username,
        "first_name": user.first_name,
        "rarity_name": preset_rarity_name,
        "rarity_done": bool(preset_rarity_name),
        "event_name": preset_event_name,
        "event_done": bool(preset_event_name),
    }

    await _advance_add_flow(pending_id, context, message=update.message)


# ---------------- Admin: /addrarity ----------------

async def add_rarity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "ADDRARITY"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: <code>/addrarity [name] [weight]</code>\n"
            "Higher weight = spawns more often. Example: <code>/addrarity Common 50</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = context.args[0]
    try:
        weight = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Weight must be a number.")
        return

    db.add_rarity(name, weight)
    await update.message.reply_text(f"✅ Rarity <b>{name}</b> set with weight {weight}!", parse_mode=ParseMode.HTML)


# ---------------- Admin: /removecharacter ----------------

async def remove_character_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "REMOVECHARACTER"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/removecharacter [ID or \"all\"]</code>\n"
            "The ID is the number shown as 🆔 when a character is claimed.",
            parse_mode=ParseMode.HTML,
        )
        return

    target = context.args[0]

    if target.lower() == "all":
        if not is_admin(user.id):
            await update.message.reply_text("⛔ Only the owner can remove all characters at once.")
            return
        db.wipe_all_characters()
        await update.message.reply_text(
            "🗑️ All characters and everyone's constellations have been wiped.\n"
            "They're in /bin for the next 30 days if you need any of them back."
        )
        return

    try:
        char_id = int(target)
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number (or \"all\").")
        return

    character = db.get_character(char_id)
    if character and db.delete_character(char_id):
        if character["archive_message_id"]:
            try:
                await context.bot.delete_message(
                    chat_id=config.ARCHIVE_CHANNEL, message_id=character["archive_message_id"],
                )
            except Exception:
                logger.exception("Failed to delete archive channel message for character #%s", char_id)
        await send_character_result(
            context, update.effective_chat.id, character,
            f"🗑️ Character #{char_id} removed (and cleared from everyone's constellation). "
            "It'll stay in /bin for 30 days.",
            reply_to_message_id=update.message.message_id,
        )
    else:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")


# ---------------- /spawnstatus (public) ----------------

async def spawn_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # In the main group the "main"-only locks apply too, so show them there.
    in_main_group = update.effective_chat.id == db.get_main_chat_id()
    rarities = db.list_rarities()
    locked_rarity_ids = db.get_locked_rarity_ids()
    locked_main_rarity_ids = db.get_locked_rarity_ids(main=True) if in_main_group else set()
    events = db.get_all_events()

    def status(locked_everywhere: bool, locked_here_only: bool) -> str:
        if locked_everywhere:
            return "🔒 Locked"
        if locked_here_only:
            return "🔒 Locked (main group only)"
        return "🔓 Open"

    lines = ["🔐 <b>Rarity spawn status</b>\n"]
    if rarities:
        for r in rarities:
            lines.append(
                f"{html.escape(r['name'])} — "
                f"{status(r['id'] in locked_rarity_ids, r['id'] in locked_main_rarity_ids)}"
            )
    else:
        lines.append("No rarities added yet.")

    lines.append("")
    lines.append("🔐 <b>Event spawn status</b>")
    if events:
        for e in events:
            lines.append(
                f"{html.escape(e['name'])} — {status(e['locked'], in_main_group and e['locked_main'])}"
            )
    else:
        lines.append("No events added yet.")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------- Admin: /removerarity ----------------

async def remove_rarity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "REMOVERARITY"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/removerarity [name or \"all\"]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = " ".join(context.args)

    if name.lower() == "all":
        if not is_admin(user.id):
            await update.message.reply_text("⛔ Only the owner can remove all rarities at once.")
            return
        db.wipe_all_rarities()
        await update.message.reply_text(
            "🗑️ All rarities have been wiped (affected characters are now Unranked).\n"
            "They're in /bin for the next 30 days if you need any of them back."
        )
        return

    if db.delete_rarity(name):
        await update.message.reply_text(
            f"🗑️ Rarity <b>{name}</b> removed (in /bin for 30 days). Characters that had it are now Unranked.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❓ No rarity found named \"{name}\".")


# ---------------- Admin: /forcespawn ----------------

async def force_spawn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "FORCESPAWN"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ This command only works inside a group.")
        return

    spawned = await try_spawn(update.effective_chat.id, context)
    if not spawned:
        await update.message.reply_text(
            "❓ No characters in the database yet - add one with /addcharacter first."
        )


# ---------------- /gallery ----------------

async def allcharacters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_chars = db.get_all_characters()
    total = len(all_chars)
    if total == 0:
        await update.message.reply_text("❌ No characters in the database yet.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🗂 Browse all {total} characters",
            switch_inline_query_current_chat="gallery:all"
        )]
    ])
    await update.message.reply_text(
        f"📚 <b>Character Gallery</b>\n"
        f"<i>{total} characters in the database.</i>\n\n"
        f"Tap the button below to open the gallery.\n"
        f"Select any card to share it with full details ✦",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ---------------- /check ----------------

def build_channel_announcement(character, updated: bool = False, editor_id: int = None, editor_username: str = None) -> str:
    if updated:
        # The person who just made this edit, not whoever originally
        # added the card - /check and build_card_caption still show the
        # original adder, only this channel repost credits the editor.
        if editor_id:
            artist_display = _mention(editor_id, format_display_name(editor_id, editor_username or "Unknown"))
        else:
            artist_display = editor_username or "Unknown"
    elif character["added_by_user_id"]:
        artist_name = character["added_by_username"] or "Unknown"
        artist_display = _mention(character["added_by_user_id"], format_display_name(character["added_by_user_id"], artist_name))
    else:
        artist_display = "Unknown"

    rarity_text = character["rarity_name"] if character["rarity_name"] else "Unranked"

    banner = "𝐂𝐀𝐑𝐃 𝐔𝐏𝐃𝐀𝐓𝐄𝐃" if updated else "𝐍𝐄𝐖 𝐂𝐀𝐑𝐃 𝐃𝐈𝐒𝐂𝐎𝐕𝐄𝐑𝐄𝐃"
    lines = [
        "╭⊱⋅ ───────── ⋅⊰╮",
        banner,
        "╰⊱⋅ ───────── ⋅⊰╯",
        "",
        "❖ 𝐂𝐇𝐀𝐑𝐀𝐂𝐓𝐄𝐑",
        f"⤷ {character['name']}",
        "",
        "❖ 𝐒𝐄𝐑𝐈𝐄𝐒",
        f"⤷ {character['series']}",
        "",
        "❖ 𝐂𝐀𝐑𝐃 𝐈𝐃",
        f"⤷ {character['id']}",
        "",
        "❖ 𝐑𝐀𝐑𝐈𝐓𝐘",
        f"⤷ {rarity_text}",
    ]
    if character["event_name"]:
        event_label = "𝐓𝐫𝐞𝐧𝐝" if _is_aevoria_rarity(character["rarity_name"]) else "𝛦𝛻𝛦𝛮𝛵"
        lines += ["", f"❖<b>{event_label}</b>", f"⤷ {character['event_name']}"]

    fighter = db.get_fighter_stats(character["id"])
    if fighter:
        elem = config.ELEMENTS.get(fighter["element"], {})
        lines += [
            "", f"❖<b>🛡 𝐹𝛪𝐺𝛨𝛵𝛦𝛤 — {elem.get('label', fighter['element'])}</b>",
            f"⤷ 𝛣𝛼𝛅𝛠 𝛢𝛕𝛕𝛼𝝇𝛋: {fighter['base_attack']}",
            f"⤷ 𝛣𝛼𝛅𝛠 𝐷𝛠ᵳ𝛠𝛈𝛅𝛠: {fighter['base_defense']}",
        ]

    label = "𝐔𝐏𝐃𝐀𝐓𝐄𝐃 𝐁𝐘" if updated else "𝐃𝐈𝐒𝐂𝐎𝐕𝐄𝐑𝐄𝐃 𝐁𝐘"
    lines += ["", "", f"╰┈➤ {label} : {artist_display} ✦"]
    return "\n".join(lines)


async def _post_character_update_to_archive(context: ContextTypes.DEFAULT_TYPE, char_id: int, editor_id: int, editor_username: str):
    """Reposts this character to the archive channel with an
    'UPDATED BY' footer (instead of 'DISCOVERED BY') whenever
    /editcharacter changes something. Best-effort - a failure here
    (e.g. the bot losing channel admin rights) never blocks the edit
    itself from succeeding, same as the original post-on-add path."""
    character = db.get_character(char_id)
    if not character:
        return
    caption_text = build_channel_announcement(character, updated=True, editor_id=editor_id, editor_username=editor_username)
    try:
        sent = await _send_character_media(
            context.bot, config.ARCHIVE_CHANNEL, character,
            caption=caption_text, parse_mode=ParseMode.HTML,
        )
        db.set_archive_message_id(char_id, sent.message_id)
    except Exception:
        logger.exception("Failed to post updated character to archive channel")


def build_inline_share_caption(character) -> str:
    """Caption shown under a card when someone sends it from the inline
    gallery (search/constellation/gallery results) into a chat."""
    rarity_text = character["rarity_name"] if character["rarity_name"] else "Unranked"
    lines = [
        "𝐇𝐄𝐘! 𝐂𝐇𝐄𝐂𝐊 𝐓𝐇𝐈𝐒 𝐎𝐔𝐓!",
        "",
        "╭─────────────── ✦",
        f"│  {character['series']}",
        f"│  {character['id']}  •  {character['name']}",
        "│",
        f"│  {rarity_text}",
    ]
    if character["event_name"]:
        lines.append(f"│  {character['event_name']}")
    lines.append("╰─────────────── ✦")
    return "\n".join(lines)


def build_inline_collection_caption(character, owner_id: int, owner_name: str, copies: int) -> str:
    """Caption under a card sent from a player's inline collection gallery."""
    lines = [
        f"୨୧ {_mention(owner_id, owner_name)} 𝐂𝐀𝐑𝐃 𝐈𝐒 𝐇𝐄𝐑𝐄! ୨୧",
        "",
        html.escape(character["series"] or ""),
        f"𝐈𝐃 𖧷 {character['id']} : {html.escape(character['name'])}" + (f" x{copies}" if copies > 1 else ""),
    ]
    if character["event_name"]:
        lines.append(f"𝐄𝐕𝐄𝐍𝐓 › {html.escape(character['event_name'])}")
    rarity_text = character["rarity_name"] if character["rarity_name"] else "Unranked"
    lines.append(f"𝐑𝐀𝐑𝐈𝐓𝐘 › {html.escape(rarity_text)}")
    return "\n".join(lines)


def build_card_caption(character, owners_count: int) -> str:
    rarity_text = character["rarity_name"] if character["rarity_name"] else "Unranked"

    if character["added_by_user_id"]:
        artist_name = character["added_by_username"] or "Unknown"
        artist_display = _mention(character["added_by_user_id"], format_display_name(character["added_by_user_id"], artist_name))
    else:
        artist_display = "Unknown"

    lines = [
        f"✦ 𝐶𝛢𝑹𝐷 𝐷𝛰𝑆𝑆𝛪𝑹 — {character['name']}",
        "",
        "──────── ✦ ────────",
        "",
        f"✦ 𝑆𝘦𝘳𝛊𝘦𝘴:{character['series']}",
        f"✦ 𝑅𝛼𝒓𝒊𝛕𝒊𝒆: {rarity_text}",
        f"✦ 𝐷𝛊𝛅𝝇𝛐𝛎𝐞𝐫𝐞𝐝 𝒃𝒚:{artist_display}",
        f"✦ 𝛪𝐷: {character['id']}",
    ]
    if character["event_name"]:
        lines.append("")
        lines.append(f"          {character['event_name']}")

    fighter = db.get_fighter_stats(character["id"])
    if fighter:
        elem = config.ELEMENTS.get(fighter["element"], {})
        lines.append("")
        lines.append(f"✦ 🛡 𝐹𝑖𝑔ℎ𝑡𝑒𝑟: {elem.get('label', fighter['element'])}")
        lines.append(f"✦ 𝐵𝑎𝑠𝑒 𝐴𝑇𝐾: {fighter['base_attack']}  |  𝐵𝑎𝑠𝑒 𝐷𝐸𝐹: {fighter['base_defense']}")

    owner_ids = db.get_random_owner_ids(character["id"], 5)
    lines += [
        "",
        "──────── ✦ ────────",
        "",
        f"🌠 𝐶la𝐢𝐦𝐞𝐝 𝛃𝛶 {owners_count} 𝛫𝐞𝐞𝛒ers",
    ]
    for uid in owner_ids:
        # account name as a link that opens the person's profile (not the @username)
        lines.append(f"▸ {_mention(uid, format_display_name(uid, db.get_display_name(uid)))}")
    return "\n".join(lines)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/check [ID]</code>", parse_mode=ParseMode.HTML
        )
        return

    try:
        char_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    if character["rarity_name"]:
        db.log_check_activity(character["rarity_name"], update.effective_user.id)
    memories.record_check(update.effective_user.id, character["id"])

    owners_count = db.count_owners(char_id)
    caption = build_card_caption(character, owners_count)

    if character["image_file_id"]:
        if character["media_type"] == "video":
            await update.message.reply_video(
                video=character["image_file_id"], caption=caption, parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_photo(
                photo=character["image_file_id"], caption=caption, parse_mode=ParseMode.HTML
            )
    else:
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML)


# ---------------- Admin: /addadmin & /removeadmin ----------------

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: <code>/addadmin [artist|manager|marzieh] [numeric Telegram ID]</code>\n"
            "Artist: /addcharacter, /removecharacter, /editcharacter, /addrarity, /removerarity, /editrarity.\n"
            "Manager: everything Artist can do, plus /new, /addevent, /removeevent.\n"
            "Marzieh: everything Manager can do, plus /ban, /unban, /forcespawn, /lockspawn, "
            "/unlockspawn, /give, /player, /setsellprice, /setpremium, /removepremium, /bin.",
            parse_mode=ParseMode.HTML,
        )
        return

    admin_type = context.args[0].upper()
    bundle = config.ADMIN_TYPE_BUNDLES.get(admin_type)
    if not bundle:
        await update.message.reply_text("⚠️ Type must be \"artist\", \"manager\", or \"marzieh\".")
        return

    try:
        new_admin_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    for permission in bundle:
        db.grant_permission(new_admin_id, permission)
    await update.message.reply_text(
        f"✅ User <code>{new_admin_id}</code> now has {admin_type.capitalize()} access "
        f"({len(bundle)} permissions - see /admin to fine-tune).",
        parse_mode=ParseMode.HTML,
    )


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/removeadmin [numeric Telegram ID or \"all\"]</code>", parse_mode=ParseMode.HTML
        )
        return

    target = context.args[0]

    if target.lower() == "all":
        count = db.remove_all_admins()
        if count:
            await update.message.reply_text(f"✅ Removed all {count} secondary admin(s).")
        else:
            await update.message.reply_text("❓ There are no secondary admins to remove.")
        return

    try:
        target_id = int(target)
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number (or \"all\").")
        return

    if db.remove_admin(target_id):
        await update.message.reply_text(f"✅ User <code>{target_id}</code> is no longer an admin.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❓ User <code>{target_id}</code> wasn't an admin.", parse_mode=ParseMode.HTML)


# ---------------- Admin: /admin (give/take individual permissions with buttons) ----------------
# A friendlier alternative to /addadmin & /removeadmin above: pick an admin
# from a list, then tap permissions on and off one at a time instead of
# retyping a command. Both older commands keep working exactly as before -
# this is just another door into the same admin_permissions table.

def _format_permissions(perm_keys) -> str:
    """"🖌 Artist, 🛡 Marzieh" style summary for a set of permission keys."""
    if not perm_keys:
        return "—"
    by_key = {p["key"]: p["label"] for p in config.ADMIN_PERMISSIONS}
    return ", ".join(by_key.get(key, key) for key in perm_keys)


def _admin_mention(user_id: int) -> str:
    return _mention(user_id, format_display_name(user_id, db.get_display_name(user_id)))


def _admin_list_keyboard():
    rows = [
        [InlineKeyboardButton(
            f"{db.get_display_name(uid)} — {_format_permissions(db.get_permissions(uid))}",
            callback_data=f"adminperm:select:{uid}",
        )]
        for uid in db.get_all_admins()
    ]
    rows.append([InlineKeyboardButton("➕ Add new admin", callback_data="adminperm:newadmin")])
    rows.append([InlineKeyboardButton("✅ Done", callback_data="adminperm:done")])
    return InlineKeyboardMarkup(rows)


def _admin_list_text() -> str:
    admins = db.get_all_admins()
    if not admins:
        return "👥 <b>Admins</b>\n\nNo secondary admins yet - add one to get started."
    return f"👥 <b>Admins ({len(admins)})</b>\n\nSelect one to edit their access:"


def _admin_menu_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Give access", callback_data=f"adminperm:give:{user_id}")],
        [InlineKeyboardButton("➖ Take access", callback_data=f"adminperm:take:{user_id}")],
        [InlineKeyboardButton("🔙 Back to list", callback_data="adminperm:list")],
    ])


def _admin_menu_text(user_id: int) -> str:
    perms = _format_permissions(db.get_permissions(user_id))
    return f"👤 {_admin_mention(user_id)} (<code>{user_id}</code>)\nCurrent access: {perms}"


def _permission_toggle_keyboard(user_id: int, action: str):
    """action is "give" (permissions this admin doesn't have yet) or "take" (ones they do)."""
    held = set(db.get_permissions(user_id))
    rows = []
    for perm in config.ADMIN_PERMISSIONS:
        has_it = perm["key"] in held
        if action == "give" and not has_it:
            rows.append([InlineKeyboardButton(
                perm["label"], callback_data=f"adminperm:grant:{user_id}:{perm['key']}"
            )])
        elif action == "take" and has_it:
            rows.append([InlineKeyboardButton(
                perm["label"], callback_data=f"adminperm:revoke:{user_id}:{perm['key']}"
            )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"adminperm:select:{user_id}")])
    return InlineKeyboardMarkup(rows)


def _permission_list_view(user_id: int, action: str):
    """(text, keyboard) for the give/take permission list."""
    heading = "➕ <b>Give access</b>" if action == "give" else "➖ <b>Take access</b>"
    keyboard = _permission_toggle_keyboard(user_id, action)
    mention = _admin_mention(user_id)
    if len(keyboard.inline_keyboard) == 1:  # only the Back button - nothing left to toggle
        note = "already has every permission." if action == "give" else "has no permissions to take yet."
        text = f"{heading}\n\n{mention} {note}"
    else:
        verb = "doesn't have yet" if action == "give" else "currently has"
        text = f"{heading}\n\nPermissions {mention} {verb}:"
    return text, keyboard


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    await update.message.reply_text(_admin_list_text(), parse_mode=ParseMode.HTML, reply_markup=_admin_list_keyboard())


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Only the bot owner can view this.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "done":
        await query.answer()
        context.user_data.pop("adminperm_awaiting_new_id", None)
        await query.edit_message_text("✅ Closed the admin panel.")
        return

    if action == "list":
        await query.answer()
        context.user_data.pop("adminperm_awaiting_new_id", None)
        await query.edit_message_text(_admin_list_text(), parse_mode=ParseMode.HTML, reply_markup=_admin_list_keyboard())
        return

    if action == "newadmin":
        await query.answer()
        context.user_data["adminperm_awaiting_new_id"] = True
        await query.edit_message_text("✏️ Send the new admin's numeric Telegram ID or @username:")
        return

    if action == "select":
        await query.answer()
        target_id = int(parts[2])
        await query.edit_message_text(
            _admin_menu_text(target_id), parse_mode=ParseMode.HTML, reply_markup=_admin_menu_keyboard(target_id)
        )
        return

    if action in ("give", "take"):
        await query.answer()
        text, keyboard = _permission_list_view(int(parts[2]), action)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if action in ("grant", "revoke"):
        target_id = int(parts[2])
        permission = parts[3]
        label = next((p["label"] for p in config.ADMIN_PERMISSIONS if p["key"] == permission), permission)

        if action == "grant":
            db.grant_permission(target_id, permission)
            await query.answer(f"✅ {label} access granted.")
            sub_action = "give"
        else:
            db.revoke_permission(target_id, permission)
            await query.answer(f"✅ {label} access removed.")
            sub_action = "take"

        # Stay on the same give/take list so several permissions can be
        # toggled one after another without hopping back and forth.
        text, keyboard = _permission_list_view(target_id, sub_action)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return


async def capture_admin_new_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Its own handler group - only acts when /admin's "Add new admin" prompt is waiting on the owner."""
    if not context.user_data.get("adminperm_awaiting_new_id"):
        return
    if not is_admin(update.effective_user.id):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    target_id = None
    if text.lstrip("+").isdigit():
        try:
            target_id = int(text)
        except ValueError:
            target_id = None
    else:
        target_id = db.get_user_id_by_username(text.lstrip("@"))

    if not target_id:
        await update.message.reply_text(
            "❓ No user found. Send a valid numeric Telegram ID or @username. "
            "For usernames, the person needs to have interacted with the bot at least once."
        )
        return

    context.user_data.pop("adminperm_awaiting_new_id", None)
    await update.message.reply_text(
        _admin_menu_text(target_id), parse_mode=ParseMode.HTML, reply_markup=_admin_menu_keyboard(target_id)
    )


# ---------------- Owner only: /filedown & /fileup (volume backup/restore) ----------------
# Not listed in /help on purpose - this is a maintenance tool for moving
# everything on the Railway volume (the DB and anything else living next
# to it) when switching services/accounts, not a day-to-day command.

async def file_down_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    volume_dir = _volume_dir()
    if not os.path.isdir(volume_dir):
        await update.message.reply_text(f"❓ Volume directory not found: <code>{volume_dir}</code>", parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text(f"📦 Packing up <code>{volume_dir}</code>, one sec...", parse_mode=ParseMode.HTML)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    archive_name = f"volume_backup_{timestamp}.tar.gz"
    archive_path = os.path.join("/tmp", archive_name)

    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(volume_dir, arcname=".")
        with open(archive_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=archive_name,
                caption=f"📦 Full backup of <code>{volume_dir}</code>.\nRestore it anywhere with /fileup.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        logger.exception("Failed to build/send volume backup")
        await update.message.reply_text("⚠️ Something went wrong while packing or sending the backup.")
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass


async def file_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    PENDING_FILE_RESTORE.add(user.id)
    await update.message.reply_text(
        "📤 Send me the <code>.tar.gz</code> backup file (from /filedown) as a document now.\n"
        "⚠️ This overwrites everything currently on the volume.",
        parse_mode=ParseMode.HTML,
    )


async def handle_file_restore_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in PENDING_FILE_RESTORE or not is_admin(user.id):
        return  # Not something this feature cares about - leave it alone.

    document = update.message.document
    if not document:
        return

    PENDING_FILE_RESTORE.discard(user.id)
    await update.message.reply_text("📥 Got it - restoring onto the volume...")

    volume_dir = _volume_dir()
    os.makedirs(volume_dir, exist_ok=True)
    download_path = os.path.join("/tmp", os.path.basename(document.file_name or "restore.tar.gz"))

    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(download_path)
        with tarfile.open(download_path, "r:*") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(volume_dir, filter="data")  # refuses ../ paths, absolute paths, special files
            else:
                tar.extractall(volume_dir)
        await update.message.reply_text(
            "✅ Volume restored. Everything from the backup is in place now - "
            "restart/redeploy the bot to be safe."
        )
    except Exception:
        logger.exception("Failed to restore volume backup")
        await update.message.reply_text(
            "⚠️ Something went wrong while restoring the backup - the volume may be partially updated."
        )
    finally:
        try:
            os.remove(download_path)
        except OSError:
            pass


# ---------------- Admin: /new channel publisher ----------------

def _new_channel_target_from_link(raw_link: str):
    """Convert the channel link supplied by the owner into a Telegram
    chat_id target that Bot API send_message can actually use."""
    raw = (raw_link or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    if re.fullmatch(r"-?\d+", raw):
        return raw
    candidate = raw if "://" in raw else "https://" + raw
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    path = parsed.path.strip("/").split("/")
    if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"} or not path:
        return None
    # Public channel: https://t.me/channelname
    if path[0] != "c" and re.fullmatch(r"[A-Za-z0-9_]{4,}", path[0]):
        return "@" + path[0]
    # Private channel links expose Telegram's internal channel id as /c/123...
    if path[0] == "c" and len(path) >= 2 and path[1].isdigit():
        return "-100" + path[1]
    return None


def _new_builder_keyboard(flow_id: str):
    flow = PENDING_NEW_FLOWS[flow_id]
    rows = []
    for i, button in enumerate(flow["buttons"]):
        rows.append([InlineKeyboardButton(f"✏️ {button['label']}", callback_data=f"new:remove:{flow_id}:{i}")])
    rows.append([
        InlineKeyboardButton("📤 Publish directly", callback_data=f"new:publish:{flow_id}"),
        InlineKeyboardButton("➕ Add button", callback_data=f"new:add:{flow_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def _new_action_keyboard(flow_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open a link", callback_data=f"new:action:{flow_id}:url")],
        [InlineKeyboardButton("🎴 Get a card", callback_data=f"new:action:{flow_id}:card")],
        [InlineKeyboardButton("💎 Get VɎ", callback_data=f"new:action:{flow_id}:vy")],
        [InlineKeyboardButton("📱 Open Mini App", callback_data=f"new:action:{flow_id}:miniapp")],
        [InlineKeyboardButton("◀️ Cancel", callback_data=f"new:cancel:{flow_id}")],
    ])


def _new_preview_text(flow):
    extra = "\n\nAdded buttons: " + ", ".join(html.escape(b["label"]) for b in flow["buttons"]) if flow["buttons"] else ""
    return f"📩 <b>Message received</b>\n\n{html.escape(flow['en_text'] or flow['fa_text'] or '')}" + extra


async def _new_flow_id_for_user(user_id: int):
    for flow_id, flow in PENDING_NEW_FLOWS.items():
        if flow.get("user_id") == user_id:
            return flow_id
    return None


def _new_miniapp_button(label: str, url: str = None):
    """Mini Apps cannot be launched with web_app buttons from channel posts.
    For /new channel posts, use the configured HTTPS/direct Mini App URL as a
    normal URL button instead. Telegram supports Direct Mini App links in any
    chat; plain HTTPS app URLs still open the configured web app normally."""
    url = (url or _miniapp_direct_link() or config.MINI_APP_URL or "").strip()
    if not url:
        return None
    return InlineKeyboardButton(label, url=url)


def _new_post_keyboard(post_id: int, buttons, showing: str = "en"):
    """Keyboard of a published post. `showing` is the language of the text currently
    displayed: English shows a 🌐 Translate button (-> Persian), Persian shows a way back."""
    if showing == "en":
        language_button = InlineKeyboardButton("🌐 Translate", callback_data=f"new:lang:{post_id}:fa")
    else:
        language_button = InlineKeyboardButton("🇬🇧 English", callback_data=f"new:lang:{post_id}:en")
    rows = [[language_button]]
    for b in buttons:
        if b["action_type"] == "url":
            rows.append([InlineKeyboardButton(b["label"], url=b["action_data"])])
        elif b["action_type"] == "miniapp":
            button = _new_miniapp_button(b["label"], b.get("action_data"))
            if button:
                rows.append([button])
        else:
            rows.append([InlineKeyboardButton(b["label"], callback_data=f"new:use:{b['id']}")])
    return InlineKeyboardMarkup(rows)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "NEW"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    existing_flow = await _new_flow_id_for_user(user.id)
    if existing_flow:
        await update.message.reply_text("⚠️ A message draft is already open. Publish or cancel it first.")
        return

    setting = db.get_new_channel_setting()
    if setting:
        # Validate the saved target before starting the wizard, so an expired/
        # inaccessible channel link is caught immediately rather than after the
        # whole message has been composed.
        try:
            await context.bot.get_chat(setting["channel_target"])
        except Exception:
            logger.exception("Saved /new channel target is no longer accessible")
            flow_id = uuid.uuid4().hex[:10]
            PENDING_NEW_FLOWS[flow_id] = {
                "user_id": user.id,
                "stage": "channel",
                "channel_target": None,
                "channel_link": None,
                "fa_text": None,
                "en_text": None,
                "buttons": [],
                "button_draft": None,
            }
            await update.message.reply_text("⚠️ The previous channel is no longer accessible.\n\n🔗 Send a new channel link.")
            return

        flow_id = uuid.uuid4().hex[:10]
        PENDING_NEW_FLOWS[flow_id] = {
            "user_id": user.id,
            "stage": "fa",
            "channel_target": setting["channel_target"],
            "channel_link": setting["channel_link"],
            "fa_text": None,
            "en_text": None,
            "buttons": [],
            "button_draft": None,
        }
        await update.message.reply_text("📝 Send the Persian version of the message.")
        return

    flow_id = uuid.uuid4().hex[:10]
    PENDING_NEW_FLOWS[flow_id] = {
        "user_id": user.id,
        "stage": "channel",
        "channel_target": None,
        "channel_link": None,
        "fa_text": None,
        "en_text": None,
        "buttons": [],
        "button_draft": None,
    }
    await update.message.reply_text("🔗 Send the channel link.")


async def _publish_new_flow(flow_id: str, context: ContextTypes.DEFAULT_TYPE, query=None):
    flow = PENDING_NEW_FLOWS.get(flow_id)
    if not flow:
        if query:
            await query.answer("⚠️ This draft has expired. Run /new again.", show_alert=True)
        return

    fa_text = flow.get("fa_text") or ""
    en_text = flow.get("en_text") or ""
    if not fa_text or not en_text:
        if query:
            await query.answer("⚠️ Both Persian and English texts are required.", show_alert=True)
        return

    # Reserve the DB post first so all callback IDs can be created before the
    # Telegram message is sent. If Telegram fails, remove the reservation.
    post_id = db.create_new_post(
        flow["channel_target"], 0, fa_text, en_text, flow["user_id"], flow["buttons"]
    )
    persisted_buttons = db.get_new_post_buttons(post_id)
    keyboard = _new_post_keyboard(post_id, persisted_buttons)

    try:
        sent = await context.bot.send_message(
            chat_id=flow["channel_target"],
            text=en_text,  # the post starts in English; the Translate button switches it to Persian
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Failed to publish /new post to %s", flow["channel_target"])
        db.delete_new_post(post_id)
        flow["stage"] = "channel"
        if query:
            await query.answer("⚠️ The channel link is invalid or the bot cannot post there.", show_alert=True)
            await query.message.reply_text("🔗 Send a new channel link.")
        return

    db.update_new_post_message_id(post_id, sent.message_id)
    db.set_new_channel_setting(flow["channel_target"], flow["channel_link"])
    del PENDING_NEW_FLOWS[flow_id]
    if query:
        await query.answer("✅ Message published.")
        try:
            await query.edit_message_text("✅ Message published successfully to the channel.")
        except Exception:
            pass


async def new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data.split(":")
    action = data[1]

    if action in {"lang", "use"}:
        # Public callbacks for already-published posts.
        if action == "lang":
            try:
                post_id = int(data[2])
                target = data[3]
            except (ValueError, IndexError):
                await query.answer("⚠️ Invalid button.", show_alert=True)
                return
            if target not in {"fa", "en"}:
                await query.answer("⚠️ Invalid language.", show_alert=True)
                return
            post = db.get_new_post(post_id)
            if not post:
                await query.answer("⚠️ This message was not found.", show_alert=True)
                return
            keyboard = _new_post_keyboard(post_id, db.get_new_post_buttons(post_id), showing=target)
            text = post["fa_text"] if target == "fa" else post["en_text"]
            await query.answer()
            try:
                await query.edit_message_text(text, reply_markup=keyboard)
            except TelegramError:
                pass  # already showing this language (someone else just tapped it)
            return

        try:
            button_id = int(data[2])
        except (ValueError, IndexError):
            await query.answer("⚠️ Invalid button.", show_alert=True)
            return

        button_info = db.get_new_post_button(button_id)
        if not button_info:
            await query.answer("⚠️ این دکمه پیدا نشد.", show_alert=True)
            return

        action_type = button_info["action_type"]
        if action_type == "card":
            result = db.claim_new_card_button(
                button_id, user.id, user.username or user.first_name
            )
            if result["status"] == "already_claimed":
                await query.answer("⚠️ You have already claimed this reward.", show_alert=True)
                return
            if result["status"] == "exhausted":
                await query.answer("⛔ This button has reached its usage limit.", show_alert=True)
                return
            if result["status"] != "ok":
                await query.answer("⚠️ This card is no longer available.", show_alert=True)
                return
            character = db.get_character(result["character_id"])
            if character:
                try:
                    memories.record_acquisition(user.id, character, "get")
                except Exception:
                    logger.exception("Failed to record /new card acquisition")
                await query.answer(f"🎴 {character['name']} received!", show_alert=True)
            else:
                await query.answer("🎴 Card received!", show_alert=True)
            return

        if action_type == "vy":
            try:
                amount = int(button_info["action_data"])
            except (TypeError, ValueError):
                await query.answer("⚠️ Invalid VɎ amount.", show_alert=True)
                return
            if amount <= 0:
                await query.answer("⚠️ Invalid VɎ amount.", show_alert=True)
                return
            result = db.claim_new_vy_button(button_id, user.id, amount)
            if result["status"] == "already_claimed":
                await query.answer("⚠️ You have already claimed this reward.", show_alert=True)
                return
            if result["status"] == "exhausted":
                await query.answer("⛔ This button has reached its usage limit.", show_alert=True)
                return
            if result["status"] != "ok":
                await query.answer("⚠️ VɎ could not be claimed.", show_alert=True)
                return
            await query.answer(f"💎 You received {amount:,} VɎ! Balance: {result['balance']:,} VɎ", show_alert=True)
            return

        await query.answer("⚠️ This button type is not supported.", show_alert=True)
        return

    if not has_command_permission(user.id, "NEW"):
        await query.answer("⛔ You're not allowed to use this.", show_alert=True)
        return

    flow_id = data[2]
    flow = PENDING_NEW_FLOWS.get(flow_id)
    if not flow:
        await query.answer("⚠️ This message draft has expired. Run /new again.", show_alert=True)
        return

    if action == "publish":
        await _publish_new_flow(flow_id, context, query)
        return
    if action == "cancel":
        del PENDING_NEW_FLOWS[flow_id]
        await query.answer("Canceled.")
        await query.edit_message_text("❌ ساخت پیام Canceled..")
        return
    if action == "add":
        flow["stage"] = "button_name"
        flow["button_draft"] = {}
        await query.answer()
        await query.message.reply_text("🔘 What should the button be called?")
        return
    if action == "remove":
        idx = int(data[3])
        if 0 <= idx < len(flow["buttons"]):
            flow["buttons"].pop(idx)
        await query.answer("Removed.")
        await query.edit_message_text(
            _new_preview_text(flow), parse_mode=ParseMode.HTML,
            reply_markup=_new_builder_keyboard(flow_id)
        )
        return
    if action == "action":
        kind = data[3]
        draft = flow["button_draft"] or {}
        draft["action_type"] = kind
        flow["button_draft"] = draft
        if kind == "url":
            flow["stage"] = "button_url"
            await query.answer()
            await query.message.reply_text("🔗 Send the link.")
        elif kind == "card":
            flow["stage"] = "button_card_id"
            await query.answer()
            await query.message.reply_text("🎴 Send the card ID.")
        elif kind == "vy":
            flow["stage"] = "button_vy_amount"
            await query.answer()
            await query.message.reply_text("💎 Enter the VɎ amount.")
        elif kind == "miniapp":
            miniapp_url = (_miniapp_direct_link() or config.MINI_APP_URL or "").strip()
            parsed = urlparse(miniapp_url) if miniapp_url else None
            if not miniapp_url or not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                await query.answer(
                    "⚠️ No valid Mini App link is set (MINI_APP_DIRECT_LINK / MINI_APP_URL).", show_alert=True
                )
                return
            flow["buttons"].append({**draft, "action_data": miniapp_url, "max_uses": None})
            flow["stage"] = "preview"
            flow["button_draft"] = None
            await query.answer("✅ Button added.")
            await query.message.reply_text(
                _new_preview_text(flow), parse_mode=ParseMode.HTML,
                reply_markup=_new_builder_keyboard(flow_id)
            )
        return


async def capture_new_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not has_command_permission(user.id, "NEW") or not update.message or not update.message.text:
        return
    flow_id = await _new_flow_id_for_user(user.id)
    if not flow_id:
        return
    flow = PENDING_NEW_FLOWS.get(flow_id)
    if not flow:
        return
    text = update.message.text.strip()
    stage = flow["stage"]
    if stage == "channel":
        target = _new_channel_target_from_link(text)
        if not target:
            await update.message.reply_text("⚠️ Invalid channel link. Send a public/private t.me link or @username.")
            raise ApplicationHandlerStop
        try:
            await context.bot.get_chat(target)
        except Exception:
            await update.message.reply_text("⚠️ I could not access this channel. Make sure the bot is an admin and the link is correct.")
            raise ApplicationHandlerStop
        flow["channel_target"] = target
        flow["channel_link"] = text
        flow["stage"] = "fa"
        await update.message.reply_text("📝 Send the Persian version of the message.")
    elif stage == "fa":
        if not text:
            await update.message.reply_text("⚠️ The Persian text cannot be empty.")
            raise ApplicationHandlerStop
        if len(text) > 4096:
            await update.message.reply_text("⚠️ The Persian text cannot exceed 4096 characters.")
            raise ApplicationHandlerStop
        flow["fa_text"] = text
        flow["stage"] = "en"
        await update.message.reply_text(
            "🇬🇧 Send the English version of the message.\n"
            "The channel post shows this one first - members tap 🌐 Translate for the Persian text."
        )
    elif stage == "en":
        if not text:
            await update.message.reply_text("⚠️ The English text cannot be empty.")
            raise ApplicationHandlerStop
        if len(text) > 4096:
            await update.message.reply_text("⚠️ The English text cannot exceed 4096 characters.")
            raise ApplicationHandlerStop
        flow["en_text"] = text
        flow["stage"] = "preview"
        await update.message.reply_text("📩 Message received", reply_markup=_new_builder_keyboard(flow_id))
    elif stage == "button_name":
        if not text or len(text) > 64:
            await update.message.reply_text("⚠️ The button name must be 1–64 characters.")
            raise ApplicationHandlerStop
        flow["button_draft"] = {"label": text}
        flow["stage"] = "button_action"
        await update.message.reply_text("⚙️ Choose what this button does.", reply_markup=_new_action_keyboard(flow_id))
    elif stage == "button_url":
        parsed = urlparse(text if "://" in text else "https://" + text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            await update.message.reply_text("⚠️ Send a valid link.")
            raise ApplicationHandlerStop
        if len(text) > 2048:
            await update.message.reply_text("⚠️ The link is too long.")
            raise ApplicationHandlerStop
        flow["button_draft"]["action_data"] = text if "://" in text else "https://" + text
        flow["buttons"].append({**flow["button_draft"], "max_uses": None})
        flow["button_draft"] = None
        flow["stage"] = "preview"
        await update.message.reply_text("✅ Button added.", reply_markup=_new_builder_keyboard(flow_id))
    elif stage == "button_card_id":
        try:
            char_id = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ The card ID must be a number.")
            raise ApplicationHandlerStop
        if not db.get_character(char_id):
            await update.message.reply_text("⚠️ No card was found with this ID.")
            raise ApplicationHandlerStop
        flow["button_draft"]["action_data"] = str(char_id)
        flow["stage"] = "button_card_uses"
        await update.message.reply_text("🔢 How many times can this button be used?")
    elif stage == "button_card_uses":
        try:
            uses = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ The usage limit must be a number.")
            raise ApplicationHandlerStop
        if uses <= 0:
            await update.message.reply_text("⚠️ The usage limit must be greater than zero.")
            raise ApplicationHandlerStop
        flow["buttons"].append({**flow["button_draft"], "max_uses": uses})
        flow["button_draft"] = None
        flow["stage"] = "preview"
        await update.message.reply_text("✅ Button added.", reply_markup=_new_builder_keyboard(flow_id))
    elif stage == "button_vy_amount":
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ The VɎ amount must be a number.")
            raise ApplicationHandlerStop
        if amount <= 0:
            await update.message.reply_text("⚠️ The amount must be greater than zero.")
            raise ApplicationHandlerStop
        flow["button_draft"]["action_data"] = str(amount)
        flow["stage"] = "button_vy_uses"
        await update.message.reply_text("🔢 How many times can this button be used?")
    elif stage == "button_vy_uses":
        try:
            uses = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ The usage limit must be a number.")
            raise ApplicationHandlerStop
        if uses <= 0:
            await update.message.reply_text("⚠️ The usage limit must be greater than zero.")
            raise ApplicationHandlerStop
        flow["buttons"].append({**flow["button_draft"], "max_uses": uses})
        flow["button_draft"] = None
        flow["stage"] = "preview"
        await update.message.reply_text("✅ Button added.", reply_markup=_new_builder_keyboard(flow_id))
    else:
        return
    raise ApplicationHandlerStop


# ---------------- Public: /send ----------------

def build_submission_keyboard(submission_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Add directly", callback_data=f"submit:add:{submission_id}")],
        [InlineKeyboardButton("🏷 Choose rarity", callback_data=f"submit:rarity:{submission_id}")],
    ])


async def _deliver_submission(context, submission_id: str, media_type: str, file_id: str, caption: str) -> int:
    """Posts a submission's review panel to the owner's DM and to the review group (if one
    is set), remembering each message so they can all be closed once the card is handled.
    Returns how many panels were delivered."""
    targets = [config.ADMIN_ID]
    review_group = db.get_review_group()
    if review_group and review_group["chat_id"] not in targets:
        targets.append(review_group["chat_id"])

    delivered = 0
    for chat_id in targets:
        kwargs = dict(
            chat_id=chat_id, caption=caption, parse_mode=ParseMode.HTML,
            reply_markup=build_submission_keyboard(submission_id),
        )
        try:
            if media_type == "video":
                sent = await context.bot.send_video(video=file_id, **kwargs)
            else:
                sent = await context.bot.send_photo(photo=file_id, **kwargs)
        except Exception:
            logger.exception("Could not post submission %s to chat %s", submission_id, chat_id)
            continue
        PENDING_SUBMISSIONS[submission_id]["messages"].append((chat_id, sent.message_id))
        delivered += 1
    return delivered


async def set_review_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setreviewgroup (inside a group) - post /send submissions there too.
    /setreviewgroup off - stop. Owner only."""
    user = update.effective_user
    chat = update.effective_chat
    if not is_admin(user.id):
        await update.message.reply_text("⛔ Only the owner can use this command.")
        return

    if context.args and context.args[0].lower() == "off":
        db.clear_review_group()
        await update.message.reply_text("✅ Submissions now go to the owner's DM only.")
        return

    if chat is None or chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "⚠️ Run /setreviewgroup inside the group where submissions should be reviewed "
            "(add the bot to it first). Use <code>/setreviewgroup off</code> to stop.",
            parse_mode=ParseMode.HTML,
        )
        return

    db.set_review_group(chat.id, chat.title)
    await update.message.reply_text(
        "✅ This group is now the submission review group.\n"
        "New /send submissions appear here and in the owner's DM, for anyone with Review /send "
        "access (see /admin). Once one is handled, the panel closes everywhere."
    )


async def submission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not has_command_permission(user.id, "REVIEWSUBMISSIONS"):
        await query.answer("⛔ You're not allowed to use this.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]
    submission_id = parts[2]

    submission = PENDING_SUBMISSIONS.get(submission_id)
    if submission is None:
        await query.answer(
            "⚠️ This submission is no longer available (already handled, or the bot restarted).",
            show_alert=True,
        )
        return

    # Swap the two buttons for the rarity picker
    if action == "rarity":
        rarities = db.list_rarities()
        if not rarities:
            await query.answer("❓ No rarities defined yet - use /addrarity first.", show_alert=True)
            return
        rows = [
            [InlineKeyboardButton(r["name"], callback_data=f"submit:setrarity:{submission_id}:{r['id']}")]
            for r in rarities
        ]
        rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"submit:back:{submission_id}")])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        return

    # Back out of the rarity picker to the original two buttons
    if action == "back":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_submission_keyboard(submission_id))
        return

    if action == "add":
        # Use whatever rarity (if any) was already in the /send caption
        rarity_name = submission["rarity_name"]
    elif action == "setrarity":
        # Same character, but the reviewer's chosen rarity overrides it
        rarity_id = int(parts[3])
        rarity_row = db.get_rarity_by_id(rarity_id)
        rarity_name = rarity_row["name"] if rarity_row else None
    else:
        await query.answer()
        return

    # Claim the submission BEFORE doing anything else: whoever gets here first adds the
    # card; anyone pressing a button on another copy of the panel finds it gone.
    submission = PENDING_SUBMISSIONS.pop(submission_id, None)
    if submission is None:
        await query.answer("⚠️ This submission was just handled by someone else.", show_alert=True)
        return

    event_was_dropped = bool(submission["event_name"]) and not db.event_exists(submission["event_name"])

    char_id = db.add_character(
        submission["name"], submission["series"], submission["file_id"], rarity_name,
        added_by_user_id=submission["sender_user_id"], added_by_username=submission["sender_username"],
        event_name=submission["event_name"], media_type=submission.get("media_type") or "photo",
    )
    fighter_element = submission.get("fighter_element")
    if fighter_element and submission.get("fighter_attack") is not None and submission.get("fighter_defense") is not None:
        db.set_fighter_stats(char_id, fighter_element, submission["fighter_attack"], submission["fighter_defense"])

    character = db.get_character(char_id)
    caption_text = build_channel_announcement(character)
    try:
        sent = await _send_character_media(
            context.bot, config.ARCHIVE_CHANNEL, character,
            caption=caption_text, parse_mode=ParseMode.HTML,
        )
        db.set_archive_message_id(char_id, sent.message_id)
    except Exception:
        logger.exception("Failed to post new character to archive channel")

    await query.answer("✅ Added!")
    rarity_display = rarity_name if rarity_name else "Unranked"
    caption = (
        f"✅ Added <b>{html.escape(submission['name'])}</b> ({html.escape(submission['series'])}) as #{char_id}\n"
        f"Rarity: {html.escape(rarity_display)}"
    )
    if fighter_element:
        elem = config.ELEMENTS.get(fighter_element, {})
        caption += (
            f"\n\n🛡 Fighter\nElement: {elem.get('label', fighter_element)}"
            f"\nBase Attack: {submission['fighter_attack']}\nBase Defense: {submission['fighter_defense']}"
        )
    if event_was_dropped:
        caption += f"\n⚠️ Event \"{html.escape(submission['event_name'])}\" isn't registered - added without an event."
    caption += f"\n\n👤 Approved by {_mention(user.id, user.first_name)}"

    # Close the panel everywhere - the one that was pressed and its copies in the other
    # chats - so this card can't be added a second time.
    await query.edit_message_caption(caption=caption, parse_mode=ParseMode.HTML)
    pressed = (query.message.chat.id, query.message.message_id)
    for chat_id, message_id in submission.get("messages", []):
        if (chat_id, message_id) == pressed:
            continue
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=caption, parse_mode=ParseMode.HTML
            )
        except TelegramError:
            logger.warning("Could not close submission panel %s/%s", chat_id, message_id)


async def send_character_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Public command - any player can submit a character for the owner
    # to review and add (banned users are already blocked globally by
    # _block_banned_users before this handler ever runs).
    user = update.effective_user

    file_id, media_type = _extract_media(update.message)
    if not file_id:
        await update.message.reply_text(
            "📸 Please send a photo or video with this caption format:\n"
            "<code>/send Name | Series</code>\n"
            "You'll then pick a rarity and event with buttons "
            "(or add them as extra <code>| Rarity | Event</code> parts to skip the buttons).",
            parse_mode=ParseMode.HTML,
        )
        return

    caption = update.message.caption or ""
    caption_body = caption.split(None, 1)[1] if " " in caption else ""
    parts = [p.strip() for p in caption_body.split("|")]

    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "⚠️ Format error. Use:\n"
            "<code>/send Name | Series</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name, series = parts[0], parts[1]
    preset_rarity_name = parts[2] if len(parts) > 2 and parts[2] else None
    preset_event_name = parts[3] if len(parts) > 3 and parts[3] else None

    # Whoever ran /send is recorded as the artist - this is what ends up
    # in "added_by_*" and therefore in every "discovered by" credit line,
    # no matter which button the owner later taps to finalize it.
    artist_username = user.username or user.first_name

    pending_id = uuid.uuid4().hex[:8]
    PENDING_ADD_FLOW[pending_id] = {
        "kind": "send",
        "name": name,
        "series": series,
        "file_id": file_id,
        "media_type": media_type,
        "user_id": user.id,
        "username": artist_username,
        "first_name": user.first_name,
        "rarity_name": preset_rarity_name,
        "rarity_done": bool(preset_rarity_name),
        "event_name": preset_event_name,
        "event_done": bool(preset_event_name),
    }

    await _advance_add_flow(pending_id, context, message=update.message)


# ---------------- Admin: /editrarity & /editcharacter (Artist/Manager/Marzieh) ----------------

async def edit_rarity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "EDITRARITY"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/editrarity [name]</code>", parse_mode=ParseMode.HTML
        )
        return

    name = " ".join(context.args)
    rarity = db.get_rarity_by_name(name)
    if not rarity:
        await update.message.reply_text(f"❓ No rarity found named \"{name}\".")
        return

    context.user_data["awaiting_editrarity"] = rarity["name"]
    await update.message.reply_text(f"✏️ Enter the new weight for <b>{rarity['name']}</b>:", parse_mode=ParseMode.HTML)


async def edit_character_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "EDITCHARACTER"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/editcharacter [ID]</code>", parse_mode=ParseMode.HTML)
        return

    try:
        char_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    character = db.get_character(char_id)
    if not character:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Name", callback_data=f"editchar:field:name:{char_id}"),
            InlineKeyboardButton("🎬 Series", callback_data=f"editchar:field:series:{char_id}"),
        ],
        [
            InlineKeyboardButton("💎 Rarity", callback_data=f"editchar:field:rarity:{char_id}"),
            InlineKeyboardButton("🎉 Event", callback_data=f"editchar:field:event:{char_id}"),
        ],
    ])
    await update.message.reply_text(
        f"✏️ Editing <b>{character['name']}</b> (#{char_id}) - what do you want to change?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def edit_character_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1 of /editcharacter: the admin just picked which field to
    change. Name/Series need a typed value next; Rarity/Event show a
    button list instead, since those have to match something that
    already exists."""
    query = update.callback_query
    user = query.from_user
    if not has_command_permission(user.id, "EDITCHARACTER"):
        await query.answer("⛔ You're not allowed to use this.", show_alert=True)
        return
    await query.answer()

    _, _, field, char_id_str = query.data.split(":")
    char_id = int(char_id_str)
    character = db.get_character(char_id)
    if not character:
        await query.edit_message_text(f"❓ Character #{char_id} no longer exists.")
        return

    if field in ("name", "series"):
        context.user_data["awaiting_editcharacter"] = {"char_id": char_id, "field": field}
        label = "name" if field == "name" else "series"
        await query.edit_message_text(
            f"✏️ Send the new {label} for <b>{character['name']}</b> (#{char_id}):",
            parse_mode=ParseMode.HTML,
        )
        return

    if field == "rarity":
        rarities = db.list_rarities()
        buttons = [
            InlineKeyboardButton(r["name"], callback_data=f"editchar:setrarity:{char_id}:{r['id']}")
            for r in rarities
        ]
        rows = _grid_rows(buttons)
        rows.append([InlineKeyboardButton("🚫 Unranked (no rarity)", callback_data=f"editchar:setrarity:{char_id}:none")])
        await query.edit_message_text(
            f"💎 Pick a new rarity for <b>{character['name']}</b> (#{char_id}):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    # field == "event"
    events = db.get_all_events()
    buttons = [
        InlineKeyboardButton(f"🔒 {ev['name']}" if ev["locked"] else ev["name"], callback_data=f"editchar:setevent:{char_id}:{i}")
        for i, ev in enumerate(events)
    ]
    rows = _grid_rows(buttons)
    rows.append([InlineKeyboardButton("🚫 No event", callback_data=f"editchar:setevent:{char_id}:none")])
    await query.edit_message_text(
        f"🎉 Pick a new event for <b>{character['name']}</b> (#{char_id}):",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def edit_character_apply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 for Rarity/Event: the admin picked one of the listed
    options, apply it straight away - name/series stay whatever they
    already were."""
    query = update.callback_query
    user = query.from_user
    if not has_command_permission(user.id, "EDITCHARACTER"):
        await query.answer("⛔ You're not allowed to use this.", show_alert=True)
        return
    await query.answer()

    _, kind, char_id_str, value = query.data.split(":")
    char_id = int(char_id_str)
    character = db.get_character(char_id)
    if not character:
        await query.edit_message_text(f"❓ Character #{char_id} no longer exists.")
        return

    if kind == "setrarity":
        if value == "none":
            new_rarity_name = None
        else:
            rarity = next((r for r in db.list_rarities() if r["id"] == int(value)), None)
            new_rarity_name = rarity["name"] if rarity else None
        db.update_character(char_id, character["name"], character["series"], new_rarity_name, character["event_name"])
        field_label, new_label = "Rarity", (new_rarity_name or "Unranked")
    else:  # setevent
        if value == "none":
            new_event_name = None
        else:
            events = db.get_all_events()
            idx = int(value)
            new_event_name = events[idx]["name"] if 0 <= idx < len(events) else None
        db.update_character(char_id, character["name"], character["series"], character["rarity_name"], new_event_name)
        field_label, new_label = "Event", (new_event_name or "No event")

    updated = db.get_character(char_id)
    await query.edit_message_text(f"✅ {field_label} for <b>{updated['name']}</b> (#{char_id}) set to: {new_label}", parse_mode=ParseMode.HTML)
    await _post_character_update_to_archive(context, char_id, user.id, user.username or user.first_name)


async def capture_edit_character_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in its own handler group - only acts when an admin has an
    active /editcharacter Name/Series prompt waiting for a typed value."""
    pending = context.user_data.get("awaiting_editcharacter")
    if not pending:
        return

    value = update.message.text.strip() if update.message.text else ""
    if not value:
        return

    char_id, field = pending["char_id"], pending["field"]
    character = db.get_character(char_id)
    if not character:
        context.user_data.pop("awaiting_editcharacter", None)
        await update.message.reply_text(f"❓ Character #{char_id} no longer exists.")
        return

    name = value if field == "name" else character["name"]
    series = value if field == "series" else character["series"]
    db.update_character(char_id, name, series, character["rarity_name"], character["event_name"])
    context.user_data.pop("awaiting_editcharacter", None)

    label = "Name" if field == "name" else "Series"
    await update.message.reply_text(f"✅ {label} for character #{char_id} updated to: {value}")
    editor = update.effective_user
    await _post_character_update_to_archive(context, char_id, editor.id, editor.username or editor.first_name)


# ---------------- Admin: /setpersonality (Chat tab) ----------------
# Sets the HIDDEN persona that drives a character's replies in the Mini
# App's Chat tab (see chat_ai.py) - name/series stay public everywhere
# else, but this text is never shown to players, only read server-side
# when building that character's AI system prompt.

async def set_personality_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    text = update.message.text or ""
    body = text.split(None, 1)[1] if " " in text else ""
    parts = [p.strip() for p in body.split("|")]

    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "⚠️ Usage: <code>/setpersonality ID | Personality description | Age(optional) | Gender(optional)</code>\n\n"
            "This is HIDDEN - players never see it. It only shapes how this character talks "
            "in the Mini App's Chat tab. Example:\n"
            "<code>/setpersonality 42 | Short-tempered and blunt, hates being kept waiting, "
            "secretly cares a lot | 24 | female</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        char_id = int(parts[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID must be a number.")
        return

    persona = parts[1]
    age = parts[2] if len(parts) > 2 and parts[2] else None
    gender = parts[3] if len(parts) > 3 and parts[3] else None

    if db.set_character_persona(char_id, persona, age, gender):
        character = db.get_character(char_id)
        name = character["name"] if character else f"#{char_id}"
        await update.message.reply_text(
            f"✅ Hidden Chat-tab personality saved for <b>{name}</b> "
            f"(age: {age or '—'}, gender: {gender or '—'}).",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❓ No character found with ID #{char_id}.")


# ---------------- Admin: /lockspawn & /unlockspawn ----------------
# Work on both rarities and events: if the name matches an existing
# rarity, that rarity's whole tier is excluded from spawning. Otherwise
# it's treated as an event name. Multiple rarities/events can be locked
# at the same time - each is tracked independently.
#
# "/lockspawn main <name>" / "/unlockspawn main <name>" only touch the MAIN
# group (the one set with /setforcejoin); without "main" the lock applies to
# every group. The two lists are independent.

def _split_spawn_lock_args(args):
    """(is_main_scope, name) from the command arguments."""
    if args and args[0].lower() == "main":
        return True, " ".join(args[1:]).strip()
    return False, " ".join(args).strip()


def _find_rarity_for_lock(name: str):
    """The rarity a typed name refers to: exact name first, then ignoring case/styled letters."""
    rarity = db.get_rarity_by_name(name)
    if rarity:
        return rarity
    wanted = _norm(name)
    return next((r for r in db.list_rarities() if _norm(r["name"]) == wanted), None)


def _canonical_event_name(name: str) -> str:
    """The registered event a typed name refers to ("🛡Fighter🛡" finds "🛡𝗙𝗶𝗴𝗵𝘁𝗲𝗿🛡"),
    or the name as typed when no registered event matches."""
    wanted = _norm(name)
    for event in db.get_all_events():
        if event["name"].lower() == name.lower() or _norm(event["name"]) == wanted:
            return event["name"]
    return name


async def _spawn_lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lock: bool):
    user = update.effective_user
    if not has_command_permission(user.id, "LOCKSPAWN" if lock else "UNLOCKSPAWN"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    command = "lockspawn" if lock else "unlockspawn"
    main, name = _split_spawn_lock_args(context.args)
    if not name:
        await update.message.reply_text(
            f"⚠️ Usage: <code>/{command} [rarity or event name]</code>\n"
            f"Add <code>main</code> to only affect the main group: <code>/{command} main [name]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if main and db.get_main_chat_id() is None:
        await update.message.reply_text(
            "⚠️ The main group isn't set yet - run /setforcejoin inside it first."
        )
        return

    scope = "in the main group" if main else "everywhere"
    rarity = _find_rarity_for_lock(name)
    if rarity:
        label, kind_text = f"Rarity <b>{html.escape(rarity['name'])}</b>", "its tier"
        if lock:
            db.lock_rarity(rarity["id"], main=main)
        else:
            db.unlock_rarity(rarity["id"], main=main)
        still_locked_globally = main and not lock and db.is_rarity_locked(rarity["id"])
    else:
        name = _canonical_event_name(name)
        label, kind_text = f"Event <b>{html.escape(name)}</b>", "its cards"
        if lock:
            db.lock_event(name, main=main)
        else:
            db.unlock_event(name, main=main)
        still_locked_globally = main and not lock and db.is_event_locked(name)

    if lock:
        text = f"🔒 {label} is now locked {scope} - {kind_text} won't spawn there."
    else:
        text = f"🔓 {label} can spawn again {scope}."
        if still_locked_globally:
            text += (
                f"\nℹ️ It is also locked for every group - use <code>/unlockspawn {html.escape(name)}</code> "
                "(without main) to open it everywhere."
            )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def lock_spawn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _spawn_lock_command(update, context, lock=True)


async def unlock_spawn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _spawn_lock_command(update, context, lock=False)


# ---------------- Admin: /addevent & /removeevent ----------------

async def add_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "ADDEVENT"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/addevent [name]</code>", parse_mode=ParseMode.HTML
        )
        return

    name = " ".join(context.args)
    if db.add_event(name):
        await update.message.reply_text(
            f"✅ Event <b>{html.escape(name)}</b> registered. Characters can now be tagged with it.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❓ Event \"{name}\" is already registered.")


async def remove_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not has_command_permission(user.id, "REMOVEEVENT"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/removeevent [name or \"all\"]</code>", parse_mode=ParseMode.HTML
        )
        return

    name = " ".join(context.args)

    if name.lower() == "all":
        if not is_admin(user.id):
            await update.message.reply_text("⛔ Only the owner can remove all events at once.")
            return
        db.wipe_all_events()
        await update.message.reply_text(
            "🗑️ All events have been wiped.\n"
            "They're in /bin for the next 30 days if you need any of them back."
        )
        return

    if db.remove_event(name):
        await update.message.reply_text(
            f"🗑️ Event <b>{html.escape(name)}</b> removed (in /bin for 30 days). Existing characters keep their tag, "
            "but it can no longer be assigned to new ones.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❓ No event found named \"{name}\".")


# ---------------- /bin (owner: 30-day trash) ----------------

BIN_CATEGORY_LABELS = {
    "character": "🧑 Characters",
    "rarity": "💎 Rarities",
    "event": "🎪 Events",
}


def _bin_menu_text_and_keyboard():
    counts = db.get_bin_counts()
    text = (
        "🗑 <b>Bin</b>\n"
        "Deleted characters, rarities, and events are kept here for 30 days "
        "before being gone for good.\n\n"
        f"🧑 Characters: <b>{counts['character']}</b>\n"
        f"💎 Rarities: <b>{counts['rarity']}</b>\n"
        f"🎪 Events: <b>{counts['event']}</b>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧑 Characters ({counts['character']})", callback_data="bin:cat:character")],
        [InlineKeyboardButton(f"💎 Rarities ({counts['rarity']})", callback_data="bin:cat:rarity")],
        [InlineKeyboardButton(f"🎪 Events ({counts['event']})", callback_data="bin:cat:event")],
    ])
    return text, keyboard


def _bin_category_text_and_keyboard(kind: str):
    items = db.get_bin_items(kind)
    title = BIN_CATEGORY_LABELS.get(kind, kind)

    if not items:
        text = f"{title}\n\n📭 Nothing in the bin right now."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="bin:menu")]])
        return text, keyboard

    shown = items[:30]
    lines = [f"{title} ({len(items)}) - kept 30 days from deletion\n"]
    buttons = []
    for item in shown:
        deleted_date = item["deleted_at"][:10]
        lines.append(f"• {item['label']} (deleted {deleted_date})")
        short_label = item["label"] if len(item["label"]) <= 28 else item["label"][:27] + "…"
        buttons.append([InlineKeyboardButton(f"♻️ {short_label}", callback_data=f"bin:restore:{kind}:{item['id']}")])

    if len(items) > len(shown):
        lines.append(f"\n…and {len(items) - len(shown)} more (use \"Restore all\" to bring back everything).")

    buttons.append([InlineKeyboardButton(f"♻️ Restore all ({len(items)})", callback_data=f"bin:restoreall:{kind}")])
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="bin:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def bin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_command_permission(uid, "BIN"):
        await update.message.reply_text("⛔ You're not allowed to use this command.")
        return

    text, keyboard = _bin_menu_text_and_keyboard()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def bin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not has_command_permission(uid, "BIN"):
        await query.answer("⛔ You're not allowed to use this.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "menu":
        await query.answer()
        text, keyboard = _bin_menu_text_and_keyboard()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if action == "cat":
        await query.answer()
        kind = parts[2]
        text, keyboard = _bin_category_text_and_keyboard(kind)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if action == "restore":
        kind, item_id = parts[2], int(parts[3])
        label = db.restore_bin_item(item_id)
        await query.answer(f"♻️ Restored {label}" if label else "❓ Already gone (expired or restored).", show_alert=not label)
        text, keyboard = _bin_category_text_and_keyboard(kind)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if action == "restoreall":
        kind = parts[2]
        count = db.restore_all_bin_items(kind)
        await query.answer(f"♻️ Restored {count} item(s)." if count else "❓ Nothing to restore.", show_alert=True)
        text, keyboard = _bin_category_text_and_keyboard(kind)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return


# ---------------- /rarities ----------------

RARITIES_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "rarities.jpg")


def _bold_sans(text: str) -> str:
    """Convert plain ASCII letters/digits to Mathematical Sans-Bold unicode
    characters. Non-ASCII characters (emoji, etc.) are left untouched."""
    out = []
    for ch in text:
        if 'A' <= ch <= 'Z':
            out.append(chr(ord(ch) - ord('A') + 0x1D5D4))
        elif 'a' <= ch <= 'z':
            out.append(chr(ord(ch) - ord('a') + 0x1D5EE))
        elif '0' <= ch <= '9':
            out.append(chr(ord(ch) - ord('0') + 0x1D7EC))
        else:
            out.append(ch)
    return ''.join(out)


def _rarity_bar(owned: int, total: int):
    """Build a 10-diamond progress bar. Returns (bar, percent), or None if
    the rarity has no cards at all (total == 0)."""
    if total == 0:
        return None
    raw_percent = (owned / total) * 100
    percent = min(int(math.ceil(raw_percent / 5) * 5), 100)  # round UP to nearest 5%
    full = percent // 10
    half = 1 if percent % 10 else 0
    empty = 10 - full - half
    bar = "◆" * full + "◈" * half + "◇" * empty
    return bar, percent


def _rarity_list_order(rarity):
    """Sort key for /rarities: the rarest tier first, following the economy tier order
    (Omnara ... Rare, Common - the reverse of config.RARITY_BASE_PRICES). Spawn weight
    can't be used for this: it isn't guaranteed to rank Rare above Common. Rarities
    the economy doesn't know go last, rarest weight first."""
    tiers = list(config.RARITY_BASE_PRICES)
    tier = economy.match_price_tier(rarity["name"])
    if tier is None:
        return (1, 0, rarity["weight"])
    return (0, -tiers.index(tier), rarity["weight"])


async def rarities_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rarities = sorted(db.list_rarities(), key=_rarity_list_order)
    if not rarities:
        await update.message.reply_text("🎖 No rarities have been added yet.")
        return

    user_id = update.effective_user.id

    lines = [
        f"🎖 ✦  {_bold_sans('RARITIES')}  ✦",
        "Your Collection Progress",
        "╰─────────────── ✦ ───────────────╯",
        "",
    ]

    for r in rarities:
        total = db.count_characters_by_rarity(r["id"])
        owned = db.count_user_owned_by_rarity(user_id, r["id"])

        lines.append(_bold_sans(r["name"]))
        result = _rarity_bar(owned, total)
        if result is None:
            lines.append("—")
        else:
            bar, percent = result
            lines.append(f"{bar}  {percent}%")
            lines.append(f"{owned} / {total}")
        lines.append("")

    text = "\n".join(lines).rstrip()

    # Telegram photo captions are capped at 1024 characters; if the rarity
    # list ever grows past that, send the image and text as two messages.
    try:
        with open(RARITIES_IMAGE_PATH, "rb") as photo:
            if len(text) <= 1024:
                await update.message.reply_photo(photo=photo, caption=text)
            else:
                await update.message.reply_photo(photo=photo)
                await update.message.reply_text(text)
    except FileNotFoundError:
        await update.message.reply_text(text)


# ---------------- /prices (dynamic rarity economy) ----------------

async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎉 Event bonuses", callback_data="prices:events"),
    ]])
    await update.message.reply_text(
        economy.format_prices_message(), parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def prices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(economy.format_event_tiers_message(), parse_mode=ParseMode.HTML)


# ---------------- /memories ----------------

async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = memories.format_memories_text(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------- /birthday ----------------

async def birthday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = db.get_user_birthday(user.id)
    if existing:
        month, day = existing
        await update.message.reply_text(
            f"🎂 Your birthday's already locked in as <b>{month:02d}-{day:02d}</b>.\n"
            f"You'll get a {config.BIRTHDAY_GIFT_RARITY_NAME} card automatically every year on that day.",
            parse_mode=ParseMode.HTML,
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, today's my birthday", callback_data="birthday:confirm"),
        InlineKeyboardButton("❌ No", callback_data="birthday:cancel"),
    ]])
    await update.message.reply_text(
        "🎂 Is today <i>really</i> your birthday?\n\n"
        "Confirming locks in today's date as your birthday for good, gives you a random "
        f"{config.BIRTHDAY_GIFT_RARITY_NAME} card right now, and the same gift again automatically "
        "every year on this day.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def birthday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user = query.from_user

    if action == "cancel":
        await query.edit_message_text("👍 No problem - come back on your actual birthday!")
        return

    # action == "confirm" - set_user_birthday() itself refuses to overwrite
    # an existing birthday, so this is safe even against a double-tap.
    today = datetime.utcnow().date()
    newly_set = db.set_user_birthday(user.id, today.month, today.day)
    if not newly_set:
        await query.edit_message_text("🎂 Your birthday's already locked in from before.")
        return

    rarity = db.get_rarity_by_name(config.BIRTHDAY_GIFT_RARITY_NAME)
    character = db.get_random_character_in_rarity_ids([rarity["id"]]) if rarity else None
    if not character:
        await query.edit_message_text(
            f"🎂 Happy birthday! Your birthday is locked in as {today.month:02d}-{today.day:02d}, "
            f"but there's no {config.BIRTHDAY_GIFT_RARITY_NAME} card in the bot yet for your gift - "
            "bug the owner about it!"
        )
        return

    db.give_character_to_user(user.id, user.username or user.first_name, character["id"])
    db.mark_birthday_gifted(user.id, today.year)

    await query.edit_message_text(
        f"🎉 Happy Birthday! Locked in as <b>{today.month:02d}-{today.day:02d}</b>.\n\n"
        f"🎁 Your gift: <b>{character['name']}</b> ({character['series']}) - "
        f"a {config.BIRTHDAY_GIFT_RARITY_NAME} card!\n\n"
        "You'll get another one automatically, every year, on this day. 🥳",
        parse_mode=ParseMode.HTML,
    )

    for extra_message in memories.record_acquisition(user.id, character, "birthday_gift"):
        try:
            await context.bot.send_message(chat_id=user.id, text=extra_message, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to send birthday milestone message to %s", user.id)


# ---------------- /start ----------------

START_TEXT = (
    "Yokoso!\n"
    "⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭⎯꯭ׁ⎯꯭ \n\n"
    "🪭𝛶ou are s𝛐 lu𝝇𝛋𝛄 because you have me now! 𝐺etter 𝛃ot!\n"
    "  \n"
    "🪭𝑊hat am I for? 𝛶ou 𝝇an 𝛕ake the card and I'll h𝛐ld it for 𝛄ou!!\n\n"
    "🪭𝛮ow 𝛼dd me to the gro𝛖𝛒 so ᥕe 𝝇a𝛈 colle𝝇𝛕 l𝛐ts of cards!!!\n"
    "‌\n"
    "🧧 Use /get [name] to claim a spawned character."
)

START_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "start.jpg")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new_user = db.register_bot_user_if_new(
        user.id, username=user.username, first_name=user.first_name, last_name=user.last_name
    )

    # Deep-link payload from an invite link: /start ref_<inviter_id>. Only
    # rewards on a genuinely first-ever /start, and never for self-invites.
    if is_new_user and context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                inviter_id = int(payload[len("ref_"):])
            except ValueError:
                inviter_id = None
            if inviter_id and inviter_id != user.id:
                await _reward_referral(inviter_id, user, context)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add me to a group", url=f"https://t.me/{context.bot.username}?startgroup=true")],
        [InlineKeyboardButton("📢 Main channel", url="https://t.me/Getterschannel")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")],
    ])

    try:
        with open(START_IMAGE_PATH, "rb") as photo:
            await update.message.reply_photo(
                photo=photo, caption=START_TEXT, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
    except FileNotFoundError:
        await update.message.reply_text(START_TEXT, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ---------------- /invite ----------------

async def _reward_referral(inviter_id: int, invited_user, context: ContextTypes.DEFAULT_TYPE):
    """Pays out the inviter's reward for a fresh, non-self referral."""
    count, reward, bonus_triggered = db.record_referral(inviter_id)
    db.add_currency(inviter_id, reward)

    invited_name = invited_user.first_name or (invited_user.username or "a new player")
    lines = [
        f"🎉 You received {reward} {config.CURRENCY_SYMBOL} because {invited_name} joined using your invite link!",
    ]
    if reward < db.INVITE_REWARD_CAP:
        lines.append("Invite another friend to double your next reward!")
    else:
        lines.append(
            f"Your reward is now capped at {db.INVITE_REWARD_CAP} {config.CURRENCY_SYMBOL} per invite - "
            "it stays this high for every future invite."
        )

    if bonus_triggered:
        db.add_currency(inviter_id, db.INVITE_FIRST_CAP_BONUS)
        nocturne_character = db.get_random_character_by_rarity_substring("Nocturne")
        lines.append(
            f"\n🌙 Bonus! For reaching the {db.INVITE_REWARD_CAP} cap for the first time, "
            f"you also got an extra {db.INVITE_FIRST_CAP_BONUS} {config.CURRENCY_SYMBOL}!"
        )
        if nocturne_character:
            db.give_character_to_user(inviter_id, None, nocturne_character["id"])
            lines.append(
                f"...and a random 🌙Nocturne card: <b>{nocturne_character['name']}</b> ({nocturne_character['series']})!"
            )
        else:
            lines.append("...and a random 🌙Nocturne card was owed to you, but none exist yet - contact the owner!")

    try:
        await context.bot.send_message(chat_id=inviter_id, text="\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to notify inviter %s about a new referral", inviter_id)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
    count = db.get_invite_count(user.id)

    text = (
        "🔗 <b>Your invite link</b>\n"
        f"{link}\n\n"
        f"Friends invited so far: <b>{count}</b>\n\n"
        "Share this link - when someone starts the bot for the first time through it, "
        f"you get {config.CURRENCY_SYMBOL}. The reward doubles with each new invite "
        f"(5 → 10 → 20 → 40 → 80 → {db.INVITE_REWARD_CAP}), then stays at "
        f"{db.INVITE_REWARD_CAP} per invite after that. The first time you hit the cap, "
        f"you also get a bonus {db.INVITE_FIRST_CAP_BONUS} {config.CURRENCY_SYMBOL} and a random 🌙Nocturne card!"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------- /help ----------------
# Every command lives in exactly one entry below: (header, description).
# build_help_text() stitches together the entries each role is allowed to see.
# Adding a command? Add it here (and to the right role list) - nothing else.

def _cmd(*variants: str) -> str:
    return " or ".join(f"<b>{v}</b>" for v in variants)


_HELP_PLAYER = [
    (_cmd("/start"), "Basic intro message."),
    (_cmd("/help"), "Show this list."),
    (_cmd("/get [Name]"),
     "Claim the currently spawned character. Just the first or last name is enough "
     f"(daily limit: {config.DAILY_CAPTURE_LIMIT}, resets at midnight Iran time)."),
    (_cmd("/constellation"),
     "See your own collection, grouped by series, with a button to browse all your character photos."),
    (_cmd("/fav [ID]"),
     "Pick your favorite card (confirm with OK / Cancel) - it becomes the picture shown on your /constellation."),
    (_cmd("/sort"),
     "Filter what your constellation shows by character, series, rarity and event - stack as many "
     "filters as you like (one word of a name is enough)."),
    (_cmd("/miniapp"), "Open the Waifu Market Mini App."),
    (_cmd("/check [ID]"), "See a character's photo, full details, who added it, and how many players own it."),
    (_cmd("/rarities"), "List all rarity tiers and how many of each you own."),
    (_cmd("/prices"),
     "See the current price range for every rarity, with a button showing how much each event tier adds."),
    (_cmd("/memories"), "See a timeline of your milestones - first cards, rarity counts, and monthly gifts."),
    (_cmd("/top"), "Leaderboards: the richest players (/top vy) and the biggest collections (/top collection)."),
    (_cmd("/premium"), "Check your premium status and perks."),
    (_cmd("/trade [ID]"), "Premium only - trade a card in for a random other character of the same rarity."),
    (_cmd("/search [term]"), "Find characters by name, series, or rarity - shows results as a photo gallery."),
    (_cmd("/gallery"), "Browse every character in the database as a photo gallery."),
    (_cmd("/gift [ID]"),
     "Reply to someone with this to give them one of your cards (asks for confirmation first)."),
    (_cmd("/spawnstatus"), "See which rarities and events are currently locked from spawning."),
    (_cmd("/dart"),
     f"Throw a dart for a chance to win currency (up to {config.DAILY_DART_LIMIT} throws/day, resets at midnight Iran time)."),
    (_cmd("/inv"), "Check your currency balance."),
    (_cmd("/vypay [amount]"), "Reply to someone with this to send them some of your currency."),
    (_cmd("/invite"),
     "Get your personal invite link - earn currency (and eventually a bonus + a 🌙Nocturne card) "
     "for every friend who joins through it."),
    (_cmd("/birthday"),
     f"Tell the bot today's your birthday: you get a random {config.BIRTHDAY_GIFT_RARITY_NAME} card right away "
     "and the same gift again every year on this day (can only be set once)."),
    (_cmd("/sell [ID] [price]"), "List one of your cards on the Waifu Market Mini App for other players to buy."),
    (_cmd("/cancelsell [listing ID]"), "Pull one of your own active market listings back."),
    (_cmd("/sellbot [ID]"), "Sell one of your cards straight to the bot for currency (amount depends on rarity)."),
    (_cmd("/market"), "Browse everyone's active Waifu Market listings."),
    (_cmd("/send"), "Submit a character (photo + caption, like /addcharacter) for the team to review and add."),
]

_ADD_CHARACTER = (
    _cmd("/addcharacter"),
    "Send a photo with caption: <code>Name | Series | Rarity(optional) | Event(optional)</code>",
)
_EDIT_CHARACTER = (
    _cmd("/editcharacter ID | Name | Series | Rarity | Event"),
    "Edit an existing character's details (no photo needed).",
)
_ADD_RARITY = (
    _cmd("/addrarity [name] [weight]"),
    "Create or update a rarity tier. Higher weight = spawns more often.",
)
_EDIT_RARITY = (
    _cmd("/editrarity [name]"),
    "Change a rarity's weight (bot will ask you to type the new number).",
)

# Each entry tagged with the one permission it needs (see config.ADMIN_PERMISSIONS
# and /admin) - build_help_text filters these down to whatever a given secondary
# admin actually holds. The owner always sees the untagged _OWNER variants instead.
_HELP_CARD_EDITING = [
    ("ADDCHARACTER",) + _ADD_CHARACTER,
    ("REMOVECHARACTER", _cmd("/removecharacter [ID]"), 'Remove a character (can\'t use "all").'),
    ("EDITCHARACTER",) + _EDIT_CHARACTER,
    ("ADDRARITY",) + _ADD_RARITY,
    ("REMOVERARITY", _cmd("/removerarity [name]"), "Remove one rarity tier."),
    ("EDITRARITY",) + _EDIT_RARITY,
]

# Owner: same tools, but "all" is allowed.
_HELP_CARD_EDITING_OWNER = [
    _ADD_CHARACTER,
    (_cmd('/removecharacter [ID or "all"]'),
     "Remove one character, or wipe all of them at once (recoverable from /bin for 30 days)."),
    _EDIT_CHARACTER,
    _ADD_RARITY,
    (_cmd('/removerarity [name or "all"]'),
     "Remove one rarity tier, or wipe all of them at once (recoverable from /bin for 30 days)."),
    _EDIT_RARITY,
]

_HELP_MODERATION = [
    ("BAN", _cmd("/ban", "/ban [days]", "/ban [user ID] [days]"),
     "Reply to a player (or give their ID) to ban them - permanent if no days given."),
    ("UNBAN", _cmd("/unban", "/unban [user ID]"), "Reply to a banned player (or give their ID) to lift their ban."),
    ("FORCESPAWN", _cmd("/forcespawn"), "Instantly spawn a random character in the current group."),
    ("LOCKSPAWN", _cmd("/lockspawn [rarity or event]", "/lockspawn main [rarity or event]"),
     "Stop a rarity tier or event's cards from spawning. Multiple locks can be active at once. "
     "With <code>main</code> the lock only applies to the main group (the /setforcejoin one)."),
    ("UNLOCKSPAWN", _cmd("/unlockspawn [rarity or event]", "/unlockspawn main [rarity or event]"),
     "Re-allow a locked rarity or event to spawn again (with <code>main</code>: only in the main group)."),
    ("GIVE", _cmd("/give [ID]", "/give [amount] vy"),
     'Reply to someone with this to give them a card by ID, or currency (add "vy" after the amount).'),
    ("PLAYER", _cmd("/player"),
     "Manage a player's account: send their @username, then add/remove a card or give/take currency."),
    ("SICK", _cmd("/sick out"),
     "Reply to a player with this for a canned public burn - purely cosmetic, no ban or mute happens."),
]

_HELP_NEW = [
    ("NEW", _cmd("/new"),
     "Write a post (Persian + English, optional buttons) and publish it to the announcement channel - "
     "it appears in English with a 🌐 Translate button for the Persian text."),
]

# Owner has the same two, with "all" - see _HELP_EVENTS_OWNER below.
_HELP_EVENTS = [
    ("ADDEVENT", _cmd("/addevent [name]"), "Register a new event name so it can be tagged onto characters."),
    ("REMOVEEVENT", _cmd("/removeevent [name]"),
     "Unregister an event (recoverable from /bin for 30 days). Only the owner can wipe them all at once."),
]

_HELP_EVENTS_OWNER = [
    (_cmd("/addevent [name]"), "Register a new event name so it can be tagged onto characters."),
    (_cmd('/removeevent [name or "all"]'),
     "Unregister an event, or wipe all of them at once (recoverable from /bin for 30 days)."),
]

_HELP_ADVANCED_TOOLS = [
    ("REVIEWSUBMISSIONS", "<b>Review /send submissions</b>",
     "Approve or reject characters submitted via /send, and set their final rarity and event."),
    ("SETSELLPRICE", _cmd("/setsellprice [rarity name] [amount]"),
     "Set how much currency players get for selling a card of that rarity to the bot with /sellbot "
     '(use "Unranked" for characters with no rarity). No args shows current prices.'),
    ("SETPREMIUM", _cmd("/setpremium", "/setpremium [days]"),
     "Reply to someone to grant premium - permanent if no days given, or for that many days."),
    ("REMOVEPREMIUM", _cmd("/removepremium"), "Reply to someone to revoke their premium."),
    ("BIN", _cmd("/bin"),
     "Browse characters, rarities, and events deleted in the last 30 days, and restore them "
     "one by one or all at once."),
]

# Owner only.
_HELP_OWNER_ONLY = [
    (_cmd("/admin"),
     "Browse admins with buttons and give/take individual command permissions (like /bin or /ban) one at a time."),
    (_cmd("/addadmin [artist|manager|marzieh] [ID]"),
     "Shortcut: grants a whole bundle of permissions matching the old Artist/Manager/Marzieh tiers at once "
     "(use /admin instead for one-by-one control)."),
    (_cmd("/removeadmin [ID or \"all\"]"),
     "Revoke a user's admin access, or every secondary admin at once."),
    (_cmd("/stats"), "See how many users and groups the bot has, with buttons to list them."),
    (_cmd("/artiststats"),
     "List everyone who has added a card (via /addcharacter or an approved /send), with buttons "
     "for cards-per-artist and rarities-per-artist breakdowns."),
    (_cmd("/character"),
     "Shows the total number of cards in the bot, with buttons to break that down by rarity, "
     "character, series, or event."),
    (_cmd("/setpersonality ID | Personality | Age(optional) | Gender(optional)"),
     "Set the HIDDEN personality that drives this character's replies in the Mini App's "
     "Chat tab (players never see this text)."),
    (_cmd("/setforcejoin"),
     "Run it inside a group to make membership in that group required before anyone can use the bot."),
    (_cmd("/setreviewgroup", "/setreviewgroup off"),
     "Run it inside a group to also post /send submissions there for review; with off they go to your DM only."),
    (_cmd("/filedown"), "Download a .tar.gz backup of the whole data volume."),
    (_cmd("/fileup"), "Restore a /filedown backup onto the volume (overwrites everything currently there)."),
]


def _help_blocks(entries):
    return [f"{header}\n{description}" for header, description in entries]


def _help_section(title: str, *entry_lists) -> list:
    blocks = [f"— — — <b>{title}</b> — — —"]
    for entries in entry_lists:
        blocks += _help_blocks(entries)
    return blocks


def _filter_tagged(entries, held: set) -> list:
    """Strips the permission tag, keeping only entries this specific admin holds."""
    return [(cmd, desc) for perm, cmd, desc in entries if perm in held]


def build_help_text(user_id: int) -> str:
    blocks = ["📖 <b>Commands</b>"] + _help_blocks(_HELP_PLAYER)

    if is_admin(user_id):
        blocks += _help_section(
            "Owner only", _HELP_CARD_EDITING_OWNER, _HELP_MODERATION, _HELP_NEW, _HELP_EVENTS_OWNER,
            _HELP_ADVANCED_TOOLS, _HELP_OWNER_ONLY,
        )
    else:
        held = set(db.get_permissions(user_id))
        if held:
            sections = [
                ("Card editing", _filter_tagged(_HELP_CARD_EDITING, held)),
                ("Posts & events", _filter_tagged(_HELP_NEW + _HELP_EVENTS, held)),
                ("Moderation", _filter_tagged(_HELP_MODERATION, held)),
                ("Other tools", _filter_tagged(_HELP_ADVANCED_TOOLS, held)),
            ]
            for title, entries in sections:
                if entries:
                    blocks += _help_section(title, entries)

    return "\n\n".join(blocks)


def _chunk_help_text(text: str, limit: int = 3500):
    """Splits help text on the blank lines between entries, so a chunk
    boundary never lands inside an entry (or an HTML tag). Telegram
    rejects any single message over 4096 chars - the owner's full help
    text alone was past that, which silently broke /help for them."""
    entries = text.split("\n\n")
    chunks = []
    current = ""
    for entry in entries:
        candidate = f"{current}\n\n{entry}" if current else entry
        if len(candidate) > limit and current:
            chunks.append(current)
            current = entry
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _deliver_help(user_id: int, chat, context: ContextTypes.DEFAULT_TYPE):
    """Sends the /help output. A secondary admin's section lists sensitive
    admin commands, so if anyone holding at least one admin permission asks
    for /help anywhere other than their own DM, we tease them in that chat
    instead of showing it there, and quietly send the real thing to DM."""
    if not is_admin(user_id) and db.get_permissions(user_id) and chat.type != "private":
        try:
            for chunk in _chunk_help_text(build_help_text(user_id)):
                await context.bot.send_message(chat_id=user_id, text=chunk, parse_mode=ParseMode.HTML)
            await context.bot.send_message(
                chat_id=chat.id,
                text="Nope nope nope — no one else should see what's in here 👀 Sent it to your PV.",
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ I couldn't DM you - start a private chat with me first, then try /help again.",
            )
        return

    for chunk in _chunk_help_text(build_help_text(user_id)):
        await context.bot.send_message(chat_id=chat.id, text=chunk, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _deliver_help(update.effective_user.id, update.effective_chat, context)


async def help_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _deliver_help(query.from_user.id, query.message.chat, context)


# ---------------- Main ----------------

def run_api_server():
    """
    Runs the Mini App's HTTP API (api_server.py) in a background thread
    inside this same process, so it shares this exact SQLite file with
    zero extra setup - no second Railway service, no shared volume.
    Imported lazily here (not at module load) so a missing fastapi/
    uvicorn install only breaks the Mini App API, never the bot itself.
    """
    try:
        import uvicorn
        from api_server import api_app
    except ImportError:
        logger.warning("fastapi/uvicorn not installed - Mini App API will not start. "
                        "Run: pip install fastapi uvicorn")
        return

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(api_app, host="0.0.0.0", port=port, log_level="info")


# ---------------- Special replies for one person ----------------
# Fixed trigger -> reply pairs for a single chosen user (plus the owner, so the
# owner can test them). The message must be EXACTLY the trigger (nothing else in it).

SPECIAL_REPLY_USER_ID = 8235775970

# Persian trigger words -> reply. Trailing "?", "!", "." and "،" are ignored.
_SPECIAL_WORD_REPLIES = {
    "سلام": "Hello, my lady 🌸💖",
    "چی": "What happened, beautiful? 🌷✨",
    "چشم": "A god never says \"as you wish\"! 😌✨",
    "جونم": "Kind soul (: 🌼💕",
    "جانم": "Kind soul (: 🌼💕",
    "باشه": "Well done! This is how a god accepts someone's request 😌✨",
    "باش": "Well done! This is how a god accepts someone's request 😌✨",
}

_SPECIAL_HAPPY_RE = re.compile(r"^(?::\){1,10}|\({1,10}:)$")  # :) (: - up to 10 brackets
_SPECIAL_SAD_RE = re.compile(r"^(?::\({1,10}|\){1,10}:)$")    # :( ): - up to 10 brackets
_SPECIAL_DOTS_RE = re.compile(r"^(?:\.{3,}|…+)$")              # ...


def _special_reply_for(text: str):
    """The reply for this exact message text, or None."""
    t = text.strip().replace("\u200c", "").replace("ي", "ی").replace("ك", "ک")
    if not t:
        return None
    if _SPECIAL_HAPPY_RE.match(t):
        return "What a sweet girl! 🥰🌸"
    if _SPECIAL_SAD_RE.match(t):
        return "What upset you, little one? 🥺🤍"
    if _SPECIAL_DOTS_RE.match(t):
        return "Yeah, I agree 😌🤝"
    return _SPECIAL_WORD_REPLIES.get(t.rstrip("?؟!.،, ").strip())


async def special_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers the special person's (and the owner's) fixed trigger messages.
    Runs in its own handler group, so it never affects the spawn counter or any other flow."""
    message = update.message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return
    if user.id not in (SPECIAL_REPLY_USER_ID, config.ADMIN_ID):
        return
    reply = _special_reply_for(message.text)
    if reply:
        await message.reply_text(reply)


def main():
    db.init_db()

    threading.Thread(target=run_api_server, daemon=True).start()
    threading.Thread(target=economy.run_price_update_loop, daemon=True).start()
    threading.Thread(target=memories.run_nightly_engagement_loop, daemon=True).start()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()
    _install_personal_button_guard()
    db.prune_personalized_buttons()

    # IMPORTANT (python-telegram-bot): inside ONE group only the FIRST matching
    # handler runs - the rest of that group is skipped. A TypeHandler(Update)
    # matches everything, so every early "gate" handler needs its OWN group or
    # the ones registered after it never fire. Groups run in ascending order.
    app.add_handler(TypeHandler(Update, _ignore_edited_messages), group=-8)
    app.add_handler(TypeHandler(Update, _bind_update_context), group=-7)
    app.add_handler(TypeHandler(Update, _block_banned_users), group=-6)

    # Count eligible group messages before normal group=0 handlers.
    # Banned users have already been stopped at group=-6.
    # on_group_message handles spam/mute before touching the spawn counter.
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS
        & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL)
        & ~filters.COMMAND,
        on_group_message,
    ), group=-5)

    # Personal buttons: strangers get "belongs to another user" before anything else
    # (including the force-join prompt) can react to their tap.
    app.add_handler(TypeHandler(Update, _block_unowned_callbacks), group=-4)
    app.add_handler(TypeHandler(Update, _block_non_members), group=-3)
    app.add_handler(TypeHandler(Update, _capture_user_info), group=-2)

    app.add_handler(CommandHandler("setforcejoin", set_force_join_command), group=0)
    app.add_handler(CommandHandler("setreviewgroup", set_review_group_command), group=0)
    app.add_handler(CallbackQueryHandler(force_join_check_callback, pattern=r"^forcejoin:check:\d+$"), group=0)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(help_button_callback, pattern=r"^show_help$"))
    app.add_handler(CommandHandler("get", get_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("gallery", allcharacters_command))
    app.add_handler(CommandHandler("rarities", rarities_command))
    app.add_handler(CommandHandler("prices", prices_command))
    app.add_handler(CallbackQueryHandler(prices_callback, pattern=r"^prices:"))
    app.add_handler(CommandHandler("memories", memories_command))
    app.add_handler(CommandHandler("birthday", birthday_command))
    app.add_handler(CallbackQueryHandler(birthday_callback, pattern=r"^birthday:"))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("sort", sort_command))
    app.add_handler(CallbackQueryHandler(sort_menu_callback, pattern=r"^sortmenu:"))
    app.add_handler(CallbackQueryHandler(sort_value_callback, pattern=r"^sortval:"))
    app.add_handler(CallbackQueryHandler(sort_remove_callback, pattern=r"^sortdel:\d+$"))
    app.add_handler(CallbackQueryHandler(constellation_page_callback, pattern=r"^conspage:"))
    app.add_handler(CommandHandler("gift", gift_command))
    app.add_handler(CallbackQueryHandler(gift_callback, pattern=r"^gift:"))
    app.add_handler(CommandHandler("addcharacter", add_character_command))
    # CommandHandler only looks at message.text, not photo/video captions -
    # these extra handlers catch the real case: a photo OR video sent WITH a
    # caption starting with /addcharacter (or /send). _extract_media() already
    # supports both media types; these registrations are what actually route
    # video messages to it.
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO) & filters.CaptionRegex(r"(?i)^/addcharacter"),
        add_character_command,
    ))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO) & filters.CaptionRegex(r"(?i)^/send"),
        send_character_command,
    ))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CallbackQueryHandler(new_callback, pattern=r"^new:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_new_input), group=0)
    app.add_handler(CommandHandler("send", send_character_command))
    app.add_handler(CallbackQueryHandler(add_flow_callback, pattern=r"^addflow:"))
    app.add_handler(CallbackQueryHandler(submission_callback, pattern=r"^submit:"))
    app.add_handler(CommandHandler("editcharacter", edit_character_command))
    app.add_handler(CallbackQueryHandler(edit_character_field_callback, pattern=r"^editchar:field:"))
    app.add_handler(CallbackQueryHandler(edit_character_apply_callback, pattern=r"^editchar:(setrarity|setevent):"))
    app.add_handler(CommandHandler("setpersonality", set_personality_command))
    app.add_handler(CommandHandler("editrarity", edit_rarity_command))
    app.add_handler(CommandHandler("addrarity", add_rarity_command))
    app.add_handler(CommandHandler("removecharacter", remove_character_command))
    app.add_handler(CommandHandler("spawnstatus", spawn_status_command))
    app.add_handler(CommandHandler("removerarity", remove_rarity_command))
    app.add_handler(CommandHandler("forcespawn", force_spawn_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^adminperm:"))
    app.add_handler(CommandHandler("filedown", file_down_command))
    app.add_handler(CommandHandler("fileup", file_up_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file_restore_upload))
    app.add_handler(CommandHandler("lockspawn", lock_spawn_command))
    app.add_handler(CommandHandler("unlockspawn", unlock_spawn_command))
    app.add_handler(CommandHandler("addevent", add_event_command))
    app.add_handler(CommandHandler("removeevent", remove_event_command))
    app.add_handler(CommandHandler("bin", bin_command))
    app.add_handler(CallbackQueryHandler(bin_callback, pattern=r"^bin:"))
    app.add_handler(CommandHandler("constellation", constellation_command))
    app.add_handler(CommandHandler("fav", fav_command))
    app.add_handler(CallbackQueryHandler(fav_callback, pattern=r"^fav:(ok|cancel):\d+"))
    app.add_handler(CallbackQueryHandler(sell_confirm_callback, pattern=r"^sell:(ok|cancel):\d+"))
    app.add_handler(CallbackQueryHandler(pay_callback, pattern=r"^pay:(ok|cancel):\d+"))
    app.add_handler(CommandHandler("miniapp", miniapp_command))
    app.add_handler(CommandHandler("dart", dart_command))
    app.add_handler(CommandHandler("inv", inventory_currency_command))
    app.add_handler(CommandHandler("vypay", pay_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern=r"^stats:"))
    app.add_handler(CommandHandler("give", give_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CallbackQueryHandler(top_callback, pattern=r"^top:"))
    app.add_handler(CommandHandler("player", player_command))
    app.add_handler(CallbackQueryHandler(player_menu_callback, pattern=r"^padmin:"))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("sick", sick_command))
    app.add_handler(CommandHandler("artiststats", artist_stats_command))
    app.add_handler(CallbackQueryHandler(artist_stats_callback, pattern=r"^artiststats:"))
    app.add_handler(CommandHandler("character", character_command))
    app.add_handler(CallbackQueryHandler(character_stats_callback, pattern=r"^charstats:"))
    app.add_handler(CommandHandler("setpremium", set_premium_command))
    app.add_handler(CommandHandler("removepremium", remove_premium_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("trade", trade_command))
    app.add_handler(CallbackQueryHandler(trade_callback, pattern=r"^trade:"))
    app.add_handler(CommandHandler("sell", sell_command))
    app.add_handler(CommandHandler("cancelsell", cancelsell_command))
    app.add_handler(CommandHandler("sellbot", sellbot_command))
    app.add_handler(CommandHandler("setsellprice", set_sell_price_command))
    app.add_handler(CallbackQueryHandler(cancelsell_button_callback, pattern=r"^cancelsell:"))
    app.add_handler(CallbackQueryHandler(sellbot_callback, pattern=r"^sellbot:"))
    app.add_handler(CommandHandler("market", market_command))
    app.add_handler(CallbackQueryHandler(market_page_callback, pattern=r"^marketpage:"))
    app.add_handler(InlineQueryHandler(constellation_inline_query))

    # separate group: only acts when a /sort prompt is waiting on this user,
    # otherwise does nothing - runs independently of the group message counter
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_sort_input), group=1)

    # its own group (not group=1) so it isn't shadowed by capture_sort_input -
    # only acts when this user has an active Fighter add-flow awaiting a number
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_fighter_stat_input), group=2)

    # its own group - only acts when the owner has an active /player prompt
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_player_input), group=3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_edit_character_input), group=4)

    # its own group - only acts when the owner has an active /admin "add new admin" prompt
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_admin_new_id), group=5)

    # its own group - only answers the special person's (and the owner's) fixed trigger messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, special_reply_handler), group=6)

    logger.info("Bot starting...")
    KNOWN_COMMANDS.update(
        command
        for handlers in app.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    )

    app.run_polling()


if __name__ == "__main__":
    main()
