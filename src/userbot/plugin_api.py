from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .health import HealthService
from .plugin_config import PluginConfig
from .protocols import EventDispatcher, PluginHost, TelegramClientLike
from .rate_limit import RateLimiter
from .storage import PluginStorage, Storage
from .task_registry import TaskGroup

#: Lifecycle hooks a plugin may implement. ``setup`` receives the context.
LIFECYCLE_HOOKS = ("migrate", "setup", "start", "stop")

#: Wall-clock budgets for the lifecycle hooks, in seconds.
MIGRATE_TIMEOUT = 30.0
SETUP_TIMEOUT = 15.0
START_TIMEOUT = 15.0
STOP_TIMEOUT = 10.0
TASK_STOP_TIMEOUT = 10.0


class PluginContractError(TypeError):
    """The entrypoint does not implement the plugin lifecycle."""


class Plugin:
    """Optional base class for plugin entry points.

    Plugins may implement any subset of the lifecycle methods. Keeping the
    methods optional makes small plugins pleasant to write while the manager
    still gives every plugin a deterministic lifecycle.
    """

    async def migrate(self, storage: PluginStorage) -> None:
        return None

    async def setup(self, ctx: PluginContext) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def validate_plugin_interface(instance: Any) -> None:
    """Fail loudly and early when an entrypoint is not a usable plugin.

    Without this the manager would only discover the problem at load time, as an
    ``AttributeError`` from deep inside ``prepare()`` or ``shutdown()``.
    """
    if not hasattr(instance, "setup") or not callable(instance.setup):
        raise PluginContractError(
            f"{type(instance).__name__} must implement an awaitable setup(ctx) method"
        )
    for hook in LIFECYCLE_HOOKS:
        attribute = getattr(instance, hook, None)
        if attribute is None:
            raise PluginContractError(f"{type(instance).__name__} is missing the {hook}() hook")
        if not callable(attribute):
            raise PluginContractError(f"{type(instance).__name__}.{hook} is not callable")
        if not inspect.iscoroutinefunction(attribute):
            raise PluginContractError(
                f"{type(instance).__name__}.{hook} must be a coroutine function"
            )
    setup_signature = inspect.signature(instance.setup)
    positional = [
        parameter
        for parameter in setup_signature.parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(positional) != 1:
        raise PluginContractError(
            "setup(self, ctx) must accept exactly one positional argument, got "
            f"{type(instance).__name__}.setup{setup_signature}"
        )


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
    """Services and resource tracking exposed to one plugin instance.

    This is the whole surface a plugin is allowed to touch, so it is the place
    worth typing precisely: the collaborators are declared as protocols rather
    than ``Any``, which is what finally makes mypy check plugin code.
    """

    def __init__(
        self,
        *,
        plugin_name: str,
        plugin_path: Path,
        client: TelegramClientLike,
        settings: Any,
        storage: PluginStorage,
        dispatcher: EventDispatcher,
        rate_limiter: RateLimiter,
        health: HealthService,
        manager: PluginHost,
        instance: Any,
        logger: logging.Logger,
        core_storage: Storage | None = None,
        config: PluginConfig | None = None,
    ):
        self.plugin_name = plugin_name
        self.path = plugin_path
        self.client = client
        self.settings = settings
        #: Sandboxed per-plugin database. Never the core bookkeeping database.
        self.storage = storage
        #: Effective settings: the plugin's manifest defaults merged with the
        #: operator's overrides from ``plugin-config.toml``.
        self.config = config if config is not None else PluginConfig(plugin_name=plugin_name)
        self.dispatcher = dispatcher
        self.rate_limiter = rate_limiter
        self.health = health
        self.manager = manager
        self.instance = instance
        self.logger = logger
        self.tasks = TaskGroup(name=f"{plugin_name}:background")
        self._core_storage = core_storage
        self._commands: list[_CommandRegistration] = []
        self._handlers: list[_HandlerRegistration] = []
        self._active_command_names: set[str] = set()
        self._active_handler_wrappers: list[tuple[Any, Any]] = []
        self._active = False
        self._stopped = False
        self._sandbox_closed = False

    @property
    def name(self) -> str:
        return self.plugin_name

    def is_owner(self, sender_id: int | None) -> bool:
        return self.dispatcher.is_owner(sender_id)

    def register_command(
        self,
        name: str,
        callback: Any,
        *,
        help_text: str = "",
        aliases: tuple[str, ...] = (),
    ) -> None:
        if self._stopped:
            raise RuntimeError(f"Plugin {self.plugin_name!r} has already stopped")
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
        if self._stopped:
            raise RuntimeError(f"Plugin {self.plugin_name!r} has already stopped")
        if not callable(callback):
            raise TypeError("Event handler must be callable")
        guarded = self._guard(callback)
        registration = _HandlerRegistration(callback=callback, event=event, guarded=guarded)
        self._handlers.append(registration)
        if self._active:
            self._activate_handler(registration)

    def spawn(self, coroutine: Any, *, name: str | None = None) -> Any:
        if self._stopped:
            coroutine.close()
            raise RuntimeError(f"Plugin {self.plugin_name!r} has already stopped")
        return self.tasks.spawn(coroutine, name=name or f"{self.plugin_name}:background")

    @property
    def active(self) -> bool:
        return self._active

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower().lstrip("/.")

    async def prepare(self, schema_version: int) -> None:
        if self._core_storage is None:
            raise RuntimeError("PluginContext is missing the core storage handle")
        current_version = await self._core_storage.migration_version(self.plugin_name)
        if schema_version > current_version:
            # A cancelled migration must not be recorded as applied, so the
            # plugin's own code has to tolerate being re-run.
            await asyncio.wait_for(self.instance.migrate(self.storage), timeout=MIGRATE_TIMEOUT)
            await self._core_storage.record_migration(self.plugin_name, schema_version)
        await asyncio.wait_for(self.instance.setup(self), timeout=SETUP_TIMEOUT)
        await asyncio.wait_for(self.instance.start(), timeout=START_TIMEOUT)

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
        if self._stopped:
            raise RuntimeError(f"Plugin {self.plugin_name!r} has already stopped")
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
        """Unregister everything this context owns. Always safe to repeat.

        Deliberately independent of ``_stopped``: a shutdown interrupted part-way
        must still be able to take the registrations down, otherwise a later
        unload skips straight past deactivation and the command stays live.
        """
        if self._active:
            for name in tuple(self._active_command_names):
                self.dispatcher.unregister(name, plugin_name=self.plugin_name)
            self._active_command_names.clear()
            for callback, event in tuple(self._active_handler_wrappers):
                if self.client is not None:
                    try:
                        self.client.remove_event_handler(callback, event)
                    except Exception:
                        self.logger.exception("could not remove an event handler")
            self._active_handler_wrappers.clear()
            self._active = False
        if cancel_tasks:
            await self.tasks.cancel_all()

    async def shutdown(self, *, budget: float | None = None) -> None:
        """Deactivate, stop background work, run ``stop()`` and close the sandbox.

        Resumable by design: an interrupted call leaves the context re-activatable
        so a failed reload can put the previous generation back, and a later call
        still completes the deactivation. ``budget`` is the caller's remaining
        shutdown budget -- with it, one slow plugin can no longer push the process
        past systemd's ``TimeoutStopSec`` and get SIGKILLed.
        """
        await self.deactivate(cancel_tasks=False)
        if self._stopped:
            await self._close_sandbox()
            return
        task_budget = TASK_STOP_TIMEOUT if budget is None else max(0.05, budget * 0.5)
        stop_budget = STOP_TIMEOUT if budget is None else max(0.05, budget - task_budget)
        stopped = await self.tasks.cancel_all(timeout_seconds=task_budget)
        if not stopped:
            self.logger.error("plugin tasks did not stop within %.1fs", task_budget)
        try:
            await asyncio.wait_for(self.instance.stop(), timeout=stop_budget)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.logger.error("plugin stop hook timed out after %.1fs", stop_budget)
        except Exception:
            self.logger.exception("plugin stop hook failed")
        self._stopped = True
        await self._close_sandbox()

    async def _close_sandbox(self) -> None:
        if self._sandbox_closed or self.storage is None:
            return
        self._sandbox_closed = True
        try:
            await self.storage.close()
        except Exception:
            self.logger.exception("could not close the plugin database")
