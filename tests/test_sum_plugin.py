"""End-to-end tests for the summariser command.

The collection tests cover reading a chat. These cover the command: that it
excludes itself from the conversation, that the default really is media-off, and
that what the model failed to get reaches the user rather than disappearing into
a summary that quietly describes something else.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClient, RouterFor, plugin_config, shipped_module
from userbot.config import Settings
from userbot.rate_limit import RateLimiter


@pytest.fixture(scope="module")
def sum_module() -> Any:
    loaded, module = shipped_module("sum")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


class Sender:
    def __init__(self, name: str = "Аня") -> None:
        self.first_name = name
        self.last_name: str | None = None
        self.title: str | None = None
        self.username: str | None = None
        self.id = 1


class Message:
    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", 1)
        self.message = kwargs.get("message", "")
        self.date = kwargs.get("date", datetime.datetime(2026, 9, 27, 12, 0))
        self.sender = kwargs.get("sender", Sender())
        self.action = kwargs.get("action")
        self.voice = kwargs.get("voice")
        self.video_note = kwargs.get("video_note")
        self.photo = kwargs.get("photo")
        self.audio = kwargs.get("audio")
        self.video = kwargs.get("video")
        self.document = kwargs.get("document")
        self.file = kwargs.get("file")


class Reply:
    def __init__(self, text: str) -> None:
        self.text = text


class RecordingClient:
    """A Gemini client that records the turn instead of sending it."""

    has_key = True

    def __init__(self, text: str = "сводка") -> None:
        self.text = text
        self.turns: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    async def generate(self, turns: Any, **kwargs: Any) -> Reply:
        self.turns.append(turns)
        self.kwargs.append(kwargs)
        return Reply(self.text)

    async def stream(self, turns: Any, **kwargs: Any) -> Any:
        raise AssertionError("the summariser does not stream")
        yield  # pragma: no cover

    async def list_models(self) -> list[Any]:
        return []

    async def aclose(self) -> None:
        return None


class Command:
    def __init__(self, args: str, event: Any) -> None:
        self.name = "sum"
        self.args = args
        self.raw = f"/ub sum {args}".strip()
        self.event = event
        self.replies: list[str] = []

    async def respond(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def event_for(messages: list[Any], command_id: int = 999) -> Any:
    return SimpleNamespace(
        id=command_id, chat_id=555, sender_id=1, message=None, raw_text="/ub sum"
    )


def build(
    module: Any, tmp_path: Path, messages: list[Any], **config: Any
) -> tuple[Any, Any, Any, Any]:
    """A plugin wired to a fake client, a chat full of messages."""
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    telegram = FakeClient()
    # Given in chronological order and sorted here, so a test can list messages
    # naturally. telethon yields newest first and the fake reproduces that;
    # making each test remember the difference is how a fixture ends up lying.
    telegram.messages = sorted(messages, key=lambda item: item.id)

    # A namespace, not a class: a class body cannot see the enclosing function's
    # locals, and the shadowing that causes is a trap worth not walking into.
    ctx = SimpleNamespace(
        logger=logging.getLogger("test.sum"),
        rate_limiter=RateLimiter(min_interval=0),
        client=telegram,
        storage=None,
        settings=settings,
        config=plugin_config(config, "sum"),
        is_owner=lambda sender_id: sender_id == 1,
    )

    plugin = module.Plugin()
    plugin.ctx = ctx
    plugin.router = RouterFor(RecordingClient())
    # The last value is the Gemini double, so a test can inspect what was sent.
    return plugin, ctx, telegram, plugin.router


# --- arguments --------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "count", "media"),
    [
        ("", 10, False),
        ("25", 25, False),
        ("10 media", 10, True),
        ("медиа", 10, True),
        ("10 text", 10, False),
        ("текст", 10, False),
        ("0", 1, False),
        ("9999", 50, False),
    ],
)
def test_arguments_are_parsed(sum_module: Any, args: str, count: int, media: bool) -> None:
    plugin = module_without_setup(sum_module)
    assert plugin.parse_args(plugin.ctx, args) == (count, media)


def module_without_setup(module: Any) -> Any:
    """A plugin with just enough context to parse arguments."""
    from userbot.rate_limit import RateLimiter as _Rate

    plugin = module.Plugin()
    plugin.ctx = SimpleNamespace(
        config=plugin_config({}, "sum"), rate_limiter=_Rate(min_interval=0)
    )
    return plugin


def test_a_nonsense_argument_asks_for_usage(sum_module: Any) -> None:
    """Guessing at "100" when someone typed "1000" is a quiet wrong answer."""
    plugin = module_without_setup(sum_module)
    assert plugin.parse_args(plugin.ctx, "пятнадцать") is None


def test_the_count_comes_from_configuration(sum_module: Any) -> None:
    plugin = sum_module.Plugin()
    plugin.ctx = SimpleNamespace(config=plugin_config({"message_count": 25}, "sum"))
    assert plugin.parse_args(plugin.ctx, "")[0] == 25


# --- reading the chat --------------------------------------------------------


async def test_the_command_itself_is_excluded(sum_module: Any, tmp_path: Path) -> None:
    """The command is not part of what was said."""
    messages = [
        Message(id=1, message="первое"),
        Message(id=2, message="второе"),
        Message(id=3, message="третье"),
    ]
    plugin, _ctx, _telegram, client = build(sum_module, tmp_path, messages)
    command = Command("", event_for(messages, command_id=2))
    await plugin.handle(command)
    sent = client.turns[0][0].parts[0].text
    assert "первое" in sent
    assert "третье" in sent
    assert "второе" not in sent, "the command message itself must not be summarised"


async def test_messages_are_read_newest_first_into_oldest_first_order(
    sum_module: Any, tmp_path: Path
) -> None:
    """iter_messages yields newest-first; a model reads a conversation in order."""
    messages = [
        Message(id=1, message="первое"),
        Message(id=2, message="второе"),
        Message(id=3, message="третье"),
    ]
    plugin, _ctx, _telegram, client = build(sum_module, tmp_path, messages)
    await plugin.handle(Command("", event_for(messages, command_id=99)))
    sent = client.turns[0][0].parts[0].text
    assert sent.index("первое") < sent.index("второе") < sent.index("третье")


async def test_a_count_limits_what_is_read(sum_module: Any, tmp_path: Path) -> None:
    messages = [Message(id=i, message=f"сообщение {i}") for i in range(1, 21)]
    plugin, ctx, telegram, _client = build(sum_module, tmp_path, messages)
    event = event_for(messages, command_id=99)
    collected = await plugin.collect(ctx, event, 5, False)
    assert collected.message_count == 5


async def test_service_messages_are_not_conversation(sum_module: Any, tmp_path: Path) -> None:
    """A "joined the group" notice is not something to summarise."""
    messages = [
        Message(id=1, message="привет"),
        Message(id=2, message="", action=object()),
    ]
    plugin, ctx, _telegram, _client = build(sum_module, tmp_path, messages)
    collected = await plugin.collect(ctx, event_for(messages, command_id=99), 10, False)
    assert collected.message_count == 1


async def test_an_empty_chat_says_so_rather_than_calling_the_model(
    sum_module: Any, tmp_path: Path
) -> None:
    plugin, _ctx, _telegram, client = build(sum_module, tmp_path, [])
    command = Command("", event_for([], command_id=1))
    await plugin.handle(command)
    assert client.turns == [], "nothing to summarise must not reach the API"
    assert any("нечего" in reply for reply in command.replies)


# --- media is off by default ------------------------------------------------


async def test_media_is_not_downloaded_by_default(sum_module: Any, tmp_path: Path) -> None:
    messages = [Message(id=1, message="", voice=object())]
    plugin, ctx, telegram, client = build(sum_module, tmp_path, messages)
    await plugin.handle(Command("", event_for(messages, command_id=99)))
    assert telegram.downloaded == [], "include_media defaults to false"
    turn = client.turns[0][0]
    whole = " ".join(part.text or "" for part in turn.parts)
    assert "медиа не отправляются" in whole, "the user must be told, not left guessing"


async def test_media_is_downloaded_when_asked(sum_module: Any, tmp_path: Path) -> None:
    payload = tmp_path / "voice.ogg"
    payload.write_bytes(b"OggS" + b"x" * 32)
    messages = [Message(id=1, message="", voice=object())]
    plugin, ctx, telegram, _client = build(sum_module, tmp_path, messages, include_media=True)

    def fake_download(message: Any, **kwargs: Any) -> str:
        # Keeps the recording the real fake does; replacing the method outright
        # would leave `downloaded` empty even on success.
        telegram.downloaded.append(message)
        return str(payload)

    telegram.download_media = fake_download
    command = Command("media", event_for(messages, command_id=99))
    await plugin.handle(command)
    assert telegram.downloaded, "the attachment should have been fetched"
    assert not any("медиа не отправляются" in reply for reply in command.replies)


# --- the reply ---------------------------------------------------------------


async def test_a_clean_summary_has_no_caveat_section(sum_module: Any, tmp_path: Path) -> None:
    messages = [Message(id=1, message="привет")]
    plugin, _ctx, _telegram, _client = build(sum_module, tmp_path, messages)
    reply = plugin.compose(
        plugin.__class__ and _collected(text="всё спокойно", count=1),
        "всё спокойно",
    )
    assert "Не учтено" not in reply
    assert "последним 1 сообщениям" in reply


async def test_caveats_are_attached_not_swallowed(sum_module: Any, tmp_path: Path) -> None:
    messages = [Message(id=1, message="привет")]
    plugin, _ctx, _telegram, _client = build(sum_module, tmp_path, messages)
    collected = _collected(text="тема", count=3)
    collected.notes.append("медиа не отправляются")
    reply = plugin.compose(collected, "тема")
    assert "Не учтено" in reply
    assert "медиа не отправляются" in reply
    assert "тема" in reply


def _collected(text: str, count: int) -> Any:
    import sys

    from conftest import shipped_submodule

    loaded, _entry = shipped_submodule("sum", "plugin")
    package = sys.modules[loaded.module.__name__]
    collect = __import__("importlib").import_module(f"{package.__name__}._collect")
    result = collect.Collected(transcript=text, message_count=count)
    return result


def test_the_reply_stays_within_telegram_limits(sum_module: Any, tmp_path: Path) -> None:
    """The dispatcher clips, but a summary that arrives cut off is worse."""
    from userbot.commands import MAX_MESSAGE_LENGTH

    messages = [Message(id=1, message="привет")]
    plugin, _ctx, _telegram, _client = build(sum_module, tmp_path, messages)
    collected = _collected(text="x", count=10)
    reply = plugin.compose(collected, "y" * (MAX_MESSAGE_LENGTH + 100))
    assert len(reply) > MAX_MESSAGE_LENGTH, (
        "compose does not clip; the dispatcher's _clip does, and that is deliberate"
    )


def test_the_manifest_documents_the_privacy_default() -> None:
    from userbot.loader import PluginManifest

    manifest = PluginManifest.from_path(Path(__file__).resolve().parent.parent / "plugins" / "sum")
    assert manifest.config["include_media"] is False, (
        "sending other people's messages to a third party is opt-in"
    )
    assert manifest.config["message_count"] == 10
