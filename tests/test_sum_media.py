"""The attachments must reach the model, and what does not must be said.

Two defects, both found by running the command rather than by reading it, and
both invisible to the suite that existed:

* ``download()`` built every ``MediaItem.path`` inside a
  ``TemporaryDirectory`` and returned the list *after* the directory had been
  removed. ``read_media`` then hit ``FileNotFoundError`` on every file and
  ``continue``d. So ``/ub sum 10 media`` downloaded attachments, sent none of
  them, and the reply said nothing was missing -- no "Не учтено" section, because
  nothing had been recorded as skipped. The existing test asserted
  ``telegram.downloaded != []``, which stays true while nothing is sent.

* The budget was spent oldest-first, while ``_prompt.py`` told the model the
  attachments came "от новых к старым" and ``download``'s own docstring promised
  newest-first. When the cap bit, the model was told the opposite of the truth.

Run with: pytest tests/test_sum_media.py -q --no-cov
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import (
    FakeClient,
    FakeEvent,
    plugin_config,
    shipped_module,
    shipped_submodule,
)
from userbot.config import Settings
from userbot.rate_limit import RateLimiter

logging.basicConfig(level=logging.CRITICAL)

VOICE_BYTES = b"\x1aOggS-fake-voice-bytes"


def settings_in(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )


def voice(id_: int) -> Any:
    return SimpleNamespace(
        id=id_, sender_id=2, out=False, message="", action=None, voice=SimpleNamespace()
    )


class Downloading(FakeClient):
    """A client that really writes the bytes it is asked to write."""

    def __init__(self, messages: list[Any], payload: bytes = VOICE_BYTES) -> None:
        super().__init__()
        # Newest last, because the plugin reverses to get chronological order.
        self.messages = sorted(messages, key=lambda m: m.id)
        self.payload = payload
        self.written: list[Path] = []

    async def download_media(self, message: Any, file: str | None = None, **_: Any) -> str:
        path = Path(str(file))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Distinct per message, so "which attachment survived the cap" is a
        # question the test can actually answer. The write is blocking on purpose:
        # this stands in for a library coroutine that writes to disk, and
        # offloading it here would test the double rather than the plugin.
        path.write_bytes(self.payload + str(message.id).encode())  # noqa: ASYNC240
        self.written.append(path)
        return str(path)


class RecordingGemini:
    """Captures the turn, which is the only place the attachments can show up."""

    #: The router asks before it uses a client; the plugin reports a missing key
    #: as a user-facing error, and this stand-in has one.
    has_key = True

    def __init__(self, text: str = "Сводка.", truncated: bool = False) -> None:
        self.text_body = text
        self.truncated = truncated
        self.turns: list[Any] = []
        self.parts: list[Any] = []
        self.notices: list[str] = []

    async def generate(self, turns: Any, **kwargs: Any) -> Any:
        self.turns.append(turns)
        self.parts.extend(turns[0].parts)
        self.notices = list(kwargs.get("notices") or [])
        return SimpleNamespace(text=self.text_body, truncated=self.truncated)

    def notice(self) -> str:
        return ""

    @property
    def media(self) -> list[Any]:
        return [p for p in self.parts if getattr(p, "inline_data", None) is not None]


class Command:
    def __init__(self, args: str = "") -> None:
        self.name = "sum"
        self.args = args
        self.raw = f"/ub sum {args}"
        self.event = FakeEvent()
        self.replies: list[str] = []

    async def respond(self, text: str, **_: Any) -> None:
        self.replies.append(text)
        await self.event.respond(text)


def build(tmp_path: Path, messages: list[Any], gemini: RecordingGemini, **config: Any):
    _loaded, module = shipped_module("sum")
    telegram = Downloading(messages)
    ctx = SimpleNamespace(
        settings=settings_in(tmp_path),
        config=plugin_config({"include_media": True, **config}, "sum"),
        client=telegram,
        rate_limiter=RateLimiter(min_interval=0),
        storage=None,
        logger=logging.getLogger("test.sum"),
        is_owner=lambda sender_id: sender_id == 1,
    )
    plugin = module.Plugin()
    plugin.ctx = ctx
    plugin.router = gemini
    return module, plugin, ctx, telegram, Command()


# --- the attachments have to arrive ----------------------------------------


async def test_the_downloaded_bytes_reach_the_request(tmp_path: Path) -> None:
    """The whole point of ``include_media``, asserted on the request itself.

    Checking that ``download_media`` was called only proves the client was
    asked. The files were then deleted before anybody read them, and that test
    still passed.
    """
    gemini = RecordingGemini()
    _module, plugin, _ctx, telegram, command = build(tmp_path, [voice(10)], gemini)

    await plugin.handle(command)

    assert telegram.written, "nothing was downloaded at all"
    assert gemini.media, (
        f"the attachments never reached the request; parts were "
        f"{[getattr(p, 'text', None) for p in gemini.parts]!r}"
    )
    sent = base64.b64decode(gemini.media[0].inline_data["data"])
    assert sent == VOICE_BYTES + b"10", f"the model got {sent!r}, not the downloaded bytes"


async def test_the_reply_says_nothing_was_lost(tmp_path: Path) -> None:
    """A caveat is only worth anything if it appears when something is wrong."""
    gemini = RecordingGemini()
    _module, plugin, _ctx, _telegram, command = build(tmp_path, [voice(10)], gemini)

    await plugin.handle(command)

    joined = "\n".join(command.replies)
    assert "Не учтено" not in joined, (
        "everything was sent, so there is nothing to report:\n" + joined
    )


async def test_every_voice_note_is_sent(tmp_path: Path) -> None:
    gemini = RecordingGemini()
    _module, plugin, _ctx, telegram, command = build(
        tmp_path, [voice(10), voice(11), voice(12)], gemini
    )

    await plugin.handle(command)

    assert len(telegram.written) == 3, telegram.written
    assert len(gemini.media) == 3, f"only {len(gemini.media)} of 3 arrived"


# --- what the model is told must be true ------------------------------------


async def test_the_budget_is_spent_newest_first(tmp_path: Path) -> None:
    """The prompt says "от новых к старым"; the order has to match it.

    The budget filled from the oldest end, so when it bit, the newest — the
    attachments the summary is usually about — were the ones dropped, while the
    model was told the opposite about the ones that survived.
    """
    gemini = RecordingGemini()
    # One byte of headroom, so only the first attachment considered can fit.
    _module, plugin, _ctx, telegram, command = build(
        tmp_path,
        [voice(10), voice(11), voice(12)],
        gemini,
        max_media_bytes=1024,
        max_total_media_bytes=len(VOICE_BYTES) + 10,
    )

    await plugin.handle(command)

    # All three are downloaded; the cap decides which one is sent.
    assert len(telegram.written) == 3, telegram.written
    assert len(gemini.media) == 1, f"the cap should admit exactly one: {len(gemini.media)}"
    admitted = base64.b64decode(gemini.media[0].inline_data["data"])
    assert admitted == VOICE_BYTES + b"12", (
        f"the newest attachment should win the budget, got {admitted!r} "
        f"(all: {[p.name for p in telegram.written]})"
    )


async def test_a_dropped_attachment_is_reported(tmp_path: Path) -> None:
    """Over the cap is not "silently fine" -- that was the original bug."""
    gemini = RecordingGemini()
    _module, plugin, _ctx, _telegram, command = build(
        tmp_path,
        [voice(10), voice(11)],
        gemini,
        max_media_bytes=1024,
        max_total_media_bytes=len(VOICE_BYTES) + 10,
    )

    await plugin.handle(command)

    joined = "\n".join(command.replies)
    assert "Не учтено" in joined, (
        f"an attachment was dropped and the reply did not say so:\n{joined}"
    )


# --- a file that is not there when it is read -------------------------------


async def test_a_vanished_file_is_reported_not_swallowed(tmp_path: Path) -> None:
    """``read_media`` skipped unreadable files with a bare ``continue``.

    That is how every attachment disappeared with no trace: the path was already
    gone, the read raised ``OSError``, and the honest-looking skip hid it. A
    failure to read has to become a caveat, because silence here means the
    summary is quietly wrong.
    """
    _loaded, module = shipped_module("sum")
    plugin = module.Plugin()
    plugin.ctx = SimpleNamespace(
        settings=settings_in(tmp_path),
        config=plugin_config({"include_media": True}, "sum"),
        logger=logging.getLogger("test.sum"),
    )
    plugin.router = RecordingGemini()

    missing = tmp_path / "vanished.ogg"
    _sum, collect_module = shipped_submodule("sum", "_collect")
    collected = collect_module.Collected(transcript="привет", message_count=1)
    collected.media.append(
        collect_module.MediaItem(
            message_id=7, kind="голосовое", mime="audio/ogg", path=missing, size=123
        )
    )

    await plugin.summarise(plugin.ctx, collected)

    assert any("7" in note or "голосовое" in note for note in collected.notes), (
        f"a listed-but-unreadable attachment is not a caveat: {collected.notes!r}"
    )


async def test_a_readable_file_produces_no_caveat(tmp_path: Path) -> None:
    """Otherwise the caveats become noise and get ignored again."""
    _loaded, module = shipped_module("sum")
    plugin = module.Plugin()
    plugin.ctx = SimpleNamespace(
        settings=settings_in(tmp_path),
        config=plugin_config({"include_media": True}, "sum"),
        logger=logging.getLogger("test.sum"),
    )
    plugin.router = RecordingGemini()

    present = tmp_path / "here.ogg"
    present.write_bytes(VOICE_BYTES)
    _sum, collect_module = shipped_submodule("sum", "_collect")
    collected = collect_module.Collected(transcript="привет", message_count=1)
    collected.media.append(
        collect_module.MediaItem(
            message_id=8, kind="голосовое", mime="audio/ogg", path=present, size=len(VOICE_BYTES)
        )
    )

    await plugin.summarise(plugin.ctx, collected)

    assert not [n for n in collected.notes if "8" in n], collected.notes


@pytest.mark.parametrize("bad", ["", "нет", "0"])
async def test_a_count_argument_still_works(tmp_path: Path, bad: str) -> None:
    """Guard against the fix disturbing the argument path."""
    gemini = RecordingGemini()
    _module, plugin, _ctx, _telegram, command = build(tmp_path, [voice(10)], gemini)
    command.args = bad
    await plugin.handle(command)
    assert command.replies, f"no reply for {bad!r}"
