"""Tests for the plugin-facing API and the supporting infrastructure."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import FakeClient, FakeEvent
from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.health import MAX_TRACKED_ERRORS, HealthService, PluginError
from userbot.logging import JsonFormatter, get_logger, setup_logging
from userbot.manager import PluginManager
from userbot.plugin_api import (
    LIFECYCLE_HOOKS,
    Plugin,
    PluginContext,
    PluginContractError,
    TaskGroup,
    validate_plugin_interface,
)
from userbot.rate_limit import RateLimiter
from userbot.storage import (
    CORE_SCHEMA_VERSION,
    PluginStorage,
    SandboxedSqlError,
    StorageError,
    create_plugin_storage,
    guard_sandbox_sql,
)
from userbot.task_registry import run_uninterruptible

# --- plugin contract validation --------------------------------------------


class GoodPlugin(Plugin):
    async def setup(self, ctx: PluginContext) -> None:
        return None


def test_validate_accepts_a_conforming_plugin() -> None:
    validate_plugin_interface(GoodPlugin())


@pytest.mark.parametrize("hook", LIFECYCLE_HOOKS)
def test_validate_rejects_a_missing_hook(hook: str) -> None:
    class Missing(GoodPlugin):
        pass

    setattr(Missing, hook, None)
    with pytest.raises(PluginContractError, match=hook):
        validate_plugin_interface(Missing())


def test_validate_rejects_a_non_callable_hook() -> None:
    class Bad(GoodPlugin):
        start = "not callable"  # type: ignore[assignment]

    with pytest.raises(PluginContractError, match="not callable"):
        validate_plugin_interface(Bad())


def test_validate_rejects_a_wrong_setup_signature() -> None:
    class Bad(GoodPlugin):
        async def setup(self) -> None:  # type: ignore[override]
            return None

    with pytest.raises(PluginContractError, match="exactly one positional"):
        validate_plugin_interface(Bad())


def test_validate_rejects_a_non_plugin_object() -> None:
    class NotAPlugin:
        pass

    with pytest.raises(PluginContractError, match="setup"):
        validate_plugin_interface(NotAPlugin())


def test_validate_rejects_a_synchronous_hook() -> None:
    class Sync(GoodPlugin):
        def start(self) -> None:  # type: ignore[override]
            return None

    with pytest.raises(PluginContractError, match="coroutine"):
        validate_plugin_interface(Sync())


# --- the protocols describe what the core actually provides ----------------


def test_the_manager_implements_everything_a_plugin_may_call() -> None:
    """A protocol only helps while the concrete class still satisfies it.

    PluginContext declares its collaborators as protocols so mypy finally checks
    plugin code; that is worthless if the core drifts away from the contract
    without anyone noticing.
    """
    from userbot.protocols import EventDispatcher, TelegramClientLike

    host_members = (
        "active_count",
        "active_names",
        "get_runtime",
        "has_plugin",
        "is_disabled",
        "error_count",
    )
    for member in host_members:
        assert hasattr(PluginManager, member), f"PluginManager is missing {member}"
    dispatcher_members = ("register", "unregister", "commands", "is_owner", "owner_ids")
    dispatcher = CommandDispatcher({1})
    for member in dispatcher_members:
        assert hasattr(dispatcher, member), f"CommandDispatcher is missing {member}"
    assert isinstance(dispatcher, EventDispatcher)
    assert isinstance(FakeClient(), TelegramClientLike)


def test_context_forwards_the_owner_check_to_the_dispatcher(tmp_path: Path) -> None:
    """ctx.is_owner must go through the dispatcher, not duplicate the logic."""
    context = build_context(tmp_path)
    assert context.is_owner(1) is True
    assert context.is_owner(2) is False
    assert context.is_owner(None) is False
    context.dispatcher.owner_ids.add(5)
    assert context.is_owner(5) is True


# --- plugin storage sandbox ------------------------------------------------


async def test_plugin_storage_has_its_own_database(tmp_path: Path) -> None:
    first = await create_plugin_storage(tmp_path, "one")
    second = await create_plugin_storage(tmp_path, "two")
    try:
        assert first.path != second.path
        await first.set_value("k", "1")
        assert await first.get_value("k") == "1"
        assert await second.get_value("k") is None
    finally:
        await first.close()
        await second.close()


async def test_plugin_storage_sql_works(tmp_path: Path) -> None:
    storage = await create_plugin_storage(tmp_path, "demo")
    try:
        await storage.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        rowid = await storage.execute_insert("INSERT INTO t (v) VALUES (?)", ("x",))
        assert rowid == 1
        assert await storage.execute("UPDATE t SET v = 'y'") == 1
        rows = await storage.fetchall("SELECT v FROM t")
        assert rows == [{"v": "y"}]
        found = await storage.fetchone("SELECT v FROM t WHERE id = ?", (1,))
        assert found is not None
        assert found["v"] == "y"
        assert await storage.execute("DELETE FROM t WHERE id = 99") == 0
        assert await storage.execute("DELETE FROM t WHERE id = 1") == 1
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE '/etc/passwd' AS evil",
        "attach database 'x' as y",
        "PRAGMA journal_mode=WAL",
        "VACUUM",
        "SELECT 1; DROP TABLE plugin_state",
    ],
)
async def test_plugin_storage_refuses_escape_statements(tmp_path: Path, sql: str) -> None:
    storage = await create_plugin_storage(tmp_path, "demo")
    try:
        for call in (storage.execute, storage.fetchall, storage.fetchone):
            with pytest.raises(SandboxedSqlError):
                await call(sql)
    finally:
        await storage.close()


def test_guard_sandbox_sql_allows_ordinary_statements() -> None:
    guard_sandbox_sql("SELECT * FROM notes WHERE chat_id = ?")
    guard_sandbox_sql("CREATE TABLE IF NOT EXISTS t (a TEXT);")
    guard_sandbox_sql("INSERT INTO t (a) VALUES (?)")


async def test_plugin_storage_delete_and_keys(tmp_path: Path) -> None:
    storage = await create_plugin_storage(tmp_path, "demo")
    try:
        await storage.set_value("b", "2")
        await storage.set_value("a", "1")
        assert await storage.keys() == ["a", "b"]
        assert await storage.delete_value("a") == 1
        assert await storage.delete_value("missing") == 0
        assert await storage.keys() == ["b"]
    finally:
        await storage.close()


async def test_plugin_storage_requires_initialization(tmp_path: Path) -> None:
    storage = PluginStorage(tmp_path / "x.sqlite3", "demo")
    with pytest.raises(StorageError):
        await storage.execute("SELECT 1")


async def test_core_schema_version_is_recorded(storage: Any) -> None:
    assert await storage.schema_version() == CORE_SCHEMA_VERSION


async def test_core_schema_migration_adds_missing_columns(tmp_path: Path) -> None:
    """A database written by the previous version must be upgraded in place."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE plugin_state (
            name TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_ref TEXT,
            version TEXT,
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO plugin_state VALUES ('old', 'git', 'abc', '0.1.0', 'active', NULL, 'now')"
    )
    connection.commit()
    connection.close()

    from userbot.storage import Storage

    store = Storage(path)
    await store.initialize()
    try:
        assert await store.schema_version() == CORE_SCHEMA_VERSION
        state = await store.get_plugin_state("old")
        assert state is not None
        assert state["source_url"] is None
        assert state["source_subpath"] is None
    finally:
        await store.close()


