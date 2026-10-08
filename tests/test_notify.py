"""Tests for the systemd notification path.

The heartbeat file had a producer but no consumer, so it was documentation of an
intention. The unit is now ``Type=notify`` with ``WatchdogSec``, which means a
wedged process is restarted instead of looking healthy forever -- but only if the
process actually pings. That contract is tested here with a real socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
from pathlib import Path

import pytest

from conftest import make_app
from userbot.app import HEARTBEAT_INTERVAL
from userbot.notify import SystemdNotifier


@pytest.fixture
def notify_socket() -> tuple[str, socket.socket]:
    """A real datagram socket standing in for the systemd notify socket."""
    directory = tempfile.mkdtemp(prefix="notify-")
    path = str(Path(directory) / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2.0)
    return path, server


def drain(server: socket.socket) -> list[bytes]:
    messages: list[bytes] = []
    try:
        while True:
            messages.append(server.recv(4096))
    except (TimeoutError, OSError):
        pass
    return messages


def test_nothing_is_sent_outside_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    notifier = SystemdNotifier()
    assert notifier.ready() is False
    assert notifier.ping() is False
    assert notifier.stopping() is False
    assert notifier.reset() is False
    assert notifier.watchdog_enabled is False


def test_watchdog_ping_requires_the_watchdog_env(
    notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    notifier = SystemdNotifier()
    assert notifier.watchdog_enabled is False
    assert notifier.ping() is False, "no watchdog configured, nothing to feed"
    assert drain(server) == []


def test_watchdog_ping_is_sent_when_configured(
    notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    notifier = SystemdNotifier()
    assert notifier.watchdog_enabled is True
    assert notifier.ping() is True
    assert b"WATCHDOG=1" in drain(server)
    assert notifier.pings == 1


def test_ready_carries_a_status(
    notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    notifier = SystemdNotifier()
    assert notifier.ready("connected as @someone") is True
    messages = drain(server)
    assert any(b"READY=1" in message for message in messages)
    assert any(b"STATUS=connected as @someone" in message for message in messages)


def test_stopping_and_reset(
    notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    notifier = SystemdNotifier()
    assert notifier.stopping("unloading") is True
    assert notifier.reset() is True
    messages = drain(server)
    assert any(b"STOPPING=1" in message for message in messages)
    reset_messages = [m for m in messages if b"RESET=1" in m]
    assert reset_messages, "RESET=1 was not sent"
    # systemctl status keeps the last STATUS it was given, so a "recovered"
    # message here would stick around and misrepresent a healthy service.
    assert all(b"STATUS=" not in m for m in reset_messages), reset_messages


def test_a_broken_socket_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/notify.sock")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    notifier = SystemdNotifier()
    # Liveness reporting must never be the reason the bot goes down.
    assert notifier.ping() is False
    assert notifier.ready() is False
    assert notifier.stopping() is False


def test_heartbeat_interval_is_well_under_the_watchdog_period() -> None:
    """The unit sets WatchdogSec=60; pinging at 60 would be a coin flip."""
    unit = (Path(__file__).resolve().parent.parent / "deploy/systemd/tguserbot.service").read_text(
        encoding="utf-8"
    )
    assert "WatchdogSec=60" in unit
    assert "Type=notify" in unit
    assert HEARTBEAT_INTERVAL * 2 <= 60, f"pings every {HEARTBEAT_INTERVAL}s against WatchdogSec=60"


async def test_the_heartbeat_loop_pings_and_writes(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    app, _gateway = make_app(app_settings)  # type: ignore[arg-type]
    task = asyncio.create_task(app._heartbeat_loop())
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    assert app.settings.heartbeat_path.is_file()
    assert int(app.settings.heartbeat_path.read_text()) > 0
    assert any(b"WATCHDOG=1" in message for message in drain(server))


async def test_the_watchdog_stops_being_fed_when_telegram_is_gone_for_good(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecoverable disconnect must not look healthy forever.

    The ping was sent unconditionally, so an auth key invalidated server-side --
    which Telethon cannot reconnect from -- left the process feeding the watchdog
    while doing nothing at all: never restarted, never reporting, indistinguishable
    from a healthy bot in ``systemctl status``. Telethon exhausts its reconnection
    attempts and stays disconnected, so the process has to stop claiming liveness;
    the watchdog then expires and systemd restarts us, and the start path turns
    the revoked session into the error it is.
    """
    from userbot import app as app_module

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "DISCONNECT_GRACE", 0.03)
    app, _gateway = make_app(app_settings)  # type: ignore[arg-type]

    # Connected first: the pings below are what proves the stop is caused by the
    # disconnect and not by the loop never starting.
    app.health.set_telegram_state(connected=True, authorized=True)
    task = asyncio.create_task(app._heartbeat_loop())
    try:
        await asyncio.sleep(0.05)
        assert any(b"WATCHDOG=1" in message for message in drain(server)), "no ping while connected"

        # Inside the grace the watchdog is still fed on purpose, so the assertion
        # is about what happens once the grace has elapsed: two drains, and only
        # the later one must be free of pings.
        app.health.set_telegram_state(connected=False, authorized=False)
        await asyncio.sleep(0.15)
        during = drain(server)
        await asyncio.sleep(0.1)
        after = drain(server)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert not any(b"WATCHDOG=1" in message for message in after), (
        "the watchdog was still fed after the connection was gone for good"
    )
    assert any(b"STATUS=" in message for message in during), (
        "systemctl status must say why the watchdog stopped, or the restart is unexplained"
    )


