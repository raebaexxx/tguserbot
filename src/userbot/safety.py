"""Static safety review of plugin source, applied before anything is loaded.

Adopting a plugin means running third-party code inside the bot's process, with
the Telegram session and the filesystem in reach. Nothing here *prevents* that,
and it is not a sandbox: a determined plugin can get around a denylist. What it
does is make the dangerous parts obvious at review time, before the owner
commits, instead of surprising them afterwards.

The checks are an AST walk, not a regex over text, so a string that merely
mentions ``os.system`` is not flagged while a call to it is. Every finding
carries the line number and the reason, because "suspicious import" is only
useful next to "on line 12, in plugin.py".
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "SafetyReport", "review_source", "review_tree"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a human should look at before trusting this code."""

    path: str
    line: int
    severity: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.severity}] {self.reason}"


@dataclass(frozen=True, slots=True)
class SafetyReport:
    findings: tuple[Finding, ...] = ()
    files_reviewed: int = 0

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "high")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity != "high")

    @property
    def ok(self) -> bool:
        """Whether adoption may proceed. Warnings do not block."""
        return not self.blocking

    def summary(self, limit: int = 8) -> str:
        if not self.findings:
            return "Замечаний нет."
        lines = [str(item) for item in self.findings[:limit]]
        if len(self.findings) > limit:
            lines.append(f"…и ещё {len(self.findings) - limit}")
        return "\n".join(lines)


#: Modules that let a plugin escape the process, reach the network on its own,
#: or execute code the loader never vetted.
_DANGEROUS_MODULES = {
    "subprocess": "запуск процессов ОС",
    "multiprocessing": "запуск процессов ОС",
    "ctypes": "вызовы в нативный код",
    "cffi": "вызовы в нативный код",
    "curses": "терминальный доступ",
    "pty": "псевдотерминалы",
    "resource": "изменение лимитов процесса",
    "signal": "манипуляция сигналами процесса",
    "gc": "обход сборщика мусора",
    "importlib": "динамический импорт модулей",
    "imp": "динамический импорт модулей",
    "pickle": "десериализация произвольных объектов",
    "cPickle": "десериализация произвольных объектов",
    "marshal": "десериализация байткода",
    "dill": "десериализация произвольных объектов",
    "shelve": "десериализация произвольных объектов",
}

#: Network clients. A plugin that needs the network should go through services
#: the core already gates, so a direct client is worth a second look.
_NETWORK_MODULES = {
    "socket",
    "socketserver",
    "http",
    "urllib",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
    "ftplib",
    "smtplib",
    "telnetlib",
    "asyncio.streams",
}

#: Builtins that turn data into running code.
_DANGEROUS_BUILTINS = {
    "eval": "выполнение произвольного выражения",
    "exec": "выполнение произвольного кода",
    "compile": "компиляция произвольного кода",
    "__import__": "динамический импорт модулей",
    "globals": "доступ к глобальному пространству",
    "locals": "доступ к локальному пространству",
    "vars": "доступ к переменным по имени",
    "breakpoint": "интерактивный отладчик",
    "input": "блокирующий ввод",
}

#: Dunder attributes used to walk out of the sandbox in-process.
_DANGEROUS_ATTRIBUTES = {
    "__globals__": "обход области видимости",
    "__builtins__": "доступ к builtins",
    "__subclasses__": "обход через иерархию классов",
    "__bases__": "обход через иерархию классов",
    "__code__": "доступ к байткоду",
    "__closure__": "доступ к замыканию",
    "__reduce__": "пикилизация произвольного объекта",
    "__reduce_ex__": "пикилизация произвольного объекта",
    "__getattribute__": "перехват доступа к атрибутам",
    "__init_subclass__": "перехват создания класса",
    "f_globals": "обход области видимости",
    "f_locals": "доступ к локальным переменным",
    "__dict__": "обход через словарь модуля или объекта",
    "__getattr__": "перехват доступа к атрибутам",
    "__mro__": "обход через иерархию классов",
}

#: ``os`` members that execute or destroy.
_DANGEROUS_OS = {
    "system": "запуск команды оболочки",
    "popen": "запуск команды оболочки",
    "execv": "запуск процесса",
    "execve": "запуск процесса",
    "execvp": "запуск процесса",
    "execvpe": "запуск процесса",
    "spawnv": "запуск процесса",
    "spawnve": "запуск процесса",
    "spawnl": "запуск процесса",
    "spawnle": "запуск процесса",
    "fork": "создание процесса",
    "posix_spawn": "создание процесса",
    "remove": "удаление файлов",
    "unlink": "удаление файлов",
    "rmdir": "удаление каталогов",
    "removedirs": "удаление каталогов",
    "truncate": "усечение файлов",
    "chmod": "смена прав",
    "chown": "смена владельца",
    "kill": "сигналы процессам",
    "killpg": "сигналы процессам",
}