async def test_close_is_idempotent_and_blocks_new_work(tmp_path: Path) -> None:
    from userbot.storage import Storage

    store = Storage(tmp_path / "c.sqlite3")
    await store.initialize()
    await store.close()
    await store.close()
    assert not store.initialized
    with pytest.raises(StorageError, match="initialize"):
        await store.execute("SELECT 1")


# --- rate limiter ----------------------------------------------------------


async def test_distinct_keys_are_not_serialised() -> None:
    """Regression: the inter-key delay used to be awaited under the global lock."""
    limiter = RateLimiter(min_interval=0.4, max_concurrency=8)
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def touch(key: str) -> float:
        async with limiter.slot(key):
            return loop.time() - started

    offsets = sorted(await asyncio.gather(*(touch(f"k{i}") for i in range(3))))
    assert offsets[0] < 0.2
    assert offsets[-1] < 0.2


async def test_the_same_key_is_spaced() -> None:
    limiter = RateLimiter(min_interval=0.2, max_concurrency=8)

    async def hold(key: str) -> None:
        async with limiter.slot(key):
            await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.gather(*(hold("same") for _ in range(3)))
    assert loop.time() - started >= 0.4


async def test_concurrency_is_capped() -> None:
    limiter = RateLimiter(min_interval=0, max_concurrency=2)
    inside = 0
    peak = 0

    async def hold() -> None:
        nonlocal inside, peak
        async with limiter.slot("k"):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(hold() for _ in range(6)))
    assert peak <= 2


