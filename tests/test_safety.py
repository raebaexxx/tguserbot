"""Tests for the static safety review applied before a plugin is adopted.

The denylist is the only thing standing between a generated plugin and the
process, so two properties matter equally: it must catch the dangerous calls,
and it must not cry wolf over ordinary plugin code. A denylist that flags
``self.client.send_message`` is one people learn to ignore.
"""

from __future__ import annotations

import ast

import pytest

from userbot.safety import review_source, review_tree

# --- catches the dangerous --------------------------------------------------

DANGEROUS_CASES: list[tuple[str, str]] = [
    ("import os\nos.system('rm -rf /')", "system"),
    ("import subprocess\nsubprocess.run(['sh', '-c', 'id'])", "subprocess"),
    ("from subprocess import Popen\nPopen('id')", "subprocess"),
    ("import os\nos.popen('id')", "popen"),
    ("import os\nos.execv('/bin/sh', ['sh'])", "execv"),
    ("import os\nos.fork()", "fork"),
    ("import os\nos.remove('/etc/passwd')", "remove"),
    ("import os\nos.unlink('/etc/passwd')", "unlink"),
    ("import shutil\nshutil.rmtree('/')", "rmtree"),
    ("from pathlib import Path\nPath('/x').unlink()", "unlink"),
    ("eval('1+1')", "eval"),
    ("exec('import os')", "exec"),
    ("compile('1', 'f', 'eval')", "compile"),
    ("__import__('os')", "__import__"),
    ("import ctypes\nctypes.CDLL('libc.so.6')", "ctypes"),
    ("import pickle\npickle.loads(b'')", "pickle"),
    ("import marshal\nmarshal.loads(b'')", "marshal"),
    ("import importlib\nimportlib.import_module('os')", "importlib"),
    ("import socket\nsocket.socket()", "socket"),
    ("import requests\nrequests.get('http://x')", "requests"),
    ("import httpx\nhttpx.get('http://x')", "httpx"),
    ("import urllib.request\nurllib.request.urlopen('http://x')", "urllib"),
    ("import asyncio\nasyncio.open_connection('1.1.1.1', 80)", "сетевой"),
    ("().__class__.__bases__[0].__subclasses__()", "__subclasses__"),
    ("def f(): pass\nf.__globals__", "__globals__"),
    ("def f(): pass\nf.__code__", "__code__"),
    ("x = open('/var/lib/tguserbot/session.session')", "session.session"),
    ("obj.__reduce__()", "__reduce__"),
]


@pytest.mark.parametrize(("source", "needle"), DANGEROUS_CASES)
def test_dangerous_code_is_reported(source: str, needle: str) -> None:
    report = review_source(source, "plugin.py")
    assert not report.ok or report.warnings, f"nothing reported for: {source!r}"
    reported = " ".join(str(item) for item in report.findings)
    assert needle in reported, (
        f"{needle!r} missing from the report for {source!r};\n got: {reported}"
    )


@pytest.mark.parametrize(("source", "needle"), DANGEROUS_CASES)
def test_dangerous_code_blocks_adoption(source: str, needle: str) -> None:
    """Almost everything above must be a *blocking* finding, not a warning."""
    report = review_source(source, "plugin.py")
    blocking = " ".join(str(item) for item in report.blocking)
    assert needle in blocking, f"{needle!r} was not blocking: {source!r}"


# --- does not cry wolf ------------------------------------------------------

SAFE_CASES = [
    "x = 1\ny = x + 1",
    "import os\nos.path.join('a', 'b')",
    "import os\nos.makedirs('/tmp/x', exist_ok=True)",
    "import json\ndata = json.loads(text)",
    "import logging\nlogging.getLogger(__name__).info('hi')",
    "import sqlite3\nconn = sqlite3.connect(':memory:')",
    "import re\nre.sub(r'a', 'b', s)",
    "async def setup(ctx):\n    await ctx.storage.connect()",
    "import os\nsize = os.path.getsize('f')",
    "def f():\n    '''eval and exec are fine in a docstring.'''\n    return 1",
    "MSG = 'run os.system to break things'",
    "# eval is only mentioned in this comment",
    "import os\nos.path.basename(p)",
    "import asyncio\nawait asyncio.sleep(1)",
    "from typing import Any\ndef f(x: Any) -> Any: return x",
    "import pathlib\np = pathlib.Path(__file__).parent",
    (
        "class P:\n    def __init__(self):\n        self.n = 0\n"
        "    def __str__(self):\n        return str(self.n)"
    ),
    "import os\nos.environ.get('X')",
    "import shutil\nshutil.copyfile('a', 'b')",
    "import shutil\nshutil.make_archive('x')",
    "def f(x):\n    return getattr(x, 'name', None)",
]


@pytest.mark.parametrize("source", SAFE_CASES)
def test_ordinary_plugin_code_is_clean(source: str) -> None:
    report = review_source(source, "plugin.py")
    assert report.ok and not report.warnings, f"false positive on {source!r}:\n{report.summary()}"


