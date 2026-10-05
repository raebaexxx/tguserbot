from __future__ import annotations

import asyncio

import pytest

from conftest import FakeClient, FakeEvent
from userbot.commands import MAX_MESSAGE_LENGTH, CommandDispatcher


def make_dispatcher() -> CommandDispatcher:
    return CommandDispatcher({1})


async def _noop(command) -> None:
    return None


# --- authorisation ---------------------------------------------------------


async def test_only_owner_commands_are_dispatched() -> None:
    dispatcher = make_dispatcher()
    calls: list[str] = []

    async def callback(command) -> None:
        calls.append(command.args)
        await command.respond(f"ok:{command.args}")

    dispatcher.register("test", callback, aliases=("check",))
    await dispatcher.handle_event(FakeEvent("/ub check value", sender_id=1))
    await dispatcher.handle_event(FakeEvent("/ub test value", sender_id=2))
    assert calls == ["value"]


async def test_anonymous_sender_is_rejected() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("test", _noop)
    event = FakeEvent("/ub test", sender_id=None)
    await dispatcher.handle_event(event)
    assert event.responses == []


def test_owner_set_is_copied_from_the_argument() -> None:
    original = {1}
    dispatcher = CommandDispatcher(original)
    dispatcher.add_owner(2)
    assert original == {1}
    assert dispatcher.owner_ids == {1, 2}


# --- parsing ---------------------------------------------------------------


async def test_unknown_command_is_reported_to_the_owner() -> None:
    dispatcher = make_dispatcher()
    event = FakeEvent("/ub nope")
    await dispatcher.handle_event(event)
    assert event.responses == ["Неизвестная команда. Отправьте /ub help"]


@pytest.mark.parametrize("raw", ["/ub   ", "/ub", "just a chat message", ""])
async def test_non_commands_are_silently_ignored(raw: str) -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("test", _noop)
    event = FakeEvent(raw)
    await dispatcher.handle_event(event)
    assert event.responses == []


async def test_command_text_inside_a_quoted_block_is_ignored() -> None:
    """A ``/ub`` line that is not the first thing in the message is not a command."""
    dispatcher = make_dispatcher()
    dispatcher.register("test", _noop)
    event = FakeEvent("смотри тут:\n/ub test")
    await dispatcher.handle_event(event)
    assert event.responses == []


async def test_command_must_be_the_first_token() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("test", _noop)
    event = FakeEvent("  /ub test")
    await dispatcher.handle_event(event)
    assert event.responses == []


async def test_long_arguments_are_truncated_to_the_telegram_limit() -> None:
    dispatcher = make_dispatcher()
    captured: list[str] = []

    async def callback(command) -> None:
        captured.append(command.args)

    dispatcher.register("long", callback)
    await dispatcher.handle_event(FakeEvent("/ub long " + "x" * (MAX_MESSAGE_LENGTH * 2)))
    assert len(captured[0]) == MAX_MESSAGE_LENGTH


def test_clipping_counts_utf16_units_not_code_points() -> None:
    """Telegram measures a message in UTF-16 code units.

    Every character outside the BMP — an emoji, most of them — is one code point
    in Python and two units in Telegram. ``len()`` therefore undercounted by
    exactly a factor of two for emoji-heavy text, and the reply went out over the
    limit and was refused by Telegram with MESSAGE_TOO_LONG: the plugin had
    "clipped" the text and the message still did not fit.
    """
    from userbot.commands import _clip

    def units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    # 4096 emoji is 4096 code points but 8192 Telegram units.
    emoji = "😀" * MAX_MESSAGE_LENGTH
    clipped = _clip(emoji)
    assert units(clipped) <= MAX_MESSAGE_LENGTH, units(clipped)
    assert clipped.endswith("…")

    # Cyrillic is BMP: one unit each, and it must not be clipped short.
    assert units(_clip("ы" * (MAX_MESSAGE_LENGTH - 1))) == MAX_MESSAGE_LENGTH - 1

    # A surrogate pair must not be split: half of an emoji is not text.
    for filler in ("a", "ы"):
        text = filler * MAX_MESSAGE_LENGTH + "😀" * 10
        result = _clip(text)
        assert "�" not in result, "the clip split a surrogate pair"
        assert units(result) <= MAX_MESSAGE_LENGTH


