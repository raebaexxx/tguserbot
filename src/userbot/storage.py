from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

#: Statements a sandboxed plugin must never run. ``ATTACH`` is the important
#: one: with it a plugin could attach the core bookkeeping database and drop
#: ``plugin_state`` from the outside.
#:
#: This is a cheap pre-check, not the boundary. It has to be, because it is what
#: produces an error a plugin author can read, and the boundary itself is the
#: authoriser installed on the connection -- see :func:`_sandbox_authorizer`.
FORBIDDEN_SQL = re.compile(
    r"^\s*(attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)

#: A single statement only; blocks stacked ``; DROP TABLE ...`` payloads.
STACKED_STATEMENT = re.compile(r";\s*\S")

#: Bumped whenever the core schema below changes. Recorded in ``core_schema``.
CORE_SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StorageError(RuntimeError):
    pass


class SandboxedSqlError(StorageError):
    pass


def strip_sql_comments(sql: str) -> str:
    """Remove comments, so a pre-check sees the statement SQLite would run.

    ``FORBIDDEN_SQL`` is anchored with ``^\\s*``, and ``\\s`` matches neither
    ``-`` nor ``/``, so ``-- x\\nATTACH DATABASE ...`` passed it while SQLite
    parsed the comment away and executed the attach. Verified before the fix:
    a plugin could attach the core database and write to ``plugin_state``
    through a payload that began with a comment.

    String literals are respected, because a ``'-- not a comment'`` in a query is
    a value and not a comment to be swallowed.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    quote: str | None = None
    while index < length:
        char = sql[index]
        if quote is not None:
            out.append(char)
            if char == quote:
                # A doubled quote is an escaped quote, not the end of the string.
                if index + 1 < length and sql[index + 1] == quote:
                    out.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


#: What a plugin's connection refuses outright. These are SQLite's own action
#: codes, asked at parse time on the statement the engine actually built, so
#: there is no spelling of ``ATTACH`` that reaches them and no comment that
#: hides one.
_DENIED_SQLITE_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA,
    }
)


def _sandbox_authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, trigger: Any) -> int:
    """The actual boundary. Returns DENY for anything that leaves the file."""
    if action in _DENIED_SQLITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def guard_sandbox_sql(sql: str) -> None:
    """Reject statements that could escape a plugin's own database file."""
    statement = strip_sql_comments(sql).lstrip("﻿ \t\r\n")
    if FORBIDDEN_SQL.match(statement):
        head = statement.split(None, 1)[0] if statement.split() else "?"
        raise SandboxedSqlError(f"statement is not allowed inside a plugin database: {head!r}")
    if STACKED_STATEMENT.search(statement.rstrip().rstrip(";")):
        raise SandboxedSqlError("only a single SQL statement is allowed")


class _SqliteGateway:
    """One serialized SQLite connection, driven from asyncio via a thread pool."""

    def __init__(self, path: Path, *, sandboxed: bool = False) -> None:
        self.path = path
        #: A plugin's connection is fenced in by SQLite itself; the core one is
        #: not, because the core legitimately runs PRAGMA and ATTACH-free
        #: migrations.
        self.sandboxed = sandboxed
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def initialized(self) -> bool:
        return self._connection is not None

    async def initialize(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await asyncio.to_thread(self._open)
        for statement in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA busy_timeout=5000",
        ):
            await self._run(partial(self._require_connection().execute, statement))
        # `migrate` reads PRAGMA table_info, so the fence goes after it: the
        # authoriser refuses PRAGMA, and these are our own statements, run before
        # any plugin holds this connection.
        await self.migrate()
        if self.sandboxed:
            self._fence()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        connection.row_factory = sqlite3.Row
        if self.sandboxed:
            # And the other way out of a file-only sandbox: a shared library
            # loaded into the process. Off unless asked for, and never asked for
            # here.
            connection.enable_load_extension(False)
        return connection

    def _fence(self) -> None:
        """Put the sandbox in place, once our own setup statements are done.

        ``guard_sandbox_sql`` reads the text of a statement; the authoriser runs
        inside the engine on the parsed one, so no comment, alias or amount of
        whitespace gets past it. The text check stays because it produces an
        error a plugin author can act on.

        Installed after the PRAGMAs in :meth:`initialize` rather than at connect
        time, because those are ours and the authoriser refuses PRAGMA. The window
        is the length of this method, on a connection no plugin has been handed
        yet.
        """
        self._require_connection().set_authorizer(_sandbox_authorizer)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("Storage.initialize() must be called first")
        return self._connection

    async def _run(self, operation: Callable[[], Any]) -> Any:
        async with self._lock:
            return await asyncio.to_thread(operation)

    async def migrate(self) -> None:
        """Create the core bookkeeping schema. Safe to run on every start."""
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS core_schema (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_state (
                name TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_ref TEXT,
                source_url TEXT,
                source_subpath TEXT,
                version TEXT,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_migrations (
                plugin_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (plugin_name, version)
            )
            """
        )
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_kv (
                plugin_name TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plugin_name, key)
            )
            """
        )
        row = await self.fetchone("SELECT version FROM core_schema WHERE id = 1")
        current = int(row["version"]) if row is not None else 0
        if current < CORE_SCHEMA_VERSION:
            await self._add_missing_columns()
            await self.execute(
                "INSERT INTO core_schema (id, version, applied_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
                "applied_at = excluded.applied_at",
                (CORE_SCHEMA_VERSION, _now()),
            )

    async def _add_missing_columns(self) -> None:
        """Bring a database written by an older version up to the current shape."""
        rows = await self.fetchall("PRAGMA table_info(plugin_state)")
        existing = {str(row["name"]) for row in rows}
        for column in ("source_url", "source_subpath"):
            if column not in existing:
                await self.execute(f"ALTER TABLE plugin_state ADD COLUMN {column} TEXT")

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run a statement and return the number of affected rows.

        Never returns ``lastrowid``: for DELETE/UPDATE that value is a stale
        rowid left over from an unrelated INSERT, which previously made
        ``notes delete`` report success for notes it never touched.
        """

        def operation() -> int:
            connection = self._require_connection()
            try:
                cursor = connection.execute(sql, tuple(parameters))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return max(cursor.rowcount, 0)

        return await self._run(operation)

    async def execute_insert(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        """Run an INSERT and return the new rowid."""

        def operation() -> int:
            connection = self._require_connection()
            try:
                cursor = connection.execute(sql, tuple(parameters))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return int(cursor.lastrowid or 0)

        return await self._run(operation)

    async def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> int:
        rows = [tuple(item) for item in parameters]

        def operation() -> int:
            connection = self._require_connection()
            try:
                cursor = connection.executemany(sql, rows)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return max(cursor.rowcount, 0)

        return await self._run(operation)

    async def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            row = self._require_connection().execute(sql, tuple(parameters)).fetchone()
            return dict(row) if row is not None else None

        return await self._run(operation)

    async def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def operation() -> list[dict[str, Any]]:
            rows = self._require_connection().execute(sql, tuple(parameters)).fetchall()
            return [dict(row) for row in rows]

        return await self._run(operation)

    async def close(self) -> None:
        if self._connection is None:
            return

        def operation(connection: sqlite3.Connection) -> None:
            try:
                connection.commit()
            finally:
                connection.close()

        # Hold the lock for the whole close so no caller can grab the
        # connection between the commit and the close.
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await asyncio.to_thread(operation, connection)

    async def kv_get(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    async def kv_set(self, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )

    async def kv_keys(self) -> list[str]:
        rows = await self.fetchall("SELECT key FROM kv ORDER BY key")
        return [str(row["key"]) for row in rows]

    async def _ensure_kv_table(self) -> None:
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


class Storage(_SqliteGateway):
    """Core bookkeeping database. Plugins never see this object directly."""

    async def upsert_plugin_state(
        self,
        name: str,
        source: str,
        status: str,
        *,
        source_ref: str | None = None,
        source_url: str | None = None,
        source_subpath: str | None = None,
        version: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO plugin_state
                (name, source, source_ref, source_url, source_subpath,
                 version, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source=excluded.source,
                source_ref=excluded.source_ref,
                source_url=excluded.source_url,
                source_subpath=excluded.source_subpath,
                version=excluded.version,
                status=excluded.status,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                name,
                source,
                source_ref,
                source_url,
                source_subpath,
                version,
                status,
                error,
                _now(),
            ),
        )

    async def plugin_states(self) -> list[dict[str, Any]]:
        return await self.fetchall("SELECT * FROM plugin_state ORDER BY name")

    async def get_plugin_state(self, name: str) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM plugin_state WHERE name = ?", (name,))

    async def delete_plugin_state(self, name: str) -> None:
        await self.execute("DELETE FROM plugin_state WHERE name = ?", (name,))

    async def migration_version(self, plugin_name: str) -> int:
        row = await self.fetchone(
            "SELECT MAX(version) AS version FROM plugin_migrations WHERE plugin_name = ?",
            (plugin_name,),
        )
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    async def record_migration(self, plugin_name: str, version: int) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO plugin_migrations "
            "(plugin_name, version, applied_at) VALUES (?, ?, ?)",
            (plugin_name, version, _now()),
        )

    async def get_value(self, plugin_name: str, key: str) -> str | None:
        row = await self.fetchone(
            "SELECT value FROM plugin_kv WHERE plugin_name = ? AND key = ?",
            (plugin_name, key),
        )
        return None if row is None else str(row["value"])

    async def set_value(self, plugin_name: str, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO plugin_kv (plugin_name, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plugin_name, key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (plugin_name, key, value, _now()),
        )

    async def schema_version(self) -> int:
        row = await self.fetchone("SELECT version FROM core_schema WHERE id = 1")
        return int(row["version"]) if row is not None else 0


class PluginStorage:
    """Isolated per-plugin data access.

    Each plugin gets its own SQLite file under ``data/plugin-data``. A plugin can
    therefore never read or destroy the core bookkeeping tables, cannot collide
    with another plugin's table names, and cannot stall the manager: a
    misbehaving query blocks only its own connection's lock.
    """

    def __init__(self, path: Path, plugin_name: str) -> None:
        self._gateway = _SqliteGateway(path, sandboxed=True)
        self.plugin_name = plugin_name
        self.path = path

    @property
    def initialized(self) -> bool:
        return self._gateway.initialized

    async def initialize(self) -> None:
        await self._gateway.initialize()

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        guard_sandbox_sql(sql)
        return await self._gateway.execute(sql, parameters)

    async def execute_insert(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        guard_sandbox_sql(sql)
        return await self._gateway.execute_insert(sql, parameters)

    async def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> int:
        guard_sandbox_sql(sql)
        return await self._gateway.executemany(sql, parameters)

    async def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        guard_sandbox_sql(sql)
        return await self._gateway.fetchone(sql, parameters)

    async def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        guard_sandbox_sql(sql)
        return await self._gateway.fetchall(sql, parameters)

    async def get_value(self, key: str) -> str | None:
        return await self._gateway.kv_get(key)

    async def set_value(self, key: str, value: str) -> None:
        await self._gateway.kv_set(key, value)

    async def delete_value(self, key: str) -> int:
        return await self._gateway.execute("DELETE FROM kv WHERE key = ?", (key,))

    async def keys(self) -> list[str]:
        return await self._gateway.kv_keys()

    async def close(self) -> None:
        await self._gateway.close()


async def create_plugin_storage(root: Path, plugin_name: str) -> PluginStorage:
    """Open (creating if needed) the sandbox database for one plugin."""
    directory = root / plugin_name
    directory.mkdir(parents=True, exist_ok=True)
    storage = PluginStorage(directory / "plugin.sqlite3", plugin_name)
    await storage.initialize()
    await storage._gateway._ensure_kv_table()
    return storage
