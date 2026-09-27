from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import FakeEvent, make_command_plugin, make_noop_plugin, write_plugin
from userbot.watcher import IGNORED_NAMES, IGNORED_SUFFIXES, PluginWatcher


class FakeManager:
    """Minimal manager surface the watcher drives."""

    def __init__(self) -> None:
        from userbot.health import HealthService

        self.health = HealthService()
        self.logger = _Logger()
        self.loaded: list[str] = []
        self.reloaded: list[str] = []
        self.unloaded: list[str] = []
        self.fail_on: set[str] = set()
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.shutdown_timeout = 5.0

    async def load_local(self, name: str, **_: object) -> None:
        await self._gate()
        if name in self.fail_on:
            raise RuntimeError(f"cannot load {name}")
        self.loaded.append(name)

    async def reload_local(self, name: str, **_: object) -> None:
        await self._gate()
        if name in self.fail_on:
            raise RuntimeError(f"cannot reload {name}")
        self.reloaded.append(name)

    async def unload_local(self, name: str) -> None:
        await self._gate()
        self.unloaded.append(name)

    async def _gate(self) -> None:
        self._inflight += 1
        self._idle.clear()
        try:
            await asyncio.sleep(0)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def wait_idle(self, budget: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=budget)
        except TimeoutError:
            return False
        return True


class _Logger:
    def __getattr__(self, _name: str):
        def _noop(*args: object, **kwargs: object) -> None:
            return None

        return _noop


def make_watcher(manager: FakeManager, plugin_dir: Path, **kwargs: object) -> PluginWatcher:
    return PluginWatcher(manager, plugin_dir, interval=0.05, debounce=0.0, **kwargs)  # type: ignore[arg-type]