async def test_a_dead_connection_monitor_stops_the_watchdog(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monitor that has died cannot vouch for the connection.

    ``monitor_connection`` is the only thing that ever writes
    ``health.telegram_connected``. Nothing observed that task: if it raised, or was
    cancelled by anything but shutdown, the flag kept its last value and the
    heartbeat kept feeding the watchdog on the strength of a reading nobody was
    taking any more. The unit stayed green and ``/ub status`` still said
    "подключён" while the Telegram connection state was simply unknown -- the same
    shape as the two-day outage, arrived at from the other side.

    Unknown must be treated as unusable: the state is marked unknown, the existing
    grace runs, and systemd restarts a process that cannot see its own connection.
    """
    from userbot import app as app_module

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "DISCONNECT_GRACE", 0.03)
    monkeypatch.setattr(app_module, "CONNECTION_MONITOR_RETRY", 0.01)
    app, _gateway = make_app(app_settings)  # type: ignore[arg-type]

    async def dying_monitor(interval: float = 5.0) -> None:
        raise RuntimeError("the poll loop died")

    # Healthy first, so the pings that stop are attributable to the monitor dying
    # rather than to the heartbeat never having started.
    app.health.set_telegram_state(connected=True, authorized=True)
    app.gateway.monitor_connection = dying_monitor
    task = asyncio.create_task(app._connection_monitor_supervisor())
    heartbeat = asyncio.create_task(app._heartbeat_loop())
    sent: list[bytes] = []
    try:
        await asyncio.sleep(0.05)
        sent += drain(server)
        assert any(b"WATCHDOG=1" in message for message in sent), "no ping while connected"
        # Long past the grace, so a ping here would be one the dead monitor is
        # still paying for.
        await asyncio.sleep(0.2)
        after = drain(server)
        sent += after
    finally:
        for running in (heartbeat, task):
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running

    assert not any(b"WATCHDOG=1" in message for message in after), (
        "the watchdog was still fed by a connection monitor that had died"
    )
    assert any(b"STATUS=" in message for message in sent), (
        "systemctl status must say the monitor died, or the restart is unexplained"
    )


async def test_the_connection_monitor_is_restarted_when_it_dies(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restarting is strictly better than stopping: the bot may well be fine.

    Telethon reconnecting under a poll loop is a normal thing to survive. So the
    supervisor brings the monitor back, and only the state it cannot vouch for
    until it reports again is withheld. The opposite choice -- treat a dead
    monitor as a restart and nothing else -- would restart a healthy bot once per
    unrelated hiccup in the poll loop.
    """
    from userbot import app as app_module

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "DISCONNECT_GRACE", 10.0)
    monkeypatch.setattr(app_module, "CONNECTION_MONITOR_RETRY", 0.01)
    app, gateway = make_app(app_settings)  # type: ignore[arg-type]

    starts: list[int] = []

    async def flaky_monitor(interval: float = 5.0) -> None:
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("died once")
        # The real loop reports the state on its first iteration, because it has
        # no previous reading to compare against. A double that skipped this would
        # agree with a supervisor that never recovers the connection.
        gateway.emit(True)
        await asyncio.sleep(3600)

    gateway.monitor_connection = flaky_monitor  # type: ignore[method-assign]
    # start() is what wires this hook up; registering it by hand keeps the test on
    # the real path (a loop reporting connected has to reach health) without
    # booting the whole app, whose gateway stub is not what is under test here.
    gateway.on_connection_state(app._on_connection_state)
    app.health.set_telegram_state(connected=True, authorized=True)
    task = asyncio.create_task(app._connection_monitor_supervisor())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert len(starts) >= 2, "the monitor was never brought back"
    assert app.health.telegram_connected is True, (
        "a healthy connection must be reported again once the monitor is back"
    )


