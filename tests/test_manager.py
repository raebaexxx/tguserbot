from __future__ import annotations

import asyncio
from pathlib import Path

from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.health import HealthService
from userbot.manager import PluginManager
from userbot.rate_limit import RateLimiter
from userbot.storage import Storage


class FakeEvent:
    def __init__(self, text: str, sender_id: int = 1) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.responses: list[str] = []

    async def respond(self, text: str, **kwargs) -> None:
        self.responses.append(text)


class FakeClient:
    def __init__(self) -> None:
        self.handlers: list[tuple[object, object]] = []

    def add_event_handler(self, callback, event) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback, event=None) -> None:
        self.handlers = [item for item in self.handlers if item[0] is not callback]


def make_manager(tmp_path: Path) -> tuple[PluginManager, Storage, FakeClient, CommandDispatcher]:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=Path("plugins").resolve(),
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    storage = Storage(settings.database_path)
    client = FakeClient()
    dispatcher = CommandDispatcher({1})
    manager = PluginManager(
        settings=settings,
        client=client,
        storage=storage,
        dispatcher=dispatcher,
        rate_limiter=RateLimiter(min_interval=0),
        health=HealthService(),
    )
    return manager, storage, client, dispatcher


def test_manager_load_reload_disable_enable() -> None:
    async def scenario(tmp_path: Path) -> None:
        manager, storage, client, dispatcher = make_manager(tmp_path)
        await storage.initialize()
        await manager.load_all_local()
        assert manager.active_count() == 4
        assert {item["name"] for item in await manager.list_plugins()} == {
            "notes",
            "status",
            "echo",
            "tiktok",
        }
        assert len(client.handlers) == 2
        assert {item.name for item in dispatcher.commands()} == {
            "notes",
            "status",
            "echo",
            "tiktok",
        }

        await manager.reload_local("echo")
        assert len(client.handlers) == 2
        await manager.disable("echo")
        assert len(client.handlers) == 1
        assert "echo" not in {item.name for item in dispatcher.commands()}
        await manager.enable("echo")
        assert len(client.handlers) == 2
        assert "echo" in {item.name for item in dispatcher.commands()}

        await manager.shutdown()
        await storage.close()

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory)))


def test_demo_commands_are_dispatched() -> None:
    async def scenario(tmp_path: Path) -> None:
        manager, storage, client, dispatcher = make_manager(tmp_path)
        await storage.initialize()
        await manager.load_all_local()
        event = FakeEvent("/ub echo hello")
        await dispatcher.handle_event(event)
        assert event.responses == ["hello"]

        note_event = FakeEvent("/ub add-note первая заметка")
        await dispatcher.handle_event(note_event)
        assert note_event.responses == ["Неизвестная команда. Отправьте /ub help"]

        note_event = FakeEvent("/ub notes add первая заметка")
        await dispatcher.handle_event(note_event)
        assert note_event.responses and note_event.responses[0].startswith("Заметка #")

        list_event = FakeEvent("/ub notes list")
        await dispatcher.handle_event(list_event)
        assert list_event.responses and "первая заметка" in list_event.responses[0]

        await manager.shutdown()
        await storage.close()

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory)))


def test_failed_reload_keeps_previous_plugin(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        plugin = plugin_dir / "demo"
        plugin.mkdir()
        (plugin / "plugin.toml").write_text(
            'name = "demo"\nversion = "0.1.0"\napi = "1"\nentrypoint = "plugin:Plugin"\n',
            encoding="utf-8",
        )
        (plugin / "__init__.py").write_text("", encoding="utf-8")
        (plugin / "plugin.py").write_text(
            "from userbot.plugin_api import Plugin\nclass Plugin(Plugin):\n    pass\n",
            encoding="utf-8",
        )
        settings = Settings(
            root_dir=tmp_path,
            data_dir=tmp_path / "data",
            plugin_dir=plugin_dir,
            log_dir=tmp_path / "data" / "logs",
            api_id=1,
            api_hash="test",
        )
        storage = Storage(settings.database_path)
        await storage.initialize()
        client = FakeClient()
        dispatcher = CommandDispatcher({1})
        manager = PluginManager(
            settings=settings,
            client=client,
            storage=storage,
            dispatcher=dispatcher,
            rate_limiter=RateLimiter(min_interval=0),
            health=HealthService(),
        )
        await manager.load_local("demo")
        assert manager.active_count() == 1
        (plugin / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        try:
            await manager.reload_local("demo")
        except Exception:
            pass
        else:
            raise AssertionError("Expected reload failure")
        assert manager.active_count() == 1
        assert len(client.handlers) == 0
        await manager.shutdown()
        await storage.close()

    asyncio.run(scenario())
