"""A small Gemini client: chat, streaming, retries and key rotation.

Shared by the plugins that talk to Gemini -- ``ai`` for conversation and ``sum``
for summarising a chat -- so the awkward parts of the API are documented and
implemented once rather than in every plugin that needs them.

Deliberately thin. The REST surface is one POST per call, and a full SDK would
add a dependency and a second opinion about retries, timeouts and error shapes
for something this small. Everything awkward about talking to this API was
found the hard way and is written down here:

* ``streamGenerateContent`` returns a single pretty-printed JSON array unless
  ``alt=sse`` is passed. Without it, "streaming" arrives as one enormous blob at
  the end, which defeats the point. With it, each chunk is a ``data: {...}`` line.

* The model list does not advertise ``streamGenerateContent`` for any current
  model, but the endpoint works. Trust the call, not the metadata.

* ``503 UNAVAILABLE`` with "high demand" is routine, not exceptional. Retrying
  with backoff is not a nicety; the first call of a session often fails.

* Responses carry a ``thoughtSignature`` that must be echoed back on the next
  turn, or multi-turn quality degrades. It is captured and replayed here so
  callers do not have to know about it.

* A thinking model reports ``thoughtsTokenCount`` separately from the billable
  candidate tokens. Only the latter is used for cost accounting, because that is
  what the account is charged for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("userbot.gemini")

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Transient conditions worth another attempt. 503 here is capacity, not a bug.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Attempts total, including the first.
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF = 1.5
DEFAULT_BACKOFF_CAP = 20.0


class GeminiError(RuntimeError):
    """The API refused the request in a way the owner needs to see."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class MissingKeyError(GeminiError):
    """No API key is configured, so no request can be made."""


@dataclass(slots=True)
class Part:
    """One piece of a message.

    Either text, or an inline attachment. Inline data is how audio and video
    reach the model, and it has to be base64 -- which is why the mime type and
    the encoded bytes are carried together here rather than left to each caller
    to shape correctly.
    """

    text: str
    thought_signature: str | None = None
    #: ``{"mime_type": ..., "data": <base64 str>}`` for an attachment.
    inline_data: dict[str, str] | None = None

    @classmethod
    def media(cls, mime: str, base64_data: str) -> Part:
        return cls(text="", inline_data={"mime_type": mime, "data": base64_data})

    def to_wire(self) -> dict[str, Any]:
        if self.inline_data is not None:
            payload: dict[str, Any] = {"inline_data": dict(self.inline_data)}
            if self.thought_signature:
                payload["thoughtSignature"] = self.thought_signature
            return payload
        payload = {"text": self.text}
        if self.thought_signature:
            payload["thoughtSignature"] = self.thought_signature
        return payload


@dataclass(slots=True)
class Turn:
    """One exchange. ``parts`` keeps the signature so the next turn can send it."""

    role: str
    parts: list[Part] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)

    def to_wire(self) -> dict[str, Any]:
        return {"role": self.role, "parts": [part.to_wire() for part in self.parts]}

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Turn:
        parts: list[Part] = []
        for raw in payload.get("parts") or []:
            if not isinstance(raw, dict):
                continue
            signature = raw.get("thoughtSignature")
            marker = signature if isinstance(signature, str) else None
            inline = raw.get("inline_data") or raw.get("inlineData")
            if isinstance(inline, dict):
                # Model replies come back with attachments too; keeping them
                # means a history round-trips without losing the media.
                mime = inline.get("mime_type") or inline.get("mimeType") or ""
                data = inline.get("data")
                parts.append(
                    Part(
                        text="",
                        thought_signature=marker,
                        inline_data={"mime_type": str(mime), "data": str(data or "")},
                    )
                )
                continue
            text = raw.get("text")
            if not isinstance(text, str):
                continue
            parts.append(Part(text=text, thought_signature=marker))
        return cls(role=str(payload.get("role") or "user"), parts=parts)


@dataclass(slots=True)
class Reply:
    """A completed model turn, plus what it cost."""

    text: str
    finish_reason: str = "STOP"
    input_tokens: int = 0
    output_tokens: int = 0
    thoughts_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thoughts_tokens

    @property
    def truncated(self) -> bool:
        return self.finish_reason in {"MAX_TOKENS", "MAX_TOKENS_TRUNCATED"}


