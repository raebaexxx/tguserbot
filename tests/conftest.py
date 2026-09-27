"""Shared, hermetic test fixtures.

Tests never touch the repository's real ``plugins/`` directory: every test
builds its own plugin tree under ``tmp_path`` so the suite is independent of
the working directory and of how many demo plugins the repository ships.
"""

from __future__ import annotations

import shutil
import tempfile
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.health import HealthService
from userbot.manager import PluginManager
from userbot.rate_limit import RateLimiter
from userbot.storage import Storage

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PLUGINS_DIR = REPO_ROOT / "plugins"


def write_plugin(
    root: Path,
    name: str,
    *,
    manifest_extra: str = "",
    manifest_overrides: dict[str, str] | None = None,
    init_source: str = "",
    body: str = "",
) -> Path:
    """Create a minimal but loadable plugin package under ``root/name``."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    values: dict[str, str] = {
        "name": name,
        "version": "0.1.0",
        "api": "1",
        "entrypoint": "plugin:Plugin",
        "description": f"Test plugin {name}",
    }
    if manifest_overrides:
        values.update(manifest_overrides)
    manifest = "".join(f'{key} = "{value}"\n' for key, value in values.items())
    manifest += manifest_extra
    (directory / "plugin.toml").write_text(manifest, encoding="utf-8")
    (directory / "__init__.py").write_text(init_source, encoding="utf-8")
    (directory / "plugin.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


def make_noop_plugin(name: str) -> str:
    """Source for a plugin that registers nothing and has no side effects."""
    return """
        from userbot.plugin_api import Plugin as BasePlugin

        class Plugin(BasePlugin):
            async def setup(self, ctx):
                self.ctx = ctx
    """


def make_command_plugin(command: str = "demo") -> str:
    """Source for a plugin that registers one dispatcher command."""
    return f"""
        from userbot.plugin_api import Plugin as BasePlugin

        class Plugin(BasePlugin):
            async def setup(self, ctx):
                self.ctx = ctx
                ctx.register_command("{command}", self.handle, help_text="test")

            async def handle(self, command):
                await command.respond("handled")
    """


def make_handler_plugin(event_pattern: str = r"^ping$") -> str:
    """Source for a plugin that registers one Telethon event handler."""
    return f"""
        from telethon import events

        from userbot.plugin_api import Plugin as BasePlugin

        class Plugin(BasePlugin):
            async def setup(self, ctx):
                self.ctx = ctx
                ctx.register_handler(self.on_event, events.NewMessage(pattern=r"{event_pattern}"))

            async def on_event(self, event):
                return None
    """


def make_slow_stop_plugin(command: str = "demo", delay: float = 5.0) -> str:
    """Source for a plugin whose ``stop`` hook blocks, to widen race windows."""
    return f"""
        import asyncio

        from userbot.plugin_api import Plugin as BasePlugin

        class Plugin(BasePlugin):
            async def setup(self, ctx):
                self.ctx = ctx
                ctx.register_command("{command}", self.handle)

            async def handle(self, command):
                return None

            async def stop(self):
                await asyncio.sleep({delay})
    """


@dataclass
class FakeEvent:
    """Minimal stand-in for ``telethon.events.NewMessage.Event``."""

    raw_text: str = ""
    sender_id: int | None = 1
    chat_id: int = 1
    out: bool = False
    responses: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    edits: list[str] = field(default_factory=list)
    deleted: bool = False
    entities: Any = None

    async def respond(self, text: str | None = None, *, file: str | None = None, **_: Any) -> None:
        if file is not None:
            self.files.append(file)
        if text is not None:
            self.responses.append(text)

    async def reply(self, text: str | None = None, **kwargs: Any) -> None:
        await self.respond(text, **kwargs)

    async def edit_text(self, text: str, **_: Any) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeClient:
    """Records Telethon event-handler registrations with identity semantics."""

    def __init__(self) -> None:
        self.handlers: list[tuple[Any, Any]] = []
        self.connected = True

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any | None = None) -> int:
        before = len(self.handlers)
        if event is None:
            self.handlers = [item for item in self.handlers if item[0] is not callback]
        else:
            self.handlers = [
                item for item in self.handlers if not (item[0] is callback and item[1] is event)
            ]
        return before - len(self.handlers)

    def is_connected(self) -> bool:
        return self.connected


class RecordingStorage(Storage):
    """Storage subclass that also records executed statements."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.statements: list[str] = []


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def settings(tmp_path: Path, plugin_root: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test-hash",
    )


@pytest.fixture
async def storage(settings: Settings) -> Any:
    store = Storage(settings.database_path)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def dispatcher() -> CommandDispatcher:
    return CommandDispatcher({1})


@pytest.fixture
def health() -> HealthService:
    return HealthService()


@pytest.fixture
async def manager(
    settings: Settings,
    storage: Storage,
    client: FakeClient,
    dispatcher: CommandDispatcher,
    health: HealthService,
) -> Any:
    instance = PluginManager(
        settings=settings,
        client=client,
        storage=storage,
        dispatcher=dispatcher,
        rate_limiter=RateLimiter(min_interval=0),
        health=health,
    )
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.fixture
def real_plugin_dir() -> Iterator[Path]:
    """Copy the repository's real plugins into an isolated tree.

    Only the few tests that genuinely need to exercise shipped plugins use this;
    they still never mutate the repository checkout.
    """
    target = Path(tempfile.mkdtemp(prefix="tguserbot-plugins-"))
    shutil.copytree(REAL_PLUGINS_DIR, target / "plugins")
    try:
        yield target / "plugins"
    finally:
        shutil.rmtree(target, ignore_errors=True)


PluginFactory = Callable[..., Path]
