from __future__ import annotations

import asyncio
from pathlib import Path

from userbot.loader import cleanup_loaded_plugin, load_plugin
from userbot.plugin_api import Plugin


def test_loads_all_demo_plugins() -> None:
    for generation, name in enumerate(("notes", "status", "echo"), 1):
        loaded = load_plugin(Path("plugins") / name, name, generation)
        assert loaded.manifest.name == name
        assert isinstance(loaded.instance, Plugin)
        cleanup_loaded_plugin(loaded)


def test_rejects_syntax_error(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        'name = "broken"\nversion = "0.1.0"\napi = "1"\nentrypoint = "plugin:Plugin"\n',
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
    try:
        load_plugin(plugin_dir, "broken", 1)
    except Exception as exc:
        assert "compile" in str(exc).lower() or "syntax" in str(exc).lower()
    else:
        raise AssertionError("Expected syntax error")


def test_task_registry_cancels_background_work() -> None:
    from userbot.task_registry import TaskGroup

    async def scenario() -> None:
        group = TaskGroup()
        task = group.spawn(asyncio.sleep(60), name="test")
        await asyncio.sleep(0)
        await group.cancel_all()
        assert task.cancelled()

    asyncio.run(scenario())
