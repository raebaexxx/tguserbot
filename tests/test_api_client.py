"""Tests for the OpenAI-compatible client.

Every wire shape here was captured from a live endpoint while building the
plugin: Groq's whisper answer is ``{"text": ...}``, Agnes' image answer is a
URL (not ``b64_json``), NVIDIA reasoning models can fill
``reasoning_content`` and leave ``content`` empty, and Groq names its retry
delay in the body ("Please retry in 56.6s") rather than the status code.

HTTP goes through ``httpx.MockTransport``, so the request the client builds is
the request that gets asserted on.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from conftest import shipped_submodule

KEY = "test-key-0123456789"


@pytest.fixture(scope="module")
def client_module() -> Any:
    loaded, module = shipped_submodule("api", "_openai")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def http_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_route(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# --- chat -------------------------------------------------------------------


async def test_chat_once_reads_content(client_module: Any) -> None:
    payload = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    async with http_for(json_route(payload)) as http:
        text = await client_module.chat_once(
            http,
            "https://api.example/v1",
            KEY,
            "m",
            [{"role": "user", "content": "hi"}],
            request_timeout=10,
        )
    assert text == "OK"


async def test_chat_once_falls_back_to_reasoning_content(client_module: Any) -> None:
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": None, "reasoning_content": "думаю"}}
        ]
    }
    async with http_for(json_route(payload)) as http:
        text = await client_module.chat_once(
            http,
            "https://api.example/v1",
            KEY,
            "m",
            [{"role": "user", "content": "hi"}],
            request_timeout=10,
        )
    assert text == "думаю"


async def test_chat_stream_parses_sse_and_separates_reasoning(client_module: Any) -> None:
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "думаю"}}]},
        {"choices": [{"delta": {"content": "Привет"}}]},
        {"choices": [{"delta": {"content": ", мир"}}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    async with http_for(handler) as http:
        pieces = [
            piece
            async for piece in client_module.chat_stream(
                http,
                "https://api.example/v1",
                KEY,
                "m",
                [{"role": "user", "content": "hi"}],
                request_timeout=10,
            )
        ]
    assert pieces == [("reasoning", "думаю"), ("content", "Привет"), ("content", ", мир")]


# --- models, transcription, images -----------------------------------------


async def test_list_models_returns_ids(client_module: Any) -> None:
    payload = {"data": [{"id": "m-one"}, {"id": "m-two"}, {"no-id": True}]}
    async with http_for(json_route(payload)) as http:
        models = await client_module.list_models(
            http, "https://api.example/v1", KEY, request_timeout=10
        )
    assert models == ["m-one", "m-two"]


async def test_transcribe_builds_a_multipart_request_with_the_model_field(
    client_module: Any,
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " привет мир"})

    async with http_for(handler) as http:
        text = await client_module.transcribe(
            http,
            "https://api.example/v1",
            KEY,
            "whisper-x",
            filename="voice.ogg",
            data=b"OggS-fake",
            mime="audio/ogg",
            request_timeout=10,
        )
    assert text == "привет мир"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'name="model"' in seen["body"]
    assert b"whisper-x" in seen["body"]
    assert b'name="file"' in seen["body"]
    assert b"OggS-fake" in seen["body"]


async def test_generate_image_decodes_b64_json(client_module: Any) -> None:
    raw = b"\x89PNG\r\n\x1a\nfake"
    payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
    async with http_for(json_route(payload)) as http:
        image = await client_module.generate_image(
            http, "https://api.example/v1", KEY, "img-x", "кот", request_timeout=10
        )
    assert image == raw


async def test_generate_image_downloads_a_url_like_agnes(client_module: Any) -> None:
    raw = b"\xff\xd8\xff\xe0fake-jpeg"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=raw)
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/a.jpg"}]})

    async with http_for(handler) as http:
        image = await client_module.generate_image(
            http, "https://api.example/v1", KEY, "img-x", "кот", request_timeout=10
        )
    assert image == raw


# --- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "auth"), (403, "denied"), (404, "not_found"), (500, "server"), (400, "bad_request")],
)
async def test_http_errors_map_to_kinds(client_module: Any, status: int, kind: str) -> None:
    handler = json_route({"error": {"message": "nope"}}, status=status)
    async with http_for(handler) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.chat_once(
                http,
                "https://api.example/v1",
                KEY,
                "m",
                [{"role": "user", "content": "hi"}],
                request_timeout=10,
            )
    assert caught.value.kind == kind


async def test_the_retry_delay_is_read_from_the_body(client_module: Any) -> None:
    body = {"error": {"message": "Rate limit exceeded. Please retry in 56.601092868s."}}
    handler = json_route(body, status=429)
    async with http_for(handler) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.chat_once(
                http,
                "https://api.example/v1",
                KEY,
                "m",
                [{"role": "user", "content": "hi"}],
                request_timeout=10,
            )
    assert caught.value.kind == "rate"
    assert caught.value.retry_after == pytest.approx(56.601092868)


async def test_the_retry_delay_is_read_from_the_header(client_module: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": "slow"})

    async with http_for(handler) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.list_models(http, "https://api.example/v1", KEY, request_timeout=10)
    assert caught.value.retry_after == 3.0


async def test_a_connection_failure_is_a_network_error(client_module: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with http_for(handler) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.chat_once(
                http,
                "https://api.example/v1",
                KEY,
                "m",
                [{"role": "user", "content": "hi"}],
                request_timeout=10,
            )
    assert caught.value.kind == "network"


# --- degenerate payloads ----------------------------------------------------


async def test_chat_once_without_choices_returns_empty(client_module: Any) -> None:
    async with http_for(json_route({"id": "x"})) as http:
        text = await client_module.chat_once(
            http,
            "https://api.example/v1",
            KEY,
            "m",
            [{"role": "user", "content": "hi"}],
            request_timeout=10,
        )
    assert text == ""


async def test_list_models_tolerates_a_nonstandard_payload(client_module: Any) -> None:
    async with http_for(json_route(["not", "an", "object"])) as http:
        models = await client_module.list_models(
            http, "https://api.example/v1", KEY, request_timeout=10
        )
    assert models == []


async def test_the_error_message_is_read_from_a_string_error(client_module: Any) -> None:
    handler = json_route({"error": "boom"}, status=500)
    async with http_for(handler) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.list_models(http, "https://api.example/v1", KEY, request_timeout=10)
    assert "boom" in str(caught.value)


async def test_transcribe_falls_back_to_plain_text(client_module: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="просто текст", headers={"content-type": "text/plain"})

    async with http_for(handler) as http:
        text = await client_module.transcribe(
            http,
            "https://api.example/v1",
            KEY,
            "whisper-x",
            filename="a.ogg",
            data=b"x",
            mime="audio/ogg",
            request_timeout=10,
        )
    assert text == "просто текст"


async def test_generate_image_reports_a_payload_without_data(client_module: Any) -> None:
    async with http_for(json_route({"data": []})) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.generate_image(
                http, "https://api.example/v1", KEY, "img-x", "кот", request_timeout=10
            )
    assert caught.value.kind == "bad_request"


async def test_generate_image_reports_an_item_without_b64_or_url(client_module: Any) -> None:
    async with http_for(json_route({"data": [{"task_id": "t"}]})) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.generate_image(
                http, "https://api.example/v1", KEY, "img-x", "кот", request_timeout=10
            )
    assert "neither b64_json nor url" in str(caught.value)


async def test_generate_image_refuses_an_oversized_image(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_IMAGE_BYTES", 8)
    payload = {"data": [{"b64_json": base64.b64encode(b"0123456789").decode()}]}
    async with http_for(json_route(payload)) as http:
        with pytest.raises(client_module.ApiError) as caught:
            await client_module.generate_image(
                http, "https://api.example/v1", KEY, "img-x", "кот", request_timeout=10
            )
    assert "over the cap" in str(caught.value)
