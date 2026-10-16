"""
Talks to whatever OpenAI Chat Completions-compatible service is
configured (OpenRouter, OpenAI, Groq, DeepSeek, a local Ollama, ...) -
see the AI_* settings in config.py. Adapted from a standalone Telegram
AI bot project; only complete_chat() (non-streaming) is used by the
Chat tab today since api_server.py answers with one full HTTP response
per message rather than a live stream - stream_chat_completion() is
kept as-is in case that changes later (see the Chat tab README notes
on adding real token-by-token streaming).

Deliberately built on plain httpx instead of an official SDK: no
pydantic v2 / Rust build step, so it stays trivial to run anywhere the
rest of this bot already runs (Railway, Termux, etc).
"""

import asyncio
import json
import logging
from typing import AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Base class for every AI-service-related error below."""


class AIConnectionError(AIClientError):
    """Couldn't reach the AI service at all (e.g. no internet)."""


class AITimeoutError(AIClientError):
    """The AI service didn't respond within the configured timeout."""


class AIAuthError(AIClientError):
    """Invalid API key or access denied (HTTP 401/403)."""


class AIRateLimitError(AIClientError):
    """Too many requests to the AI service (HTTP 429)."""


class AIServerError(AIClientError):
    """The AI provider had an internal error (HTTP 5xx)."""


class AIModelUnavailableError(AIClientError):
    """The requested model wasn't found / is no longer available (HTTP 404).

    Kept separate from the generic AIClientError because it's the only
    error complete_chat() and stream_chat_completion() automatically
    retry with config.AI_FALLBACK_MODEL - free OpenRouter models get
    retired without notice, and there's no user to ask to fix .env."""


class AIClient:
    """Minimal client for one OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, config):
        # `config` just needs these attributes - the config module
        # itself is passed in from api_server.py/chat_ai.py.
        self.config = config

    async def stream_chat_completion(
        self,
        messages: List[Dict[str, str]],
        stop_event: Optional[asyncio.Event] = None,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Yields the model's reply piece by piece. Not currently used by
        the Chat tab (see module docstring) - kept for a future
        streaming upgrade. Falls back to AI_FALLBACK_MODEL once on a
        404, same as complete_chat() below."""
        primary_model = model or self.config.AI_MODEL
        try:
            async for piece in self._stream_once(messages, stop_event, primary_model):
                yield piece
            return
        except AIModelUnavailableError as e:
            fallback_model = self.config.AI_FALLBACK_MODEL
            if not fallback_model or fallback_model == primary_model:
                raise
            logger.warning("Model %r unavailable (%s) - retrying with %r", primary_model, e, fallback_model)

        async for piece in self._stream_once(messages, stop_event, fallback_model):
            yield piece

    async def _stream_once(self, messages, stop_event, model) -> AsyncGenerator[str, None]:
        url = f"{self.config.AI_API_BASE_URL}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.AI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": self.config.AI_TEMPERATURE, "stream": True}
        timeout = httpx.Timeout(self.config.AI_REQUEST_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        self._raise_for_status(response.status_code, body, model)

                    async for line in response.aiter_lines():
                        if stop_event is not None and stop_event.is_set():
                            return
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data_str)
                            piece = chunk["choices"][0].get("delta", {}).get("content")
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
                        if piece:
                            yield piece
        except httpx.ConnectError as e:
            raise AIConnectionError("Couldn't connect to the AI service.") from e
        except httpx.TimeoutException as e:
            raise AITimeoutError(f"AI service didn't respond within {self.config.AI_REQUEST_TIMEOUT}s.") from e
        except httpx.HTTPError as e:
            raise AIConnectionError(f"Network error talking to the AI service: {e}") from e

    async def complete_chat(self, messages: List[Dict[str, str]], model: Optional[str] = None) -> str:
        """One non-streaming call - what the Chat tab actually uses.
        Same automatic fallback-model retry on a 404 as above."""
        primary_model = model or self.config.AI_MODEL
        try:
            return await self._complete_once(messages, primary_model)
        except AIModelUnavailableError as e:
            fallback_model = self.config.AI_FALLBACK_MODEL
            if not fallback_model or fallback_model == primary_model:
                raise
            logger.warning("Model %r unavailable (%s) - retrying with %r", primary_model, e, fallback_model)
            return await self._complete_once(messages, fallback_model)

    async def _complete_once(self, messages: List[Dict[str, str]], model: str) -> str:
        url = f"{self.config.AI_API_BASE_URL}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.AI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": self.config.AI_TEMPERATURE, "stream": False}
        timeout = httpx.Timeout(self.config.AI_REQUEST_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    self._raise_for_status(response.status_code, response.content, model)
                data = response.json()
                return data["choices"][0]["message"]["content"] or ""
        except httpx.ConnectError as e:
            raise AIConnectionError("Couldn't connect to the AI service.") from e
        except httpx.TimeoutException as e:
            raise AITimeoutError(f"AI service didn't respond within {self.config.AI_REQUEST_TIMEOUT}s.") from e
        except httpx.HTTPError as e:
            raise AIConnectionError(f"Network error talking to the AI service: {e}") from e

    def _raise_for_status(self, status_code: int, body: bytes, model: Optional[str] = None) -> None:
        try:
            parsed = json.loads(body)
            message = (parsed.get("error") or {}).get("message") or str(parsed)
        except Exception:
            message = body.decode(errors="ignore")[:300] or f"HTTP {status_code}"

        if status_code in (401, 403):
            raise AIAuthError(f"Invalid API key or access denied ({message})")
        if status_code == 429:
            raise AIRateLimitError(f"Rate limited by the AI service ({message})")
        if status_code == 404:
            model_part = f"{model!r} " if model else ""
            raise AIModelUnavailableError(f"Model {model_part}not found or no longer available ({message})")
        if 500 <= status_code < 600:
            raise AIServerError(f"AI provider server error ({message})")
        raise AIClientError(f"Unexpected error - HTTP {status_code} ({message})")
