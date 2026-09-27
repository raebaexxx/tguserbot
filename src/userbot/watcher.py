from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Files that never affect the loaded code.
IGNORED_NAMES = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".mypy_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log", ".sqlite3", ".session"}


@dataclass(frozen=True, slots=True)
class _Entry:
    """Cheap change signal plus, only when needed, the content digest."""

    signature: tuple[tuple[str, int, int], ...]
    digest: str | None = None


class PluginWatcher:
    """Polling watcher for local plugin source changes.

    Two properties matter here. First, cost: the scan reads file *contents*, so
    it only hashes when a cheap ``(size, mtime)`` signature moved -- otherwise a
    24/7 deployment re-read every plugin file every tick. Second, safety:
    ``stop()`` quiesces instead of cancelling, because a reload interrupted
    mid-transaction is exactly what used to strand a live context.
    """

    def __init__(
        self,
        manager: Any,
        plugin_dir: Path,
        *,
        interval: float = 2.0,
        debounce: float = 0.5,
    ):
        self.manager = manager
        self.plugin_dir = plugin_dir
        self.interval = max(0.1, interval)
        self.debounce = max(0.0, debounce)
        self._task: asyncio.Task[None] | None = None
        self._snapshot: dict[str, _Entry] = {}
        self._stopping = asyncio.Event()
        self._scan_count = 0
        self._reload_count = 0

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @property
    def reload_count(self) -> int:
        return self._reload_count

    def _iter_files(self, plugin_path: Path):
        for source_path in sorted(plugin_path.rglob("*")):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(plugin_path)
            if any(part in IGNORED_NAMES for part in relative.parts):
                continue
            if source_path.suffix.lower() in IGNORED_SUFFIXES:
                continue
            yield relative, source_path

    def _signature(self, plugin_path: Path) -> tuple[tuple[str, int, int], ...]:
        signature: list[tuple[str, int, int]] = []
        for relative, source_path in self._iter_files(plugin_path):
            try:
                stat = source_path.stat()
            except OSError:
                continue
            signature.append((relative.as_posix(), stat.st_size, stat.st_mtime_ns))
        return tuple(signature)

    def _digest(self, plugin_path: Path) -> str:
        digest = hashlib.sha256()
        for relative, source_path in self._iter_files(plugin_path):
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(source_path.read_bytes())
            except OSError:
                continue
            digest.update(b"\0")
        return digest.hexdigest()

    def _scan(self) -> dict[str, _Entry]:
        """Return per-plugin entries, hashing only where the signature changed."""
        if not self.plugin_dir.is_dir():
            return {}
        try:
            plugin_paths = sorted(self.plugin_dir.iterdir())
        except OSError:
            return {}
        result: dict[str, _Entry] = {}
        for plugin_path in plugin_paths:
            if not plugin_path.is_dir() or not (plugin_path / "plugin.toml").is_file():
                continue
            signature = self._signature(plugin_path)
            previous = self._snapshot.get(plugin_path.name)
            if previous is not None and previous.signature == signature:
                result[plugin_path.name] = previous
                continue
            digest = None
            if previous is None or previous.digest is None:
                digest = self._digest(plugin_path)
            else:
                # Signature changed: re-hash so a touch that did not alter the
                # bytes does not trigger a pointless reload.
                candidate = self._digest(plugin_path)
                if candidate == previous.digest:
                    digest = previous.digest
                else:
                    digest = candidate
            result[plugin_path.name] = _Entry(signature=signature, digest=digest)
        return result

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._snapshot = await asyncio.to_thread(self._scan)
        self._task = asyncio.create_task(self._run(), name="plugin-watcher")
        self.manager.health.watcher_running = True

    async def stop(self, *, quiesce_timeout: float | None = None) -> None:
        """Quiesce, then stop.

        The current iteration is allowed to finish so an in-flight reload is not
        torn apart. Only if it overruns the budget is the task cancelled.
        """
        task = self._task
        self._task = None
        self._stopping.set()
        self.manager.health.watcher_running = False
        if task is None:
            return
        budget = (
            self.manager._shutdown_timeout if quiesce_timeout is None else quiesce_timeout
        )
        if not await self.manager.wait_idle(budget):
            self.manager.logger.warning(
                "plugin operations did not settle within %.1fs; forcing the watcher to stop",
                budget,
            )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, self.debounce + 0.5))
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
                return  # stop() was requested
            except TimeoutError:
                pass
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.manager.logger.exception("Plugin watcher tick failed")

    async def _tick(self) -> None:
        current = await asyncio.to_thread(self._scan)
        self._scan_count += 1
        previous = self._snapshot
        if current == previous:
            return
        self._snapshot = current
        if self.debounce:
            await asyncio.sleep(self.debounce)
        for name in sorted(set(current) - set(previous)):
            try:
                await self.manager.load_local(name)
                self._reload_count += 1
            except Exception:
                self.manager.logger.exception("Watcher could not load plugin %s", name)
        for name in sorted(set(previous) - set(current)):
            try:
                await self.manager.unload_local(name)
                self._reload_count += 1
            except Exception:
                self.manager.logger.exception("Watcher could not unload plugin %s", name)
        for name in sorted(set(current) & set(previous)):
            if current[name].digest == previous[name].digest:
                continue
            try:
                await self.manager.reload_local(name)
                self._reload_count += 1
            except Exception:
                self.manager.logger.exception("Watcher could not reload plugin %s", name)
