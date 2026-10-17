"""End-to-end tests for the api plugin's command surface.

The HTTP layer is an ``httpx.MockTransport`` router keyed by host and path, so
one test can have a rate-limited Groq key, a working NVIDIA key and an image
CDN at the same time. The Telegram side is the conftest fake event and client,
with a downloading client double for the media flows.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from conftest import FakeClient, FakeContext, FakeEvent, plugin_config, shipped_submodule
from userbot.commands import CommandContext
from userbot.storage import create_plugin_storage


@pytest.fixture(scope="module")
def api_modules() -> Any:
    loaded, _entry = shipped_submodule("api", "plugin")
    package = loaded.module.__name__
    namespace = SimpleNamespace(
        entry=importlib.import_module(f"{package}.plugin"),
        channel=importlib.import_module(f"{package}._channel"),
        prompts=importlib.import_module(f"{package}._prompts"),
    )
    yield namespace
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def sse(text: str) -> httpx.Response:
    chunk = {"choices": [{"delta": {"content": text}}]}
    body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


async def build(
    api_modules: Any,
    tmp_path: Path,
    handler: Any,
    *,
    seed_cursor: bool = True,
    client: FakeClient | None = None,
    **config: Any,
) -> tuple[Any, Any]:
    plugin = api_modules.entry.Plugin()
    values: dict[str, Any] = {"channel": "testchan", "join_on_setup": False}
    values.update(config)
    ctx: Any = FakeContext(config=plugin_config(values, name="api"))
    storage = await create_plugin_storage(tmp_path, "api")
    ctx.storage = storage
    ctx.client = client if client is not None else FakeClient()
    await plugin.setup(ctx)
    if seed_cursor:
        # The cursor is seeded so the bootstrap task skips the backlog scan;
        # the scan itself is exercised through /ub api refresh and one test
        # that deliberately leaves the cursor empty.
        await plugin.catalog.set_meta("cursor", "1")
    await asyncio.sleep(0)
    if plugin.http is not None:
        await plugin.http.aclose()
    plugin.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return plugin, ctx


async def shutdown(plugin: Any, ctx: FakeContext) -> None:
    await plugin.stop()
    await ctx.storage.close()


async def seed(
    api_modules: Any, plugin: Any, *, base: str, key: str, model: str, working: bool = True
) -> int:
    parsed = api_modules.channel.ParsedPost(
        base_url=base,
        api_key=key,
        post_date="2026-10-16T07:06:00+00:00",
        tier=None,
        working=(api_modules.channel.ModelMention(model),) if working else (),
        denied=(),
        listed=(),
        listed_truncated=False,
    )
    endpoint_id, _created = await plugin.catalog.upsert_post(parsed, post_id=1)
    return endpoint_id


def command(args: str = "", event: Any = None) -> tuple[CommandContext, FakeEvent]:
    event = event if event is not None else FakeEvent(raw_text=f"/ub api {args}".strip())
    return CommandContext(name="api", args=args, raw="", event=event), event


# --- chat -------------------------------------------------------------------


async def test_a_question_is_answered_and_delivered(api_modules: Any, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return sse("Привет, мир")
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")
    cmd, event = command("привет")
    await plugin._command(cmd)
    joined = "\n".join(event.responses)
    assert "Привет, мир" in joined
    assert "Ответила" not in joined  # the first candidate answered; no notice
    await shutdown(plugin, ctx)


async def test_a_rate_limited_key_falls_back_with_a_notice(
    api_modules: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        streaming = False
        try:
            streaming = bool(json.loads(request.content).get("stream"))
        except (ValueError, AttributeError):
            pass
        if "groq" in request.url.host and streaming:
            return httpx.Response(
                429, headers={"retry-after": "120"}, json={"error": {"message": "quota"}}
            )
        if request.url.path.endswith("/chat/completions"):
            if streaming:
                return sse("ответ из nvidia")
            return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(
        api_modules, plugin, base="https://api.groq.com/openai/v1", key="k-g", model="grok-broken"
    )
    await seed(
        api_modules,
        plugin,
        base="https://integrate.api.nvidia.com/v1",
        key="k-n",
        model="llama-good",
    )
    cmd, event = command("привет")
    await plugin._command(cmd)
    joined = "\n".join(event.responses)
    assert "ответ из nvidia" in joined
    assert "Ответила llama-good (nvidia)." in joined
    await shutdown(plugin, ctx)


# --- catalogue commands -----------------------------------------------------


async def test_use_pins_unpins_and_rejects_strangers(api_modules: Any, tmp_path: Path) -> None:
    plugin, ctx = await build(api_modules, tmp_path, lambda _request: httpx.Response(404))
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")

    cmd, event = command("use m-chat")
    await plugin._command(cmd)
    assert "Закреплено: m-chat" in "\n".join(event.responses)
    assert await plugin.catalog.get_meta("pinned_model") == "m-chat"

    cmd, event = command("use auto")
    await plugin._command(cmd)
    assert "Автопилот включён." in "\n".join(event.responses)
    assert await plugin.catalog.get_meta("pinned_model") == ""

    cmd, event = command("use stranger")
    await plugin._command(cmd)
    assert "нет в каталоге" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_keys_and_models_report_the_catalogue(api_modules: Any, tmp_path: Path) -> None:
    plugin, ctx = await build(api_modules, tmp_path, lambda _request: httpx.Response(404))
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")

    cmd, event = command("keys")
    await plugin._command(cmd)
    assert "groq: ключей 1" in "\n".join(event.responses)

    cmd, event = command("models")
    await plugin._command(cmd)
    assert "m-chat" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_refresh_reads_the_channel_and_stores_keys(api_modules: Any, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m-chat"}]})
        if request.url.path.endswith("/chat/completions"):
            return sse("pong")
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    text = "🌐 Base URL: https://api.groq.com/openai/v1\n🔑 Ключ: k-fresh\n"
    ctx.client.messages.append(SimpleNamespace(raw_text=text, message=text, id=42))
    cmd, event = command("refresh 5")
    await plugin._command(cmd)
    assert "новых ключей: 1" in "\n".join(event.responses)
    assert await plugin.catalog.endpoint_count() == 1
    await asyncio.sleep(0.1)  # let the spawned validation finish
    await shutdown(plugin, ctx)


async def test_test_pings_a_matching_model(api_modules: Any, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")
    cmd, event = command("test m-chat")
    await plugin._command(cmd)
    joined = "\n".join(event.responses)
    assert "✅" in joined and "m-chat" in joined
    await shutdown(plugin, ctx)


# --- media ------------------------------------------------------------------


class FileInfo:
    def __init__(self, *, size: int | None = None, mime: str | None = None) -> None:
        self.size = size
        self.mime_type = mime


class MediaMessage:
    def __init__(
        self,
        *,
        voice: bool = False,
        video: bool = False,
        photo: bool = False,
        mime: str | None = None,
        size: int | None = None,
    ) -> None:
        self.voice = object() if voice else None
        self.video = object() if video else None
        self.video_note = None
        self.photo = object() if photo else None
        self.audio = None
        self.document = None
        self.file = FileInfo(size=size, mime=mime)


class Carrier:
    """A command message whose reply is the media under test."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply

    async def get_reply_message(self) -> Any:
        return self.reply


