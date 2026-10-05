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
