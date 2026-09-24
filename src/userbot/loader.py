from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


class PluginLoadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    version: str
    api: str
    entrypoint: str
    description: str
    schema_version: int

    @classmethod
    def from_path(cls, path: Path) -> PluginManifest:
        manifest_path = path / "plugin.toml"
        if not manifest_path.is_file():
            raise PluginLoadError(f"Missing plugin.toml in {path}")
        try:
            with manifest_path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PluginLoadError(f"Cannot read {manifest_path}: {exc}") from exc

        name = str(data.get("name", "")).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise PluginLoadError("Plugin name must match [a-z0-9][a-z0-9_-]{0,63}")
        version = str(data.get("version", "0.1.0")).strip()
        api = str(data.get("api", data.get("api_version", "1"))).strip()
        entrypoint = str(data.get("entrypoint", "plugin:Plugin")).strip()
        description = str(data.get("description", "")).strip()
        try:
            schema_version = int(data.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise PluginLoadError("schema_version must be an integer") from exc
        if not api or not entrypoint or schema_version < 1:
            raise PluginLoadError("Invalid plugin manifest values")
        return cls(
            name=name,
            version=version,
            api=api,
            entrypoint=entrypoint,
            description=description,
            schema_version=schema_version,
        )


@dataclass(slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    instance: Any
    module: ModuleType
    module_names: tuple[str, ...]


def _safe_identifier(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _compile_python_files(path: Path) -> None:
    for source_path in path.rglob("*.py"):
        if any(part in {"__pycache__", ".git"} for part in source_path.parts):
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
            compile(source, str(source_path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise PluginLoadError(f"Cannot compile {source_path}: {exc}") from exc


def _remove_modules(module_names: tuple[str, ...]) -> None:
    for module_name in module_names:
        sys.modules.pop(module_name, None)


def load_plugin(path: Path, expected_name: str, generation: int) -> LoadedPlugin:
    """Load one plugin into an isolated, disposable module namespace."""
    path = path.resolve()
    manifest = PluginManifest.from_path(path)
    if manifest.name != expected_name:
        raise PluginLoadError(
            f"Plugin name mismatch: directory={expected_name!r}, manifest={manifest.name!r}"
        )
    if manifest.api != "1":
        raise PluginLoadError(f"Unsupported plugin API version: {manifest.api}")

    init_path = path / "__init__.py"
    entry_module_path = path / "plugin.py"
    if not init_path.is_file() or not entry_module_path.is_file():
        raise PluginLoadError("Plugin must contain __init__.py and plugin.py")

    _compile_python_files(path)
    module_name = f"_tguserbot_plugin_{_safe_identifier(manifest.name)}_{generation}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_path,
        submodule_search_locations=[str(path)],
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        module_part, _, attribute = manifest.entrypoint.partition(":")
        if not module_part or not attribute:
            raise PluginLoadError("entrypoint must use the form module:attribute")
        if module_part in {"__init__", "__init__.py"}:
            entry_module = module
        else:
            entry_module = importlib.import_module(f"{module_name}.{module_part}")
        entry_object = getattr(entry_module, attribute)
        if not isinstance(entry_object, type):
            raise PluginLoadError(f"Entrypoint {manifest.entrypoint!r} is not a class")
        instance = entry_object()
    except Exception as exc:
        module_names = tuple(
            name
            for name in sys.modules
            if name == module_name or name.startswith(f"{module_name}.")
        )
        _remove_modules(module_names)
        if isinstance(exc, PluginLoadError):
            raise
        raise PluginLoadError(f"Cannot load plugin {manifest.name}: {exc}") from exc

    module_names = tuple(
        name for name in sys.modules if name == module_name or name.startswith(f"{module_name}.")
    )
    return LoadedPlugin(
        manifest=manifest,
        instance=instance,
        module=module,
        module_names=module_names,
    )


def cleanup_loaded_plugin(loaded: LoadedPlugin) -> None:
    _remove_modules(loaded.module_names)
