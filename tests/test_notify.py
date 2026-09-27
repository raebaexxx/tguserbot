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
    assert any(b"RESET=1" in message for message in messages)


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
