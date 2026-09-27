"""Tests for the application wiring: core commands, start/stop, and reconnection.

``app.py`` and ``gateway.py`` previously had no tests at all. Telethon is
replaced with an in-memory double so nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    OWNER_ID,
    FakeEvent,
    make_app,
    make_command_plugin,
    make_handler_plugin,
    write_plugin,
)
from userbot.app import SHUTDOWN_TIMEOUT, UserbotApp
from userbot.commands import parse_plugin_action
from userbot.config import Settings
from userbot.gateway import (
    GatewayError,
    SessionLockedError,
    SessionRevokedError,
    TelegramGateway,
    _try_lock,
)

# --- command argument parsing ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("list", ("list", None, None, None, None)),
        ("reload notes", ("reload", "notes", None, None, None)),
        ("enable a", ("enable", "a", None, None, None)),
        ("disable a b", ("disable", "a", None, None, None)),
        (
            "install https://x/y.git main",
            ("install", None, "https://x/y.git", "main", None),
        ),
        (
            "install https://x/y.git main plugins/notes",
            ("install", None, "https://x/y.git", "main", "plugins/notes"),
        ),
        ("update a", ("update", "a", None, None, None)),
        ("update a v2", ("update", "a", None, "v2", None)),
    ],
)
def test_parse_plugin_action(text: str, expected: tuple) -> None:
    parsed = parse_plugin_action(text)
    assert parsed is not None
    assert (
        parsed.action,
        parsed.name,
        parsed.ref,
        parsed.commit,
        parsed.subpath,
    ) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "frobnicate", "reload", "enable", "install", "update", "install url"],
)
def test_parse_plugin_action_rejects_unusable_input(text: str) -> None:
    assert parse_plugin_action(text) is None


def test_parse_plugin_action_rejects_a_multiword_subpath() -> None:
    assert parse_plugin_action("install url ref a b") is None


# --- application -----------------------------------------------------------


class FakeMe:
    def __init__(self) -> None:
        self.id = OWNER_ID
        self.username = "tester"


async def test_start_and_shutdown(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, gateway = make_app(app_settings)
    await app.start()
    try:
        assert app._started
        assert app.health.authorized
        assert app.health.telegram_connected
        assert app.dispatcher.owner_ids == {OWNER_ID, *app_settings.owner_ids}
        names = {item.name for item in app.dispatcher.commands()}
        assert {"help", "version", "plugins", "plugin"} <= names
    finally:
        await app.shutdown()
    assert not app._started
    assert gateway.disconnected
    assert not app.health.telegram_connected


async def test_start_is_idempotent(app_settings: Settings) -> None:
    app, gateway = make_app(app_settings)
    await app.start()
    try:
        await app.start()
        assert len([c for c in app.dispatcher.commands() if c.name == "help"]) == 1
    finally:
        await app.shutdown()


async def test_start_rejects_an_unauthorized_account(app_settings: Settings) -> None:
    app, gateway = make_app(app_settings)
    gateway.raise_on_connect = RuntimeError("Telegram session is not authorized")
    with pytest.raises(RuntimeError, match="not authorized"):
        await app.start()
    assert not app._started


async def test_start_rejects_a_missing_account_id(app_settings: Settings) -> None:
    app, gateway = make_app(app_settings)
    gateway.raise_on_connect = None
    gateway._me = object()
    with pytest.raises(RuntimeError, match="did not return the authorized account"):
        await app.start()


async def test_shutdown_after_a_failed_start_is_safe(app_settings: Settings) -> None:
    app, gateway = make_app(app_settings)
    gateway.raise_on_connect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await app.start()
    await app.shutdown()
    assert not app._started


async def test_shutdown_without_start_is_a_noop(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.shutdown()


async def test_watcher_can_be_disabled_by_configuration(tmp_path: Path, plugin_root: Path) -> None:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
        watch_enabled=False,
    )
    app, _gateway = make_app(settings)
    await app.start()
    try:
        assert app.watcher._task is None
        assert app.health.watcher_running is False
    finally:
        await app.shutdown()


async def test_heartbeat_file_is_written(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        await asyncio.sleep(0.05)
        assert app_settings.heartbeat_path.is_file()
        assert app.health.heartbeat_age_seconds() is not None
    finally:
        await app.shutdown()


async def test_world_readable_env_file_is_flagged(tmp_path: Path, plugin_root: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TGUSERBOT_API_ID=1\n", encoding="utf-8")
    env_file.chmod(0o644)
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    app, _gateway = make_app(settings)
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture()
    app.logger.addHandler(handler)
    try:
        app._warn_about_world_readable_secrets()
    finally:
        app.logger.removeHandler(handler)
    assert any("readable by other users" in message for message in records)


async def test_tight_env_file_permissions_are_not_flagged(
    tmp_path: Path, plugin_root: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TGUSERBOT_API_ID=1\n", encoding="utf-8")
    env_file.chmod(0o600)
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    app, _gateway = make_app(settings)
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture()
    app.logger.addHandler(handler)
    try:
        app._warn_about_world_readable_secrets()
    finally:
        app.logger.removeHandler(handler)
    assert records == []


async def test_connection_drop_is_reflected_in_health(app_settings: Settings) -> None:
    app, gateway = make_app(app_settings)
    await app.start()
    try:
        assert app.health.telegram_connected is True
        gateway.emit(False)
        assert app.health.telegram_connected is False
        assert app.health.last_error == "Telegram connection lost"
        gateway.emit(True)
        assert app.health.telegram_connected is True
    finally:
        await app.shutdown()


# --- core commands ---------------------------------------------------------


async def run_core(app: UserbotApp, text: str) -> FakeEvent:
    event = FakeEvent(text, sender_id=OWNER_ID)
    await app.dispatcher.handle_event(event)
    return event


async def test_help_command(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub help")
        assert event.responses
        assert "/ub help" in event.responses[0]
        assert "/ub version" in event.responses[0]
    finally:
        await app.shutdown()


async def test_version_command(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub version")
        assert event.responses
        assert "tguserbot" in event.responses[0]
        assert "Схема БД:" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugins_command_lists_loaded_plugins(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugins")
        assert event.responses
        assert "alpha: active" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugins_command_on_an_empty_installation(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugins")
        assert event.responses == ["Активных плагинов нет."]
    finally:
        await app.shutdown()


async def test_plugin_usage_is_printed_for_bad_input(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin nonsense")
        assert event.responses
        assert "Использование" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_list_alias(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin list")
        assert event.responses and "alpha" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_reload_of_a_local_plugin(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin reload alpha")
        assert event.responses == ["Плагин alpha перезагружен."]
    finally:
        await app.shutdown()


async def test_plugin_reload_of_a_disabled_plugin_is_not_reported_as_success(
    app_settings: Settings,
) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        await app.manager.disable("alpha")
        event = await run_core(app, "/ub plugin reload alpha")
        assert event.responses
        assert "выключен" in event.responses[0]
        assert "перезагружен" not in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_reload_of_an_unknown_plugin(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin reload nope")
        assert event.responses == ["Плагин nope не найден."]
    finally:
        await app.shutdown()


async def test_plugin_enable_disable_round_trip(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        assert (await run_core(app, "/ub plugin disable alpha")).responses == [
            "Плагин alpha выключен."
        ]
        assert (await run_core(app, "/ub plugin enable alpha")).responses == [
            "Плагин alpha включён."
        ]
    finally:
        await app.shutdown()


async def test_plugin_disable_of_an_unknown_plugin_reports_the_error(
    app_settings: Settings,
) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin disable typo")
        assert event.responses
        assert "Ошибка" in event.responses[0]
        assert "не найден" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_enable_blocked_by_environment_is_explained(
    app_settings: Settings,
) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        app.manager._env_disabled.add("alpha")
        event = await run_core(app, "/ub plugin enable alpha")
        assert event.responses
        assert "TGUSERBOT_DISABLED_PLUGINS" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_install_requires_a_ref(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin install https://example.com/r.git")
        assert event.responses
        assert "Использование" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_install_reports_a_rejected_url(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin install https://evil.example/r.git main")
        assert event.responses
        assert "Ошибка" in event.responses[0]
        assert "TGUSERBOT_GIT_ALLOWED_REPOS" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_update_requires_a_name(app_settings: Settings) -> None:
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin update")
        assert event.responses
        assert "Использование" in event.responses[0]
    finally:
        await app.shutdown()


async def test_plugin_update_of_a_local_plugin_reports_the_error(
    app_settings: Settings,
) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        event = await run_core(app, "/ub plugin update alpha")
        assert event.responses
        assert "не является активным Git-плагином" in event.responses[0]
    finally:
        await app.shutdown()


async def test_reload_releases_a_failed_plugin(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, _gateway = make_app(app_settings)
    await app.start()
    try:
        before = app.health.reload_count
        await app.manager.reload_local("alpha")
        assert app.health.reload_count == before + 1
    finally:
        await app.shutdown()


async def test_start_catches_up_after_plugins_are_loaded(app_settings: Settings) -> None:
    """Missed commands are replayed only once plugin handlers are attached."""
    write_plugin(app_settings.plugin_dir, "alpha", body=make_command_plugin("alpha"))
    app, gateway = make_app(app_settings)
    await app.start()
    try:
        assert gateway.catch_up_calls == 1
        assert app.manager.active_count() == 1
    finally:
        await app.shutdown()


async def test_start_survives_a_failing_catch_up(
    app_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, gateway = make_app(app_settings)

    async def boom() -> None:
        raise RuntimeError("catch-up failed")

    monkeypatch.setattr(gateway, "catch_up", boom)
    await app.start()
    try:
        assert app._started
    finally:
        await app.shutdown()


async def test_handlers_are_removed_on_shutdown(app_settings: Settings) -> None:
    write_plugin(app_settings.plugin_dir, "beta", body=make_handler_plugin(r"^ping$"))
    app, gateway = make_app(app_settings)
    await app.start()
    try:
        # One handler for the /ub dispatcher plus the plugin's own.
        assert len(gateway.client.handlers) == 2
    finally:
        await app.shutdown()
    assert gateway.client.handlers == []


# --- gateway ---------------------------------------------------------------


def make_gateway_settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )


def test_session_file_path(tmp_path: Path) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    assert gateway.session_file.name == "session.session"


def test_secure_session_permissions_covers_sidecars(tmp_path: Path) -> None:
    settings = make_gateway_settings(tmp_path)
    gateway = TelegramGateway(settings)
    gateway.session_file.parent.mkdir(parents=True, exist_ok=True)
    targets = [
        gateway.session_file,
        Path(f"{gateway.session_file}-journal"),
        Path(f"{gateway.session_file}-wal"),
    ]
    for path in targets:
        path.write_bytes(b"x")
        path.chmod(0o644)
    gateway._secure_session_permissions()
    for path in targets:
        assert path.stat().st_mode & 0o777 == 0o600, f"{path} was left readable"


def test_secure_session_permissions_tolerates_missing_files(tmp_path: Path) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    gateway._secure_session_permissions()


async def test_session_lock_is_exclusive(tmp_path: Path) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    await gateway.acquire_session_lock()
    try:
        other = TelegramGateway(make_gateway_settings(tmp_path))
        with pytest.raises(SessionLockedError, match="single-writer"):
            await other.acquire_session_lock()
    finally:
        gateway.release_session_lock()


async def test_session_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    await gateway.acquire_session_lock()
    gateway.release_session_lock()
    other = TelegramGateway(make_gateway_settings(tmp_path))
    await other.acquire_session_lock()
    other.release_session_lock()


def test_try_lock_reports_a_failure(tmp_path: Path) -> None:
    handle, error = _try_lock(tmp_path / "s.lock", timeout=0.01)
    assert error is None
    other, error = _try_lock(tmp_path / "s.lock", timeout=0.05)
    assert isinstance(error, SessionLockedError)
    other_handle = handle
    try:
        import fcntl

        fcntl.flock(other_handle.fileno(), fcntl.LOCK_UN)
        other_handle.close()
    except OSError:
        pass


async def test_connect_releases_the_lock_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    async def failing_connect(catch_up: bool = True) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(gateway.client, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="network down"):
        await gateway.connect()
    other = TelegramGateway(make_gateway_settings(tmp_path))
    await other.acquire_session_lock()
    other.release_session_lock()


async def test_connect_rejects_an_unauthorized_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    async def fake_connect(catch_up: bool = True) -> None:
        return None

    async def unauthorized() -> bool:
        return False

    monkeypatch.setattr(gateway.client, "connect", fake_connect)
    monkeypatch.setattr(gateway.client, "is_user_authorized", unauthorized)
    with pytest.raises(GatewayError, match="not authorized"):
        await gateway.connect()


async def test_connect_reports_a_revoked_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.errors import AuthKeyUnregisteredError

    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    async def fake_connect(catch_up: bool = True) -> None:
        return None

    async def unauthorized() -> bool:
        raise AuthKeyUnregisteredError(request=None)

    monkeypatch.setattr(gateway.client, "connect", fake_connect)
    monkeypatch.setattr(gateway.client, "is_user_authorized", unauthorized)
    with pytest.raises(SessionRevokedError, match="invalidated"):
        await gateway.connect()
    assert gateway._lock_file is None, "a revoked session must not keep the lock"


def test_every_telethon_call_site_matches_the_installed_library() -> None:
    """Guard against calling Telethon with keywords it does not accept.

    Passing ``catch_up=True`` to ``TelegramClient.connect()`` crashed the
    service on every start, and the unit tests missed it because they mocked
    the very methods whose signatures were wrong. Telethon is pinned exactly,
    so its signatures are stable and cheap to assert against -- do this
    whenever a Telethon call is added or changed.
    """
    import ast
    import inspect
    from pathlib import Path

    from telethon import TelegramClient

    source_root = Path(__file__).resolve().parent.parent / "src" / "userbot"
    # Methods we invoke on the client, mapped to the real signature.
    checked: dict[str, inspect.Signature] = {
        name: inspect.signature(getattr(TelegramClient, name))
        for name in (
            "connect",
            "disconnect",
            "is_connected",
            "is_user_authorized",
            "get_me",
            "start",
            "sign_in",
            "catch_up",
            "add_event_handler",
            "remove_event_handler",
        )
    }

    problems: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Attribute):
                continue
            if func.value.attr not in {"client", "self"}:
                continue
            if func.attr not in checked:
                continue
            signature = checked[func.attr]
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if accepts_kwargs:
                continue
            allowed = {
                parameter.name
                for parameter in signature.parameters.values()
                if parameter.kind
                not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                and parameter.name != "self"
            }
            for keyword in node.keywords:
                if keyword.arg is not None and keyword.arg not in allowed:
                    problems.append(
                        f"{path.name}:{node.lineno} client.{func.attr}() does not accept "
                        f"{keyword.arg!r}; it takes {sorted(allowed) or 'no arguments'}"
                    )
    assert not problems, "\n".join(problems)


async def test_connect_uses_the_real_telethon_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `TelegramClient.connect()` takes no arguments in Telethon 1.45.

    Passing ``catch_up=True`` there crashed the service on every start. The
    other tests mocked ``connect`` and so could not see it; this asserts our
    call site against the real library signature.
    """
    import inspect

    from telethon import TelegramClient

    signature = inspect.signature(TelegramClient.connect)
    accepted = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert accepted == ["self"], f"connect() signature changed to {signature}; re-check the call"

    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    seen: list[dict[str, object]] = []

    async def fake_connect(*args: object, **kwargs: object) -> None:
        seen.append(kwargs)
        if kwargs:
            raise TypeError(f"connect() got an unexpected keyword argument {list(kwargs)}")

    async def authorized() -> bool:
        return True

    class Me:
        id = 1

    async def get_me() -> Any:
        return Me()

    monkeypatch.setattr(gateway.client, "connect", fake_connect)
    monkeypatch.setattr(gateway.client, "is_user_authorized", authorized)
    monkeypatch.setattr(gateway.client, "get_me", get_me)
    me = await gateway.connect()
    assert me.id == 1
    assert seen == [{}], "connect() must be called without arguments"
    await gateway.disconnect()


