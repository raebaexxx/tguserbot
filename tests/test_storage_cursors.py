"""A statement left in progress makes the *next* commit fail.

`Storage.initialize()` ran each PRAGMA as

    await self._run(partial(self._require_connection().execute, statement))

`Connection.execute` returns a cursor, and for a statement that produces rows the
cursor is stepped once and then left holding an unfinished statement. The cursor
is not freed when the `await` returns: it is the *result of the Future* that
`asyncio.to_thread` built, so the Future still references it, and it is freed only
when the collector finalizes that Future — on whichever thread gets there first.
So whether the statement is still in progress when `migrate()` reaches its
`INSERT` … `COMMIT` is not decided by this code at all; it is decided by the GC.

When it loses that race, SQLite refuses the commit:

    sqlite3.OperationalError: cannot commit transaction - SQL statements in progress

and `initialize()` raises, so the bot does not start. Observed about once in 500
starts under load, which is why it survived 1079 passing tests: a real defect
that presents as an intermittent startup failure. A `CREATE TABLE` does not open
a transaction, so every probe that only did DDL saw nothing — `commit()` was a
no-op and never ran the check. It takes an INSERT to surface.

The fix closes each cursor inside the locked operation, so no statement outlives
the call that started it. These tests pin that by construction: the cursors are
pinned by the double so a collection pass cannot rescue the buggy code, which
turns a once-in-500 race into an always failure.
"""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from userbot import storage
from userbot.storage import PluginStorage, Storage