async def test_key_map_stays_bounded_with_a_zero_interval() -> None:
    """Even with no spacing, the bookkeeping must not grow without limit."""
    limiter = RateLimiter(min_interval=0)
    for index in range(3000):
        await limiter.wait(f"key-{index}")
    assert limiter.tracked_keys <= 1024


async def test_key_map_is_bounded() -> None:
    limiter = RateLimiter(min_interval=0.001)
    for index in range(3000):
        async with limiter.slot(f"key-{index}"):
            pass
    assert limiter.tracked_keys <= 1024


async def test_slot_releases_the_semaphore_on_error() -> None:
    limiter = RateLimiter(min_interval=0, max_concurrency=1)
    with pytest.raises(RuntimeError):
        async with limiter.slot("k"):
            raise RuntimeError("boom")
    await limiter.wait("k")


def test_negative_interval_is_clamped() -> None:
    assert RateLimiter(min_interval=-5).min_interval == 0.0


# --- health ----------------------------------------------------------------


def test_plugin_errors_do_not_clobber_each_other() -> None:
    health = HealthService()
    health.mark_error("alpha broke", plugin="alpha")
    health.mark_error("beta broke", plugin="beta")
    health.mark_error(None, plugin="alpha")
    assert list(health.plugin_errors) == ["beta"]
    assert health.last_error is None


def test_process_level_error_is_tracked_separately() -> None:
    health = HealthService()
    health.mark_error("gateway died")
    health.mark_error(None, plugin="alpha")
    assert health.last_error == "gateway died"
    health.mark_error(None)
    assert health.last_error is None


def test_error_map_is_bounded() -> None:
    health = HealthService()
    for index in range(MAX_TRACKED_ERRORS * 2):
        health.mark_error(f"err {index}", plugin=f"p{index}")
    assert len(health.plugin_errors) <= MAX_TRACKED_ERRORS


def test_snapshot_reports_errors_with_age() -> None:
    health = HealthService()
    health.mark_error("broken", plugin="alpha")
    snapshot = health.snapshot(plugin_count=2, plugin_errors=1)
    assert snapshot["plugin_count"] == 2
    assert snapshot["plugin_errors"] == 1
    assert snapshot["plugin_error_details"][0]["plugin"] == "alpha"
    assert snapshot["plugin_error_details"][0]["age_seconds"] >= 0
    assert snapshot["uptime_seconds"] >= 0
    assert "started_at" in snapshot


def test_heartbeat_age_is_none_without_a_path() -> None:
    assert HealthService().heartbeat_age_seconds() is None


def test_heartbeat_age_reads_the_file(tmp_path: Path) -> None:
    health = HealthService()
    health.heartbeat_path = tmp_path / "beat"
    assert health.heartbeat_age_seconds() is None
    health.heartbeat_path.write_text("1", encoding="utf-8")
    assert health.heartbeat_age_seconds() is not None


def test_plugin_error_age() -> None:
    error = PluginError(plugin="a", message="m", at=100.0)
    assert error.age_seconds(110.0) == 10.0


# --- logging ---------------------------------------------------------------


