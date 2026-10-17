"""A small OpenAI-compatible client, shaped by what the channel publishes.

Every path here was checked against live endpoints before it was written down:

* ``/chat/completions`` both plain and ``stream: true`` (SSE);
* ``/models`` (Groq lists 11 ids, NVIDIA 81, Mistral 45);
* ``/audio/transcriptions`` multipart -- Groq's ``whisper-large-v3-turbo``
  answered ``{"text": ...}`` for a generated wav;
* ``/images/generations`` -- Agnes answered with a URL in ``data[0].url``
  (plus a ``task_id``), not ``b64_json``, so the URL is downloaded here;
* NVIDIA vision models accept an OpenAI-style ``image_url`` part with a
  ``data:`` URI and answered "Grey." for a grey test frame.

Two lessons are baked in. Reasoning models (``z-ai/glm-5.3`` on NVIDIA,
``openai/gpt-oss-*`` on Groq) may fill ``reasoning_content`` and leave
``content`` empty, so both are read and the caller decides what to show. And a
provider's error body is the only place the retry delay lives -- Groq names a
per-minute quota there, not in the status code -- so it is parsed, not ignored.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

#: Error kinds, mapped to the reactions they deserve. ``auth`` and ``denied``
#: are about the key; ``not_found`` about the (endpoint, model) pair; ``rate``
#: names its own delay when the API does; ``server`` and ``network`` are
#: transient; ``bad_request`` is our shape, and is neither remembered nor
#: retried against the same model.
ERROR_KINDS = frozenset({"auth", "denied", "not_found", "rate", "server", "network", "bad_request"})

_RETRY_HINT = re.compile(r"(?:retry|again) in\s+([\d.]+)\s*s", re.IGNORECASE)

#: Images larger than this are treated as a broken response rather than saved.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ApiError(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "bad_request"
        self.status = status
        self.retry_after = retry_after


def _endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        body = response.text
    except Exception:  # pragma: no cover - a body that cannot be decoded
        return None
    found = _RETRY_HINT.search(body)
    if not found:
        return None
    try:
        return float(found.group(1))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        text = ""
        try:
            text = response.text
        except Exception:  # pragma: no cover
            pass
        return text[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(error, str):
            return error[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return ""


def raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    if status == 401:
        kind = "auth"
    elif status == 403:
        kind = "denied"
    elif status == 404:
        kind = "not_found"
    elif status == 429:
        kind = "rate"
    elif status >= 500:
        kind = "server"
    else:
        kind = "bad_request"
    message = _error_message(response) or response.reason_phrase
    raise ApiError(
        kind,
        f"HTTP {status}: {message}",
        status=status,
        retry_after=_retry_after(response) if kind == "rate" else None,
    )


def _network_error(exc: httpx.HTTPError) -> ApiError:
    return ApiError("network", f"{type(exc).__name__}: {exc}")


async def list_models(
    http: httpx.AsyncClient, base_url: str, key: str, *, request_timeout: float
) -> list[str]:
    try:
        response = await http.get(
            _endpoint(base_url, "/models"), headers=_bearer(key), timeout=request_timeout
        )
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    raise_for_status(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError("bad_request", f"/models returned non-JSON: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    models: list[str] = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return models


def _text_of_choice(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if content:
        return str(content)
    reasoning = message.get("reasoning_content")
    return str(reasoning or "")


async def chat_once(
    http: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    messages: Sequence[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    request_timeout: float,
) -> str:
    payload: dict[str, Any] = {"model": model, "messages": list(messages)}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    try:
        response = await http.post(
            _endpoint(base_url, "/chat/completions"),
            headers=_bearer(key),
            json=payload,
            timeout=request_timeout,
        )
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    raise_for_status(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise ApiError("bad_request", f"non-JSON chat answer: {exc}") from exc
    return _text_of_choice(body)


#: A streamed chunk is either answer text or the model thinking out loud. They
#: are yielded apart: what is shown is the answer, and the reasoning is only a
#: fallback for models that produce nothing else within the token budget.
ChunkKind = str  # "content" | "reasoning"


def _delta_pieces(chunk: Any) -> list[tuple[ChunkKind, str]]:
    if not isinstance(chunk, dict):
        return []
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return []
    pieces: list[tuple[ChunkKind, str]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if delta.get("content"):
            pieces.append(("content", str(delta["content"])))
        elif delta.get("reasoning_content"):
            pieces.append(("reasoning", str(delta["reasoning_content"])))
    return pieces


async def chat_stream(
    http: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    messages: Sequence[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    request_timeout: float,
) -> AsyncIterator[tuple[ChunkKind, str]]:
    payload: dict[str, Any] = {"model": model, "messages": list(messages), "stream": True}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    try:
        async with http.stream(
            "POST",
            _endpoint(base_url, "/chat/completions"),
            headers=_bearer(key),
            json=payload,
            timeout=request_timeout,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise_for_status(response)
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                for piece in _delta_pieces(chunk):
                    yield piece
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc


async def transcribe(
    http: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    *,
    filename: str,
    data: bytes,
    mime: str,
    request_timeout: float,
) -> str:
    files = {"file": (filename, data, mime)}
    try:
        response = await http.post(
            _endpoint(base_url, "/audio/transcriptions"),
            headers=_bearer(key),
            files=files,
            data={"model": model},
            timeout=request_timeout,
        )
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    raise_for_status(response)
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()
    if isinstance(payload, dict) and payload.get("text") is not None:
        return str(payload["text"]).strip()
    return ""


async def generate_image(
    http: httpx.AsyncClient,
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    *,
    request_timeout: float,
) -> bytes:
    payload = {"model": model, "prompt": prompt, "n": 1}
    try:
        response = await http.post(
            _endpoint(base_url, "/images/generations"),
            headers=_bearer(key),
            json=payload,
            timeout=request_timeout,
        )
    except httpx.HTTPError as exc:
        raise _network_error(exc) from exc
    raise_for_status(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise ApiError("bad_request", f"non-JSON image answer: {exc}") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ApiError("bad_request", "image answer carried no data")
    item = data[0]
    if item.get("b64_json"):
        try:
            image = base64.b64decode(str(item["b64_json"]), validate=True)
        except (ValueError, TypeError) as exc:
            raise ApiError("bad_request", f"b64_json did not decode: {exc}") from exc
        return _checked_size(image)
    url = item.get("url")
    if url:
        try:
            download = await http.get(str(url), timeout=request_timeout)
        except httpx.HTTPError as exc:
            raise _network_error(exc) from exc
        raise_for_status(download)
        return _checked_size(download.content)
    raise ApiError("bad_request", "image answer had neither b64_json nor url")


def _checked_size(data: bytes) -> bytes:
    if len(data) > MAX_IMAGE_BYTES:
        raise ApiError("bad_request", f"image is {len(data)} bytes, over the cap")
    return data


__all__ = [
    "ApiError",
    "chat_once",
    "chat_stream",
    "generate_image",
    "list_models",
    "raise_for_status",
    "transcribe",
]