class DownloadingClient(FakeClient):
    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.payload = payload

    async def download_media(self, message: Any, **kwargs: Any) -> Any:
        buffer = kwargs["file"]
        buffer.write(self.payload)
        return buffer


async def test_a_voice_note_is_transcribed_with_a_one_time_notice(
    api_modules: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "привет мир"})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(
        api_modules,
        plugin,
        base="https://api.groq.com/openai/v1",
        key="k1",
        model="whisper-large-v3-turbo",
    )
    ctx.client = DownloadingClient(b"OggS-fake")

    event = FakeEvent()
    event.message = Carrier(MediaMessage(voice=True, mime="audio/ogg", size=4))
    cmd = CommandContext(name="api", args="", raw="", event=event)
    await plugin._command(cmd)
    joined = "\n".join(event.responses)
    assert "Расшифровка" in joined
    assert "привет мир" in joined
    assert "публичный сторонний API" in joined

    second = FakeEvent()
    second.message = Carrier(MediaMessage(voice=True, mime="audio/ogg", size=4))
    cmd = CommandContext(name="api", args="", raw="", event=second)
    await plugin._command(cmd)
    assert "публичный сторонний API" not in "\n".join(second.responses)
    await shutdown(plugin, ctx)


async def test_a_question_on_a_voice_note_goes_through_the_chat(
    api_modules: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "сегодня будет дождь"})
        if request.url.path.endswith("/chat/completions"):
            return sse("возьми зонт")
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(
        api_modules,
        plugin,
        base="https://api.groq.com/openai/v1",
        key="k1",
        model="whisper-large-v3-turbo",
    )
    await seed(
        api_modules,
        plugin,
        base="https://integrate.api.nvidia.com/v1",
        key="k2",
        model="m-chat",
    )
    ctx.client = DownloadingClient(b"OggS-fake")

    event = FakeEvent()
    event.message = Carrier(MediaMessage(voice=True, mime="audio/ogg", size=4))
    cmd = CommandContext(name="api", args="что делать?", raw="", event=event)
    await plugin._command(cmd)
    assert "возьми зонт" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_img_generates_and_sends_an_image_file(api_modules: Any, tmp_path: Path) -> None:
    raw = b"\x89PNG\r\n\x1a\nDATA"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=raw)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.test/i.png"}]})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(
        api_modules,
        plugin,
        base="https://apihub.agnes-ai.com/v1",
        key="k1",
        model="agnes-image-2.0-flash",
    )
    cmd, event = command("img кот в шляпе")
    await plugin._command(cmd)
    assert len(event.files) == 1
    assert event.files[0].endswith(".png")
    await shutdown(plugin, ctx)


