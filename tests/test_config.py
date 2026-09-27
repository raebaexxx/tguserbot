from __future__ import annotations

from pathlib import Path

import pytest

from userbot.config import Settings, parse_env_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("TGUSERBOT_"):
            monkeypatch.delenv(key, raising=False)


def test_reads_credentials_and_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "123")
    monkeypatch.setenv("TGUSERBOT_API_HASH", "secret")
    monkeypatch.setenv("TGUSERBOT_OWNER_IDS", "10, 11")
    monkeypatch.setenv("TGUSERBOT_DATA_DIR", "runtime")
    settings = Settings.from_env(tmp_path)
    settings.validate()
    assert settings.api_id == 123
    assert settings.api_hash == "secret"
    assert settings.owner_ids == frozenset({10, 11})
    assert settings.data_dir == (tmp_path / "runtime").resolve()
    assert settings.plugin_dir == (tmp_path / "plugins").resolve()
    assert settings.log_dir == (tmp_path / "runtime" / "logs").resolve()
    assert settings.session_path.name == "session"
    assert settings.database_path.name == "userbot.sqlite3"
    assert settings.git_plugin_dir == (tmp_path / "runtime" / "git-plugins").resolve()


def test_secret_fields_are_hidden_from_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "1")
    monkeypatch.setenv("TGUSERBOT_API_HASH", "super-secret")
    monkeypatch.setenv("TGUSERBOT_PHONE", "+10000000000")
    text = repr(Settings.from_env(tmp_path))
    assert "super-secret" not in text
    assert "+10000000000" not in text


def test_reads_an_explicit_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "TGUSERBOT_API_ID=456\nTGUSERBOT_API_HASH=secret\nTGUSERBOT_OWNER_IDS=99\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(tmp_path, env_file=env_file)
    assert settings.api_id == 456
    assert settings.owner_ids == frozenset({99})


def test_env_file_does_not_override_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "1")
    env_file = tmp_path / "service.env"
    env_file.write_text("TGUSERBOT_API_ID=456\n", encoding="utf-8")
    assert Settings.from_env(tmp_path, env_file=env_file).api_id == 1


def test_reading_an_env_file_does_not_mutate_process_environment(tmp_path: Path) -> None:
    """Regression: the parser must not leak values into ``os.environ``."""
    import os

    env_file = tmp_path / "service.env"
    env_file.write_text(
        "TGUSERBOT_API_ID=456\nTGUSERBOT_API_HASH=secret\nTGUSERBOT_OWNER_IDS=99\n",
        encoding="utf-8",
    )
    Settings.from_env(tmp_path, env_file=env_file)
    assert "TGUSERBOT_API_ID" not in os.environ
    assert "TGUSERBOT_API_HASH" not in os.environ
    assert "TGUSERBOT_OWNER_IDS" not in os.environ


def test_a_second_env_file_is_not_shadowed_by_the_first(tmp_path: Path) -> None:
    first = tmp_path / "a.env"
    first.write_text("TGUSERBOT_API_ID=111\nTGUSERBOT_API_HASH=hashA\n", encoding="utf-8")
    second = tmp_path / "b.env"
    second.write_text("TGUSERBOT_API_ID=222\nTGUSERBOT_API_HASH=hashB\n", encoding="utf-8")
    assert Settings.from_env(tmp_path, env_file=first).api_id == 111
    assert Settings.from_env(tmp_path, env_file=second).api_id == 222


def test_env_file_supports_export_quotes_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "export TGUSERBOT_API_ID='7'\n"
        'TGUSERBOT_API_HASH="hash with spaces"\n'
        "  TGUSERBOT_OWNER_IDS = 5,6 , 7 \n"
        "malformed-line-without-equals\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(tmp_path, env_file=env_file)
    assert settings.api_id == 7
    assert settings.api_hash == "hash with spaces"
    assert settings.owner_ids == frozenset({5, 6, 7})


def test_relative_env_file_is_resolved_against_root(tmp_path: Path) -> None:
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "s.env").write_text("TGUSERBOT_API_ID=8\n", encoding="utf-8")
    settings = Settings.from_env(tmp_path, env_file=Path("conf/s.env"))
    assert settings.api_id == 8


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert Settings.from_env(tmp_path, env_file=tmp_path / "absent.env").api_id == 0


def test_parse_env_file_returns_mapping(tmp_path: Path) -> None:
    env_file = tmp_path / "x.env"
    env_file.write_text("A=1\nB='two'\nexport C=3\n", encoding="utf-8")
    assert parse_env_file(env_file) == {"A": "1", "B": "two", "C": "3"}


def test_rejects_non_integer_api_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "not-a-number")
    with pytest.raises(RuntimeError, match="must be an integer"):
        Settings.from_env(tmp_path)


