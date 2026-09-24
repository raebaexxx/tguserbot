from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commands import CommandContext
from .config import Settings
from .health import HealthService
from .rate_limit import RateLimiter
from .storage import Storage
from .task_registry import TaskGroup


class Plugin:
    """Optional base class for plugin entry points.

    Plugins may implement any subset of the lifecycle methods. Keeping the
    methods optional makes small plugins pleasant to write while the manager
    still gives every plugin a deterministic lifecycle.
    """

    async def migrate(self, storage: Storage) -> None:
        return None

    async def setup(self, ctx: PluginContext) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@dataclass(slots=True)
class _CommandRegistration:
    name: str
    callback: Any
    help_text: str
    aliases: tuple[str, ...]


@dataclass(slots=True)
class _HandlerRegistration:
    callback: Any
    event: Any
    guarded: Any


class PluginContext:
    """Services and resource tracking exposed to one plugin instance."""

    def __init__(
        self,
        *,
        plugin_name: str,
        plugin_path: Path,
        client: Any,
        settings: Settings,
        storage: Storage,
        dispatcher: Any,
        rate_limiter: RateLimiter,
        health: HealthService,
        manager: Any,
        instance: Any,
        logger: logging.Logger,
    ):
        self.plugin_name = plugin_name
        self.path = plugin_path
        self.client = client
        self.settings = settings
        self.storage = storage
        self.dispatcher = dispatcher
        self.rate_limiter = rate_limiter
        self.health = health
        self.manager = manager
        self.instance = instance
        self.logger = logger
        self.tasks = TaskGroup()
        self._commands: list[_CommandRegistration] = []
        self._handlers: list[_HandlerRegistration] = []
        self._active_command_names: set[str] = set()
        self._active_handler_wrappers: list[tuple[Any, Any]] = []
        self._active = False

    @property
    def name(self) -> str:
        return self.plugin_name

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id in self.dispatcher.owner_ids

    def register_command(
        self,
        name: str,
        callback: Any,
        *,
        help_text: str = "",
        aliases: tuple[str, ...] = (),
    ) -> None:
        normalized = self._normalize_name(name)
        if not normalized:
            raise ValueError("Command name must not be empty")
        if not callable(callback):
            raise TypeError("Command callback must be callable")
        registration = _CommandRegistration(
            name=normalized,
            callback=callback,
            help_text=help_text,
            aliases=tuple(self._normalize_name(alias) for alias in aliases if alias),
        )
        self._commands.append(registration)
        if self._active:
            self._activate_command(registration)

    def register_handler(self, callback: Any, event: Any) -> None:
        if not callable(callback):
            raise TypeError("Event handler must be callable")
        guarded = self._guard(callback)
        registration = _HandlerRegistration(callback=callback, event=event, guarded=guarded)
        self._handlers.append(registration)
        if self._active:
            self._activate_handler(registration)

    def spawn(self, coroutine: Any, *, name: str | None = None) -> Any:
        return self.tasks.spawn(coroutine, name=name or f"{self.plugin_name}:background")

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower().lstrip("/.")

    async def prepare(self, schema_version: int) -> None:
        current_version = await self.storage.migration_version(self.plugin_name)
        if schema_version > current_version:
            await self.instance.migrate(self.storage)
            await self.storage.record_migration(self.plugin_name, schema_version)
        await self.instance.setup(self)
        await self.instance.start()

    def _guard(self, callback: Any) -> Any:
        async def guarded(event: Any) -> None:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("event handler failed")

        return guarded

    def _activate_command(self, registration: _CommandRegistration) -> None:
        self.dispatcher.register(
            registration.name,
            registration.callback,
            help_text=registration.help_text,
            aliases=registration.aliases,
            plugin_name=self.plugin_name,
        )
        self._active_command_names.add(registration.name)
        self._active_command_names.update(registration.aliases)

    def _activate_handler(self, registration: _HandlerRegistration) -> None:
        if self.client is None:
            raise RuntimeError("A Telegram client is required for event handlers")
        self.client.add_event_handler(registration.guarded, registration.event)
        self._active_handler_wrappers.append((registration.guarded, registration.event))

    async def activate(self) -> None:
        if self._active:
            return
        self._active = True
        try:
            for command_registration in self._commands:
                self._activate_command(command_registration)
            for handler_registration in self._handlers:
                self._activate_handler(handler_registration)
        except Exception:
            await self.deactivate(cancel_tasks=False)
            raise

    async def deactivate(self, *, cancel_tasks: bool = True) -> None:
        if self._active:
            for name in tuple(self._active_command_names):
                self.dispatcher.unregister(name, plugin_name=self.plugin_name)
            self._active_command_names.clear()
            for callback, event in tuple(self._active_handler_wrappers):
                if self.client is not None:
                    self.client.remove_event_handler(callback, event)
            self._active_handler_wrappers.clear()
            self._active = False
        if cancel_tasks:
            await self.tasks.cancel_all()

    async def shutdown(self) -> None:
        await self.deactivate(cancel_tasks=True)
        try:
            await self.instance.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("plugin stop hook failed")

    async def command(self, command: CommandContext) -> None:
        """Convenience hook for plugins that expose one command callback."""
        raise NotImplementedError
