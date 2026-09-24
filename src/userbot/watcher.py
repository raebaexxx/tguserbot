from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any


class PluginWatcher:
    """Polling watcher for local plugin source changes."""

    def __init__(self, manager: Any, plugin_dir: Path, *, interval: float = 0.75):
        self.manager = manager
        self.plugin_dir = plugin_dir
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._snapshot: dict[str, str] = {}

    def _calculate_snapshot(self) -> dict[str, str]:
        if not self.plugin_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for plugin_path in sorted(self.plugin_dir.iterdir()):
            if not plugin_path.is_dir() or not (plugin_path / "plugin.toml").is_file():
                continue
            digest = hashlib.sha256()
            for source_path in sorted(plugin_path.rglob("*")):
                if not source_path.is_file():
                    continue
                relative = source_path.relative_to(plugin_path).as_posix()
                if "__pycache__" in relative or relative.startswith(".git"):
                    continue
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                try:
                    digest.update(source_path.read_bytes())
                except OSError:
                    continue
                digest.update(b"\0")
            result[plugin_path.name] = digest.hexdigest()
        return result

    async def start(self) -> None:
        if self._task is not None:
            return
        self._snapshot = await asyncio.to_thread(self._calculate_snapshot)
        self._task = asyncio.create_task(self._run(), name="plugin-watcher")
        self.manager.health.watcher_running = True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self.manager.health.watcher_running = False
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            current = await asyncio.to_thread(self._calculate_snapshot)
            previous = self._snapshot
            if current == previous:
                continue
            self._snapshot = current
            for name in sorted(set(current) - set(previous)):
                try:
                    await self.manager.load_local(name)
                except Exception:
                    self.manager.logger.exception("Watcher could not load plugin %s", name)
            for name in sorted(set(previous) - set(current)):
                try:
                    await self.manager.unload_local(name)
                except Exception:
                    self.manager.logger.exception("Watcher could not unload plugin %s", name)
            for name in sorted(set(current) & set(previous)):
                if current[name] == previous[name]:
                    continue
                try:
                    await self.manager.reload_local(name)
                except Exception:
                    self.manager.logger.exception("Watcher could not reload plugin %s", name)
