from __future__ import annotations

import asyncio
from pathlib import Path

from userbot.config import Settings
from userbot.storage import Storage


def test_settings_reads_credentials_and_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGUSERBOT_API_ID", "123")
    monkeypatch.setenv("TGUSERBOT_API_HASH", "secret")
    monkeypatch.setenv("TGUSERBOT_OWNER_IDS", "10, 11")
    monkeypatch.setenv("TGUSERBOT_DATA_DIR", "runtime")
    settings = Settings.from_env(tmp_path)
    settings.validate()
    assert settings.api_id == 123
    assert settings.owner_ids == frozenset({10, 11})
    assert settings.data_dir == (tmp_path / "runtime").resolve()
    assert settings.session_path.name == "session"


def test_storage_round_trip_and_plugin_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "runtime.sqlite3")
        await storage.initialize()
        note_id = await storage.execute(
            "INSERT INTO plugin_kv (plugin_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
            ("notes", "greeting", "hello", "now"),
        )
        assert note_id > 0
        assert await storage.get_value("notes", "greeting") == "hello"
        await storage.upsert_plugin_state("notes", "local", "active", version="0.1.0")
        state = await storage.get_plugin_state("notes")
        assert state is not None
        assert state["status"] == "active"
        await storage.close()

    asyncio.run(scenario())
