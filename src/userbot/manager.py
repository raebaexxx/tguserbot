from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .git_source import GitPluginSource
from .health import HealthService
from .loader import PluginLoadError, cleanup_loaded_plugin, load_plugin
from .logging import get_logger
from .plugin_api import PluginContext
from .storage import Storage


@dataclass(slots=True)
class PluginRuntime:
    manifest: Any
    path: Path
    source: str
    source_ref: str | None
    source_url: str | None
    source_subpath: str | None
    loaded: Any
    context: PluginContext

    @property
    def name(self) -> str:
        return self.manifest.name


class PluginManager:
    def __init__(
        self,
        *,
        settings: Settings,
        client: Any,
        storage: Storage,
        dispatcher: Any,
        rate_limiter: Any,
        health: HealthService,
    ):
        self.settings = settings
        self.client = client
        self.storage = storage
        self.dispatcher = dispatcher
        self.rate_limiter = rate_limiter
        self.health = health
        self.logger = get_logger("plugins")
        self._runtimes: dict[str, PluginRuntime] = {}
        self._disabled = set(settings.disabled_plugins)
        self._lock = asyncio.Lock()
        self._generation = 0
        self._git_source = GitPluginSource(settings)

    async def initialize_state(self) -> None:
        for state in await self.storage.plugin_states():
            if state.get("status") == "disabled" and state.get("name"):
                self._disabled.add(str(state["name"]))

    async def load_all_local(self) -> None:
        for name in self.discover_local_names():
            if name in self._disabled:
                await self._mark_disabled(name)
                continue
            try:
                await self.load_local(name)
            except Exception:
                self.logger.exception("Could not load local plugin %s", name)
        await self._load_persisted_git_plugins()

    async def _load_persisted_git_plugins(self) -> None:
        for state in await self.storage.plugin_states():
            name = state.get("name")
            if not name or state.get("source") != "git" or name in self._disabled:
                continue
            if str(name) in self.discover_local_names():
                self.logger.error(
                    "Git plugin %s conflicts with a local plugin and was not loaded",
                    name,
                )
                continue
            if state.get("status") not in {"active", "unloaded", "failed"}:
                continue
            source_ref = state.get("source_ref")
            source_url = state.get("source_url")
            if not source_ref or not source_url:
                continue
            path = self.settings.git_plugin_dir / str(name) / str(source_ref)
            if not path.is_dir():
                self.logger.error(
                    "Git plugin %s is not available at %s; use /ub plugin install to fetch it",
                    name,
                    path,
                )
                continue
            try:
                await self._load_path(
                    name=str(name),
                    path=path,
                    source="git",
                    source_ref=str(source_ref),
                    source_url=str(source_url),
                    source_subpath=state.get("source_subpath"),
                    force=True,
                )
            except Exception:
                self.logger.exception("Could not load persisted Git plugin %s", name)

    def discover_local_names(self) -> list[str]:
        if not self.settings.plugin_dir.is_dir():
            return []
        names = []
        for path in self.settings.plugin_dir.iterdir():
            if path.is_dir() and (path / "plugin.toml").is_file():
                names.append(path.name)
        return sorted(names)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def get_runtime(self, name: str) -> PluginRuntime | None:
        return self._runtimes.get(name)

    async def load_local(self, name: str, *, force: bool = False) -> PluginRuntime | None:
        if name in self._disabled:
            await self._mark_disabled(name)
            return None
        path = self.settings.plugin_dir / name
        return await self._load_path(
            name=name,
            path=path,
            source="local",
            source_ref=None,
            source_url=None,
            force=force,
        )

    async def reload_local(self, name: str) -> PluginRuntime | None:
        if name in self._disabled:
            return None
        return await self.load_local(name, force=True)

    async def _load_path(
        self,
        *,
        name: str,
        path: Path,
        source: str,
        source_ref: str | None,
        source_url: str | None,
        source_subpath: str | None = None,
        force: bool = False,
    ) -> PluginRuntime | None:
        if name in self._disabled:
            return None
        async with self._lock:
            old = self._runtimes.get(name)
            if old is not None and not force:
                return old
            self._generation += 1
            loaded = None
            context: PluginContext | None = None
            old_deactivated = False
            old_restored = False
            try:
                loaded = await asyncio.to_thread(load_plugin, path, name, self._generation)
                if loaded.manifest.api != "1":
                    raise PluginLoadError(f"Unsupported API version: {loaded.manifest.api}")
                context = PluginContext(
                    plugin_name=name,
                    plugin_path=path,
                    client=self.client,
                    settings=self.settings,
                    storage=self.storage,
                    dispatcher=self.dispatcher,
                    rate_limiter=self.rate_limiter,
                    health=self.health,
                    manager=self,
                    instance=loaded.instance,
                    logger=get_logger(f"plugin.{name}"),
                )
                await context.prepare(loaded.manifest.schema_version)
                if old is not None:
                    await old.context.deactivate(cancel_tasks=False)
                    old_deactivated = True
                try:
                    await context.activate()
                except Exception:
                    if old is not None and old_deactivated:
                        try:
                            await old.context.activate()
                            old_restored = True
                        except Exception:
                            self.logger.exception("Could not restore previous plugin %s", name)
                    raise
                if old is not None:
                    await old.context.shutdown()
                    cleanup_loaded_plugin(old.loaded)
                runtime = PluginRuntime(
                    manifest=loaded.manifest,
                    path=path,
                    source=source,
                    source_ref=source_ref,
                    source_url=source_url,
                    source_subpath=source_subpath,
                    loaded=loaded,
                    context=context,
                )
                self._runtimes[name] = runtime
                await self.storage.upsert_plugin_state(
                    name,
                    source,
                    "active",
                    source_ref=source_ref,
                    source_url=source_url,
                    source_subpath=source_subpath,
                    version=loaded.manifest.version,
                )
                if old is not None:
                    self.health.reload_count += 1
                self.health.mark_error(None)
                return runtime
            except Exception as exc:
                if context is not None:
                    try:
                        await context.shutdown()
                    except Exception:
                        self.logger.exception("Could not clean up failed plugin %s", name)
                if loaded is not None:
                    cleanup_loaded_plugin(loaded)
                if old is not None and old_deactivated and not old_restored:
                    try:
                        await old.context.activate()
                    except Exception:
                        self.logger.exception("Could not restore previous plugin %s", name)
                await self.storage.upsert_plugin_state(
                    name,
                    source,
                    "failed",
                    source_ref=source_ref,
                    source_url=source_url,
                    source_subpath=source_subpath,
                    version=None,
                    error=str(exc),
                )
                self.health.mark_error(f"plugin {name}: {exc}")
                raise

    async def unload(self, name: str) -> None:
        async with self._lock:
            await self._unload_locked(name, final_status="unloaded")

    async def _unload_locked(self, name: str, *, final_status: str) -> None:
        runtime = self._runtimes.get(name)
        if runtime is None:
            return
        try:
            await runtime.context.shutdown()
        except Exception:
            self.logger.exception("Error while unloading plugin %s", name)
        finally:
            cleanup_loaded_plugin(runtime.loaded)
            self._runtimes.pop(name, None)
        await self.storage.upsert_plugin_state(
            name,
            runtime.source,
            final_status,
            source_ref=runtime.source_ref,
            source_url=runtime.source_url,
            source_subpath=runtime.source_subpath,
            version=runtime.manifest.version,
        )

    async def unload_local(self, name: str) -> None:
        await self.unload(name)

    async def enable(self, name: str) -> PluginRuntime | None:
        self._disabled.discard(name)
        if name in self.discover_local_names():
            return await self.load_local(name, force=True)
        runtime = self._runtimes.get(name)
        if runtime is not None:
            return runtime
        state = await self.storage.get_plugin_state(name)
        if state and state.get("source") == "git" and state.get("source_ref"):
            path = self.settings.git_plugin_dir / name / str(state["source_ref"])
            if path.is_dir():
                return await self._load_path(
                    name=name,
                    path=path,
                    source="git",
                    source_ref=str(state["source_ref"]),
                    source_url=str(state.get("source_url")) if state.get("source_url") else None,
                    source_subpath=state.get("source_subpath"),
                    force=True,
                )
        raise PluginLoadError(f"Plugin {name!r} was not found")

    async def disable(self, name: str) -> None:
        self._disabled.add(name)
        await self.unload(name)
        await self._mark_disabled(name)

    async def _mark_disabled(self, name: str) -> None:
        state = await self.storage.get_plugin_state(name)
        await self.storage.upsert_plugin_state(
            name,
            str(state.get("source", "local")) if state else "local",
            "disabled",
            source_ref=state.get("source_ref") if state else None,
            source_url=state.get("source_url") if state else None,
            source_subpath=state.get("source_subpath") if state else None,
            version=state.get("version") if state else None,
            error=None,
        )

    async def list_plugins(self) -> list[dict[str, Any]]:
        states = {str(state["name"]): state for state in await self.storage.plugin_states()}
        names = set(self.discover_local_names()) | set(self._runtimes) | set(states)
        result: list[dict[str, Any]] = []
        for name in sorted(names):
            runtime = self._runtimes.get(name)
            state = states.get(name, {})
            if runtime is not None:
                result.append(
                    {
                        "name": name,
                        "status": "active",
                        "source": runtime.source,
                        "source_ref": runtime.source_ref,
                        "source_url": runtime.source_url,
                        "source_subpath": runtime.source_subpath,
                        "version": runtime.manifest.version,
                        "error": None,
                    }
                )
            else:
                result.append(
                    {
                        "name": name,
                        "status": (
                            "disabled" if name in self._disabled else state.get("status", "unknown")
                        ),
                        "source": state.get("source", "local"),
                        "source_ref": state.get("source_ref"),
                        "source_url": state.get("source_url"),
                        "source_subpath": state.get("source_subpath"),
                        "version": state.get("version"),
                        "error": state.get("error"),
                    }
                )
        return result

    def active_count(self) -> int:
        return len(self._runtimes)

    async def error_count(self) -> int:
        return sum(1 for item in await self.list_plugins() if item.get("status") == "failed")

    async def install_git(
        self,
        url: str,
        ref: str,
        *,
        subpath: str | None = None,
    ) -> PluginRuntime:
        package = await self._git_source.fetch(url=url, ref=ref, subpath=subpath)
        if package.name in self.discover_local_names() and self._runtimes.get(package.name) is None:
            raise PluginLoadError(f"Git plugin {package.name!r} conflicts with a local plugin")
        old = self._runtimes.get(package.name)
        if old is not None:
            await self.unload(package.name)
        try:
            runtime = await self._load_path(
                name=package.name,
                path=package.path,
                source="git",
                source_ref=package.commit,
                source_url=package.url,
                source_subpath=package.subpath,
                force=True,
            )
            if runtime is None:
                raise PluginLoadError(f"Plugin {package.name!r} was disabled")
            return runtime
        except Exception:
            if old is not None and old.path.exists():
                try:
                    await self._load_path(
                        name=old.name,
                        path=old.path,
                        source=old.source,
                        source_ref=old.source_ref,
                        source_url=old.source_url,
                        source_subpath=old.source_subpath,
                        force=True,
                    )
                except Exception:
                    self.logger.exception("Could not restore plugin %s after Git failure", old.name)
            raise

    async def update_git(self, name: str, ref: str | None = None) -> PluginRuntime:
        runtime = self._runtimes.get(name)
        if runtime is None or runtime.source != "git" or not runtime.source_url:
            raise PluginLoadError(f"Plugin {name!r} is not an active Git plugin")
        package = await self._git_source.fetch(
            url=runtime.source_url,
            ref=ref or "HEAD",
            subpath=runtime.source_subpath,
        )
        if package.name != name:
            raise PluginLoadError("Git repository manifest name changed during update")
        old = runtime
        await self.unload(name)
        try:
            updated = await self._load_path(
                name=name,
                path=package.path,
                source="git",
                source_ref=package.commit,
                source_url=runtime.source_url,
                source_subpath=package.subpath,
                force=True,
            )
            if updated is None:
                raise PluginLoadError(f"Plugin {name!r} was disabled")
            return updated
        except Exception:
            if old.path.exists():
                await self._load_path(
                    name=old.name,
                    path=old.path,
                    source=old.source,
                    source_ref=old.source_ref,
                    source_url=old.source_url,
                    source_subpath=old.source_subpath,
                    force=True,
                )
            raise

    async def shutdown(self) -> None:
        for name in tuple(self._runtimes):
            await self.unload(name)