def test_setup_logging_adds_stream_and_file(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs", "INFO")
    assert logger.propagate is False
    assert len(logger.handlers) == 2
    logger.info("hello")
    for handler in logger.handlers:
        handler.flush()
    assert (tmp_path / "logs" / "userbot.log").is_file()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    first = setup_logging(tmp_path / "logs")
    handlers = list(first.handlers)
    second = setup_logging(tmp_path / "logs")
    assert second is first
    assert len(second.handlers) == len(handlers)


def test_setup_logging_falls_back_on_a_bad_level(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs", "NONSENSE")
    assert logger.level == logging.INFO


def test_json_formatter_emits_one_object_per_line() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "userbot.test", logging.WARNING, __file__, 1, "что-то %s", ("пошло",), None
    )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "userbot.test"
    assert payload["message"] == "что-то пошло"
    assert "ts" in payload


def test_json_formatter_includes_exceptions() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "userbot", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    payload = json.loads(formatter.format(record))
    assert "kaboom" in payload["exception"]


def test_json_logging_mode(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs", "INFO", json=True)
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    logger.info("structured")
    logger.handlers[0].flush()
    lines = (tmp_path / "logs" / "userbot.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["message"] == "structured"


def test_get_logger_namespaces() -> None:
    assert get_logger().name == "userbot"
    assert get_logger("plugins").name == "userbot.plugins"


# --- task registry ---------------------------------------------------------


async def test_run_uninterruptible_completes_under_cancellation() -> None:
    """A cleanup path must finish even when its caller is being cancelled."""
    finished = False

    async def slow_cleanup() -> None:
        nonlocal finished
        await asyncio.sleep(0.15)
        finished = True

    async def victim() -> None:
        await run_uninterruptible(slow_cleanup())

    task = asyncio.create_task(victim())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished, "the cleanup coroutine must run to completion"


async def test_run_uninterruptible_swallows_cleanup_errors() -> None:
    async def boom() -> None:
        raise RuntimeError("cleanup failed")

    await run_uninterruptible(boom())


async def test_task_group_reports_surviving_tasks() -> None:
    async def stubborn() -> None:
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

    group = TaskGroup(name="g")
    task = group.spawn(stubborn(), name="stubborn")
    await asyncio.sleep(0)
    assert await group.cancel_all(timeout_seconds=0.05) is False
    assert task in group.pending
    task.cancel()


async def test_task_group_logs_task_failures(caplog: pytest.LogCaptureFixture) -> None:
    group = TaskGroup(name="g")

    async def boom() -> None:
        raise RuntimeError("task failed")

    group.spawn(boom(), name="boom")
    await asyncio.sleep(0.01)
    assert group.closed is False


async def test_task_group_pending_is_empty_after_cancel() -> None:
    group = TaskGroup(name="g")
    group.spawn(asyncio.sleep(60), name="sleeper")
    await asyncio.sleep(0)
    assert await group.cancel_all() is True
    assert group.pending == ()


# --- plugin context --------------------------------------------------------


NO_CLIENT = object()


def build_context(
    tmp_path: Path,
    name: str = "demo",
    *,
    client: Any = NO_CLIENT,
) -> PluginContext:
    resolved = FakeClient() if client is NO_CLIENT else client
    return PluginContext(
        plugin_name=name,
        plugin_path=tmp_path,
        client=resolved,
        settings=Settings(
            root_dir=tmp_path,
            data_dir=tmp_path / "data",
            plugin_dir=tmp_path / "plugins",
            log_dir=tmp_path / "logs",
            api_id=1,
            api_hash="t",
        ),
        storage=PluginStorage(tmp_path / "p.sqlite3", name),
        dispatcher=CommandDispatcher({1}),
        rate_limiter=RateLimiter(0),
        health=HealthService(),
        manager=cast(Any, None),
        instance=GoodPlugin(),
        logger=logging.getLogger(f"userbot.plugin.{name}"),
    )


async def test_context_normalises_command_names(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    context.register_command("  /Demo  ", _noop, aliases=("/Alias",))
    # Nothing reaches the dispatcher before activation.
    assert context.dispatcher.commands() == []
    await context.activate()
    # commands() reports one entry per command, not per alias.
    assert [item.name for item in context.dispatcher.commands()] == ["demo"]
    assert context._active_command_names == {"demo", "alias"}


def test_context_rejects_bad_registrations(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        context.register_command("  ", _noop)
    with pytest.raises(TypeError, match="must be callable"):
        context.register_command("x", 42)
    with pytest.raises(TypeError, match="must be callable"):
        context.register_handler(42, object())


async def test_context_requires_a_client_for_handlers(tmp_path: Path) -> None:
    from telethon import events

    context = build_context(tmp_path, client=None)
    context.register_handler(_noop, events.NewMessage(pattern="x"))
    with pytest.raises(RuntimeError, match="client"):
        await context.activate()
    assert context.active is False


async def test_context_guard_swallows_handler_errors(tmp_path: Path) -> None:
    context = build_context(tmp_path)

    def boom(event: Any) -> None:
        raise RuntimeError("handler failed")

    guarded = context._guard(boom)
    await guarded(FakeEvent("x"))  # must not raise


async def test_context_guard_propagates_cancellation(tmp_path: Path) -> None:
    context = build_context(tmp_path)

    async def cancelled(event: Any) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await context._guard(cancelled)(FakeEvent("x"))


async def test_context_spawn_after_shutdown_is_refused(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    await context.shutdown()
    with pytest.raises(RuntimeError, match="already stopped"):
        context.register_command("late", _noop)
    with pytest.raises(RuntimeError, match="already stopped"):
        context.spawn(asyncio.sleep(0))


async def test_context_activate_after_stop_is_refused(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    await context.shutdown()
    with pytest.raises(RuntimeError, match="already stopped"):
        await context.activate()


async def test_context_shutdown_is_idempotent(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    await context.shutdown()
    await context.shutdown()


async def test_context_registration_conflict_leaves_nothing_active(
    tmp_path: Path,
) -> None:
    from telethon import events

    context = build_context(tmp_path)
    context.dispatcher.register("taken", _noop, plugin_name="core")
    context.register_command("taken", _noop)
    context.register_handler(_noop, events.NewMessage(pattern="x"))
    with pytest.raises(ValueError, match="already registered"):
        await context.activate()
    assert context.active is False
    assert [item.name for item in context.dispatcher.commands()] == ["taken"]


async def test_context_prepare_requires_core_storage(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    with pytest.raises(RuntimeError, match="core storage"):
        await context.prepare(1)


async def test_context_prepare_runs_migration_once(tmp_path: Path) -> None:
    from userbot.storage import Storage

    core = Storage(tmp_path / "core.sqlite3")
    await core.initialize()
    context = build_context(tmp_path)
    context._core_storage = core
    calls: list[int] = []

    class Migrating(GoodPlugin):
        async def migrate(self, storage: Any) -> None:
            calls.append(1)

    context.instance = Migrating()
    try:
        await context.prepare(2)
        await context.prepare(2)
        assert calls == [1], "a recorded migration must not run again"
        assert await core.migration_version("demo") == 2
    finally:
        await core.close()


async def test_context_prepare_times_out_a_stuck_hook(tmp_path: Path) -> None:
    from userbot.storage import Storage

    core = Storage(tmp_path / "core.sqlite3")
    await core.initialize()
    context = build_context(tmp_path)
    context._core_storage = core

    class Stuck(GoodPlugin):
        async def setup(self, ctx: PluginContext) -> None:
            await asyncio.sleep(30)

    context.instance = Stuck()
    try:
        with pytest.raises(TimeoutError):
            await context.prepare(1)
    finally:
        await core.close()


async def test_context_prepare_records_nothing_when_migration_fails(
    tmp_path: Path,
) -> None:
    from userbot.storage import Storage

    core = Storage(tmp_path / "core.sqlite3")
    await core.initialize()
    context = build_context(tmp_path)
    context._core_storage = core

    class Failing(GoodPlugin):
        async def migrate(self, storage: Any) -> None:
            raise RuntimeError("migration failed")

    context.instance = Failing()
    try:
        with pytest.raises(RuntimeError, match="migration failed"):
            await context.prepare(1)
        assert await core.migration_version("demo") == 0
    finally:
        await core.close()


async def test_context_stop_hook_failure_is_contained(tmp_path: Path) -> None:
    class Exploding(GoodPlugin):
        async def stop(self) -> None:
            raise RuntimeError("stop exploded")

    context = build_context(tmp_path)
    context.instance = Exploding()
    await context.shutdown()
    assert context.active is False


async def test_context_stop_hook_timeout_is_contained(tmp_path: Path) -> None:
    class Slow(GoodPlugin):
        async def stop(self) -> None:
            await asyncio.sleep(30)

    context = build_context(tmp_path)
    context.instance = Slow()
    await context.shutdown(budget=0.2)


async def test_context_deactivate_clears_registrations(tmp_path: Path) -> None:
    from telethon import events

    client = FakeClient()
    context = build_context(tmp_path, client=client)
    context.register_command("demo", _noop, aliases=("d",))
    context.register_handler(_noop, events.NewMessage(pattern="x"))
    await context.activate()
    assert len(client.handlers) == 1
    assert {item.name for item in context.dispatcher.commands()} == {"demo"}
    await context.deactivate()
    assert client.handlers == []
    assert context.dispatcher.commands() == []


async def test_context_handler_removal_failure_is_contained(tmp_path: Path) -> None:
    from telethon import events

    class RudeClient(FakeClient):
        def remove_event_handler(self, callback: Any, event: Any = None) -> int:
            raise RuntimeError("cannot remove")

    client = RudeClient()
    context = build_context(tmp_path, client=client)
    context.register_handler(_noop, events.NewMessage(pattern="x"))
    await context.activate()
    await context.deactivate()
    assert context.active is False


async def test_lifecycle_hooks_are_optional() -> None:
    """A plugin may implement none of the hooks; the base class is a no-op."""
    bare = Plugin()
    context = cast(Any, None)
    await bare.migrate(context)
    await bare.setup(context)
    await bare.start()
    await bare.stop()


async def _noop(command: Any = None) -> None:
    return None
