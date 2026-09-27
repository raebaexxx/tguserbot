"""Shared, hermetic test fixtures.

Tests never touch the repository's real ``plugins/`` directory: every test
builds its own plugin tree under ``tmp_path`` so the suite is independent of
the working directory and of how many demo plugins the repository ships.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.health import HealthService
from userbot.manager import PluginManager
from userbot.plugin_config import PluginConfig
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
class FakeContext:
    """The slice of ``PluginContext`` that plugins actually touch."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        config: PluginConfig | None = None,
    ) -> None:
        self.logger = logging.getLogger("test.plugin")
        self.rate_limiter = RateLimiter(min_interval=0)
        self.client = SimpleNamespace()
        self.commands: list[str] = []
        self.handlers: list[object] = []
        self.settings = settings
        self.config = config if config is not None else PluginConfig(plugin_name="test")
        #: Plugins that never touch the database leave this alone; the ones that
        #: do replace it. Typed loosely because a test's fake need not be.
        self.storage: Any = None

    def register_command(self, name: str, callback: Any, **kwargs: Any) -> None:
        self.commands.append(name)

    def register_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == 1

    def spawn(self, coroutine: Any, *, name: str | None = None) -> Any:
        return asyncio.ensure_future(coroutine)


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


class _AsyncList:
    """Async iteration over a list, with telethon's ``limit`` semantics.

    Telethon's ``iter_messages`` is an async generator yielding newest first, and
    a plugin that reads a chat depends on both facts.
    """

    def __init__(self, items: list[Any], limit: Any = None) -> None:
        self._items = list(items)
        self._limit = limit

    async def _walk(self) -> Any:
        for index, item in enumerate(self._items):
            if self._limit is not None and index >= int(self._limit):
                return
            yield item

    def __aiter__(self) -> Any:
        return self._walk()


class FakeClient:
    """Stands in for the Telethon client, with the surface plugins actually use."""

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.downloaded: list[Any] = []
        self.entities: dict[Any, Any] = {}
        self.handlers: list[tuple[Any, Any]] = []
        self.connected = True

    async def get_input_entity(self, entity: Any) -> Any:
        return self.entities.get(entity, entity)

    def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
        """Newest first, like telethon, and asynchronously as the code uses it."""
        return _AsyncList(list(reversed(self.messages)), limit=kwargs.get("limit"))

    async def aiter(self, entity: Any, **kwargs: Any) -> Any:
        async for message in self.iter_messages(entity, **kwargs):
            yield message

    def download_media(self, message: Any, **kwargs: Any) -> Any:
        self.downloaded.append(message)
        return kwargs.get("file")

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any | None = None) -> int:
        """Identity-based, so a test can tell two equal callbacks apart."""
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


@pytest.fixture
def app_settings(tmp_path: Path, plugin_root: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )


# --- the application under test -------------------------------------------

OWNER_ID = 42  # the id the stub gateway reports as the logged-in account


class FakeMe:
    def __init__(self) -> None:
        self.id = OWNER_ID
        self.username = "tester"


class StubGateway:
    """Stands in for ``TelegramGateway`` without touching the network."""

    def __init__(self, settings: Settings, *, me: Any = None) -> None:
        self.settings = settings
        self.client = FakeClient()
        self._me = FakeMe() if me is None else me
        self.connected = False
        self.disconnected = False
        self.raise_on_connect: BaseException | None = None
        self.hooks: list[Any] = []
        self.catch_up_calls = 0

    async def catch_up(self) -> None:
        self.catch_up_calls += 1

    async def connect(self) -> Any:
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.connected = True
        return self._me

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    def on_connection_state(self, hook: Any) -> None:
        self.hooks.append(hook)

    async def monitor_connection(self, interval: float = 5.0) -> None:
        await asyncio.sleep(3600)

    def emit(self, connected: bool) -> None:
        for hook in self.hooks:
            hook(connected=connected)


def make_app(settings: Settings) -> tuple[Any, StubGateway]:
    """Build a ``UserbotApp`` whose gateway is a stub."""
    from userbot.app import UserbotApp

    app = UserbotApp(settings)
    gateway = StubGateway(settings)
    app.gateway = gateway  # type: ignore[assignment]
    app.manager.client = gateway.client
    return app, gateway


def load_shipped_plugin(name: str, generation: int = 1) -> Any:
    """Load a plugin straight from the repository's ``plugins/`` directory.

    Tests that assert on shipped code (option mappings, manifests) read the real
    files rather than a copy, so a local edit cannot be masked by a stale
    fixture. Callers must call ``cleanup_loaded_plugin`` when done.
    """
    from userbot.loader import load_plugin

    return load_plugin(REAL_PLUGINS_DIR / name, name, generation)


def shipped_module(name: str, generation: int = 1) -> tuple[Any, Any]:
    """Return ``(LoadedPlugin, module)`` for a shipped plugin."""
    loaded = load_shipped_plugin(name, generation)
    import sys

    return loaded, sys.modules[f"{loaded.module.__name__}.plugin"]


def shipped_submodule(name: str, submodule: str, generation: int = 1) -> tuple[Any, Any]:
    """Load a shipped plugin and return one of its private submodules.

    A plugin that splits its logic into ``_client.py``-style modules deserves the
    same coverage as the entry point, and importing it through the real loader is
    what proves the relative import works at runtime rather than only in tests.
    """
    import importlib

    loaded = load_shipped_plugin(name, generation)
    module = importlib.import_module(f"{loaded.module.__name__}.{submodule}")
    return loaded, module


def plugin_config(values: dict[str, Any] | None = None, name: str = "test") -> PluginConfig:
    """A ``PluginConfig`` carrying real values.

    ``PluginConfig`` is frozen, so a test that needs an override has to build a
    new one rather than assign into the mapping.
    """
    return PluginConfig(plugin_name=name, values=dict(values or {}))


PluginFactory = Callable[..., Path]


class RouterFor:
    """A ``ModelRouter`` stand-in over a plain client double.

    The plugins hold a router rather than a client, because quota fallback
    belongs between them. Rather than rewrite every double into a router, this
    forwards to one and reports a key so ``require_router`` accepts it.
    """

    def __init__(self, client: Any, *, has_key: bool | None = None) -> None:
        self._client = client
        #: Overridable so a test can simulate a missing key without a real one.
        self._has_key = has_key

    @property
    def has_key(self) -> bool:
        if self._has_key is not None:
            return self._has_key
        return bool(getattr(self._client, "has_key", True))

    @has_key.setter
    def has_key(self, value: bool) -> None:
        self._has_key = value

    @property
    def active(self) -> str:
        return "test-model"

    @property
    def degraded(self) -> bool:
        return False

    def notice(self) -> str:
        return ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def aclose(self) -> None:
        closer = getattr(self._client, "aclose", None)
        if closer is not None:
            await closer()


def routers_for(module: Any, client: Any) -> dict[str, Any]:
    """One stand-in router per role, as the ai plugin now holds.

    A single shared double is deliberate: these tests are about delivery and
    configuration, not about which role routes where. ``test_gemini_wiring``
    covers the routing itself with the real thing.
    """
    roles = getattr(module, "ROLES", ("chat",))
    return {kind: RouterFor(client) for kind in roles}