async def test_video_summary_merges_frames_and_speech(
    api_modules: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "речь из видео"})
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            content = payload["messages"][0]["content"]
            if isinstance(content, list):
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "кадр: кот на столе"}}]}
                )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "краткий пересказ видео"}}]}
            )
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    entry = api_modules.entry
    monkeypatch.setattr(entry, "has_ffmpeg", lambda: True)
    monkeypatch.setattr(
        entry, "extract_frames", lambda source, target_dir, *, count, width: [(1.0, b"frame")]
    )
    monkeypatch.setattr(
        entry,
        "extract_audio",
        lambda source, target_dir: SimpleNamespace(read_bytes=lambda: b"audio"),
    )
    await seed(
        api_modules,
        plugin,
        base="https://api.groq.com/openai/v1",
        key="k1",
        model="whisper-large-v3-turbo",
    )
    await seed(
        api_modules,
        plugin,
        base="https://integrate.api.nvidia.com/v1",
        key="k2",
        model="test-vision",
    )
    ctx.client = DownloadingClient(b"fake-video")

    event = FakeEvent()
    event.message = Carrier(MediaMessage(video=True, mime="video/mp4", size=100))
    cmd = CommandContext(name="api", args="", raw="", event=event)
    await plugin._command(cmd)
    joined = "\n".join(event.responses)
    assert "краткий пересказ видео" in joined
    assert "Источники" in joined
    await shutdown(plugin, ctx)


# --- failure paths and the bootstrap ---------------------------------------


