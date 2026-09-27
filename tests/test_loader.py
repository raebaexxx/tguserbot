from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from conftest import make_command_plugin, make_handler_plugin, make_noop_plugin, write_plugin
from userbot.loader import PluginLoadError, cleanup_loaded_plugin, load_plugin
from userbot.task_registry import TaskGroup


def test_loads_a_plugin_and_exposes_its_instance(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    loaded = load_plugin(plugin_root / "alpha", "alpha", 1)
    try:
        assert loaded.manifest.name == "alpha"
        assert loaded.manifest.api == "1"
        assert loaded.manifest.schema_version == 1
        assert loaded.module.__name__ == "_tguserbot_plugin_alpha_1"
    finally:
        cleanup_loaded_plugin(loaded)


def test_rejects_directory_name_manifest_mismatch(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", manifest_overrides={"name": "beta"}, body="")
    with pytest.raises(PluginLoadError, match="name mismatch"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_rejects_invalid_manifest_name(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", manifest_overrides={"name": "Bad Name!"}, body="")
    with pytest.raises(PluginLoadError, match="name must match"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_rejects_unsupported_api_version(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", manifest_overrides={"api": "2"}, body="")
    with pytest.raises(PluginLoadError, match="Unsupported plugin API"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_rejects_missing_manifest(plugin_root: Path) -> None:
    (plugin_root / "broken").mkdir()
    with pytest.raises(PluginLoadError, match="Missing plugin.toml"):
        load_plugin(plugin_root / "broken", "broken", 1)


def test_rejects_broken_toml(plugin_root: Path) -> None:
    directory = plugin_root / "alpha"
    directory.mkdir()
    (directory / "plugin.toml").write_text("name = 'unterminated\n", encoding="utf-8")
    with pytest.raises(PluginLoadError, match="Cannot read"):
        load_plugin(directory, "alpha", 1)


def test_rejects_non_integer_schema_version(plugin_root: Path) -> None:
    write_plugin(
        plugin_root,
        "alpha",
        manifest_extra='schema_version = "not-a-number"\n',
        body="",
    )
    with pytest.raises(PluginLoadError, match="schema_version must be an integer"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_rejects_missing_package_files(plugin_root: Path) -> None:
    directory = plugin_root / "alpha"
    directory.mkdir()
    (directory / "plugin.toml").write_text(
        'name = "alpha"\nversion = "0.1.0"\napi = "1"\nentrypoint = "plugin:Plugin"\n',
        encoding="utf-8",
    )
    with pytest.raises(PluginLoadError, match="must contain __init__.py and plugin.py"):
        load_plugin(directory, "alpha", 1)


def test_rejects_syntax_error_in_any_module(plugin_root: Path) -> None:
    directory = write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    (directory / "helper.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(PluginLoadError, match="Cannot compile"):
        load_plugin(directory, "alpha", 1)


def test_rejects_entrypoint_that_is_not_a_class(plugin_root: Path) -> None:
    write_plugin(
        plugin_root,
        "alpha",
        manifest_overrides={"entrypoint": "plugin:not_a_class"},
        body="not_a_class = 42\n",
    )
    with pytest.raises(PluginLoadError, match="is not a class"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_rejects_malformed_entrypoint(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", manifest_overrides={"entrypoint": "plugin"}, body="")
    with pytest.raises(PluginLoadError, match="module:attribute"):
        load_plugin(plugin_root / "alpha", "alpha", 1)


def test_supports_init_dunder_entrypoint(plugin_root: Path) -> None:
    write_plugin(
        plugin_root,
        "alpha",
        manifest_overrides={"entrypoint": "__init__:Plugin"},
        init_source=(
            "from userbot.plugin_api import Plugin as BasePlugin\n"
            "class Plugin(BasePlugin):\n    pass\n"
        ),
        body="",
    )
    loaded = load_plugin(plugin_root / "alpha", "alpha", 1)
    try:
        assert type(loaded.instance).__name__ == "Plugin"
    finally:
        cleanup_loaded_plugin(loaded)


def test_load_failure_does_not_leak_module_namespace(plugin_root: Path) -> None:
    import sys

    write_plugin(
        plugin_root,
        "alpha",
        manifest_overrides={"entrypoint": "plugin:Missing"},
        body="value = 1\n",
    )
    with pytest.raises(PluginLoadError):
        load_plugin(plugin_root / "alpha", "alpha", 7)
    assert not [name for name in sys.modules if name.startswith("_tguserbot_plugin_alpha_7")]


def test_cleanup_removes_module_namespace(plugin_root: Path) -> None:
    import sys

    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    loaded = load_plugin(plugin_root / "alpha", "alpha", 3)
    prefix = "_tguserbot_plugin_alpha_3"
    assert prefix in sys.modules
    cleanup_loaded_plugin(loaded)
    assert not [name for name in sys.modules if name.startswith(prefix)]


def test_each_generation_gets_a_distinct_namespace(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    first = load_plugin(plugin_root / "alpha", "alpha", 1)
    second = load_plugin(plugin_root / "alpha", "alpha", 2)
    try:
        assert first.module is not second.module
        assert first.instance is not second.instance
    finally:
        cleanup_loaded_plugin(first)
        cleanup_loaded_plugin(second)


def test_shipped_plugins_declare_a_usable_entrypoint(real_plugin_dir: Path) -> None:
    names = sorted(path.name for path in real_plugin_dir.iterdir() if path.is_dir())
    assert names, "the repository must ship at least one plugin"
    for generation, name in enumerate(names, 1):
        loaded = load_plugin(real_plugin_dir / name, name, generation)
        try:
            assert loaded.manifest.name == name
            assert loaded.manifest.api == "1"
        finally:
            cleanup_loaded_plugin(loaded)


def test_handler_plugin_registers_through_context(real_plugin_dir: Path) -> None:
    """The shipped ``echo`` plugin exercises the handler registration path."""
    loaded = load_plugin(real_plugin_dir / "echo", "echo", 1)
    try:
        assert hasattr(loaded.instance, "handle")
    finally:
        cleanup_loaded_plugin(loaded)


def test_command_plugin_source_is_valid(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    loaded = load_plugin(plugin_root / "alpha", "alpha", 1)
    try:
        assert loaded.manifest.entrypoint == "plugin:Plugin"
    finally:
        cleanup_loaded_plugin(loaded)


def test_manifest_plugin_variant(plugin_root: Path) -> None:
    write_plugin(
        plugin_root,
        "alpha",
        manifest_overrides={"entrypoint": "plugin:Plugin"},
        body=make_handler_plugin("alpha"),
    )
    loaded = load_plugin(plugin_root / "alpha", "alpha", 1)
    try:
        assert loaded.manifest.name == "alpha"
    finally:
        cleanup_loaded_plugin(loaded)


async def test_task_group_cancels_background_work() -> None:
    group = TaskGroup()
    task = group.spawn(asyncio.sleep(60), name="test")
    await asyncio.sleep(0)
    assert await group.cancel_all() is True
    assert task.cancelled()


async def test_task_group_rejects_spawn_after_close() -> None:
    group = TaskGroup()
    await group.cancel_all()
    with pytest.raises(RuntimeError, match="closed task group"):
        group.spawn(asyncio.sleep(60))


async def test_task_group_reports_timeout_for_stubborn_task() -> None:
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass  # deliberately swallows cancellation

    group = TaskGroup()
    leaked = group.spawn(stubborn())
    await asyncio.sleep(0)  # let the task reach its first await
    try:
        assert await group.cancel_all(timeout_seconds=0.05) is False
    finally:
        # It has to be released, not abandoned. A task that swallows every
        # cancellation outlives the test, and pytest-asyncio 1.x drains the loop
        # at teardown, so the leak turns into a hang rather than a slow exit.
        release.set()
        leaked.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await leaked
