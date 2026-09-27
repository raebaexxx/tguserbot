from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .git_source import GitPluginSource, GitSourceError
from .health import HealthService
from .loader import PluginLoadError, cleanup_loaded_plugin, load_plugin
from .logging import get_logger
from .plugin_api import PluginContext, validate_plugin_interface
from .storage import PluginStorage, Storage, create_plugin_storage
from .task_registry import run_uninterruptible

#: Default budget for the whole manager shutdown, independent of plugin count.
DEFAULT_SHUTDOWN_TIMEOUT = 25.0

#: How many installed Git revisions to keep per plugin.
GIT_KEEP_REVISIONS = 3


def _revision_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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
    storage: PluginStorage

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
        self._env_disabled = set(settings.disabled_plugins)
        self._lock = asyncio.Lock()
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._generation = 0
        self._shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT
        self._git_source = GitPluginSource(settings)

    # -- discovery ----------------------------------------------------------

    def discover_local_names(self) -> list[str]:
        """Names of loadable local plugins, from one ``iterdir`` pass.

        Deliberately uncached: a directory's mtime does not change when a file
        *inside* it is edited, so an mtime-keyed cache hid exactly the edits the
        watcher exists to notice. The real cost was ``error_count()`` rebuilding
        the whole listing, and that is fixed on its own.
        """
        return self._scan_local_names()

    def _scan_local_names(self) -> list[str]:
        plugin_dir = self.settings.plugin_dir
        if not plugin_dir.is_dir():
            return []
        try:
            entries = sorted(plugin_dir.iterdir())
        except OSError:
            self.logger.warning("cannot read the plugin directory %s", plugin_dir)
            return []
        return [path.name for path in entries if path.is_dir() and (path / "plugin.toml").is_file()]

    def _known_names(self) -> set[str]:
        return set(self._scan_local_names()) | set(self._runtimes)

    def has_plugin(self, name: str) -> bool:
        """Whether the manager knows about this plugin at all."""
        return name in self._known_names()

    # -- state --------------------------------------------------------------

    async def initialize_state(self) -> None:
        for state in await self.storage.plugin_states():
            if state.get("status") == "disabled" and state.get("name"):
                self._disabled.add(str(state["name"]))

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def get_runtime(self, name: str) -> PluginRuntime | None:
        return self._runtimes.get(name)

    def set_shutdown_timeout(self, seconds: float) -> None:
        self._shutdown_timeout = max(0.0, seconds)

    @contextlib.asynccontextmanager
    async def _operation(self, name: str):
        """Serialize operations per plugin and track in-flight work.

        The watcher waits on :meth:`wait_idle` before cancelling, which is what
        keeps a shutdown from interrupting a half-applied reload.
        """
        lock = self._operation_locks.setdefault(name, asyncio.Lock())
        async with lock:
            self._inflight += 1
            self._idle.clear()
            try:
                yield
            finally:
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()

    async def wait_idle(self, budget: float) -> bool:
        """Wait until no plugin operation is in flight."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=max(0.0, budget))
        except TimeoutError:
            return False
        return True

    # -- loading ------------------------------------------------------------

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
        local_names = set(self.discover_local_names())
        for state in await self.storage.plugin_states():
            name = state.get("name")
            if not name or state.get("source") != "git" or name in self._disabled:
                continue
            if str(name) in local_names:
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

    async def load_local(self, name: str, *, force: bool = False) -> PluginRuntime | None:
        if name in self._disabled:
            await self._mark_disabled(name)
            return None
        async with self._operation(name):
            return await self._load_path(
                name=name,
                path=self.settings.plugin_dir / name,
                source="local",
                source_ref=None,
                source_url=None,
                force=force,
            )

    async def reload_local(self, name: str) -> PluginRuntime | None:
        if name in self._disabled:
            return None
        async with self._operation(name):
            return await self._load_path(
                name=name,
                path=self.settings.plugin_dir / name,
                source="local",
                source_ref=None,
                source_url=None,
                force=True,
            )

    async def _build_context(
        self,
        *,
        name: str,
        path: Path,
        instance: Any,
        logger: Any,
    ) -> PluginContext:
        plugin_storage = await create_plugin_storage(self.settings.plugin_data_dir, name)
        return PluginContext(
            plugin_name=name,
            plugin_path=path,
            client=self.client,
            settings=self.settings,
            storage=plugin_storage,
            dispatcher=self.dispatcher,
            rate_limiter=self.rate_limiter,
            health=self.health,
            manager=self,
            instance=instance,
            logger=logger,
            core_storage=self.storage,
        )

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
                validate_plugin_interface(loaded.instance)
                context = await self._build_context(
                    name=name,
                    path=path,
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
                    storage=context.storage,
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
                self.health.mark_error(None, plugin=None)
                return runtime
            except asyncio.CancelledError:
                # The critical section above mutates global state (dispatcher
                # registrations, Telethon handlers, the runtime map). Cleanup
                # must finish even though we are being cancelled, otherwise the
                # new context stays live while the map still points at the old
                # one -- a zombie that survives shutdown.
                await run_uninterruptible(
                    self._rollback_load(
                        name=name,
                        loaded=loaded,
                        context=context,
                        old=old,
                        old_deactivated=old_deactivated,
                        old_restored=old_restored,
                    )
                )
                raise
            except Exception as exc:
                await run_uninterruptible(
                    self._rollback_load(
                        name=name,
                        loaded=loaded,
                        context=context,
                        old=old,
                        old_deactivated=old_deactivated,
                        old_restored=old_restored,
                    )
                )
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
                self.health.mark_error(f"plugin {name}: {exc}", plugin=name)
                raise

    async def _rollback_load(
        self,
        *,
        name: str,
        loaded: Any,
        context: PluginContext | None,
        old: PluginRuntime | None,
        old_deactivated: bool,
        old_restored: bool,
    ) -> None:
        """Undo a partially applied load so the manager stays consistent."""
        if context is not None:
            try:
                await context.shutdown()
            except Exception:
                self.logger.exception("Could not clean up failed plugin %s", name)
        if loaded is not None:
            cleanup_loaded_plugin(loaded)
        if old is not None and old_deactivated and not old_restored:
            if old in self._runtimes.values():
                try:
                    await old.context.activate()
                except Exception:
                    self.logger.exception("Could not restore previous plugin %s", name)

    # -- unloading ----------------------------------------------------------

    async def unload(self, name: str) -> None:
        async with self._operation(name):
            async with self._lock:
                await self._unload_locked(name, final_status="unloaded")

    async def _unload_locked(
        self,
        name: str,
        *,
        final_status: str,
        deadline: float | None = None,
    ) -> None:
        runtime = self._runtimes.get(name)
        if runtime is None:
            return
        self._runtimes.pop(name, None)
        budget = None
        if deadline is not None:
            budget = max(0.1, deadline - asyncio.get_running_loop().time())
        try:
            await runtime.context.shutdown(budget=budget)
        except Exception:
            self.logger.exception("Error while unloading plugin %s", name)
        finally:
            cleanup_loaded_plugin(runtime.loaded)
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
        if name in self._env_disabled:
            raise PluginLoadError(
                f"Плагин {name!r} выключен в TGUSERBOT_DISABLED_PLUGINS; "
                "уберите его оттуда и повторите"
            )
        self._disabled.discard(name)
        if name in self._known_names():
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
        self._disabled.add(name)
        raise PluginLoadError(f"Плагин {name!r} не найден")

    async def disable(self, name: str) -> None:
        if name not in self._known_names():
            state = await self.storage.get_plugin_state(name)
            if state is None:
                raise PluginLoadError(f"Плагин {name!r} не найден")
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

    # -- listing ------------------------------------------------------------

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

    def active_names(self) -> list[str]:
        return sorted(self._runtimes)

    async def error_count(self) -> int:
        """Count failed plugins without rebuilding the whole listing.

        ``list_plugins()`` walks the plugin directory and reads every state row;
        ``/ub status`` only needs a number.
        """
        failures = 0
        for state in await self.storage.plugin_states():
            name = str(state.get("name", ""))
            if not name or name in self._runtimes:
                continue
            if name in self._disabled:
                continue
            if state.get("status") == "failed":
                failures += 1
        return failures

    # -- git sources --------------------------------------------------------

    async def install_git(
        self,
        url: str,
        ref: str,
        *,
        subpath: str | None = None,
    ) -> PluginRuntime:
        async with self._lock:
            package = await self._git_source.fetch(url=url, ref=ref, subpath=subpath)
        local_names = set(self.discover_local_names())
        if package.name in local_names:
            raise PluginLoadError(
                f"Git-плагин {package.name!r} конфликтует с локальным плагином "
                "с таким же именем; переименуйте или удалите локальный"
            )
        old = self._runtimes.get(package.name)
        if old is not None:
            await self.unload(package.name)
        try:
            async with self._operation(package.name):
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
                raise PluginLoadError(f"Плагин {package.name!r} выключен")
            await self._prune_old_revisions(package.name, package.commit)
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
            raise PluginLoadError(f"Плагин {name!r} не является активным Git-плагином")
        package = await self._git_source.fetch(
            url=runtime.source_url,
            ref=ref or "HEAD",
            subpath=runtime.source_subpath,
        )
        if package.name != name:
            raise GitSourceError("Git repository manifest name changed during update")
        old = runtime
        await self.unload(name)
        try:
            async with self._operation(name):
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
                raise PluginLoadError(f"Плагин {name!r} выключен")
            await self._prune_old_revisions(name, package.commit)
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

    async def _prune_old_revisions(self, name: str, keep_commit: str) -> None:
        """Keep only the newest few fetched revisions of a Git plugin.

        Staged revisions are ordered by their on-disk mtime, which
        ``_stage_revision`` sets at fetch time. Sorting by directory name would
        order by commit SHA and therefore keep an arbitrary set.
        """
        parent = self.settings.git_plugin_dir / name
        try:
            revisions = [path for path in parent.iterdir() if path.is_dir()]
        except OSError:
            return
        if len(revisions) <= GIT_KEEP_REVISIONS:
            return
        ordered = sorted(revisions, key=_revision_mtime)
        for stale in ordered[: len(ordered) - GIT_KEEP_REVISIONS]:
            if stale.name == keep_commit:
                continue
            await asyncio.to_thread(self._remove_tree, stale)
            self.logger.info("pruned old revision %s of %s", stale.name, name)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    # -- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        names = tuple(self._runtimes)
        if not names:
            return
        self.logger.info("Unloading %d plugin(s)", len(names))
        # One shared deadline for the whole shutdown: plugins unload in parallel,
        # and a plugin that ignores cancellation can no longer push the process
        # past systemd's TimeoutStopSec.
        deadline = asyncio.get_running_loop().time() + self._shutdown_timeout
        results = await asyncio.gather(
            *(self._safe_unload(name, deadline) for name in names), return_exceptions=True
        )
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                self.logger.error("Could not unload plugin %s: %r", name, result)

    async def _safe_unload(self, name: str, deadline: float) -> None:
        try:
            async with self._operation(name):
                async with self._lock:
                    await self._unload_locked(name, final_status="unloaded", deadline=deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Error while unloading plugin %s", name)