async def test_chat_that_exhausts_candidates_forgets_the_question(
    api_modules: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")
    cmd, event = command("привет")
    await plugin._command(cmd)
    assert "Ответа нет" in "\n".join(event.responses)
    rows = await ctx.storage.fetchall("SELECT id FROM history")
    assert rows == []
    await shutdown(plugin, ctx)


async def test_a_failed_transcription_says_so(api_modules: Any, tmp_path: Path) -> None:
    plugin, ctx = await build(api_modules, tmp_path, lambda _request: httpx.Response(404))
    await seed(
        api_modules,
        plugin,
        base="https://api.groq.com/openai/v1",
        key="k1",
        model="whisper-large-v3-turbo",
    )
    ctx.client = DownloadingClient(b"OggS-fake")
    event = FakeEvent()
    event.message = Carrier(MediaMessage(voice=True, mime="audio/ogg", size=4))
    cmd = CommandContext(name="api", args="", raw="", event=event)
    await plugin._command(cmd)
    assert "Расшифровка недоступна" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_a_photo_is_answered_by_a_vision_model(api_modules: Any, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "на фото кот"}}]})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    await seed(
        api_modules,
        plugin,
        base="https://integrate.api.nvidia.com/v1",
        key="k1",
        model="test-vision",
    )
    ctx.client = DownloadingClient(b"\xff\xd8jpeg")
    event = FakeEvent()
    event.message = Carrier(MediaMessage(photo=True, mime="image/jpeg", size=6))
    cmd = CommandContext(name="api", args="что на фото?", raw="", event=event)
    await plugin._command(cmd)
    assert "на фото кот" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_img_without_image_models_explains(api_modules: Any, tmp_path: Path) -> None:
    plugin, ctx = await build(api_modules, tmp_path, lambda _request: httpx.Response(404))
    cmd, event = command("img кот")
    await plugin._command(cmd)
    assert "Не получилось сгенерировать" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_use_without_arguments_reports_the_pin(api_modules: Any, tmp_path: Path) -> None:
    plugin, ctx = await build(api_modules, tmp_path, lambda _request: httpx.Response(404))
    await seed(api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="m-chat")
    await plugin.catalog.set_meta("pinned_model", "m-chat")
    cmd, event = command("use")
    await plugin._command(cmd)
    assert "Закреплено: m-chat" in "\n".join(event.responses)

    cmd, event = command("models zzz")
    await plugin._command(cmd)
    assert "Ничего не нашлось" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_bootstrap_joins_the_channel_and_reads_the_backlog(
    api_modules: Any, tmp_path: Path
) -> None:
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.types import InputPeerChannel

    class JoiningClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[Any] = []

        async def __call__(self, request: Any) -> Any:
            self.requests.append(request)
            raise UserAlreadyParticipantError(request=request)

    client = JoiningClient()
    client.entities["testchan"] = InputPeerChannel(channel_id=42, access_hash=7)
    placeholder = "SOMETHING HAS BEEN HERE!"
    post = "🌐 Base URL: https://api.groq.com/openai/v1\n🔑 Ключ: k-boot\n"
    client.messages.extend(
        [
            SimpleNamespace(raw_text=placeholder, message=placeholder, id=9),
            SimpleNamespace(raw_text=post, message=post, id=10),
        ]
    )
    plugin, ctx = await build(
        api_modules,
        tmp_path,
        lambda _request: httpx.Response(404),
        seed_cursor=False,
        client=client,
        join_on_setup=True,
    )
    await asyncio.sleep(0.2)
    assert client.requests  # the join was attempted
    assert ctx.handlers  # the channel listener is registered
    assert await plugin.catalog.endpoint_count() == 1
    await shutdown(plugin, ctx)


async def test_refresh_reports_a_channel_failure(api_modules: Any, tmp_path: Path) -> None:
    class BrokenScanClient(FakeClient):
        def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
            raise RuntimeError("network down")

    plugin, ctx = await build(
        api_modules, tmp_path, lambda _request: httpx.Response(404), client=BrokenScanClient()
    )
    cmd, event = command("refresh 5")
    await plugin._command(cmd)
    assert "Не получилось прочитать канал" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_validation_cooldowns_a_revoked_key(api_modules: Any, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    text = "🌐 Base URL: https://api.groq.com/openai/v1\n🔑 Ключ: k-revoked\n"
    ctx.client.messages.append(SimpleNamespace(raw_text=text, message=text, id=7))
    cmd, event = command("refresh 5")
    await plugin._command(cmd)
    await asyncio.sleep(0.2)  # let the spawned validation finish
    cmd, event = command("keys")
    await plugin._command(cmd)
    assert "в кулдауне 1" in "\n".join(event.responses)
    await shutdown(plugin, ctx)


async def test_the_sweep_validates_a_key_whose_check_never_finished(
    api_modules: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "swept-model"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
        return httpx.Response(404)

    plugin, ctx = await build(api_modules, tmp_path, handler)
    endpoint_id = await seed(
        api_modules, plugin, base="https://api.groq.com/openai/v1", key="k1", model="swept-model"
    )
    assert not await plugin.catalog.is_checked(endpoint_id)
    queued = await plugin._validate_unchecked()
    assert queued == 1
    await asyncio.sleep(0.2)
    assert await plugin.catalog.is_checked(endpoint_id)
    rows = await plugin.catalog.model_rows()
    assert rows[0]["alive"] == 1
    await shutdown(plugin, ctx)


def test_video_merge_message_marks_what_is_missing(api_modules: Any) -> None:
    text = api_modules.prompts.video_merge_message(
        transcript=None, frame_notes=[(1.0, "кот")], focus="кот"
    )
    assert "Расшифровки речи нет" in text
    assert "кот" in text

    text = api_modules.prompts.video_merge_message(transcript="речь", frame_notes=[], focus=None)
    assert "Описаний кадров нет" in text
    assert "речь" in text