class PinningConnection:
    """A connection proxy that keeps every cursor ``execute`` hands out alive.

    The whole point: the defect is a cursor that outliving its statement, and in
    production the collector eventually frees one. Pinning takes the collector out
    of the picture, so the buggy code fails on every run instead of one run in five
    hundred.

    It has to be a proxy rather than a ``sqlite3.Connection`` subclass:
    ``Connection.execute`` is implemented in C and does not call the overridable
    ``cursor()``, so a subclass's ``cursor()`` override is simply never called --
    which is exactly how the first version of this double came to pin nothing and
    let the buggy code pass. :func:`test_the_pinning_double_really_pins` exists
    because of that.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        #: Every cursor handed out, kept alive on purpose.
        self.pinned_cursors: list[sqlite3.Cursor] = []

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, tuple(parameters))
        self.pinned_cursors.append(cursor)
        return cursor

    def __getattr__(self, name: str) -> object:
        # Everything else (commit, rollback, close, set_authorizer, cursors, ...)
        # goes straight to the real connection.
        return getattr(self._connection, name)

    def release(self) -> None:
        for cursor in self.pinned_cursors:
            with_close = getattr(cursor, "close", None)
            if callable(with_close):
                with_close()
        self.pinned_cursors.clear()


def use_pinned_connection(gateway: storage._SqliteGateway) -> PinningConnection:
    """Make ``gateway`` open a connection whose cursors cannot be collected."""
    proxy = PinningConnection(_open_raw(gateway.path))
    gateway._open = lambda: proxy  # type: ignore[assignment, method-assign, return-value]
    return proxy


def _open_raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


@pytest.fixture
def pinned_gateway(tmp_path: Path) -> Iterator[storage._SqliteGateway]:
    gateway = storage._SqliteGateway(tmp_path / "pinned.sqlite3")
    proxy = use_pinned_connection(gateway)
    yield gateway
    # A cursor left open here would keep a statement in progress on a connection
    # that is about to be closed, and the next test would inherit the noise.
    proxy.release()


# --- the defect ------------------------------------------------------------


async def test_a_pinned_pragma_cursor_does_not_break_the_migration(
    pinned_gateway: storage._SqliteGateway,
) -> None:
    """The failure itself, made deterministic.

    With the PRAGMA cursors pinned, ``initialize()`` on the current code raises
    ``cannot commit transaction - SQL statements in progress`` from the
    ``INSERT INTO core_schema`` commit. With the fix, the cursors are closed
    inside the locked operation and there is nothing left in progress.
    """
    await pinned_gateway.initialize()
    row = await pinned_gateway.fetchone("SELECT version FROM core_schema WHERE id = 1")
    assert row is not None
    assert int(row["version"]) == storage.CORE_SCHEMA_VERSION


async def test_the_statement_is_finished_before_the_caller_waits_for_it(
    tmp_path: Path,
) -> None:
    """The invariant, stated as behaviour rather than as a race.

    After ``initialize()`` returns, nothing on the connection may still be in
    progress -- which is what ``INSERT`` then ``commit`` measures, because at the
    default isolation level a DDL statement opens no transaction and the commit
    would be a no-op that hides the problem.
    """
    gateway = Storage(tmp_path / "core.sqlite3")
    await gateway.initialize()
    try:
        await gateway.execute(
            "INSERT INTO plugin_kv (plugin_name, key, value, updated_at) "
            "VALUES ('probe', 'k', 'v', 'now')"
        )
        await gateway.execute("UPDATE plugin_kv SET value = 'w' WHERE key = 'k'")
        rows = await gateway.fetchall("SELECT value FROM plugin_kv WHERE key = 'k'")
        assert [row["value"] for row in rows] == ["w"]
    finally:
        await gateway.close()


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_a_sandboxed_database_does_not_survive_on_hope(
    tmp_path: Path, sandboxed: bool
) -> None:
    """A plugin's own database goes through the same ``initialize()``.

    The plugin sandbox is the other caller of this code, and its ``PRAGMA
    table_info`` runs through the very same path, so the same failure applies to
    every plugin that migrates.
    """
    gateway = storage._SqliteGateway(
        tmp_path / f"sandboxed-{sandboxed}.sqlite3", sandboxed=sandboxed
    )
    proxy = use_pinned_connection(gateway)
    try:
        await gateway.initialize()
        await gateway.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await gateway.execute_insert("INSERT INTO kv (key, value) VALUES ('a', 'b')")
        assert await gateway.fetchone("SELECT value FROM kv WHERE key = 'a'") == {"value": "b"}
    finally:
        proxy.release()


# --- the doubles must not be the thing that hides the defect --------------


async def test_the_pinning_double_really_pins(tmp_path: Path) -> None:
    """A double that did not pin would make the test above pass on buggy code.

    That has happened in this suite before -- twice, in this project -- so the
    double itself is checked: it must hand out a cursor, and that cursor must
    still be usable after a collection pass.
    """
    gateway = storage._SqliteGateway(tmp_path / "double.sqlite3")
    proxy = use_pinned_connection(gateway)
    gateway._connection = proxy  # type: ignore[assignment]
    try:
        # One statement, exactly as initialize() runs it, without migrating: this
        # test is about the double, not about the fix.
        await gateway._run(lambda: proxy.execute("PRAGMA journal_mode=WAL"))
        assert proxy.pinned_cursors, "the double handed out no cursor, so it pinned nothing"
        gc.collect()  # the collector that rescues the buggy code in production
        alive = [cursor for cursor in proxy.pinned_cursors if _is_usable(cursor)]
        assert alive, "the pinned cursors did not survive the collector"
    finally:
        proxy.release()
        await gateway.close()


def _is_usable(cursor: sqlite3.Cursor) -> bool:
    try:
        cursor.fetchone()
    except sqlite3.ProgrammingError:
        return False
    return True


async def test_a_cursored_statement_really_blocks_a_commit(tmp_path: Path) -> None:
    """The claim the diagnosis rests on, verified against real SQLite.

    Without this, "closing the cursors fixes it" would be a belief about SQLite
    rather than a fact, and a future SQLite could make it false without any test
    noticing. This is the two lines that fail.
    """
    connection = sqlite3.connect(tmp_path / "x.sqlite3", check_same_thread=False, timeout=5.0)
    try:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        # `PRAGMA journal_mode=WAL` is the statement ``initialize()`` runs first,
        # and it is the one that blocks: SQLite holds it as an open read of the
        # database header, so the statement stays in progress until it is reset.
        # Checked against the real engine rather than assumed -- a pragma that
        # merely returns a row does not block, and assuming otherwise would have
        # made this test assert something untrue.
        stale = connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO t DEFAULT VALUES")
        assert connection.in_transaction, "an INSERT must open a transaction"
        with pytest.raises(sqlite3.OperationalError, match="statements in progress"):
            connection.commit()
        stale.close()
        connection.execute("INSERT INTO t DEFAULT VALUES")
        connection.commit()  # no longer blocked
    finally:
        connection.close()


async def test_a_ddl_only_probe_would_have_hidden_it(tmp_path: Path) -> None:
    """Why this went unnoticed: DDL does not open a transaction.

    The obvious probe -- run a PRAGMA, then run some DDL and commit -- passes
    either way, which is why reading the code and testing DDL both said the code
    was fine.
    """
    connection = sqlite3.connect(tmp_path / "x.sqlite3", check_same_thread=False, timeout=5.0)
    try:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        stale = connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")
        assert not connection.in_transaction, "DDL alone opens no transaction"
        connection.commit()  # a no-op: the blocked statement goes unnoticed
        stale.close()
    finally:
        connection.close()


# --- the plugin-facing path -----------------------------------------------


async def test_plugin_storage_initialises_cleanly(tmp_path: Path) -> None:
    """``PluginStorage`` is the shipped caller, so it is the one worth asserting."""
    plugin = PluginStorage(tmp_path / "notes.sqlite3", "notes")
    await plugin.initialize()
    try:
        await plugin.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        await plugin.execute_insert("INSERT INTO notes (body) VALUES ('hello')")
        rows = await plugin.fetchall("SELECT body FROM notes")
        assert [row["body"] for row in rows] == ["hello"]
    finally:
        await plugin.close()


async def test_repeated_initialisation_stays_clean(tmp_path: Path) -> None:
    """Startup runs this on every start, so the second one must be as safe."""
    gateway = Storage(tmp_path / "core.sqlite3")
    await gateway.initialize()
    try:
        # migrate() is idempotent by design; run it again through the real path.
        await gateway.migrate()
        await gateway.execute(
            "INSERT INTO plugin_kv (plugin_name, key, value, updated_at) "
            "VALUES ('probe', 'k2', 'v', 'now')"
        )
        assert await gateway.fetchone("SELECT key FROM plugin_kv WHERE key = 'k2'")
    finally:
        await gateway.close()


# --- the concurrency the production code actually has ----------------------


async def test_a_collection_pass_on_another_thread_cannot_break_a_commit(
    tmp_path: Path,
) -> None:
    """The environment that produced the incident: several pool threads, and a
    collector running on one of them.

    Not a race test: it asserts nothing about how often it would have failed, only
    that the fix holds under the shape of the real workload -- repeated
    initializations with a thread churning collections throughout.
    """
    failures: list[str] = []
    stop = threading.Event()

    def churn() -> None:
        # With a pause, so the thread perturbs the collector's timing without
        # starving the event loop of the GIL -- the point is that a collection
        # pass happens on another thread while this test runs, not that this test
        # is slow.
        while not stop.is_set():
            gc.collect()
            time.sleep(0.001)

    # One thread, not four: this is a guard against the fix regressing, not a
    # search for the failure rate, and several collectors only make the suite slow
    # without making the assertion sharper.
    threads = [threading.Thread(target=churn, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        for index in range(10):
            gateway = Storage(tmp_path / f"db-{index}.sqlite3")
            try:
                await gateway.initialize()
                await gateway.execute(
                    "INSERT INTO plugin_kv (plugin_name, key, value, updated_at) "
                    "VALUES ('probe', 'k', 'v', 'now')"
                )
            except sqlite3.OperationalError as exc:
                failures.append(str(exc))
            finally:
                await gateway.close()
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    assert not failures, failures
