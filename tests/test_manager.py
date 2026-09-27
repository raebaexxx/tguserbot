from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import (
    FakeClient,
    FakeEvent,
    make_command_plugin,
    make_handler_plugin,
    make_noop_plugin,
    make_slow_stop_plugin,
    write_plugin,
)
from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.health import HealthService
from userbot.loader import PluginLoadError
from userbot.manager import PluginManager
from userbot.rate_limit import RateLimiter
from userbot.storage import Storage


async def build_manager(
    tmp_path: Path,
    plugin_dir: Path,
    *,
    health: HealthService | None = None,
    **settings_kwargs: object,
) -> tuple[PluginManager, Storage, FakeClient, CommandDispatcher]:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_dir,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
        **settings_kwargs,  # type: ignore[arg-type]
    )
    storage = Storage(settings.database_path)
    await storage.initialize()
    client = FakeClient()
    dispatcher = CommandDispatcher({1})
    manager = PluginManager(
        settings=settings,
        client=client,
        storage=storage,
        dispatcher=dispatcher,
        rate_limiter=RateLimiter(min_interval=0),
        health=health or HealthService(),
    )
    return manager, storage, client, dispatcher


def module_of(manager: PluginManager, name: str) -> str:
    """Namespace of the generation currently running for ``name``."""
    runtime = manager.get_runtime(name)
    assert runtime is not None, f"{name} is not loaded"
    return runtime.loaded.module.__name__


# --- discovery and loading -------------------------------------------------


