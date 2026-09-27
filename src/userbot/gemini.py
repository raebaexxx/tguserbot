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
import math
import os
import random
import re
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
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


class QuotaExhausted(GeminiError):
    """The model is unavailable right now, and another one has to answer.

    Separate from a rate limit worth waiting out, which shares the 429 and the
    ``RESOURCE_EXHAUSTED`` status. Gemini's own message is the only thing that
    tells them apart, and guessing wrong costs four attempts with backoff on a
    condition that will not clear. Recoverable by using a different model, not by
    retrying.

    ``wait`` is the delay the API asked for, when it asked for one. It is
    carried rather than re-parsed from the message because the two cases are
    worth telling apart afterwards: a limit that clears in a minute is a
    different thing from an allowance that is gone for the day, and saying
    "quota exhausted" about the first sends the reader to a billing page that
    will look normal.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        wait: float | None = None,
    ) -> None:
        super().__init__(message, status=status, retryable=retryable)
        #: Seconds the API suggested, or None when it did not say.
        self.wait = wait


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


#: Phrases that mean the allowance is gone rather than merely busy. Taken from
#: the API's own wording, captured live when the free tier ran out.
_QUOTA_MARKERS = (
    "exceeded your current quota",
    "check your plan and billing",
    "billing",
    "free tier",
    "quota exceeded",
    "out of quota",
)


#: How long a wait is worth sitting through before answering from another model.
#:
#: A Telegram command that pauses for a minute and a half gets cancelled, retried
#: by the user, or forgotten. A pause of a second or two is invisible. Ten
#: seconds is the point where waiting stops being cheaper than switching, which
#: makes it a judgement call rather than a number with an obvious right answer --
#: hence it being named and documented here instead of buried in a condition.
SWITCH_AFTER_SECONDS = 10.0

#: The clause that says how long for. Matched case-insensitively against the
#: whole message, which is why the leading ``.lower()`` is not there.
_RETRY_HINT = re.compile(r"please retry in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I)


def _retry_hint_seconds(message: str) -> float | None:
    """How long the API says to wait, if it says.

    Worth reading carefully, because the headline is the same either way. Live,
    the free tier's refusal read:

        You exceeded your current quota, please check your plan and billing
        details. ... Quota exceeded for metric:
        generativelanguage.googleapis.com/generate_content_free_tier_requests,
        limit: 20, model: gemini-3.8-flash
        Please retry in 56.601092868s.

    That is twenty requests per minute, not an allowance gone for the day. Read
    only the headline, the honest answer becomes "your quota is spent, check
    your billing" -- which sends the reader to a billing page that will show
    nothing wrong.
    """
    match = _RETRY_HINT.search(message)
    return float(match.group(1)) if match else None


def _is_quota_exhausted(message: str) -> bool:
    """Whether a 429 means "come back later" rather than "come back now".

    Both arrive as 429 with status ``RESOURCE_EXHAUSTED``, so the message is the
    only signal, and the message is ambiguous. A model that is merely busy
    says ``Resource has been exhausted (e.g. check quota)``; the free tier's
    per-request cap says ``exceeded your current quota ... check your plan and
    billing``, which is the same sentence whether it clears in a minute or at
    midnight.

    The discriminator is the retry hint, so it decides: a wait under
    ``SWITCH_AFTER_SECONDS`` is worth sitting through, and anything longer -- or
    no hint at all -- is not.
    """
    wait = _retry_hint_seconds(message)
    if wait is not None and wait <= SWITCH_AFTER_SECONDS:
        return False
    lowered = message.lower()
    if "resource has been exhausted" in lowered:
        # The generic form. Only the explicit forms below are conclusive.
        return any(marker in lowered for marker in _QUOTA_MARKERS)
    return any(marker in lowered for marker in _QUOTA_MARKERS)


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
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"message": response.text[:200]}}
        error = self._describe_error(response.status_code, payload)
        # Scrub here, where the keys are in scope. The classifier is static and
        # knows nothing about them, so an error body that echoed the key would
        # otherwise travel straight into a Telegram reply.
        scrubbed = _redact(str(error), self._keys)
        if isinstance(error, QuotaExhausted):
            # The wait rides along; rebuilding the exception by type alone would
            # quietly drop it, and it is the difference between the two wordings
            # the user sees.
            return QuotaExhausted(
                scrubbed, status=error.status, retryable=error.retryable, wait=error.wait
            )
        return type(error)(scrubbed, status=error.status, retryable=error.retryable)

    @staticmethod
    def _describe_error(status: int, payload: dict[str, Any]) -> GeminiError:
        """Turn an error body into something worth reading in Telegram.

        The API's own ``error.message`` is better than anything invented here.
        A spent quota is singled out because it is the one 429 that a different
        model can answer and a retry cannot.
        """
        error = payload.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else None
        if not isinstance(detail, str):
            detail = ""
        if not detail:
            detail = "запрос отклонён"
        if status == 429 and _is_quota_exhausted(detail):
            return QuotaExhausted(
                f"Gemini: {detail}",
                status=status,
                retryable=False,
                wait=_retry_hint_seconds(detail),
            )
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


class ModelRouter:
    """Tries models in order and remembers the one that answered.

    A single configured model is not a fallback: when its quota runs out, the
    only thing left to do is fail. That is not hypothetical -- the free-tier daily
    allowance on the configured default was spent, and every question after it
    produced an error until a second model was tried.

    Two rules keep this from being worse than the failure it fixes:

    * **Only a spent quota moves the chain on.** A 404, a bad key, a blocked
      request: those are not going to be answered by a different model, and
      retrying them elsewhere just multiplies the error.
    * **The working model is remembered, and the chain only forgets when that
      model fails too.** Otherwise every question re-probes a dead model and pays
      a 429 before getting an answer.
    """

    def __init__(
        self,
        models: Sequence[str],
        *,
        client: GeminiClient | None = None,
        key_getter: Callable[[], list[str]] | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | None = None,
    ) -> None:
        unique: list[str] = []
        for name in models:
            name = str(name).strip()
            # A duplicate is a wasted request, and a second probe of a dead model.
            if name and name not in unique:
                unique.append(name)
        if not unique:
            raise ValueError("a model router needs at least one model")
        self.models = tuple(unique)
        #: None means "whatever the client defaults to", so there is only one
        #: number to keep correct rather than a second copy of it here.
        self.timeout = timeout
        self._base_url = base_url
        self._key_getter = key_getter
        self._client = client
        self._owns_client = client is None
        self._active: str | None = None
        self._exhausted: set[str] = set()
        #: How long each refused model asked us to wait, when it said. The
        #: difference between "try again in a minute" and "come back tomorrow",
        #: and the two deserve different words in the reply.
        self._waits: dict[str, float] = {}

    # -- state ------------------------------------------------------------

    @property
    def has_key(self) -> bool:
        """Whether a key is configured at all.

        Asked of the router rather than tracked beside it, so there is one place
        that knows whether a request can be made and nothing to keep in step.
        """
        client = self._get_client()
        return bool(getattr(client, "has_key", False))

    @property
    def active(self) -> str:
        """The model that will be tried first."""
        for name in self._ordered():
            return name
        return self.models[0]

    @property
    def degraded(self) -> bool:
        """Whether the configured first choice is not the one in use."""
        return self._active is not None and self._active != self.models[0]

    def notice(self) -> str:
        """A line for the user, only when something is worth saying.

        Says which of the two things happened, and how long the API asked for,
        because on the free tier it is almost always the per-request cap --
        twenty requests a minute -- and telling someone their quota is spent when
        it resets in under a minute sends them to a billing page that will look
        perfectly normal.
        """
        if not self.degraded:
            return ""
        wait = self._waits.get(self.models[0])
        if wait is None:
            return f"Квота модели {self.models[0]} исчерпана, отвечает {self._active}."
        return (
            f"Лимит запросов к {self.models[0]} исчерпан "
            f"(повтор через ~{math.ceil(wait)} с), отвечает {self._active}."
        )

    def reset(self) -> None:
        """Forget what was learned, so the chain probes from the top again."""
        self._active = None
        self._exhausted.clear()
        self._waits.clear()

    def _ordered(self) -> list[str]:
        """The remembered model first, then the rest in configured order."""
        if self._active is not None and self._active in self.models:
            return [self._active] + [m for m in self.models if m != self._active]
        return list(self.models)

    # -- client -------------------------------------------------------------

    def _get_client(self) -> GeminiClient:
        if self._client is None:
            keys = self._key_getter() if self._key_getter is not None else []
            extra: dict[str, Any] = {}
            if self.timeout is not None:
                extra["timeout"] = self.timeout
            self._client = GeminiClient(base_url=self._base_url, api_keys=keys, **extra)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def list_models(self) -> list[ModelInfo]:
        """Whatever the key can see. Not routed: this is the diagnostic."""
        return await self._get_client().list_models()

    # -- calls ---------------------------------------------------------------

    async def generate(self, turns: Sequence[Turn], **kwargs: Any) -> Reply:
        """Answer, moving to the next model when this one's quota is spent."""
        client = self._get_client()
        tried: list[str] = []
        for model in self._ordered():
            tried.append(model)
            try:
                reply = await client.generate(turns, model=model, **kwargs)
            except QuotaExhausted as exc:
                self._exhausted.add(model)
                if exc.wait is not None:
                    self._waits[model] = exc.wait
                if model == self._active:
                    self._active = None
                logger.warning("Gemini model %s is out of quota: %s", model, exc)
                continue
            self._active = model
            return reply
        raise QuotaExhausted(
            "Gemini: квота исчерпана на всех моделях ("
            + ", ".join(tried)
            + "). Проверьте план или ключ."
        )

    async def stream(self, turns: Sequence[Turn], **kwargs: Any) -> AsyncIterator[str]:
        """Stream, moving to the next model when this one's quota is spent.

        A stream that has already yielded cannot be resumed on another model --
        the user would see two answers spliced together -- so the switch only
        happens before the first chunk, which is where a quota refusal lands.
        """
        client = self._get_client()
        tried: list[str] = []
        for model in self._ordered():
            tried.append(model)
            produced = False
            try:
                async for piece in client.stream(turns, model=model, **kwargs):
                    produced = True
                    self._active = model
                    yield piece
            except QuotaExhausted as exc:
                if produced:
                    logger.warning("Gemini model %s ran out of quota mid-stream: %s", model, exc)
                    raise
                self._exhausted.add(model)
                if exc.wait is not None:
                    self._waits[model] = exc.wait
                if model == self._active:
                    self._active = None
                logger.warning("Gemini model %s is out of quota: %s", model, exc)
                continue
            return
        raise QuotaExhausted(
            "Gemini: квота исчерпана на всех моделях ("
            + ", ".join(tried)
            + "). Проверьте план или ключ."
        )
