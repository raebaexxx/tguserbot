"""Turning a model reply into files, safely.

The model is constrained to a JSON schema, so its output is parseable by
construction rather than by hoping. What is left is the part that actually
matters: every path it produces is attacker-influenced data, and writing
``../../.bashrc`` or an absolute path would put generated code outside the
staging directory entirely.

So paths are validated rather than sanitised: a path that is not a plain
relative POSIX path is rejected outright, and the resolved location is checked
against the staging root afterwards. Two independent checks, because the second
one is the one that matters and the first one gives a readable error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: What a generated file may be called. Deliberately strict: no spaces, no
#: leading dot, nothing that would need quoting.
#:
#: A leading underscore is allowed, and has to be: ``__init__.py`` is one of the
#: three required files. Rejecting it would make generation fail for every plugin.
FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")

#: A path may have at most this many segments, so "../../../.." is obvious.
MAX_DEPTH = 6

#: Refuse to write more than this for one plugin.
MAX_TOTAL_BYTES = 400 * 1024

#: The schema handed to the model. Also the contract the parser validates
#: against, so the two cannot drift apart without a test noticing.
PLUGIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "required": ["name", "summary", "files"],
}


class GeneratedFileError(ValueError):
    """The model's output cannot be turned into files."""


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class GeneratedPlugin:
    name: str
    summary: str
    files: tuple[GeneratedFile, ...]

    def as_mapping(self) -> dict[str, str]:
        return {item.path: item.content for item in self.files}


def validate_relative_path(raw: Any) -> str:
    """Return the path if it is safe to write, or explain why it is not.

    Returns the normalised path rather than the original, so a name like
    ``a//b.py`` cannot be used to write two different files than the one that
    was reviewed.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GeneratedFileError("путь к файлу пуст")
    candidate = raw.strip().replace("\\", "/")
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        raise GeneratedFileError(f"абсолютный путь запрещён: {raw!r}")
    if "\x00" in candidate:
        raise GeneratedFileError("путь содержит нулевой байт")
    segments = [segment for segment in candidate.split("/") if segment not in ("", ".")]
    if not segments:
        raise GeneratedFileError(f"путь не указывает на файл: {raw!r}")
    if len(segments) > MAX_DEPTH:
        raise GeneratedFileError(f"путь слишком глубокий: {raw!r}")
    for segment in segments:
        if segment == "..":
            # The one case that must never be tolerated, whatever the prefix.
            raise GeneratedFileError(f"выход за пределы каталога запрещён: {raw!r}")
        if not FILENAME.match(segment):
            raise GeneratedFileError(f"недопустимое имя файла: {segment!r}")
    return "/".join(segments)


def parse_generated(raw: str, *, expected_name: str | None = None) -> GeneratedPlugin:
    """Parse and validate a model reply into a plugin description.

    Raises :class:`GeneratedFileError` with a message meant for a human; every
    branch here is reachable by a model that misunderstood the task, so a clean
    error beats a traceback.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Some models wrap JSON in a fence despite the schema. Unwrap it rather
        # than failing, since the content is still structured.
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise GeneratedFileError(f"ответ модели не является JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GeneratedFileError("ожидался объект JSON")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GeneratedFileError("в ответе нет имени плагина")
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise GeneratedFileError(
            f"имя {name!r} не подходит: только латиница в нижнем регистре, цифры, дефис "
            "и подчёркивание, до 64 символов"
        )
    if expected_name is not None and name != expected_name:
        raise GeneratedFileError(
            f"модель назвала плагин {name!r}, а запрошен был {expected_name!r}"
        )

    summary = payload.get("summary")
    files_raw = payload.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise GeneratedFileError("в ответе нет ни одного файла")

    files: list[GeneratedFile] = []
    seen: set[str] = set()
    total = 0
    for item in files_raw:
        if not isinstance(item, dict):
            raise GeneratedFileError("элемент files не является объектом")
        path = validate_relative_path(item.get("path"))
        if path in seen:
            raise GeneratedFileError(f"файл {path!r} указан дважды")
        content = item.get("content")
        if not isinstance(content, str):
            raise GeneratedFileError(f"содержимое {path!r} не является строкой")
        seen.add(path)
        total += len(content.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise GeneratedFileError(f"суммарный объём превышает {MAX_TOTAL_BYTES // 1024} КБ")
        files.append(GeneratedFile(path=path, content=content))

    required = {"plugin.toml", "__init__.py", "plugin.py"}
    missing = required - seen
    if missing:
        raise GeneratedFileError(f"модель не создала: {', '.join(sorted(missing))}")

    return GeneratedPlugin(
        name=name,
        summary=summary.strip() if isinstance(summary, str) else "",
        files=tuple(files),
    )


def write_plugin(root: Path, plugin: GeneratedPlugin) -> Path:
    """Write a validated plugin under ``root`` and return its directory.

    The path is resolved and checked against the root even though
    :func:`validate_relative_path` already rejected traversal: this is the check
    that would actually stop a write, and it costs one ``resolve()``.
    """
    target_root = root.resolve()
    destination = (target_root / plugin.name).resolve()
    if target_root != destination.parent:
        raise GeneratedFileError(f"проверка пути не пройдена для {plugin.name!r}")
    if destination.exists():
        import shutil

        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for item in plugin.files:
        path = destination
        for segment in item.path.split("/"):
            path = path / segment
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
    return destination