async def test_discovers_only_directories_with_a_manifest(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    write_plugin(plugin_root, "beta", body=make_noop_plugin("beta"))
    (plugin_root / "not-a-plugin").mkdir()
    (plugin_root / "loose.toml").write_text("x = 1\n", encoding="utf-8")

    manager, storage, client, dispatcher = await build_manager(plugin_root.parent, plugin_root)
    try:
        assert manager.discover_local_names() == ["alpha", "beta"]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_discovery_on_a_missing_directory_is_empty(tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, tmp_path / "absent")
    try:
        assert manager.discover_local_names() == []
        await manager.load_all_local()
        assert manager.active_count() == 0
    finally:
        await manager.shutdown()
        await storage.close()


async def test_load_registers_commands_and_handlers(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    write_plugin(plugin_root, "beta", body=make_handler_plugin(r"^ping$"))
    manager, storage, client, dispatcher = await build_manager(plugin_root.parent, plugin_root)
    try:
        await manager.load_all_local()
        assert manager.active_count() == 2
        assert {item.name for item in dispatcher.commands()} == {"alpha"}
        assert len(client.handlers) == 1
    finally:
        await manager.shutdown()
        await storage.close()


async def test_load_is_recorded_in_storage(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(plugin_root.parent, plugin_root)
    try:
        await manager.load_all_local()
        state = await storage.get_plugin_state("alpha")
        assert state is not None
        assert state["status"] == "active"
        assert state["source"] == "local"
        assert state["version"] == "0.1.0"
        assert state["error"] is None
    finally:
        await manager.shutdown()
        await storage.close()


async def test_loading_an_unloadable_plugin_is_reported_not_fatal(
    plugin_root: Path,
) -> None:
    write_plugin(plugin_root, "good", body=make_noop_plugin("good"))
    broken = write_plugin(plugin_root, "broken", body="")
    (broken / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
    manager, storage, client, dispatcher = await build_manager(plugin_root.parent, plugin_root)
    try:
        await manager.load_all_local()
        assert manager.active_count() == 1
        listed = {item["name"]: item for item in await manager.list_plugins()}
        assert listed["broken"]["status"] == "failed"
        assert listed["broken"]["error"]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_load_is_skipped_when_not_forced(plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(plugin_root.parent, plugin_root)
    try:
        first = await manager.load_local("alpha")
        second = await manager.load_local("alpha")
        assert first is second
    finally:
        await manager.shutdown()
        await storage.close()


async def test_health_counts_reloads(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    health = HealthService()
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root, health=health)
    try:
        await manager.load_all_local()
        assert health.reload_count == 0
        await manager.reload_local("alpha")
        assert health.reload_count == 1
    finally:
        await manager.shutdown()
        await storage.close()


# --- reload, disable, enable ----------------------------------------------


async def test_failed_reload_keeps_the_previous_version_running(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        assert manager.active_count() == 1
        module_before = module_of(manager, "alpha")

        (plugin_root / "alpha" / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        with pytest.raises(PluginLoadError):
            await manager.reload_local("alpha")

        assert manager.active_count() == 1
        assert module_of(manager, "alpha") == module_before
        assert [item.name for item in dispatcher.commands()] == ["alpha"]
        assert len(client.handlers) == 0
    finally:
        await manager.shutdown()
        await storage.close()


async def test_failed_reload_keeps_the_previous_handlers_attached(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "beta", body=make_handler_plugin(r"^ping$"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        assert len(client.handlers) == 1
        (plugin_root / "beta" / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        with pytest.raises(PluginLoadError):
            await manager.reload_local("beta")
        assert len(client.handlers) == 1
    finally:
        await manager.shutdown()
        await storage.close()


async def test_successful_reload_swaps_the_generation(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        before = module_of(manager, "alpha")
        await manager.reload_local("alpha")
        after = module_of(manager, "alpha")
        assert before != after
        assert [item.name for item in dispatcher.commands()] == ["alpha"]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_disable_unloads_and_persists(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        await manager.disable("alpha")
        assert manager.active_count() == 0
        assert manager.is_disabled("alpha")
        assert dispatcher.commands() == []
        state = await storage.get_plugin_state("alpha")
        assert state is not None
        assert state["status"] == "disabled"
    finally:
        await manager.shutdown()
        await storage.close()


async def test_enable_reloads_a_disabled_local_plugin(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        await manager.disable("alpha")
        await manager.enable("alpha")
        assert manager.active_count() == 1
        assert not manager.is_disabled("alpha")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_enable_reports_a_plugin_that_does_not_exist(
    plugin_root: Path, tmp_path: Path
) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        with pytest.raises(PluginLoadError, match="не найден"):
            await manager.enable("absent")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_disabling_an_unknown_plugin_is_rejected(plugin_root: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        with pytest.raises(PluginLoadError, match="не найден"):
            await manager.disable("typo-name")
        assert await storage.plugin_states() == []
    finally:
        await manager.shutdown()
        await storage.close()


async def test_disabled_plugin_from_settings_is_not_loaded(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    write_plugin(plugin_root, "beta", body=make_command_plugin("beta"))
    manager, storage, client, dispatcher = await build_manager(
        tmp_path, plugin_root, disabled_plugins=frozenset({"beta"})
    )
    try:
        await manager.load_all_local()
        assert manager.active_count() == 1
        listed = {item["name"]: item for item in await manager.list_plugins()}
        assert listed["beta"]["status"] == "disabled"
    finally:
        await manager.shutdown()
        await storage.close()


async def test_disabled_state_survives_a_restart(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.load_all_local()
    await manager.disable("alpha")
    await manager.shutdown()

    manager2, storage2, client2, dispatcher2 = await build_manager(tmp_path, plugin_root)
    try:
        await manager2.initialize_state()
        assert manager2.is_disabled("alpha")
        await manager2.load_all_local()
        assert manager2.active_count() == 0
    finally:
        await manager2.shutdown()
        await storage2.close()
    await storage.close()


async def test_settings_disable_is_reported_when_enabling(
    plugin_root: Path, tmp_path: Path
) -> None:
    """Regression: an env-level disable must not silently reappear after a restart."""
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(
        tmp_path, plugin_root, disabled_plugins=frozenset({"alpha"})
    )
    try:
        await manager.initialize_state()
        await manager.load_all_local()
        with pytest.raises(PluginLoadError, match="TGUSERBOT_DISABLED_PLUGINS"):
            await manager.enable("alpha")
        assert manager.is_disabled("alpha")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_reload_of_a_disabled_plugin_is_a_noop(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(
        tmp_path, plugin_root, disabled_plugins=frozenset({"alpha"})
    )
    try:
        await manager.initialize_state()
        await manager.load_all_local()
        assert await manager.reload_local("alpha") is None
        assert manager.active_count() == 0
    finally:
        await manager.shutdown()
        await storage.close()


# --- listing ---------------------------------------------------------------


async def test_list_plugins_merges_disk_state_and_runtimes(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    write_plugin(plugin_root, "beta", body=make_noop_plugin("beta"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        assert {item["status"] for item in await manager.list_plugins()} == {"active"}

        # A plugin that is running stays "active" even after its source breaks;
        # only a *failed load* is reported as "failed".
        (plugin_root / "beta" / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        with pytest.raises(PluginLoadError):
            await manager.reload_local("beta")
        listed = {item["name"]: item for item in await manager.list_plugins()}
        assert listed["alpha"]["status"] == "active"
        assert listed["alpha"]["source"] == "local"
        # beta keeps running the last good generation, so it is still active.
        assert listed["beta"]["status"] == "active"
        assert await manager.error_count() == 0

        # A plugin that never loaded successfully is reported as failed.
        (plugin_root / "gamma").mkdir()
        (plugin_root / "gamma" / "plugin.toml").write_text(
            'name = "gamma"\nversion = "0.1.0"\napi = "1"\nentrypoint = "plugin:Plugin"\n',
            encoding="utf-8",
        )
        (plugin_root / "gamma" / "__init__.py").write_text("", encoding="utf-8")
        (plugin_root / "gamma" / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        with pytest.raises(PluginLoadError):
            await manager.load_local("gamma")
        listed = {item["name"]: item for item in await manager.list_plugins()}
        assert listed["gamma"]["status"] == "failed"
        assert listed["gamma"]["error"]
        assert await manager.error_count() == 1
    finally:
        await manager.shutdown()
        await storage.close()


async def test_list_plugins_on_an_empty_installation(plugin_root: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        assert await manager.list_plugins() == []
        assert await manager.error_count() == 0
    finally:
        await manager.shutdown()
        await storage.close()


async def test_error_count_does_not_walk_the_filesystem_on_every_call(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        await manager.error_count()
        calls: list[int] = []
        original = manager.discover_local_names

        def counting() -> list[str]:
            calls.append(1)
            return original()

        manager.discover_local_names = counting  # type: ignore[method-assign]
        await manager.error_count()
        assert calls == []
    finally:
        await manager.shutdown()
        await storage.close()


# --- shutdown --------------------------------------------------------------


async def test_shutdown_unloads_everything(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_command_plugin("alpha"))
    write_plugin(plugin_root, "beta", body=make_handler_plugin(r"^ping$"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.load_all_local()
    await manager.shutdown()
    assert manager.active_count() == 0
    assert dispatcher.commands() == []
    assert client.handlers == []
    await storage.close()


async def test_shutdown_marks_plugins_unloaded(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.load_all_local()
    await manager.shutdown()
    state = await storage.get_plugin_state("alpha")
    assert state is not None
    assert state["status"] == "unloaded"
    await storage.close()


async def test_shutdown_survives_a_failing_stop_hook(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(
        plugin_root,
        "alpha",
        body="""
            from userbot.plugin_api import Plugin as BasePlugin

            class Plugin(BasePlugin):
                async def setup(self, ctx):
                    self.ctx = ctx

                async def stop(self):
                    raise RuntimeError("stop exploded")
        """,
    )
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.load_all_local()
    await manager.shutdown()
    assert manager.active_count() == 0
    await storage.close()


async def test_shutdown_respects_a_global_deadline(plugin_root: Path, tmp_path: Path) -> None:
    write_plugin(plugin_root, "alpha", body=make_slow_stop_plugin("alpha", delay=5.0))
    write_plugin(plugin_root, "beta", body=make_slow_stop_plugin("beta", delay=5.0))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.load_all_local()
    manager.set_shutdown_timeout(0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await manager.shutdown()
    elapsed = loop.time() - started
    assert elapsed < 2.0
    assert manager.active_count() == 0
    await storage.close()


# --- cancellation safety (B1) ---------------------------------------------


async def test_cancelling_a_reload_does_not_leave_a_zombie_context(
    plugin_root: Path, tmp_path: Path
) -> None:
    """Regression: a reload interrupted by shutdown used to strand the new context.

    The manager kept the *old*, already-stopped runtime while the dispatcher and
    the Telethon client still referenced the *new* generation, so the new
    context was never deactivated and survived ``shutdown()``.
    """
    write_plugin(plugin_root, "alpha", body=make_slow_stop_plugin("alpha", delay=5.0))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        (plugin_root / "alpha" / "plugin.py").write_text(
            (plugin_root / "alpha" / "plugin.py").read_text() + "\n# v2\n"
        )
        task = asyncio.create_task(manager.reload_local("alpha"))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The manager must be internally consistent again: whatever the
        # dispatcher points at must be the runtime the map holds.
        assert manager.get_runtime("alpha") is not None
        live = [item for item in dispatcher.commands() if item.name == "alpha"]
        assert live, "the command vanished but the old version is dead too"
        assert live[0].callback.__self__.__class__.__module__ == (
            f"{module_of(manager, 'alpha')}.plugin"
        )

        await manager.shutdown()
        assert manager.active_count() == 0
        assert dispatcher.commands() == []
        assert client.handlers == [], "a zombie context survived shutdown"
    finally:
        await storage.close()


async def test_shutdown_while_a_reload_is_in_flight_is_clean(
    plugin_root: Path, tmp_path: Path
) -> None:
    write_plugin(plugin_root, "alpha", body=make_slow_stop_plugin("alpha", delay=5.0))
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        (plugin_root / "alpha" / "plugin.py").write_text(
            (plugin_root / "alpha" / "plugin.py").read_text() + "\n# v2\n"
        )
        reload_task = asyncio.create_task(manager.reload_local("alpha"))
        await asyncio.sleep(0.15)
        manager.set_shutdown_timeout(0.3)
        await manager.shutdown()
        reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)
        assert dispatcher.commands() == []
        assert client.handlers == []
    finally:
        await storage.close()


# --- end-to-end dispatch ---------------------------------------------------


async def test_shipped_plugins_register_their_commands(
    real_plugin_dir: Path, tmp_path: Path
) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        names = {item.name for item in dispatcher.commands()}
        assert {"notes", "status", "echo", "tt"} <= names
    finally:
        await manager.shutdown()
        await storage.close()


async def test_echo_command_round_trip(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        event = FakeEvent("/ub echo привет")
        await dispatcher.handle_event(event)
        assert event.responses == ["привет"]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_echo_rejects_an_overlong_argument(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        event = FakeEvent("/ub echo " + "x" * 5000)
        await dispatcher.handle_event(event)
        assert event.responses == ["Использование: /ub echo <текст>"] or all(
            len(item) <= 4096 for item in event.responses
        )
    finally:
        await manager.shutdown()
        await storage.close()


async def test_notes_round_trip(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        added = FakeEvent("/ub notes add первая заметка", chat_id=7)
        await dispatcher.handle_event(added)
        assert added.responses and added.responses[0].startswith("Заметка #")

        listed = FakeEvent("/ub notes list", chat_id=7)
        await dispatcher.handle_event(listed)
        assert listed.responses and "первая заметка" in listed.responses[0]

        note_id = added.responses[0].split("#")[1].split()[0]
        deleted = FakeEvent(f"/ub notes delete {note_id}", chat_id=7)
        await dispatcher.handle_event(deleted)
        assert deleted.responses == [f"Заметка #{note_id} удалена."]

        again = FakeEvent("/ub notes list", chat_id=7)
        await dispatcher.handle_event(again)
        assert again.responses == ["Заметок пока нет."]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_notes_delete_reports_a_miss_honestly(real_plugin_dir: Path, tmp_path: Path) -> None:
    """Regression: ``Storage.execute`` used to return a stale ``lastrowid`` for
    DELETE, so ``notes delete`` always claimed success."""
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        FakeEvent("/ub notes add первая", chat_id=7)
        missing = FakeEvent("/ub notes delete 4242", chat_id=7)
        await dispatcher.handle_event(missing)
        assert missing.responses == ["Заметка не найдена в этом чате."]

        wrong_chat = FakeEvent("/ub notes delete 1", chat_id=999)
        await dispatcher.handle_event(wrong_chat)
        assert wrong_chat.responses == ["Заметка не найдена в этом чате."]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_notes_are_scoped_per_chat(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        await dispatcher.handle_event(FakeEvent("/ub notes add из седьмого", chat_id=7))
        other = FakeEvent("/ub notes list", chat_id=8)
        await dispatcher.handle_event(other)
        assert other.responses == ["Заметок пока нет."]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_notes_rejects_bad_input(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        await manager.load_all_local()
        empty = FakeEvent("/ub notes add")
        await dispatcher.handle_event(empty)
        assert empty.responses == ["Текст заметки не указан."]

        not_a_number = FakeEvent("/ub notes delete abc")
        await dispatcher.handle_event(not_a_number)
        assert not_a_number.responses == ["ID заметки должен быть числом."]

        unknown = FakeEvent("/ub notes frobnicate")
        await dispatcher.handle_event(unknown)
        assert unknown.responses and "Неизвестная подкоманда" in unknown.responses[0]

        usage = FakeEvent("/ub notes")
        await dispatcher.handle_event(usage)
        assert usage.responses and "Использование" in usage.responses[0]
    finally:
        await manager.shutdown()
        await storage.close()


async def test_status_command_reports_health(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        health = manager.health
        health.telegram_connected = True
        health.authorized = True
        await manager.load_all_local()
        event = FakeEvent("/ub status")
        await dispatcher.handle_event(event)
        assert event.responses
        text = event.responses[0]
        assert "Uptime:" in text
        assert "Telegram: подключён" in text
        assert "Плагины:" in text
    finally:
        await manager.shutdown()
        await storage.close()


async def test_unknown_ub_command_is_reported(real_plugin_dir: Path, tmp_path: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, real_plugin_dir)
    try:
        event = FakeEvent("/ub add-note первая заметка")
        await dispatcher.handle_event(event)
        assert event.responses == ["Неизвестная команда. Отправьте /ub help"]
    finally:
        await manager.shutdown()
        await storage.close()