#: ``asyncio`` helpers that open a real socket. The module itself is ordinary.
_NETWORK_CALLS = {
    "open_connection",
    "open_unix_connection",
    "start_server",
    "create_datagram_endpoint",
}

#: Attribute names on any object that destroy or execute.
_DANGEROUS_METHODS = {
    "rmtree": "рекурсивное удаление",
    "unlink": "удаление файла",
    "rmdir": "удаление каталога",
    "system": "запуск команды оболочки",
    "popen": "запуск команды оболочки",
    "disconnect": "отключение клиента Telegram",
    "disconnect_": "отключение клиента Telegram",
    "send_file": "отправка файлов от имени бота",
    "Delete": "удаление",
}


def _joined_constants(node: ast.AST) -> str:
    """Every constant string fragment in a ``+`` chain, concatenated.

    Not a full evaluation: a non-constant operand is skipped rather than making
    the whole expression unknown. That is deliberate for this one check. In
    ``d + '/ses' + 'sion.session'`` the fragments join to ``/session.session``,
    which is the file. Bailing out on the first unknown operand handed the whole
    thing back, and a path split in two is precisely what someone writes after
    reading a denylist.

    The cost is a possible false positive on a sentence that happens to end in
    those characters, which is one line for the owner to look at. The cost of
    missing the session file is the Telegram account.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined_constants(node.left) + _joined_constants(node.right)
    return ""


def _dotted(node: ast.AST) -> str:
    """Render ``a.b.c`` from nested attribute/name nodes, else empty."""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


class _Reviewer(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self.imported: set[str] = set()
        #: Local name -> the dotted path it really refers to.
        #:
        #: ``import os as o`` binds ``o``, so ``o.system(...)`` is a shell
        #: execution that reads as a call on an unknown name. ``visit_Call``
        #: matched a literal ``os`` head, and one word of renaming was enough to
        #: walk past the whole list. ``_check_module`` already took the qualified
        #: name for this and never read it.
        self.aliases: dict[str, str] = {}

    def _add(self, node: ast.AST, severity: str, reason: str) -> None:
        self.findings.append(
            Finding(
                path=self.path, line=getattr(node, "lineno", 0), severity=severity, reason=reason
            )
        )

    # -- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imported.add(alias.name)
            bound = alias.asname or alias.name.split(".")[0]
            self.aliases[bound] = alias.asname and alias.name or alias.name.split(".")[0]
            self._check_module(node, alias.name, alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # A relative import ("from . import x") stays inside the plugin package.
        if node.level and node.level > 0:
            self.generic_visit(node)
            return
        module = node.module or ""
        self.imported.add(module)
        for alias in node.names:
            if module:
                qualified = f"{module}.{alias.name}"
                # `from os import system as run` binds `run` to `os.system`.
                self.aliases[alias.asname or alias.name] = qualified
                self._check_module(node, module, qualified)
            else:
                self.aliases[alias.asname or alias.name] = alias.name
                self._check_module(node, alias.name, alias.name)
        self.generic_visit(node)

    def _check_module(self, node: ast.AST, module: str, _qualified: str) -> None:
        # Matched on the package root, not the exact spelling. `import
        # importlib.machinery` binds `importlib`; `import ctypes.util` binds
        # `ctypes`, so `ctypes.CDLL(...)` works after it. An exact match refused
        # one name out of a package and waved the rest through.
        root = module.split(".")[0]
        if module in _DANGEROUS_MODULES or root in _DANGEROUS_MODULES:
            self._add(
                node,
                "high",
                f"импорт {module!r} — {_DANGEROUS_MODULES.get(module) or _DANGEROUS_MODULES[root]}",
            )
            return
        if module in _NETWORK_MODULES or root in _NETWORK_MODULES:
            # Blocking, not a warning: direct network access is the realistic way
            # for a generated plugin to exfiltrate whatever the bot can read. An
            # owner who genuinely needs it can delete the line during review.
            self._add(node, "high", f"импорт {module!r} — прямой сетевой доступ в обход ядра")

    def _resolve(self, target: str) -> str:
        """Map a written name onto the one it was imported as.

        ``o.system`` becomes ``os.system`` when ``o`` was bound by
        ``import os as o``, which is the difference between a report that is
        right and one that is reassuring.
        """
        head, _, tail = target.rpartition(".")
        if not head:
            return self.aliases.get(target, target)
        resolved = self._resolve(head)
        return f"{resolved}.{tail}" if resolved else target

    # -- calls and attributes --------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        written = _dotted(node.func)
        # What the name means, not how it was spelled. Everything below matches
        # on the resolved form, so a renamed import is judged as the thing it is.
        target = self._resolve(written) if written else ""
        if target:
            head, _, tail = target.rpartition(".")
            if head in {"os", "os.path"} and tail in _DANGEROUS_OS:
                self._add(node, "high", f"{written}() — {_DANGEROUS_OS[tail]}")
            elif head in {"subprocess", "pty"}:
                self._add(
                    node,
                    "high",
                    f"{written}() — {_DANGEROUS_OS.get(tail, 'запуск процесса')}",
                )
            elif head == "builtins" and tail in _DANGEROUS_BUILTINS:
                # `import builtins` gives the same reach as the bare name, and
                # was on no list at all.
                self._add(node, "high", f"{written}() — {_DANGEROUS_BUILTINS[tail]}")
        if isinstance(node.func, ast.Name) and node.func.id in _DANGEROUS_BUILTINS:
            self._add(node, "high", f"{node.func.id}() — {_DANGEROUS_BUILTINS[node.func.id]}")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _NETWORK_CALLS
            and target.startswith("asyncio.")
        ):
            # asyncio itself is used by every async plugin and stays clean;
            # opening a raw socket through it does not.
            self._add(node, "high", f"{written}() — прямой сетевой доступ в обход ядра")
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _DANGEROUS_METHODS:
            reason = _DANGEROUS_METHODS[node.func.attr]
            # shutil.rmtree and friends are the common case; client.disconnect is
            # a real hazard because it drops the session out from under the bot.
            severity = "high" if node.func.attr in {"rmtree", "unlink", "rmdir"} else "medium"
            # `written` is empty for `Path('/x').unlink()`, where the receiver is
            # a call rather than a name. The method name is what identifies the
            # finding, so fall back to it rather than reporting "() — удаление".
            name = written or node.func.attr
            self._add(node, severity, f"{name}() — {reason}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _DANGEROUS_ATTRIBUTES:
            self._add(node, "high", f"доступ к {node.attr!r} — {_DANGEROUS_ATTRIBUTES[node.attr]}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # A file path is not suspicious, but the auth key is.
        if isinstance(node.value, str) and node.value.endswith("session.session"):
            self._add(
                node,
                "high",
                f"обращение к файлу сессии Telegram: {node.value!r}",
            )
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """Concatenated string literals are still one string.

        The constant check only saw a literal ending in ``session.session``, and
        ``open(d + '/ses' + 'sion.session')`` puts the same path together at
        runtime. Splitting a path is what someone does when they have read a
        denylist, which is exactly the moment the report needs to be right.
        """
        if isinstance(node.op, ast.Add):
            folded = _joined_constants(node)
            if folded.endswith("session.session"):
                self._add(
                    node,
                    "high",
                    f"обращение к файлу сессии Telegram: {folded!r}",
                )
        self.generic_visit(node)


def review_source(source: str, path: str = "<generated>") -> SafetyReport:
    """Review one Python file.

    A syntax error is reported rather than raised: code that does not parse
    cannot be loaded anyway, and the caller wants one report, not an exception.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return SafetyReport(
            findings=(
                Finding(
                    path=path,
                    line=exc.lineno or 0,
                    severity="high",
                    reason=f"синтаксическая ошибка: {exc.msg}",
                ),
            ),
            files_reviewed=1,
        )
    reviewer = _Reviewer(path)
    reviewer.visit(tree)
    return SafetyReport(findings=tuple(reviewer.findings), files_reviewed=1)


