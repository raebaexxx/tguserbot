"""The per-plugin database has to actually be a sandbox.

`SECURITY.md` promises that a plugin "cannot read or destroy the core
bookkeeping tables or another plugin's tables", and that "`ATTACH`, `PRAGMA`,
`VACUUM`, and stacked SQL statements are refused". It was enforced by matching a
regular expression against the statement text, and SQLite's parser is not that
regular expression:

    REFUSED  'ATTACH DATABASE ? AS core'
    ALLOWED  '-- x\\nATTACH DATABASE ? AS core'      -> executed, wrote to the attached file
    ALLOWED  '/* x */ ATTACH DATABASE ? AS core'      -> executed

`^\\s*` does not match `-` or `/`, and a comment is not a statement, so the guard
approved a payload that SQLite then ran. The gap was found by running it, not by
reading it.

Matching text can never be the boundary here — there is always another spelling.
These tests are written against what SQLite actually does, and the fix installs
an authoriser, which runs inside the engine on the parsed statement where no
comment, alias or amount of whitespace can reach it.

Run with: pytest tests/test_storage_sandbox.py -q --no-cov
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from userbot import storage
from userbot.storage import PluginStorage, SandboxedSqlError


def make(tmp_path: Path, name: str = "probe") -> PluginStorage:
    plugin = PluginStorage(tmp_path / f"{name}.sqlite3", name)
    return plugin


# --- the bypasses that were verified against a real SQLite -----------------


BYPASSES = [
    pytest.param("-- x\nATTACH DATABASE ? AS core", id="line-comment"),
    pytest.param("/* x */ ATTACH DATABASE ? AS core", id="block-comment"),
    pytest.param("\n\t--x\n ATTACH DATABASE ? AS core", id="indented-comment"),
    pytest.param("  \n--\nPRAGMA core.journal_mode", id="comment-then-pragma"),
    pytest.param("/*a*//*b*/ATTACH DATABASE ? AS core", id="stacked-comments"),
    pytest.param("-- x\nDETACH DATABASE core", id="comment-then-detach"),
    pytest.param("/* x */ VACUUM", id="block-comment-then-vacuum"),
    pytest.param("﻿ATTACH DATABASE ? AS core", id="byte-order-mark"),
]


@pytest.mark.parametrize("sql", BYPASSES)
def test_a_comment_does_not_get_past_the_guard(sql: str) -> None:
    """The guard is the cheap check; it must not approve what SQLite would run."""
    with pytest.raises(SandboxedSqlError):
        storage.guard_sandbox_sql(sql)


@pytest.mark.parametrize("sql", BYPASSES)
async def test_the_whole_path_refuses_them_too(sql: str, tmp_path: Path) -> None:
    """Through the public API, which is where a plugin would actually call it."""
    plugin = make(tmp_path)
    await plugin.initialize()
    with pytest.raises(SandboxedSqlError):
        await plugin.execute(sql, (str(tmp_path / "core.sqlite3"),))


async def test_the_core_database_is_untouched(tmp_path: Path) -> None:
    """The payload runs against a real second connection, and the data survives.

    Refusing a string is not evidence the database is protected. Opening the core
    file separately and finding the row still there is.
    """
    core = tmp_path / "core.sqlite3"
    with sqlite3.connect(core) as setup:
        setup.execute("CREATE TABLE plugin_state(name TEXT)")
        setup.execute("INSERT INTO plugin_state VALUES ('victim')")
        setup.commit()

    plugin = make(tmp_path)
    await plugin.initialize()

    with pytest.raises(SandboxedSqlError):
        await plugin.execute("-- x\nATTACH DATABASE ? AS core", (str(core),))
    # The plain spelling is caught by the text guard; the authoriser is what
    # catches the one that is not, which the next test goes straight at.
    with pytest.raises((SandboxedSqlError, sqlite3.DatabaseError)):
        await plugin.execute("ATTACH DATABASE ? AS core", (str(core),))

    with sqlite3.connect(core) as check:
        rows = check.execute("SELECT name FROM plugin_state").fetchall()
    assert rows == [("victim",)], f"the core table was modified: {rows}"


async def test_the_authoriser_denies_even_without_the_text_guard(tmp_path: Path) -> None:
    """The text guard is early rejection and a good error message, not the wall.

    Removing it must not turn the sandbox into a suggestion.
    """
    core = tmp_path / "core.sqlite3"
    plugin = make(tmp_path)
    await plugin.initialize()
    # Straight at the engine, with the authoriser installed by the plugin.
    connection = plugin._gateway._require_connection()
    with pytest.raises(sqlite3.DatabaseError) as info:
        connection.execute("ATTACH DATABASE ? AS core", (str(core),))
    assert "not authorized" in str(info.value).lower(), str(info.value)


# --- the statements that must keep working ----------------------------------


async def test_ordinary_sql_still_works(tmp_path: Path) -> None:
    """A sandbox that blocks the plugin's own work is not a sandbox, a wall."""
    plugin = make(tmp_path)
    await plugin.initialize()
    await plugin.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    await plugin.execute("INSERT INTO notes (body) VALUES (?)", ("привет",))
    rows = await plugin.fetchall("SELECT body FROM notes")
    assert [row["body"] for row in rows] == ["привет"]


async def test_a_multi_row_write_still_works(tmp_path: Path) -> None:
    """A sandbox that refuses the plugin's own work is a wall, not a sandbox.

    This also stands in for a transaction: the authoriser must not deny the
    ordinary reads and writes that a real plugin does, or the feature it is
    meant to protect becomes unusable.
    """
    plugin = make(tmp_path)
    await plugin.initialize()
    await plugin.execute("CREATE TABLE t(x INTEGER)")
    for value in range(5):
        await plugin.execute("INSERT INTO t VALUES (?)", (value,))
    await plugin.execute("UPDATE t SET x = x + 100 WHERE x > 2")
    rows = await plugin.fetchall("SELECT x FROM t ORDER BY x")
    assert [row["x"] for row in rows] == [0, 1, 2, 103, 104], rows


async def test_a_string_containing_the_word_attach_is_fine(tmp_path: Path) -> None:
    """The guard must not become so eager that it refuses real work."""
    plugin = make(tmp_path)
    await plugin.initialize()
    await plugin.execute("CREATE TABLE t(body TEXT)")
    await plugin.execute("INSERT INTO t VALUES (?)", ("attach the file to the end",))
    rows = await plugin.fetchall("SELECT body FROM t")
    assert rows[0]["body"] == "attach the file to the end"


async def test_load_extension_is_refused(tmp_path: Path) -> None:
    """The other way out of a file-only sandbox: a shared library in the process."""
    plugin = make(tmp_path)
    await plugin.initialize()
    # Reaches past the public API on purpose: the text guard is not the wall, and
    # the wall has to be tested where a payload would actually meet it.
    connection = plugin._gateway._require_connection()
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        connection.load_extension("libsqlite3.so.0")