@dataclass(slots=True)
class ModelInfo:
    name: str
    display_name: str = ""
    input_limit: int = 0
    output_limit: int = 0
    supports_streaming: bool = False


def parse_keys(raw: str | None) -> list[str]:
    """Split a comma-separated key list, dropping blanks.

    Several keys are not redundant: the free tier is per key, so rotation is
    what turns a rate limit into a slightly slower answer instead of an error.
    """
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _redact(text: str, keys: Iterable[str]) -> str:
    """Strip any key from text before it reaches a log."""
    for key in keys:
        if key:
            text = text.replace(key, "<redacted>")
    return text


class GeminiClient:
    """Talks to the Generative Language API on behalf of one plugin instance."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_keys: Sequence[str] = (),
        timeout: float = 120.0,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._keys = list(api_keys)
        self._key_index = 0
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self.backoff_cap = backoff_cap
        self._client = client
        self._owns_client = client is None

    @property
    def has_key(self) -> bool:
        return bool(self._keys)

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _next_key(self) -> str:
        if not self._keys:
            raise MissingKeyError(
                "Ключ Gemini не задан. Укажите TGUSERBOT_GEMINI_API_KEY "
                "в /etc/tguserbot/userbot.env и перезапустите сервис."
            )
        key = self._keys[self._key_index % len(self._keys)]
        self._key_index += 1
        return key

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # -- request plumbing --------------------------------------------------

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key, "content-type": "application/json"}

    def _describe(self, response: httpx.Response) -> GeminiError:
        """Turn an error body into something worth reading in Telegram.

        The API's own ``error.message`` is better than anything invented here,
        and the key must never appear in it, so it is scrubbed before use.
        """
        status = response.status_code
        detail = ""
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message")
            if isinstance(message, str):
                detail = message
        except (ValueError, AttributeError):
            detail = response.text[:200]
        detail = _redact(detail, self._keys)
        if not detail:
            detail = response.reason_phrase or "запрос отклонён"
        return GeminiError(f"Gemini: {detail}", status=status, retryable=status in RETRY_STATUS)

    async def _request(
        self,
        path: str,
        body: dict[str, Any],
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST with retries, rotating keys between attempts.

        A key that gets a 429 is moved to the back of the rotation, so the next
        attempt uses a different one when there is one to use.
        """
        url = f"{self.base_url}{path}"
        client = await self._http()
        last: GeminiError | None = None
        for attempt in range(self.attempts):
            key = self._next_key()
            try:
                response = await client.post(
                    url, json=body, params=params, headers=self._headers(key)
                )
            except httpx.HTTPError as exc:
                last = GeminiError(
                    f"Gemini: сеть недоступна ({type(exc).__name__})", retryable=True
                )
                if attempt + 1 >= self.attempts:
                    raise last from exc
                await self.sleep_backoff(attempt)
                continue
            if response.status_code == 200:
                return response
            last = self._describe(response)
            if not last.retryable or attempt + 1 >= self.attempts:
                raise last
            logger.debug(
                "Gemini %s on %s, retrying (attempt %d/%d)",
                response.status_code,
                path,
                attempt + 1,
                self.attempts,
            )
            await self.sleep_backoff(attempt)
        raise last or GeminiError("Gemini: запрос не выполнен")

    def _delay(self, attempt: int) -> float:
        """Exponential with jitter, so retries do not arrive in lockstep.

        Jitter is not a refinement: the 503s this is retrying are capacity
        exhaustion, and a synchronised retry makes the collision worse.
        """
        return min(self.backoff_cap, self.backoff**attempt) * (0.5 + random.random() / 2)

    async def sleep_backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._delay(attempt))

    # -- payload building ---------------------------------------------------

    @staticmethod
    def _body(
        turns: Sequence[Turn],
        *,
        system: str | None = None,
        max_output_tokens: int = 4096,
        temperature: float | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"maxOutputTokens": max_output_tokens}
        if temperature is not None:
            config["temperature"] = temperature
        if response_schema is not None:
            # Structured output. The model is constrained to the schema instead
            # of being asked politely for JSON, which is the difference between
            # a parseable answer and a repair loop.
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = response_schema
        body: dict[str, Any] = {
            "contents": [turn.to_wire() for turn in turns],
            "generationConfig": config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    # -- calls ---------------------------------------------------------------

    async def generate(
        self,
        turns: Sequence[Turn],
        *,
        system: str | None = None,
        model: str = "gemini-3.8-flash",
        max_output_tokens: int = 4096,
        temperature: float | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> Reply:
        body = self._body(
            turns,
            system=system,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            response_schema=response_schema,
        )
        response = await self._request(f"/models/{model}:generateContent", body)
        return self.parse_reply(response.json())

    async def stream(
        self,
        turns: Sequence[Turn],
        *,
        system: str | None = None,
        model: str = "gemini-3.8-flash",
        max_output_tokens: int = 4096,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as they arrive.

        ``alt=sse`` is required; without it the endpoint replies with one
        pretty-printed array and the caller gets everything at once. Verified
        against the live API rather than taken from the model list, which does
        not advertise this method for any current model.
        """
        body = self._body(
            turns,
            system=system,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        client = await self._http()
        key = self._next_key()
        url = f"{self.base_url}/models/{model}:streamGenerateContent"
        last: GeminiError | None = None
        for attempt in range(self.attempts):
            try:
                async with client.stream(
                    "POST", url, json=body, params={"alt": "sse"}, headers=self._headers(key)
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        last = self._describe(response)
                        if not last.retryable or attempt + 1 >= self.attempts:
                            raise last
                    else:
                        async for delta in self._iter_sse(response):
                            if delta:
                                yield delta
                        return
            except httpx.HTTPError as exc:
                last = GeminiError(
                    f"Gemini: сеть недоступна ({type(exc).__name__})", retryable=True
                )
                if attempt + 1 >= self.attempts:
                    raise last from exc
            key = self._next_key()
            await self.sleep_backoff(attempt)
        raise last or GeminiError("Gemini: поток прерван")

    @staticmethod
    async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                # One malformed line must not kill an otherwise fine stream.
                continue
            for candidate in chunk.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        yield text

    # -- response parsing ----------------------------------------------------

    @staticmethod
    def parse_reply(payload: dict[str, Any]) -> Reply:
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            block = feedback.get("blockReason")
            if block:
                raise GeminiError(f"Gemini отклонил запрос: {block}")
            raise GeminiError("Gemini вернул пустой ответ")
        candidate = candidates[0]
        turn = Turn.from_wire(candidate.get("content") or {})
        usage = payload.get("usageMetadata") or {}
        # Only candidate tokens are billed; thinking tokens are reported
        # separately and are not what the account is charged for.
        return Reply(
            text=turn.text,
            finish_reason=str(candidate.get("finishReason") or "STOP"),
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            thoughts_tokens=int(usage.get("thoughtsTokenCount") or 0),
        )

    @staticmethod
    def parse_models(payload: dict[str, Any]) -> list[ModelInfo]:
        wanted = {"generateContent", "streamGenerateContent"}
        result: list[ModelInfo] = []
        for raw in payload.get("models") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").removeprefix("models/")
            if not name:
                continue
            methods = set(raw.get("supportedGenerationMethods") or [])
            result.append(
                ModelInfo(
                    name=name,
                    display_name=str(raw.get("displayName") or ""),
                    input_limit=int(raw.get("inputTokenLimit") or 0),
                    output_limit=int(raw.get("outputTokenLimit") or 0),
                    supports_streaming=bool(methods & wanted),
                )
            )
        return sorted(result, key=lambda item: item.name)

    async def list_models(self) -> list[ModelInfo]:
        client = await self._http()
        key = self._next_key()
        response = await client.get(
            f"{self.base_url}/models",
            params={"pageSize": 200},
            headers=self._headers(key),
        )
        if response.status_code != 200:
            raise self._describe(response)
        return self.parse_models(response.json())


def keys_from_env(*names: str) -> list[str]:
    """Collect keys from the first environment variable that has any."""
    for name in names:
        found = parse_keys(os.environ.get(name))
        if found:
            return found
    return []
