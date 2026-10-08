"""The three plugins that shipped with no tests at all: echo, notes, status.

Coverage reported `plugins/echo`, `plugins/notes` and `plugins/status` at 0%, and
the number was right: these were the only shipped plugins whose code no test ever
executed. That matters more than the percentage, because each is a place where a
wrong answer looks like a working one:

* `echo` answers with whatever it was given, so a regression in the clamp, the
  owner check or the usage text is indistinguishable from correct behaviour.
* `notes` is the one plugin that keeps user data, and its whole surface is "did the
  right note get stored, listed and deleted, in this chat".
* `status` is the command an operator runs *when something is wrong*, so a field it
  drops is a fact they cannot get.

Two things are deliberately not stubbed. Commands go through a real
``CommandDispatcher``, so what a plugin sees as ``args`` is what the dispatcher
really parses -- a hand-built ``CommandContext`` would have let every argument
test here be subtly wrong about where the command name ends. And `notes` runs over a
real ``PluginStorage``, because the questions that matter are about which rows exist:
a double that kept notes in a list would agree with a plugin that never wrote a
``WHERE`` clause.

Run with: pytest tests/test_shipped_small_plugins.py -q --no-cov
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeClient, FakeEvent, plugin_config, shipped_module
from userbot.commands import CommandDispatcher
from userbot.health import HealthService
from userbot.storage import PluginStorage


@pytest.fixture(scope="module")
def echo_module() -> Iterator[Any]:
    loaded, module = shipped_module("echo")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


@pytest.fixture(scope="module")
def notes_module() -> Iterator[Any]:
    loaded, module = shipped_module("notes")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


@pytest.fixture(scope="module")
def status_module() -> Iterator[Any]:
    loaded, module = shipped_module("status")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


OWNER = 1
STRANGER = 999


class ReplyEvent(FakeEvent):
    """A ``FakeEvent`` that records what a reply to it contained.

    Subclasses ``FakeEvent`` rather than replacing it, so ``CommandContext.respond``
    runs for real -- including its ``_clip`` -- and a clamp asserted here is a clamp
    that actually happened.
    """

    def __init__(self, raw_text: str, chat_id: int = 1, sender_id: int = OWNER) -> None:
        super().__init__(raw_text=raw_text, chat_id=chat_id, sender_id=sender_id)
        self.replies: list[str] = []

    async def respond(self, text: str | None = None, **kwargs: Any) -> None:
        if text is not None:
            self.replies.append(text)
        await super().respond(text, **kwargs)


class RecordingContext:
    """The slice of ``PluginContext`` a plugin sets itself up against.

    ``register_command`` is recorded rather than discarded, so the plugin's *own*
    setup hook decides the command name and callback -- the harness never guesses
    them. A plugin that registered under a different name, or with a different
    callback, would simply not be reachable here.
    """

    def __init__(self, logger: logging.Logger, **extra: Any) -> None:
        self.logger = logger
        self.commands: list[tuple[str, Any]] = []
        self.handlers: list[tuple[Any, Any]] = []
        for key, value in extra.items():
            setattr(self, key, value)

    def register_command(self, name: str, callback: Any, **_: Any) -> None:
        self.commands.append((name, callback))

    def register_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == OWNER


class Harness:
    """A plugin set up the way the manager does it, then driven by real messages."""

    def __init__(
        self,
        plugin: Any,
        context: Any,
        *,
        owners: set[int] | None = None,
        plugin_name: str = "test",
    ) -> None:
        self.plugin = plugin
        self.context = context
        self.dispatcher = CommandDispatcher(owners if owners is not None else {OWNER})
        self.client = FakeClient()
        for name, callback in context.commands:
            self.dispatcher.register(name, callback, help_text="", plugin_name=plugin_name)

    @property
    def handlers(self) -> list[tuple[Any, Any]]:
        return self.context.handlers

    async def send(self, raw_text: str, *, chat_id: int = 1, sender_id: int = OWNER) -> list[str]:
        """Type ``raw_text`` into the chat and return what came back."""
        event = ReplyEvent(raw_text, chat_id=chat_id, sender_id=sender_id)
        await self.dispatcher.handle_event(event)
        return event.replies


async def echo_harness(module: Any, owners: set[int] | None = None) -> Harness:
    plugin = module.Plugin()
    context = RecordingContext(logging.getLogger("test.echo"))
    await plugin.setup(context)
    return Harness(plugin, context, owners=owners, plugin_name="echo")


# --- echo -------------------------------------------------------------------


async def test_echo_repeats_the_text(echo_module: Any) -> None:
    harness = await echo_harness(echo_module)
    assert await harness.send("/ub echo привет") == ["привет"]


async def test_echo_without_arguments_asks_for_usage(echo_module: Any) -> None:
    """An empty ``/ub echo`` must not look like ``/ub echo ""``."""
    harness = await echo_harness(echo_module)
    replies = await harness.send("/ub echo")
    assert replies and "Использование" in replies[0]


async def test_echo_clamps_what_telegram_would_refuse(echo_module: Any) -> None:
    """The clamp is the plugin's own: Telegram rejects the message, not the caller.

    ``MAX_ECHO_LENGTH`` is read from the module rather than repeated, so a change to
    the limit cannot leave the test asserting the old number.
    """
    harness = await echo_harness(echo_module)
    replies = await harness.send("/ub echo " + "x" * 9000)
    assert len(replies[0]) == echo_module.MAX_ECHO_LENGTH


async def test_the_dispatcher_keeps_a_stranger_out(echo_module: Any) -> None:
    harness = await echo_harness(echo_module, owners={OWNER})
    assert await harness.send("/ub echo привет", sender_id=STRANGER) == []


async def test_the_ping_handler_answers_the_owner_only(echo_module: Any) -> None:
    """The check is on the handler as well as on the dispatcher.

    ``/ubping`` is a raw Telethon event, so it never passes through the dispatcher's
    owner check -- the plugin has to do it, and this is the only place that fact is
    pinned.
    """
    plugin = echo_module.Plugin()
    asked: list[int | None] = []

    def is_owner(sender_id: int | None) -> bool:
        asked.append(sender_id)
        return sender_id == OWNER

    plugin.ctx = SimpleNamespace(is_owner=is_owner)

    stranger = FakeEvent(sender_id=STRANGER)
    await plugin.ping(stranger)
    assert stranger.responses == [], "a stranger got an answer"

    friend = FakeEvent(sender_id=OWNER)
    await plugin.ping(friend)
    assert friend.responses == ["pong"]
    assert asked == [STRANGER, OWNER]


async def test_status_reports_update_liveness(status_module: Any) -> None:
    """The two facts that separate "idle" from "not being fed".

    A bot can look connected and answer nothing, and the difference shows up only
    here: an idle bot has a *growing* last-update age with a pts that moves when it
    does hear something, while a starved one has a stale age against a pts that
    stopped advancing. The outage this was added for produced neither number.
    """
    health = HealthService()
    health.set_telegram_state(connected=True, authorized=True)
    health.note_update()
    health.note_session_state(5666718, time.time() - 90)
    harness = await status_harness(status_module, health)
    report = (await harness.send("/ub status"))[0]
    assert "Апдейты: последний" in report
    assert "Session pts: 5666718" in report
    assert "состояние" in report


async def test_status_says_when_no_update_has_arrived(status_module: Any) -> None:
    """The cold-start case reads as its own fact, not as an empty field."""
    harness = await status_harness(status_module, HealthService())
    report = (await harness.send("/ub status"))[0]
    assert "не получено ни одного" in report
    assert "Session pts: неизвестен" in report


async def test_the_ping_handler_registers_a_pattern_that_matches(echo_module: Any) -> None:
    """The handler is registered against a real Telethon event builder.

    Asserting that ``register_handler`` was called with *something* would pass for a
    pattern that can never match; the built event is compiled and matched instead.
    """
    registered: list[tuple[Any, Any]] = []
    commands: dict[str, Any] = {}

    class Ctx:
        def register_command(self, name: str, callback: Any, **_: Any) -> None:
            commands[name] = callback

        def register_handler(self, callback: Any, event: Any) -> None:
            registered.append((callback, event))

        def is_owner(self, sender_id: int | None) -> bool:
            return True

    await echo_module.Plugin().setup(Ctx())
    assert "echo" in commands, "the echo command was never registered"

    _callback, event = registered[0]
    # `pattern` on a Telethon event builder is the compiled pattern's *match*
    # method, not the pattern object.
    assert callable(event.pattern)
    assert event.pattern("/ubping")
    assert event.pattern("/UBPING"), "the pattern claims to be case-insensitive"
    assert not event.pattern("/ubping now")
    assert not event.pattern("say /ubping")


# --- notes ------------------------------------------------------------------


async def build_notes(module: Any, tmp_path: Path, **config: Any) -> Harness:
    """The notes plugin over a real sandboxed database.

    A real ``PluginStorage``, because the questions that matter are about which rows
    exist: a double keeping notes in a list would agree with a plugin that never
    wrote a ``WHERE`` clause. The order is the manager's -- initialise the sandbox,
    run the plugin's own ``migrate``, then ``setup`` -- so the schema is created
    through the guarded connection a plugin gets in production.
    """
    plugin = module.Plugin()
    storage = PluginStorage(tmp_path / "notes.sqlite3", "notes")
    await storage.initialize()
    await plugin.migrate(storage)
    context = RecordingContext(
        logging.getLogger("test.notes"),
        storage=storage,
        config=plugin_config(config, "notes"),
    )
    await plugin.setup(context)
    harness = Harness(plugin, context, plugin_name="notes")
    harness.storage = storage  # type: ignore[attr-defined]
    return harness


async def close(harness: Harness) -> None:
    await harness.storage.close()  # type: ignore[attr-defined]


async def test_the_plugin_refuses_to_work_before_setup(notes_module: Any) -> None:
    """``storage`` before ``setup()`` is a ``RuntimeError``, not an ``AttributeError``.

    The guard exists so the failure names the mistake. A test that only ever calls
    ``setup`` would leave it unexercised, and it is the one line of this plugin that
    defends against being used in the wrong order.
    """
    plugin = notes_module.Plugin()
    with pytest.raises(RuntimeError, match="setup"):
        _ = plugin.storage


async def test_a_note_survives_a_round_trip(notes_module: Any, tmp_path: Path) -> None:
    harness = await build_notes(notes_module, tmp_path)
    try:
        added = await harness.send("/ub notes add купить хлеб")
        assert added[0].startswith("Заметка #")

        listed = await harness.send("/ub notes list")
        assert any("купить хлеб" in line for line in listed[0].splitlines())
    finally:
        await close(harness)


async def test_notes_are_kept_per_chat(notes_module: Any, tmp_path: Path) -> None:
    """A note stored in one chat must not appear in another's list.

    ``list`` and ``delete`` both filter on ``chat_id``. A dropped filter does not
    raise -- it quietly shows another chat's notes, which in a group is a disclosure.
    """
    harness = await build_notes(notes_module, tmp_path)
    try:
        await harness.send("/ub notes add моя заметка", chat_id=100)
        await harness.send("/ub notes add чужая заметка", chat_id=200)

        mine = await harness.send("/ub notes list", chat_id=100)
        assert "моя заметка" in mine[0]
        assert "чужая" not in mine[0]

        theirs = await harness.send("/ub notes list", chat_id=200)
        assert "чужая заметка" in theirs[0]
        assert "моя заметка" not in theirs[0]
    finally:
        await close(harness)


async def test_notes_list_is_empty_until_something_is_stored(
    notes_module: Any, tmp_path: Path
) -> None:
    """The empty case has its own message, not a bare header."""
    harness = await build_notes(notes_module, tmp_path)
    try:
        assert await harness.send("/ub notes list") == ["Заметок пока нет."]
    finally:
        await close(harness)


async def test_deleting_a_note_reports_what_actually_happened(
    notes_module: Any, tmp_path: Path
) -> None:
    """``Storage.execute`` returns affected rows, not ``lastrowid``.

    It used to return ``lastrowid``, which for a DELETE is a stale rowid left over
    from an unrelated INSERT -- so ``notes delete`` claimed success on a note it
    never touched. Driven through the plugin, because the bug was in what the plugin
    asked the storage and the reply it built from the answer.
    """
    harness = await build_notes(notes_module, tmp_path)
    try:
        added = await harness.send("/ub notes add первая")
        note_id = added[0].split("#")[1].split()[0]

        deleted = await harness.send(f"/ub notes delete {note_id}")
        assert f"#{note_id} удалена" in deleted[0]

        # And again: the second delete must now say it found nothing.
        assert await harness.send(f"/ub notes delete {note_id}") == [
            "Заметка не найдена в этом чате."
        ]
    finally:
        await close(harness)


async def test_deleting_in_another_chat_is_not_a_success(notes_module: Any, tmp_path: Path) -> None:
    harness = await build_notes(notes_module, tmp_path)
    try:
        await harness.send("/ub notes add моя", chat_id=100)
        assert await harness.send("/ub notes delete 1", chat_id=200) == [
            "Заметка не найдена в этом чате."
        ]
    finally:
        await close(harness)


async def test_a_note_body_is_capped(notes_module: Any, tmp_path: Path) -> None:
    harness = await build_notes(notes_module, tmp_path)
    try:
        await harness.send("/ub notes add " + "y" * 9000)
        listed = await harness.send("/ub notes list")
        # The stored body is capped, and list shows a preview of that.
        assert len(listed[0]) <= notes_module.PREVIEW_LENGTH + 40
    finally:
        await close(harness)


async def test_usage_and_unknown_subcommands(notes_module: Any, tmp_path: Path) -> None:
    harness = await build_notes(notes_module, tmp_path)
    try:
        usage = await harness.send("/ub notes")
        assert "/ub notes add" in usage[0]

        unknown = await harness.send("/ub notes frobnicate")
        assert "Неизвестная подкоманда" in unknown[0]

        assert await harness.send("/ub notes add") == ["Текст заметки не указан."]
        assert await harness.send("/ub notes delete abc") == ["ID заметки должен быть числом."]
    finally:
        await close(harness)


async def test_the_page_size_setting_reaches_the_query(notes_module: Any, tmp_path: Path) -> None:
    """The one setting this plugin has, and the value has to reach the query.

    ``test_plugin_settings.py`` proves every manifest key is read *somewhere*; this
    proves the number changes what the user sees, which is what the setting promises.
    """
    harness = await build_notes(notes_module, tmp_path, page_size=2)
    try:
        for index in range(5):
            await harness.send(f"/ub notes add заметка {index}")
        listed = await harness.send("/ub notes list")
        rows = [line for line in listed[0].splitlines() if line.startswith("#")]
        assert len(rows) == 2, rows
    finally:
        await close(harness)


# --- status -----------------------------------------------------------------


class ManagerDouble:
    """The slice of ``PluginManager`` the status plugin reads."""

    def __init__(self, active: int = 3, errors: int = 1) -> None:
        self._active = active
        self._errors = errors

    def active_count(self) -> int:
        return self._active

    async def error_count(self) -> int:
        return self._errors


async def status_harness(module: Any, health: HealthService, manager: Any = None) -> Harness:
    plugin = module.Plugin()
    context = RecordingContext(
        logging.getLogger("test.status"),
        health=health,
        manager=manager if manager is not None else ManagerDouble(),
    )
    await plugin.setup(context)
    return Harness(plugin, context, plugin_name="status")


async def test_status_reports_every_field_a_reader_needs(
    status_module: Any, tmp_path: Path
) -> None:
    """This is the command an operator runs when something is wrong.

    A dropped field is a fact they cannot get, and this reply is the only place it
    appears -- so the whole field list is asserted rather than spot-checked.
    """
    health = HealthService()
    health.heartbeat_path = tmp_path / "heartbeat"
    health.set_telegram_state(connected=True, authorized=True)
    health.watcher_running = True
    health.reload_count = 7
    health.mark_error("Telegram connection lost")

    harness = await status_harness(status_module, health)
    report = (await harness.send("/ub status"))[0]
    for expected in (
        "Uptime:",
        "Telegram: подключён",
        "Авторизация: да",
        "Watcher: работает",
        "Плагины: 3 активных, 1 с ошибками",
        "Reload: 7",
        "Последняя ошибка: Telegram connection lost",
    ):
        assert expected in report, f"{expected!r} missing from:\n{report}"


async def test_status_says_so_when_it_is_not_connected(status_module: Any) -> None:
    """The other half: a bot in trouble must not read as healthy.

    ``telegram_connected`` is the flag that used to be written once at startup and
    never again, so both spellings are load-bearing.
    """
    health = HealthService()
    health.set_telegram_state(connected=False, authorized=False)
    harness = await status_harness(status_module, health)
    report = (await harness.send("/ub status"))[0]
    assert "Telegram: не подключён" in report
    assert "Авторизация: нет" in report
    assert "Watcher: остановлен" in report


async def test_status_lists_each_plugin_error_with_its_age(status_module: Any) -> None:
    """Plugin errors are tracked per plugin, so the report has to name each one.

    The reason a load cannot erase another's error is only useful if the surviving
    error is still visible here.
    """
    health = HealthService()
    health.mark_error("cannot import name 'X'", plugin="sum")
    harness = await status_harness(status_module, health)
    report = (await harness.send("/ub status"))[0]
    assert "Ошибка sum (" in report
    assert "cannot import name 'X'" in report


async def test_status_counts_the_errors_it_is_told_about(status_module: Any) -> None:
    """The numbers come from the manager, and a zero has to read as zero."""
    manager = ManagerDouble(active=0, errors=0)
    harness = await status_harness(status_module, HealthService(), manager)
    report = (await harness.send("/ub status"))[0]
    assert "Плагины: 0 активных, 0 с ошибками" in report