async def test_long_emoji_arguments_stay_within_the_telegram_limit() -> None:
    """The same defect through the dispatcher, where the limit is actually applied."""
    dispatcher = make_dispatcher()
    captured: list[str] = []

    async def callback(command) -> None:
        captured.append(command.args)

    dispatcher.register("long", callback)
    await dispatcher.handle_event(FakeEvent("/ub long " + "😀" * MAX_MESSAGE_LENGTH))
    assert len(captured[0].encode("utf-16-le")) // 2 <= MAX_MESSAGE_LENGTH


async def test_command_metadata_reaches_the_callback() -> None:
    dispatcher = make_dispatcher()
    seen: list[tuple[str, str, str]] = []

    async def callback(command) -> None:
        seen.append((command.name, command.args, command.raw))

    dispatcher.register("test", callback, aliases=("t",))
    raw = "/ub test hello world"
    await dispatcher.handle_event(FakeEvent(raw))
    assert seen == [("test", "hello world", raw)]


# --- registration ----------------------------------------------------------


def test_conflicting_alias_does_not_partially_register() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("first", _noop)
    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register("second", _noop, aliases=("first",))
    assert [item.name for item in dispatcher.commands()] == ["first"]


def test_duplicate_command_name_is_rejected() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("one", _noop)
    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register("one", _noop)


def test_core_commands_cannot_be_shadowed() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("plugin", _noop, plugin_name="core")
    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register("plugin", _noop, plugin_name="rogue")


@pytest.mark.parametrize("name", ["", "   ", "/", ".."])
def test_empty_command_name_is_rejected(name: str) -> None:
    dispatcher = make_dispatcher()
    with pytest.raises(ValueError, match="must not be empty"):
        dispatcher.register(name, _noop)


def test_non_callable_command_is_rejected() -> None:
    dispatcher = make_dispatcher()
    with pytest.raises(TypeError, match="must be callable"):
        dispatcher.register("x", 42)


def test_unregister_only_affects_the_owning_plugin() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("mine", _noop, plugin_name="alpha")
    dispatcher.unregister("mine", plugin_name="beta")
    assert [item.name for item in dispatcher.commands()] == ["mine"]
    dispatcher.unregister("mine", plugin_name="alpha")
    assert dispatcher.commands() == []


def test_unregister_removes_aliases_too() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("main", _noop, aliases=("m1", "m2"), plugin_name="alpha")
    dispatcher.unregister("main", plugin_name="alpha")
    assert dispatcher.commands() == []


def test_unregister_of_an_unknown_name_is_a_noop() -> None:
    dispatcher = make_dispatcher()
    dispatcher.unregister("absent", plugin_name="alpha")
    assert dispatcher.commands() == []


def test_alias_duplicate_and_self_alias_are_dropped() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("main", _noop, aliases=("other", "other", "main", "  "))
    assert {item.name for item in dispatcher.commands()} == {"main"}


def test_commands_are_sorted_and_deduplicated() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("zeta", _noop, aliases=("z",))
    dispatcher.register("alpha", _noop)
    assert [item.name for item in dispatcher.commands()] == ["alpha", "zeta"]


# --- help ------------------------------------------------------------------


def test_help_text_lists_unique_commands_with_help() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("one", _noop, help_text="первая", aliases=("uno",))
    dispatcher.register("two", _noop, help_text="вторая")
    text = dispatcher.help_text()
    assert text.startswith("Команды userbot:")
    assert "/ub one — первая" in text
    assert "/ub two — вторая" in text
    assert "/ub uno" not in text


def test_help_text_omits_commands_without_help() -> None:
    dispatcher = make_dispatcher()
    dispatcher.register("bare", _noop)
    assert dispatcher.help_text().endswith("/ub bare")


# --- client attachment -----------------------------------------------------