def test_rejects_invalid_owner_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_OWNER_IDS", "10, abc")
    with pytest.raises(RuntimeError, match="Invalid owner ID"):
        Settings.from_env(tmp_path)


def test_rejects_non_numeric_tuning_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_MIN_INTERVAL", "soon")
    with pytest.raises(RuntimeError, match="must be numbers"):
        Settings.from_env(tmp_path)


def test_validate_reports_missing_credentials(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="TGUSERBOT_API_ID"):
        Settings(
            root_dir=tmp_path,
            data_dir=tmp_path / "d",
            plugin_dir=tmp_path / "p",
            log_dir=tmp_path / "l",
            api_id=0,
            api_hash="x",
        ).validate()


def test_validate_reports_missing_api_hash(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="TGUSERBOT_API_HASH"):
        Settings(
            root_dir=tmp_path,
            data_dir=tmp_path / "d",
            plugin_dir=tmp_path / "p",
            log_dir=tmp_path / "l",
            api_id=1,
            api_hash="   ",
        ).validate()


def test_absolute_paths_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGUSERBOT_DATA_DIR", "/srv/tguserbot-data")
    settings = Settings.from_env(tmp_path)
    assert settings.data_dir == Path("/srv/tguserbot-data")


def test_tuning_values_are_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TGUSERBOT_FLOOD_THRESHOLD", "12.5")
    monkeypatch.setenv("TGUSERBOT_MIN_INTERVAL", "0.25")
    monkeypatch.setenv("TGUSERBOT_LOG_LEVEL", "debug")
    monkeypatch.setenv("TGUSERBOT_DISABLED_PLUGINS", "a, b ,a")
    monkeypatch.setenv("TGUSERBOT_GIT_ALLOWED_REPOS", "https://example.com/r.git,")
    settings = Settings.from_env(tmp_path)
    assert settings.flood_sleep_threshold == 12.5
    assert settings.min_request_interval == 0.25
    assert settings.log_level == "DEBUG"
    assert settings.disabled_plugins == frozenset({"a", "b"})
    assert settings.git_allowed_repositories == ("https://example.com/r.git",)


def test_root_comes_from_environment_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TGUSERBOT_ROOT", str(tmp_path))
    assert Settings.from_env().root_dir == tmp_path.resolve()


async def test_storage_round_trip_and_plugin_state(tmp_path: Path) -> None:
    from userbot.storage import Storage

    storage = Storage(tmp_path / "runtime.sqlite3")
    await storage.initialize()
    try:
        await storage.set_value("notes", "greeting", "hello")
        assert await storage.get_value("notes", "greeting") == "hello"
        assert await storage.get_value("notes", "absent") is None
        assert await storage.get_value("other", "greeting") is None
        await storage.set_value("notes", "greeting", "updated")
        assert await storage.get_value("notes", "greeting") == "updated"
        await storage.upsert_plugin_state("notes", "local", "active", version="0.1.0")
        state = await storage.get_plugin_state("notes")
        assert state is not None
        assert state["status"] == "active"
        assert state["version"] == "0.1.0"
        await storage.upsert_plugin_state("notes", "local", "failed", error="boom")
        state = await storage.get_plugin_state("notes")
        assert state is not None
        assert state["status"] == "failed"
        assert state["error"] == "boom"
    finally:
        await storage.close()


async def test_migration_version_tracking(tmp_path: Path) -> None:
    from userbot.storage import Storage

    storage = Storage(tmp_path / "m.sqlite3")
    await storage.initialize()
    try:
        assert await storage.migration_version("demo") == 0
        await storage.record_migration("demo", 1)
        await storage.record_migration("demo", 3)
        assert await storage.migration_version("demo") == 3
        assert await storage.migration_version("other") == 0
    finally:
        await storage.close()


async def test_storage_requires_initialization(tmp_path: Path) -> None:
    from userbot.storage import Storage

    storage = Storage(tmp_path / "u.sqlite3")
    with pytest.raises(RuntimeError, match="initialize"):
        await storage.execute("SELECT 1")


async def test_storage_initialize_is_idempotent(tmp_path: Path) -> None:
    from userbot.storage import Storage

    storage = Storage(tmp_path / "i.sqlite3")
    await storage.initialize()
    connection = storage._connection
    await storage.initialize()
    assert storage._connection is connection
    await storage.close()
    await storage.close()
    assert not storage.initialized


async def test_executemany_round_trip(tmp_path: Path) -> None:
    from userbot.storage import Storage

    storage = Storage(tmp_path / "many.sqlite3")
    await storage.initialize()
    try:
        await storage.execute("CREATE TABLE t (v TEXT)")
        await storage.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",)])
        rows = await storage.fetchall("SELECT v FROM t ORDER BY v")
        assert [row["v"] for row in rows] == ["a", "b"]
    finally:
        await storage.close()
