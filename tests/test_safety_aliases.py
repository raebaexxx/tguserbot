"""The denylist has to see through a renamed import.

`README.md` says adoption "refuses anything that reaches for a shell, a socket,
a native library, or the session file". It refused `import os` + `os.system(...)`
and waved through the same thing with one word changed:

    import os;            os.system('...')   -> refused
    import os as o;       o.system('...')    -> allowed
    from os import system as run; run('...') -> allowed
    import builtins;      builtins.exec(...) -> allowed

`_dotted` renders the chain as written, and `visit_Call` only matched a head of
`os`, `subprocess` or `pty`, so a local name was enough to disappear. The
`_qualified` argument of `_check_module` was there for exactly this and was
never read.

This is a denylist, and `safety.py` says plainly that it is not a sandbox. The
defect being fixed is not that it is a denylist — it is that the report the owner
is asked to trust says "clean" for code that runs a shell. These tests are the
difference between the report being wrong and the report being as good as a
denylist can be.

Run with: pytest tests/test_safety_aliases.py -q --no-cov
"""

from __future__ import annotations

import pytest

from userbot.safety import review_source

PLUGIN = "plugin:Plugin"


def verdict(source: str) -> tuple[bool, list[str]]:
    report = review_source(source, "plugin.py")
    return report.ok, [finding.reason for finding in report.findings]


def body(code: str) -> str:
    """A whole plugin around the interesting line, so nothing is reported for
    a file that would not load anyway."""
    return (
        "from userbot.plugin_api import BasePlugin\n"
        "\n"
        "\n"
        "class Plugin(BasePlugin):\n"
        "    async def handle(self, command):\n"
        f"        {code}\n"
    )


# --- the same payload, spelled four ways ------------------------------------


PAYLOADS = [
    pytest.param("import os", "os.system('id')", id="plain"),
    pytest.param("import os as o", "o.system('id')", id="module-aliased"),
    pytest.param("import subprocess as sp", "sp.run(['id'])", id="subprocess-aliased"),
    pytest.param("import pty as p", "p.spawn(['id'])", id="pty-aliased"),
]


@pytest.mark.parametrize("statement, call", PAYLOADS)
def test_a_renamed_import_is_still_a_renamed_import(statement: str, call: str) -> None:
    ok, reasons = verdict(body(f"{statement}\n        {call}"))
    assert not ok, f"accepted: {statement} / {call}"
    assert reasons, "refused without saying why is barely better than accepted"


def test_a_from_import_alias_is_caught() -> None:
    ok, reasons = verdict(body("from os import system as run\n        run('id')"))
    assert not ok, "'from os import system as run' was accepted"
    assert any("run" in r or "os" in r for r in reasons), reasons


def test_a_from_import_of_a_dangerous_module_is_caught() -> None:
    ok, _ = verdict(body("from subprocess import run\n        run(['id'])"))
    assert not ok, "'from subprocess import run' was accepted"


def test_the_alias_still_points_at_the_right_reason() -> None:
    """A refusal that names something else is its own kind of useless."""
    _ok, reasons = verdict(body("import os as o\n        o.system('id')"))
    assert any("system" in r for r in reasons), reasons


# --- builtins are reachable, and always were --------------------------------


def test_builtins_exec_is_caught() -> None:
    ok, _ = verdict(body("import builtins\n        builtins.exec('import os; os.system(1)')"))
    assert not ok, "builtins.exec() was accepted"


def test_builtins_eval_is_caught() -> None:
    ok, _ = verdict(body("import builtins\n        builtins.eval('1+1')"))
    assert not ok, "builtins.eval() was accepted"


def test_compile_is_caught() -> None:
    ok, _ = verdict(body("compile('x = 1', 'f', 'exec')"))
    assert not ok, "compile() was accepted"


# --- submodules of a dangerous package --------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "import importlib.machinery",
        "import ctypes.util",
        "import multiprocessing.connection",
    ],
)
def test_a_submodule_of_a_refused_package_is_refused(statement: str) -> None:
    """The network list matched on the package root; the dangerous one did not.

    `import ctypes.util` also binds `ctypes`, so `ctypes.CDLL(...)` works after
    it — and a denylist that only refuses the exact spelling is a list of
    suggestions.

    `concurrent` is deliberately not in this list: a plugin that can start a
    process through it can already do so through `multiprocessing`, which is
    listed, and it runs only the plugin's own code. Refusing it would be noise.
    """
    ok, reasons = verdict(body(statement))
    assert not ok, f"accepted: {statement}"
    assert reasons


# --- the escape hatches Python offers ----------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "shutil.rmtree('/')",
        "os.unlink('/etc/passwd')",
    ],
)
def test_deletion_is_still_high(code: str) -> None:
    report = review_source(body(f"import shutil, os\n        {code}"), "plugin.py")
    severities = {f.reason.split("(")[0].strip() for f in report.findings}
    assert not report.ok, f"accepted: {code}"
    assert any("high" in str(f.severity) for f in report.findings), f"{code}: {severities}"


@pytest.mark.parametrize(
    "code",
    [
        "open('/var/lib/tguserbot/x/session' + '.session', 'rb')",
        "Path(d + '/session' '.session').read_bytes()",
    ],
)
def test_a_split_session_path_is_still_the_session_file(code: str) -> None:
    """The check matched a literal ending in `session.session`.

    Concatenation defeats it, and the concatenation is a thing a person writes
    when they are being careful about a denylist — which is exactly when the
    report needs to be right.
    """
    ok, _ = verdict(body(f"from pathlib import Path\n        {code}"))
    assert not ok, f"accepted: {code}"


def test_the_dunder_escape_hatches_are_flagged() -> None:
    ok, reasons = verdict(body("f = eval.__globals__\n        print(f)"))
    assert not ok, "eval.__globals__ was accepted"
    assert any("globals" in r for r in reasons), reasons


# --- and the plugin's own work is still clean -------------------------------


def test_an_ordinary_plugin_is_clean() -> None:
    ok, reasons = verdict(
        body(
            "import asyncio\n"
            "        from userbot.storage import Storage\n"
            "        text = await command.respond('привет')\n"
            "        await ctx.storage.execute('INSERT INTO t VALUES (?)', (1,))"
        )
    )
    assert ok, f"a normal plugin was flagged: {reasons}"


def test_a_string_mentioning_a_forbidden_word_is_not_flagged() -> None:
    """It is an AST walk precisely so that this is true."""
    ok, reasons = verdict(body("msg = 'не вызывайте os.system в плагинах'"))
    assert ok, reasons