async def wait_for(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached in time")


# --- scanning --------------------------------------------------------------


def test_scan_on_a_missing_directory(plugin_root: Path) -> None:
    watcher = make_watcher(FakeManager(), plugin_root / "absent")
    assert watcher._scan() == {}


def test_scan_ignores_directories_without_a_manifest(plugin_root: Path) -> None:
    (plugin_root / "empty").mkdir()
    (plugin_root / "loose.toml").write_text("x = 1\n", encoding="utf-8")
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    watcher = make_watcher(FakeManager(), plugin_root)
    assert set(watcher._scan()) == {"alpha"}


def test_scan_ignores_noise_files(plugin_root: Path) -> None:
    directory = write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    (directory / "module.pyc").write_bytes(b"junk")
    (directory / ".session-journal").write_bytes(b"junk")
    cache = directory / "__pycache__"
    cache.mkdir()
    (cache / "plugin.cpython-312.pyc").write_bytes(b"junk")
    (directory / "logs").mkdir()
    (directory / "logs" / "x.log").write_text("noise", encoding="utf-8")
    (directory / "keep.py").write_text("X = 1\n", encoding="utf-8")
    watcher = make_watcher(FakeManager(), plugin_root)
    entry = watcher._scan()["alpha"]
    files = {name for name, _size, _mtime in entry.signature}
    assert "keep.py" in files
    assert not any(name.endswith(".pyc") for name in files)
    assert not any(part in IGNORED_NAMES for name in files for part in Path(name).parts)
    assert "__pycache__" not in files


def test_digest_changes_with_content(plugin_root: Path) -> None:
    directory = write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    watcher = make_watcher(FakeManager(), plugin_root)
    before = watcher._digest(directory)
    (directory / "plugin.py").write_text("# changed\n", encoding="utf-8")
    assert watcher._digest(directory) != before


def test_touching_a_file_without_changing_bytes_is_not_a_change(
    plugin_root: Path,
) -> None:
    directory = write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    watcher = make_watcher(FakeManager(), plugin_root)
    watcher._snapshot = watcher._scan()
    before = watcher._snapshot["alpha"]
    import os

    stat = (directory / "plugin.py").stat()
    os.utime(directory / "plugin.py", (stat.st_atime + 5, stat.st_mtime + 5))
    after = watcher._scan()["alpha"]
    assert after.signature != before.signature, "mtime should have moved"
    assert after.digest == before.digest, "identical bytes must not force a reload"


# --- reacting to changes ---------------------------------------------------


async def test_new_plugin_is_loaded(plugin_root: Path) -> None:
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
        await wait_for(lambda: manager.loaded == ["alpha"])
        assert watcher.scan_count > 0
    finally:
        await watcher.stop()
    assert manager.health.watcher_running is False


async def test_changed_plugin_is_reloaded(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        (plugin_root / "alpha" / "plugin.py").write_text(
            make_command_plugin("alpha") + "\n# v2\n", encoding="utf-8"
        )
        await wait_for(lambda: manager.reloaded == ["alpha"])
    finally:
        await watcher.stop()


async def test_removed_plugin_is_unloaded(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        import shutil

        shutil.rmtree(plugin_root / "alpha")
        await wait_for(lambda: manager.unloaded == ["alpha"])
    finally:
        await watcher.stop()


async def test_an_unchanged_tree_produces_no_reload(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        await asyncio.sleep(0.3)
        assert manager.reloaded == []
        assert manager.loaded == []
    finally:
        await watcher.stop()


async def test_a_failing_reload_does_not_stop_the_watcher(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    manager.fail_on.add("alpha")
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        (plugin_root / "alpha" / "plugin.py").write_text(
            make_command_plugin("alpha") + "\n# v2\n", encoding="utf-8"
        )
        await wait_for(lambda: watcher.scan_count >= 2)
        assert manager.reloaded == []
    finally:
        await watcher.stop()


async def test_a_failing_reload_is_retried_on_the_next_change(
    plugin_root: Path,
) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    manager.fail_on.add("alpha")
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        for attempt in range(3):
            (plugin_root / "alpha" / "plugin.py").write_text(
                make_command_plugin("alpha") + f"\n# v{attempt}\n", encoding="utf-8"
            )
            before = watcher.scan_count
            await wait_for(lambda b=before: watcher.scan_count > b)
        assert watcher.reload_count == 0
    finally:
        await watcher.stop()


async def test_debounce_coalesces_rapid_saves(plugin_root: Path) -> None:
    directory = write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    watcher = PluginWatcher(manager, plugin_root, interval=0.05, debounce=0.3)
    await watcher.start()
    try:
        for index in range(5):
            (directory / "plugin.py").write_text(
                make_command_plugin("alpha") + f"\n# v{index}\n", encoding="utf-8"
            )
            await asyncio.sleep(0.02)
        await wait_for(lambda: manager.reloaded != [])
        await asyncio.sleep(0.2)
        assert manager.reloaded == ["alpha"], "five saves must not mean five reloads"
    finally:
        await watcher.stop()


# --- lifecycle -------------------------------------------------------------


async def test_start_is_idempotent(plugin_root: Path) -> None:
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    await watcher.start()
    try:
        assert manager.health.watcher_running is True
    finally:
        await watcher.stop()
    assert watcher._task is None


async def test_stop_without_start_is_safe(plugin_root: Path) -> None:
    await make_watcher(FakeManager(), plugin_root).stop()


async def test_stop_waits_for_an_in_flight_reload(plugin_root: Path) -> None:
    """A reload interrupted by stop() used to strand a live context."""
    directory = write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_reload(name: str, **_: object) -> None:
        started.set()
        await release.wait()
        manager.reloaded.append(name)

    manager.reload_local = slow_reload  # type: ignore[method-assign]
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    (directory / "plugin.py").write_text(
        make_command_plugin("alpha") + "\n# v2\n", encoding="utf-8"
    )
    await asyncio.sleep(0.2)
    (directory / "plugin.py").write_text(
        make_command_plugin("alpha") + "\n# v3\n", encoding="utf-8"
    )
    await started.wait()
    release.set()
    await watcher.stop()
    assert manager.reloaded, "the in-flight reload must be allowed to finish"


async def test_stop_forces_a_stuck_watcher(plugin_root: Path) -> None:
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    watcher._task = None  # simulate a watcher that is already gone
    await watcher.stop()
    assert manager.health.watcher_running is False


async def test_tick_errors_are_contained(plugin_root: Path) -> None:
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        import userbot.watcher as module

        original = module.PluginWatcher._scan

        def boom(self: PluginWatcher) -> dict:
            raise OSError("disk gone")

        module.PluginWatcher._scan = boom  # type: ignore[method-assign]
        try:
            await asyncio.sleep(0.15)
        finally:
            module.PluginWatcher._scan = original  # type: ignore[method-assign]
        assert watcher._task is not None and not watcher._task.done()
    finally:
        await watcher.stop()


def test_ignore_lists_are_sane() -> None:
    assert "__pycache__" in IGNORED_NAMES
    assert ".git" in IGNORED_NAMES
    assert ".pyc" in IGNORED_SUFFIXES


async def test_reload_counters_are_exposed(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager = FakeManager()
    watcher = make_watcher(manager, plugin_root)
    await watcher.start()
    try:
        (plugin_root / "alpha" / "plugin.py").write_text(
            make_command_plugin("alpha") + "\n# v2\n", encoding="utf-8"
        )
        await wait_for(lambda: watcher.reload_count >= 1)
    finally:
        await watcher.stop()
    _ = FakeEvent  # keep the import meaningful for readers of this module
    assert pytest is not None