async def test_detach_client_removes_the_handler() -> None:
    client = FakeClient()
    dispatcher = make_dispatcher()
    dispatcher.attach_client(client)
    assert len(client.handlers) == 1
    dispatcher.attach_client(client)
    assert len(client.handlers) == 1
    dispatcher.detach_client()
    assert client.handlers == []


async def test_attach_client_requires_a_client() -> None:
    dispatcher = make_dispatcher()
    with pytest.raises(RuntimeError, match="client"):
        dispatcher.attach_client(None)


# --- reliability -----------------------------------------------------------


async def test_failing_callback_reports_a_generic_error() -> None:
    dispatcher = make_dispatcher()

    async def callback(command) -> None:
        raise ValueError("secret internal detail")

    dispatcher.register("boom", callback)
    event = FakeEvent("/ub boom")
    await dispatcher.handle_event(event)
    assert event.responses == ["Ошибка выполнения команды; подробности записаны в лог."]
    assert "secret internal detail" not in event.responses[0]


async def test_timeout_on_a_stuck_command_is_reported() -> None:
    dispatcher = make_dispatcher()

    async def callback(command) -> None:
        await asyncio.sleep(30)

    dispatcher.set_command_timeout(0.05)
    dispatcher.register("slow", callback)
    event = FakeEvent("/ub slow")
    await dispatcher.handle_event(event)
    assert event.responses
    assert "врем" in event.responses[0].lower()


async def test_command_cancellation_is_not_swallowed() -> None:
    dispatcher = make_dispatcher()
    started = asyncio.Event()

    async def callback(command) -> None:
        started.set()
        await asyncio.sleep(30)

    dispatcher.register("slow", callback)
    task = asyncio.create_task(dispatcher.handle_event(FakeEvent("/ub slow")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_duplicate_commands_are_coalesced_while_running() -> None:
    """A double tap must not run the same command twice concurrently."""
    dispatcher = make_dispatcher()
    runs = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def callback(command) -> None:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()

    dispatcher.register("slow", callback)
    first = asyncio.create_task(dispatcher.handle_event(FakeEvent("/ub slow")))
    await started.wait()
    second = asyncio.create_task(dispatcher.handle_event(FakeEvent("/ub slow")))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert runs == 1


async def test_rate_limit_blocks_a_flood_of_commands() -> None:
    dispatcher = make_dispatcher()
    dispatcher.set_cooldown(0.2)
    calls = 0

    async def callback(command) -> None:
        nonlocal calls
        calls += 1

    dispatcher.register("fast", callback)
    await dispatcher.handle_event(FakeEvent("/ub fast"))
    blocked = FakeEvent("/ub fast")
    await dispatcher.handle_event(blocked)
    assert calls == 1
    assert blocked.responses == ["Слишком часто. Повторите позже."]
    await asyncio.sleep(0.25)
    await dispatcher.handle_event(FakeEvent("/ub fast"))
    assert calls == 2


async def test_cooldown_is_tracked_per_command() -> None:
    dispatcher = make_dispatcher()
    dispatcher.set_cooldown(5.0)
    dispatcher.register("a", _noop)
    dispatcher.register("b", _noop)
    await dispatcher.handle_event(FakeEvent("/ub a"))
    blocked = FakeEvent("/ub b")
    await dispatcher.handle_event(blocked)
    assert blocked.responses == []


async def test_cooldown_is_tracked_per_sender() -> None:
    dispatcher = CommandDispatcher({1, 2})
    dispatcher.set_cooldown(5.0)
    dispatcher.register("a", _noop)
    await dispatcher.handle_event(FakeEvent("/ub a", sender_id=1))
    other = FakeEvent("/ub a", sender_id=2)
    await dispatcher.handle_event(other)
    assert other.responses == []


async def test_cooldown_map_is_bounded() -> None:
    dispatcher = make_dispatcher()
    dispatcher.set_cooldown(0.01)
    dispatcher.set_max_cooldown_entries(4)
    dispatcher.register("a", _noop)
    for index in range(50):
        await dispatcher.handle_event(FakeEvent("/ub a", sender_id=index + 1))
    assert dispatcher.cooldown_entry_count() <= 4
