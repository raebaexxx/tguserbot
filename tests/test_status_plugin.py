"""The status plugin: the command an operator runs when something is wrong.

Coverage reported this plugin at 0%, and the number was right -- no test had ever
executed a line of it. That is worth more than the percentage, because a field this
reply drops is a fact the reader cannot get anywhere else, and a field that reads
wrong is worse than one that is absent: "подключён" on a bot that is not receiving
updates is what made the two-day outage invisible.

The plugin runs through a real ``CommandDispatcher`` rather than a hand-built
``CommandContext``, so what it sees as ``args`` is what the dispatcher really parses.
That is not ceremony: the first version of this file built the context by hand and
every argument test was subtly wrong about where the command name ends.

``echo`` and ``notes`` were tested here too and have since been removed from the
tree. What they were covering did not go with them: the ``Storage.execute``
regression they surfaced now has a direct test in ``tests/test_config.py``.

Run with: pytest tests/test_status_plugin.py -q --no-cov
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeEvent, shipped_module
from userbot.commands import CommandDispatcher
from userbot.health import HealthService


@pytest.fixture(scope="module")
def status_module() -> Iterator[Any]:
    loaded, module = shipped_module("status")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


OWNER = 1


class ReplyEvent(FakeEvent):
    """A ``FakeEvent`` that records what a reply to it contained.

    Subclasses ``FakeEvent`` rather than replacing it, so ``CommandContext.respond``
    runs for real -- including its ``_clip`` -- and what is asserted here is what was
    actually sent.
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
    them. A plugin that registered under another name would simply not be reachable.
    """

    def __init__(self, logger: logging.Logger, **extra: Any) -> None:
        self.logger = logger
        self.commands: list[tuple[str, Any]] = []
        for key, value in extra.items():
            setattr(self, key, value)

    def register_command(self, name: str, callback: Any, **_: Any) -> None:
        self.commands.append((name, callback))

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == OWNER


class ManagerDouble:
    """The slice of ``PluginManager`` the status plugin reads."""

    def __init__(self, active: int = 3, errors: int = 1) -> None:
        self._active = active
        self._errors = errors

    def active_count(self) -> int:
        return self._active

    async def error_count(self) -> int:
        return self._errors


async def report_for(module: Any, health: HealthService, manager: Any = None) -> str:
    """Set the plugin up and return what ``/ub status`` prints."""
    plugin = module.Plugin()
    context = RecordingContext(
        logging.getLogger("test.status"),
        health=health,
        manager=manager if manager is not None else ManagerDouble(),
    )
    await plugin.setup(context)
    dispatcher = CommandDispatcher({OWNER})
    for name, callback in context.commands:
        dispatcher.register(name, callback, help_text="", plugin_name="status")

    event = ReplyEvent("/ub status")
    await dispatcher.handle_event(event)
    assert event.replies, "/ub status answered nothing"
    return event.replies[0]


async def test_status_reports_every_field_a_reader_needs(
    status_module: Any, tmp_path: Path
) -> None:
    """The whole field list is asserted rather than spot-checked.

    A dropped field is invisible from the outside -- there is no error, the command
    just answers -- so nothing else in the suite would notice it going missing.
    """
    health = HealthService()
    health.heartbeat_path = tmp_path / "heartbeat"
    health.set_telegram_state(connected=True, authorized=True)
    health.watcher_running = True
    health.reload_count = 7
    health.mark_error("Telegram connection lost")

    report = await report_for(status_module, health)
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
    report = await report_for(status_module, health)
    assert "Telegram: не подключён" in report
    assert "Авторизация: нет" in report
    assert "Watcher: остановлен" in report


async def test_status_lists_each_plugin_error_with_its_age(status_module: Any) -> None:
    """Plugin errors are tracked per plugin, so the report has to name each one.

    The reason one load cannot erase another's error is only useful if the surviving
    error is still visible here.
    """
    health = HealthService()
    health.mark_error("cannot import name 'X'", plugin="sum")
    report = await report_for(status_module, health)
    assert "Ошибка sum (" in report
    assert "cannot import name 'X'" in report


async def test_status_counts_the_errors_it_is_told_about(status_module: Any) -> None:
    """The numbers come from the manager, and a zero has to read as zero."""
    report = await report_for(status_module, HealthService(), ManagerDouble(active=0, errors=0))
    assert "Плагины: 0 активных, 0 с ошибками" in report


async def test_status_reports_update_liveness(status_module: Any) -> None:
    """The two facts that separate "idle" from "not being fed".

    A bot can look connected and answer nothing, and the difference shows up only
    here: an idle bot has a *growing* last-update age with a ``pts`` that moves when
    it does hear something, while a starved one has a stale age against a ``pts``
    that stopped advancing. The outage this was added for produced neither number.
    """
    health = HealthService()
    health.set_telegram_state(connected=True, authorized=True)
    health.note_update()
    health.note_session_state(5666718, time.time() - 90)
    report = await report_for(status_module, health)
    assert "Апдейты: последний" in report
    assert "Session pts: 5666718" in report
    assert "состояние" in report


async def test_status_says_when_no_update_has_arrived(status_module: Any) -> None:
    """The cold-start case reads as its own fact, not as an empty field.

    "Unknown" and "none yet" are different claims, and on a bot that has just come
    up the difference is the whole answer.
    """
    report = await report_for(status_module, HealthService())
    assert "не получено ни одного" in report
    assert "Session pts: неизвестен" in report


async def test_status_prints_the_pts_when_only_it_is_known(status_module: Any) -> None:
    """The middle case: a ``pts`` with no timestamp beside it.

    ``note_session_state`` takes the two together, but the update watcher stamps the
    update count on every dispatch and only reads the session state on a timer -- so
    for the first interval after a start there is genuinely a pts and no date. It
    must read as the number alone, not as "unknown".
    """
    health = HealthService()
    health.set_telegram_state(connected=True, authorized=True)
    health.note_update()
    health.note_session_state(5666718, None)
    report = await report_for(status_module, health)
    assert "Session pts: 5666718" in report
    assert "неизвестен" not in report, "a known pts was reported as unknown"