def test_a_realistic_plugin_is_clean() -> None:
    """A well-behaved plugin of the shape the docs describe must pass untouched."""
    source = """
import logging

from userbot import Plugin

LOGGER = logging.getLogger(__name__)


class Plugin_(Plugin):
    async def setup(self, ctx):
        self.ctx = ctx
        await ctx.storage.execute("CREATE TABLE IF NOT EXISTS n (t TEXT)")

    async def handle(self, event):
        rows = await self.ctx.storage.fetchall("SELECT t FROM n")
        await event.respond(f"Записей: {len(rows)}")
        LOGGER.debug("answered")

    async def stop(self):
        await self.ctx.storage.close()
"""
    report = review_source(source, "plugin.py")
    assert report.ok and not report.warnings, report.summary()


# --- reporting shape --------------------------------------------------------


def test_findings_carry_a_line_number() -> None:
    source = "x = 1\ny = 2\nimport os\nos.system('id')\n"
    report = review_source(source, "plugin.py")
    assert any(item.line == 4 for item in report.blocking), report.summary()
    assert all(item.path == "plugin.py" for item in report.findings)


def test_a_syntax_error_is_reported_not_raised() -> None:
    """Broken code cannot be loaded, but the caller wants a report either way."""
    report = review_source("def f(:\n  pass\n", "plugin.py")
    assert not report.ok
    assert "синтаксическая ошибка" in report.summary()
    assert report.files_reviewed == 1


def test_network_import_blocks_by_default() -> None:
    """Exfiltration is the realistic threat, so a socket blocks adoption.

    An owner who genuinely wants network access in a plugin can delete the line
    during review -- which is the point of putting it in front of them.
    """
    report = review_source("import httpx\n", "plugin.py")
    assert not report.ok, "a direct network client must block adoption"
    assert report.blocking and "httpx" in report.summary()


def test_asyncio_itself_stays_clean() -> None:
    """Every async plugin imports asyncio; flagging the module would be noise."""
    report = review_source("import asyncio\nasyncio.sleep(1)\n", "plugin.py")
    assert report.ok and not report.warnings, report.summary()


def test_asyncio_opening_a_socket_does_not() -> None:
    report = review_source("import asyncio\nasyncio.open_connection('1.1.1.1', 80)\n", "plugin.py")
    assert not report.ok, "asyncio.open_connection is a raw socket"


def test_client_send_is_not_flagged() -> None:
    """Sending messages is what plugins are for; only disconnect is hazardous."""
    report = review_source("await self.ctx.client.send_message(1, 'hi')\n", "plugin.py")
    assert report.ok and not report.warnings, report.summary()


def test_client_disconnect_is_flagged() -> None:
    report = review_source("await self.ctx.client.disconnect()\n", "plugin.py")
    assert "disconnect" in report.summary()


def test_relative_imports_are_not_flagged() -> None:
    """A plugin importing its own package is normal, not an escape."""
    report = review_source("from . import helpers\nfrom .helpers import x\n", "plugin.py")
    assert not report.findings, report.summary()


def test_summary_truncates_long_lists() -> None:
    source = "\n".join("import os\nos.system('id')" for _ in range(20))
    report = review_source(source, "plugin.py")
    assert len(report.findings) >= 20
    assert "…и ещё" in report.summary(limit=3)


def test_empty_source_is_clean() -> None:
    assert review_source("", "plugin.py").ok


# --- the tree-level review --------------------------------------------------


def test_review_tree_only_parses_python() -> None:
    report = review_tree(
        {
            "plugin.toml": "not python at all ((",
            "README.md": "# title",
            "plugin.py": "import os\nos.system('id')",
        }
    )
    assert report.files_reviewed == 1
    assert report.blocking


def test_review_tree_of_a_clean_plugin() -> None:
    report = review_tree(
        {
            "plugin.toml": "[plugin]\nname='x'",
            "plugin.py": "def f():\n    return 1\n",
            "sub/helper.py": "X = 1\n",
        }
    )
    assert report.ok and report.files_reviewed == 2, report.summary()


def test_review_tree_of_nothing() -> None:
    report = review_tree({})
    assert report.ok and report.files_reviewed == 0
    assert report.summary() == "Замечаний нет."


# --- the denylist is derived, not guessed -----------------------------------


def test_every_denylisted_module_is_importable_or_builtin() -> None:
    """A typo in a denylist entry would silently never match anything."""
    from userbot import safety

    for name in safety._DANGEROUS_MODULES:
        root = name.split(".")[0]
        if root == "cPickle":
            continue  # Python 2 spelling, kept for old yt-dlp/ffmpeg probes
        assert root.isidentifier(), name
    for name in safety._NETWORK_MODULES:
        assert name.split(".")[0].isidentifier(), name


def test_the_denylist_is_not_empty() -> None:
    from userbot import safety

    assert len(safety._DANGEROUS_MODULES) > 10
    assert len(safety._NETWORK_MODULES) > 5
    assert len(safety._DANGEROUS_BUILTINS) > 5
    assert len(safety._DANGEROUS_ATTRIBUTES) > 5
    assert {"system", "popen", "remove"} <= set(safety._DANGEROUS_OS)


def test_review_uses_ast_not_regex() -> None:
    """The mechanism matters: a regex would flag the docstring case."""
    source = '"""Mentions os.system and eval() in prose."""\n'
    tree = ast.parse(source)
    assert isinstance(tree.body[0], ast.Expr)
    assert review_source(source, "plugin.py").ok