def test_catch_up_is_enabled_on_the_client(tmp_path: Path) -> None:
    """Telethon defaults catch_up to False, which drops offline commands."""
    import inspect

    from telethon import TelegramClient

    parameter = inspect.signature(TelegramClient.__init__).parameters["catch_up"]
    assert parameter.default is False, "Telethon's default changed; re-check whether we need it"
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    assert gateway.client._catch_up is True, "the client must be built with catch_up=True"


async def test_explicit_catch_up_is_tolerant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catch-up failure must never stop the bot from coming up."""
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    calls: list[bool] = []

    async def fake_catch_up() -> None:
        calls.append(True)

    monkeypatch.setattr(gateway.client, "catch_up", fake_catch_up)
    await gateway.catch_up()
    assert calls == [True]

    async def boom() -> None:
        raise RuntimeError("catch-up failed")

    monkeypatch.setattr(gateway.client, "catch_up", boom)
    await gateway.catch_up()  # must not raise


async def test_monitor_connection_reports_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    states: list[bool] = []
    gateway.on_connection_state(lambda *, connected: states.append(connected))
    connected = [True, True, False, False, True]

    async def fake_sleep(_seconds: float) -> None:
        if len(states) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    state = {"index": 0}

    def is_connected() -> bool:
        value = connected[min(state["index"], len(connected) - 1)]
        state["index"] += 1
        return value

    monkeypatch.setattr(gateway.client, "is_connected", is_connected)
    with pytest.raises(asyncio.CancelledError):
        await gateway.monitor_connection(interval=0)
    assert states == [True, False, True]


async def test_monitor_survives_a_broken_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    def boom(*, connected: bool) -> None:
        raise RuntimeError("hook exploded")

    gateway.on_connection_state(boom)
    monkeypatch.setattr(gateway.client, "is_connected", lambda: True)

    async def stop_immediately(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_immediately)
    with pytest.raises(asyncio.CancelledError):
        await gateway.monitor_connection(interval=0)


async def test_authenticate_keeps_the_lock_until_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session is in use after auth, so the lock stays until disconnect()."""
    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    class Me:
        id = 5
        username = "operator"

    async def fake_start(phone: str | None = None) -> Any:
        return None

    async def get_me() -> Any:
        return Me()

    monkeypatch.setattr(gateway.client, "start", fake_start)
    monkeypatch.setattr(gateway.client, "get_me", get_me)
    me = await gateway.authenticate()
    assert me.username == "operator"
    assert gateway._lock_file is not None
    other = TelegramGateway(make_gateway_settings(tmp_path))
    with pytest.raises(SessionLockedError):
        await other.acquire_session_lock()
    await gateway.disconnect()
    assert gateway._lock_file is None
    await other.acquire_session_lock()
    other.release_session_lock()