async def test_start_supervises_the_connection_monitor(app_settings: object) -> None:
    """The wiring, not just the supervisor.

    A supervisor that nothing runs would leave the real task unwatched exactly as
    before, and the two tests above would still pass.
    """
    app, gateway = make_app(app_settings)  # type: ignore[arg-type]
    supervised: list[bool] = []
    original = app._connection_monitor_supervisor

    async def spy() -> None:
        supervised.append(True)
        await original()

    app._connection_monitor_supervisor = spy
    await app.start()
    try:
        # The task is created, not run, by start(); give it a turn so the assertion
        # is about the wiring rather than about scheduling.
        await asyncio.sleep(0)
        assert supervised == [True]
    finally:
        await app.shutdown()


async def test_a_brief_blip_keeps_the_watchdog_fed(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telethon reconnects by itself; a blip must not cost a restart.

    The opposite mistake: stopping the ping on the first disconnected poll would
    restart the bot every time the network hiccuped.
    """
    from userbot import app as app_module

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setattr(app_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(app_module, "DISCONNECT_GRACE", 10.0)
    app, _gateway = make_app(app_settings)  # type: ignore[arg-type]

    app.health.set_telegram_state(connected=True, authorized=True)
    task = asyncio.create_task(app._heartbeat_loop())
    try:
        app.health.set_telegram_state(connected=False, authorized=False)
        await asyncio.sleep(0.05)
        app.health.set_telegram_state(connected=True, authorized=True)
        await asyncio.sleep(0.05)
        messages = drain(server)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert any(b"WATCHDOG=1" in message for message in messages)


def test_the_disconnect_grace_outlasts_telethons_own_reconnection() -> None:
    """The grace must exceed the time Telethon itself spends trying to recover.

    Otherwise the process withholds the watchdog in the middle of a reconnect
    that was about to succeed, and the restart becomes self-inflicted. The budget
    is read from a real client's defaults rather than hard-coded, so a dependency
    bump that changes them fails here.
    """
    import inspect

    from telethon import TelegramClient

    from userbot import app as app_module

    parameters = inspect.signature(TelegramClient.__init__).parameters
    retries = int(parameters["connection_retries"].default)
    delay = float(parameters["retry_delay"].default)
    connect_timeout = float(parameters["timeout"].default)
    # Worst case: every attempt burns the connect timeout, then waits the delay.
    recovery_budget = retries * (connect_timeout + delay)

    assert app_module.DISCONNECT_GRACE > recovery_budget, (
        f"a {app_module.DISCONNECT_GRACE}s grace cannot outlast Telethon's "
        f"{recovery_budget:.0f}s reconnection budget"
    )


def test_start_sends_ready_when_systemd_is_present(
    app_settings: object, notify_socket: tuple[str, socket.socket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Type=notify blocks until READY=1, so start() must send it."""

    path, server = notify_socket
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    app, _gateway = make_app(app_settings)  # type: ignore[arg-type]

    async def scenario() -> None:
        await app.start()
        try:
            messages = drain(server)
            assert any(b"READY=1" in message for message in messages)
        finally:
            await app.shutdown()

    asyncio.run(scenario())
    assert any(b"STOPPING=1" in message for message in drain(server))
