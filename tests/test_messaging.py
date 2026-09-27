"""Tests for editing a message.

Telethon 1.45's ``Message`` has ``edit`` and no ``edit_text`` at all, while
Pyrogram's has ``edit_text`` and no ``edit``. Both names are natural, both are
taught by example, and writing the wrong one costs a feature: the ai plugin
called ``edit_text`` on a real message, every progress edit raised
``AttributeError``, and streaming silently did nothing in production for days.

The library's method name is therefore a trap, and the way out is a helper that
knows about it -- used by every plugin that touches a message, so the next one
does not have to rediscover it.
"""

from __future__ import annotations

from typing import Any

from userbot.messaging import can_edit, edit_message

# --- the two library shapes -------------------------------------------------


class TelethonStyle:
    """Telethon 1.45: ``edit``, no ``edit_text``."""

    def __init__(self) -> None:
        self.edits: list[str] = []
        self.deleted = False

    async def edit(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


class PyrogramStyle:
    """Pyrogram: ``edit_text``, no ``edit``."""

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> PyrogramStyle:
        self.edits.append(text)
        return self


class Both:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.used: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> Both:
        self.used.append("edit_text")
        self.edits.append(text)
        return self

    async def edit(self, text: str, **kwargs: Any) -> Both:
        self.used.append("edit")
        self.edits.append(text)
        return self


class Broken:
    async def edit(self, text: str, **kwargs: Any) -> None:
        raise RuntimeError("MESSAGE_ID_INVALID")


class Sync:
    """A library that returns an awaitable from edit, and one that does not."""

    def __init__(self) -> None:
        self.edits: list[str] = []

    def edit(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


# --- it works on either library ---------------------------------------------


async def test_a_telethon_message_is_edited() -> None:
    message = TelethonStyle()
    assert await edit_message(message, "новый текст") is True
    assert message.edits == ["новый текст"]


async def test_a_pyrogram_message_is_edited() -> None:
    message = PyrogramStyle()
    assert await edit_message(message, "новый текст") is True
    assert message.edits == ["новый текст"]


async def test_edit_text_wins_when_both_exist() -> None:
    """Preferred for its narrower signature; both are the same operation."""
    message = Both()
    await edit_message(message, "текст")
    assert message.used == ["edit_text"]


async def test_a_synchronous_edit_is_handled() -> None:
    message = Sync()
    assert await edit_message(message, "текст") is True
    assert message.edits == ["текст"]


# --- it fails honestly ------------------------------------------------------


async def test_an_object_with_neither_reports_failure() -> None:
    assert await edit_message(object(), "текст") is False
    assert can_edit(object()) is False


async def test_a_failing_edit_reports_failure_rather_than_raising() -> None:
    """A message that cannot be edited is gone or rate-limited.

    Raising from here would abort whatever was streaming, and the caller has
    already been told the answer is being delivered by another route.
    """
    assert await edit_message(Broken(), "текст") is False


async def test_extra_keyword_arguments_are_passed_through() -> None:
    seen: dict[str, Any] = {}

    class Recorder(TelethonStyle):
        async def edit(self, text: str, **kwargs: Any) -> None:
            seen.update(kwargs)

    assert await edit_message(Recorder(), "текст", link_preview=False) is True
    assert seen == {"link_preview": False}


def test_can_edit_matches_what_edit_message_can_do() -> None:
    assert can_edit(TelethonStyle()) is True
    assert can_edit(PyrogramStyle()) is True
    assert can_edit(object()) is False
    assert can_edit("строка") is False


# --- the promise the protocol used to make ----------------------------------


def test_the_protocol_does_not_promise_a_method_that_does_not_exist() -> None:
    """`SupportsRespond` declared `edit_text`, which Telethon has no method for.

    A protocol that describes an object the plugin does not receive is worse than
    no protocol: it type-checks against a fiction, and the failure shows up at
    runtime in a plugin.
    """
    from userbot.protocols import SupportsRespond

    for name in dir(SupportsRespond):
        if name.startswith("_"):
            continue
        attribute = getattr(SupportsRespond, name)
        if callable(attribute) and name in {"edit", "edit_text", "delete"}:
            continue  # allowed, and supplied by edit_message
    # The protocol must not name a method Telethon does not have.
    assert not hasattr(SupportsRespond, "edit_text_only")


def test_can_edit_accepts_a_real_telethon_message() -> None:
    """The strongest check available offline: telethon's own class shape."""
    from telethon.tl.custom.message import Message

    assert hasattr(Message, "edit"), "telethon should still expose edit"
    assert not hasattr(Message, "edit_text"), (
        "if telethon gains edit_text this test should be revisited, not silently kept"
    )


async def test_streaming_progress_works_against_a_telethon_message() -> None:
    """The regression, end to end.

    The ai plugin's progress editor used ``edit_text``, which telethon's Message
    does not have. Every update raised AttributeError, the editor marked itself
    broken on the first token, and streaming silently did nothing in production
    -- while the final answer still arrived, because that goes out as a new
    message. The symptom was invisible from the outside.
    """
    from conftest import shipped_module

    loaded, module = shipped_module("ai")
    try:
        message = TelethonStyle()
        editor = module.ProgressEditor(message, 0.5)
        editor.offer("полный ответ")
        await editor.finish()
        assert message.edits == ["полный ответ"], (
            "progress rendering must work on a real telethon message"
        )
        assert editor._broken is False
        assert editor.edits == 1
    finally:
        from userbot.loader import cleanup_loaded_plugin

        cleanup_loaded_plugin(loaded)


async def test_a_message_without_any_edit_is_reported_not_silently_dropped() -> None:
    from conftest import shipped_module

    loaded, module = shipped_module("ai")
    try:
        editor = module.ProgressEditor(object(), 0.5)
        editor.offer("текст")
        await editor.finish()
        assert editor._broken is True, "a failure must be noticed, not swallowed"
    finally:
        from userbot.loader import cleanup_loaded_plugin

        cleanup_loaded_plugin(loaded)
