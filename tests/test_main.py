"""Tests for the CLI entry point: argument parsing and exit codes."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from userbot import __main__ as cli
from userbot.gateway import GatewayError, SessionLockedError, SessionRevokedError


def make_env(tmp_path: Path) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text("TGUSERBOT_API_ID=1\nTGUSERBOT_API_HASH=hash\n", encoding="utf-8")
    return env_file


def test_parser_defaults_to_run() -> None:
    args = cli.build_parser().parse_args([])
    assert args.command == "run"
    assert args.root is None
    assert args.env_file is None


def test_parser_accepts_auth_with_paths() -> None:
    args = cli.build_parser().parse_args(
        ["auth", "--root", "/srv/app", "--env-file", "/etc/app.env"]
    )
    assert args.command == "auth"
    assert args.root == Path("/srv/app")
    assert args.env_file == Path("/etc/app.env")


def test_parser_rejects_an_unknown_command() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["frobnicate"])
    assert excinfo.value.code == 2


def test_main_reports_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["userbot", "run", "--root", str(tmp_path)])
    for key in ("TGUSERBOT_API_ID", "TGUSERBOT_API_HASH"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main() == 1


def test_main_reports_a_non_integer_api_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "not-a-number")
    monkeypatch.setattr(sys, "argv", ["userbot", "run", "--root", str(tmp_path)])
    assert cli.main() == 1


def test_main_runs_auth_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = make_env(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["userbot", "auth", "--root", str(tmp_path), "--env-file", str(env_file)]
    )

    async def fake_authenticate(settings: Any) -> None:
        assert settings.api_id == 1

    monkeypatch.setattr(cli, "_authenticate", fake_authenticate)
    assert cli.main() == 0
    assert capsys.readouterr().out == ""


def test_main_returns_zero_after_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = make_env(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["userbot", "run", "--root", str(tmp_path), "--env-file", str(env_file)]
    )

    async def fake_run(settings: Any) -> int:
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main() == 0


def test_main_returns_130_on_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = make_env(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["userbot", "run", "--root", str(tmp_path), "--env-file", str(env_file)]
    )

    async def fake_run(settings: Any) -> int:
        return 130

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main() == 130


def test_main_handles_a_bare_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = make_env(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["userbot", "run", "--root", str(tmp_path), "--env-file", str(env_file)]
    )

    def fake_run(settings: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main() == 130


async def test_run_reports_a_session_lock_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = make_env(tmp_path)
    settings = cli.Settings.from_env(tmp_path, env_file=env_file)

    async def boom(self: Any, **_: object) -> None:
        raise SessionLockedError("another process holds the session")

    monkeypatch.setattr(cli.UserbotApp, "run", boom)
    assert await cli._run(settings) == 4
    assert "another process" in capsys.readouterr().err


async def test_run_reports_a_revoked_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = make_env(tmp_path)
    settings = cli.Settings.from_env(tmp_path, env_file=env_file)

    async def boom(self: Any, **_: object) -> None:
        raise SessionRevokedError("session was invalidated")

    monkeypatch.setattr(cli.UserbotApp, "run", boom)
    assert await cli._run(settings) == 5
    assert "invalidated" in capsys.readouterr().err


async def test_run_shuts_down_cleanly_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C during startup must not exit 0 with live state."""
    env_file = make_env(tmp_path)
    settings = cli.Settings.from_env(tmp_path, env_file=env_file)
    state: dict[str, bool] = {"requested": False, "shutdown": False}

    class FakeApp:
        def __init__(self, _settings: Any) -> None:
            pass

        async def run(self) -> None:
            raise KeyboardInterrupt

        def request_stop(self) -> None:
            state["requested"] = True

        async def shutdown(self) -> None:
            state["shutdown"] = True

    monkeypatch.setattr(cli, "UserbotApp", FakeApp)
    assert await cli._run(settings) == 130
    assert state == {"requested": True, "shutdown": True}


async def test_authenticate_always_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = make_env(tmp_path)
    settings = cli.Settings.from_env(tmp_path, env_file=env_file)
    disconnected = False

    class FakeGateway:
        def __init__(self, _settings: Any) -> None:
            pass

        async def authenticate(self) -> Any:
            raise GatewayError("auth failed")

        async def disconnect(self) -> None:
            nonlocal disconnected
            disconnected = True

    monkeypatch.setattr(cli, "TelegramGateway", FakeGateway)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    with pytest.raises(GatewayError):
        await cli._authenticate(settings)
    assert disconnected


async def test_authenticate_prints_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = make_env(tmp_path)
    settings = cli.Settings.from_env(tmp_path, env_file=env_file)

    class FakeMe:
        id = 7
        username = "operator"

    class FakeGateway:
        def __init__(self, _settings: Any) -> None:
            pass

        async def authenticate(self) -> Any:
            return FakeMe()

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(cli, "TelegramGateway", FakeGateway)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    await cli._authenticate(settings)
    assert "Авторизация успешна: operator" in capsys.readouterr().out


def test_module_entry_point_exists() -> None:
    assert callable(cli.main)
    assert asyncio.iscoroutinefunction(cli._run)