def review_tree(files: dict[str, str]) -> SafetyReport:
    """Review every ``.py`` in a mapping of relative path to source."""
    findings: list[Finding] = []
    reviewed = 0
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        reviewed += 1
        findings.extend(review_source(files[path], path).findings)
    return SafetyReport(findings=tuple(findings), files_reviewed=reviewed)


def format_findings(findings: Iterable[Finding], limit: int = 8) -> str:
    items: Sequence[Finding] = tuple(findings)
    lines = [str(item) for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"…и ещё {len(items) - limit}")
    return "\n".join(lines) if lines else "—"


#: Anything bigger than this is not a plugin; a generated tree that explodes in
#: size is a mistake or an attempt to exhaust the disk.
MAX_SOURCE_BYTES = 512 * 1024

#: Files that are never worth reading, and that would be a red flag themselves.
_SKIP_SUFFIXES = (".session", ".sqlite3", ".pyc", ".so", ".dylib", ".dll", ".jar")


def read_tree_sources(root: Path, max_bytes: int = MAX_SOURCE_BYTES) -> dict[str, str]:
    """Read every reviewable source file under ``root``, keyed by relative path.

    Symlinks are skipped rather than followed: one pointing at
    ``/etc/tguserbot/userbot.env`` would otherwise be reviewed as if it were part
    of the plugin, and one pointing outside the tree is a way to smuggle code in
    past a reviewer who only reads what ``read_tree_sources`` returned.
    """
    sources: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        # Only text we can actually review.
        if path.suffix.lower() not in (".py", ".toml", ".cfg", ".txt", ".md", ".json", ".sh"):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        total += len(text)
        if total > max_bytes:
            break
        sources[relative] = text
    return sources