async def test_authenticate_handles_two_factor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.errors import SessionPasswordNeededError

    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    signed_in: list[str] = []

    async def fake_start(phone: str | None = None) -> Any:
        raise SessionPasswordNeededError(request=None)

    async def fake_sign_in(password: str | None = None, **_: object) -> Any:
        signed_in.append(password or "")

        class Me:
            id = 5
            username = None

        return Me()

    async def get_me() -> Any:
        class Me:
            id = 5
            username = "twofactor"

        return Me()

    monkeypatch.setattr(gateway.client, "start", fake_start)
    monkeypatch.setattr(gateway.client, "sign_in", fake_sign_in)
    monkeypatch.setattr(gateway.client, "get_me", get_me)

    async def fake_ask() -> str:
        return "hunter2"

    monkeypatch.setattr(gateway, "_ask_password", fake_ask)
    me = await gateway.authenticate()
    assert me.username == "twofactor"
    assert signed_in == ["hunter2"]


async def test_authenticate_releases_the_lock_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = TelegramGateway(make_gateway_settings(tmp_path))

    async def fake_start(phone: str | None = None) -> Any:
        raise RuntimeError("network down")

    monkeypatch.setattr(gateway.client, "start", fake_start)
    with pytest.raises(RuntimeError, match="network down"):
        await gateway.authenticate()
    assert gateway._lock_file is None
    other = TelegramGateway(make_gateway_settings(tmp_path))
    await other.acquire_session_lock()
    other.release_session_lock()


def test_gateway_reports_an_unusable_data_directory(tmp_path: Path) -> None:
    settings = make_gateway_settings(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = Settings(
        root_dir=settings.root_dir,
        data_dir=blocker / "nested",
        plugin_dir=settings.plugin_dir,
        log_dir=settings.log_dir,
        api_id=1,
        api_hash="t",
    )
    with pytest.raises(GatewayError, match="Cannot create the data directory"):
        TelegramGateway(settings)


def test_bind_health_reflects_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from userbot.health import HealthService

    gateway = TelegramGateway(make_gateway_settings(tmp_path))
    health = HealthService()
    monkeypatch.setattr(gateway.client, "is_connected", lambda: True)
    gateway.bind_health(health)
    assert health.telegram_connected is True
    assert health.authorized is True
    monkeypatch.setattr(gateway.client, "is_connected", lambda: False)
    gateway.bind_health(health)
    assert health.telegram_connected is False


def test_shutdown_timeout_default_is_configured() -> None:
    assert 0 < SHUTDOWN_TIMEOUT < 120
