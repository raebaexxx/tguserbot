from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    """Small serialized SQLite gateway suitable for one userbot process."""

    def __init__(self, path: Path):
        self.path = path
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
        await self.execute("PRAGMA journal_mode=WAL")
        await self.execute("PRAGMA synchronous=NORMAL")
        await self.execute("PRAGMA foreign_keys=ON")
        await self.execute("PRAGMA busy_timeout=5000")
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
        columns = {
            str(row["name"]) for row in await self.fetchall("PRAGMA table_info(plugin_state)")
        }
        for column in ("source_url", "source_subpath"):
            if column not in columns:
                try:
                    await self.execute(f"ALTER TABLE plugin_state ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

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

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Storage.initialize() must be called first")
        return self._connection

    async def _run(self, operation: Callable[[], Any]) -> Any:
        async with self._lock:
            return await asyncio.to_thread(operation)

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        def operation() -> int:
            connection = self._require_connection()
            cursor = connection.execute(sql, tuple(parameters))
            connection.commit()
            return (
                cursor.lastrowid
                if cursor.lastrowid is not None and cursor.lastrowid >= 0
                else cursor.rowcount
            )

        return await self._run(operation)

    async def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> int:
        def operation() -> int:
            connection = self._require_connection()
            cursor = connection.executemany(sql, parameters)
            connection.commit()
            return cursor.rowcount

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

    async def close(self) -> None:
        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        await self._run(connection.commit)
        await self._run(connection.close)
