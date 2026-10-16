"""
Character reply generation for the Mini App's Chat tab.

api_server.py only ever calls generate_reply(character, history,
user_message) and stores whatever string it returns - this is the one
module that knows HOW that string gets made, so swapping providers,
tuning the persona rules, or changing the language policy only ever
touches this file.

Two things happen before any API call, entirely deterministic and
free:
  1. Language gate - if the player's message contains non-English
     script (Persian, Arabic, Cyrillic, CJK, etc.), we skip the AI
     entirely and send back a fixed "English only" line. This is
     cheaper AND more reliable than asking the model to enforce it -
     an instruction in the system prompt is a strong nudge, not a
     guarantee, especially on weaker/free models.
  2. The same script check runs again on the MODEL's own reply, as a
     safety net, in case it drifts into another language anyway.

Everything else - persona, tone, emoji, age/gender - is steered
through the system prompt built in _build_system_prompt().
"""

import logging
import re

import config
from ai_client import AIClient, AIClientError

logger = logging.getLogger(__name__)

_client = AIClient(config)

# Matches Arabic/Persian, Hebrew, Cyrillic, Greek, Devanagari, and
# CJK/Japanese/Korean script blocks. Deliberately script-based, not "any
# non-ASCII" - that would also flag emoji, accented letters, and curly
# quotes, all of which we want to allow.
_NON_ENGLISH_SCRIPT = re.compile(
    "["
    "\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"  # Arabic / Persian
    "\u0590-\u05FF"  # Hebrew
    "\u0400-\u04FF"  # Cyrillic
    "\u0370-\u03FF"  # Greek
    "\u0900-\u097F"  # Devanagari
    "\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF"  # CJK / Japanese / Korean
    "]"
)

_ENGLISH_ONLY_LINE = "I only speak English, sweetheart 😊 Try me in English!"

# Shown when the AI service itself is unreachable/erroring - still
# in-voice enough not to break immersion, and never leaks the raw
# exception to the player.
_SERVICE_DOWN_LINE = "Sorry, I'm having trouble finding the words right now... give me a moment and try again? 💭"


def _contains_non_english(text: str) -> bool:
    return bool(_NON_ENGLISH_SCRIPT.search(text))


def _build_system_prompt(character: dict) -> str:
    name = character.get("name") or "this character"
    series = character.get("series")
    age = character.get("chat_age")
    gender = character.get("chat_gender")
    persona = character.get("chat_persona")

    lines = [
        f"You are role-playing as {name}"
        + (f" from {series}" if series else "")
        + ", chatting one-on-one with a player inside a mobile card-collecting game.",
    ]
    if age:
        lines.append(f"Your age: {age}.")
    if gender:
        lines.append(f"Your gender: {gender}.")
    if persona:
        lines.append(f"Your personality: {persona}")
    else:
        lines.append(
            "No specific personality has been written for you yet, so default to a warm, "
            "friendly, upbeat personality."
        )

    lines += [
        "Let your age and gender genuinely shape your tone and word choice, and stay fully "
        "in character at all times - never say you are an AI or a language model.",
        "Every character has their OWN distinct voice - do not default to a generic friendly "
        "chatbot tone unless that's genuinely what this personality calls for. If the "
        "personality above is short-tempered, cold, shy, sarcastic, dramatic, or anything "
        "other than sweet, actually sound that way - an annoyed character should sound "
        "genuinely annoyed, not politely pretend to be.",
        "Use emojis often to add warmth and expression, in a way that fits your personality.",
        "Keep replies short and conversational, like real chat messages, not essays.",
        "Respond ONLY in English, no matter what language the player writes in.",
    ]
    return "\n".join(lines)


def _to_openai_messages(character: dict, history: list) -> list:
    messages = [{"role": "system", "content": _build_system_prompt(character)}]
    recent = history[-config.AI_MAX_HISTORY_MESSAGES:]
    for row in recent:
        role = "assistant" if row.get("sender") == "character" else "user"
        messages.append({"role": role, "content": row.get("content", "")})
    return messages


async def generate_reply(character: dict, history: list, user_message: str) -> str:
    """Returns the character's reply text for `user_message`.

    `character` is a characters-table row (dict) - name/series plus the
    hidden chat_persona/chat_age/chat_gender fields set via
    /setpersonality. `history` is this player+character's chat_messages
    rows oldest-first, already including `user_message` as the last
    entry (see api_server.py's chat_send)."""
    if not config.AI_API_KEY:
        logger.warning("Chat tab message received but AI_API_KEY is not configured.")
        return _SERVICE_DOWN_LINE

    if _contains_non_english(user_message):
        return _ENGLISH_ONLY_LINE

    messages = _to_openai_messages(character, history)

    try:
        reply = await _client.complete_chat(messages)
    except AIClientError:
        logger.exception("Chat tab AI call failed for character_id=%s", character.get("id"))
        return _SERVICE_DOWN_LINE

    reply = (reply or "").strip()
    if not reply:
        return _SERVICE_DOWN_LINE
    if _contains_non_english(reply):
        # The model drifted into another language despite the system
        # prompt - the deterministic line is always safe to show instead.
        return _ENGLISH_ONLY_LINE

    return reply
