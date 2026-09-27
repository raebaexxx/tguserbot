from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .loader import PluginManifest


class PluginConfigError(ValueError):
    """A plugin's config.toml could not be read or is not a table."""


@dataclass(frozen=True, slots=True)
class PluginConfig:
    """Effective configuration for one plugin.

    Values come from two places, in increasing precedence:

    1. ``[config]`` in the plugin's own ``plugin.toml`` -- the shipped default;
    2. ``[<plugin>]`` in ``<data_dir>/plugin-config.toml`` -- the operator's
       override, so retuning a plugin never means editing its code.

    Types are not coerced; a mismatch between the override and the default is
    reported rather than silently accepted.
    """

    plugin_name: str
    values: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    sources: tuple[str, ...] = ()

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def require(self, key: str) -> Any:
        try:
            return self.values[key]
        except KeyError:
            raise PluginConfigError(
                f"plugin {self.plugin_name!r} has no configuration key {key!r}"
            ) from None

    def int_value(self, key: str, default: int) -> int:
        raw = self.values.get(key, default)
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise PluginConfigError(
                f"plugin {self.plugin_name!r}: {key!r} must be a number, got {raw!r}"
            ) from exc

    def float_value(self, key: str, default: float) -> float:
        raw = self.values.get(key, default)
        if isinstance(raw, bool):
            # bool is an int subclass, so True would silently become 1.0 here.
            raise PluginConfigError(
                f"plugin {self.plugin_name!r}: {key!r} must be a number, got {raw!r}"
            )
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise PluginConfigError(
                f"plugin {self.plugin_name!r}: {key!r} must be a number, got {raw!r}"
            ) from exc

    def bool_value(self, key: str, default: bool) -> bool:
        raw = self.values.get(key, default)
        if isinstance(raw, bool):
            return raw
        raise PluginConfigError(
            f"plugin {self.plugin_name!r}: {key!r} must be true or false, got {raw!r}"
        )

    def str_value(self, key: str, default: str) -> str:
        raw = self.values.get(key, default)
        if not isinstance(raw, str):
            raise PluginConfigError(
                f"plugin {self.plugin_name!r}: {key!r} must be a string, got {raw!r}"
            )
        return raw

    def str_list(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        raw = self.values.get(key, default)
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise PluginConfigError(
                f"plugin {self.plugin_name!r}: {key!r} must be a list of strings, got {raw!r}"
            )
        return tuple(str(item) for item in raw)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)

    def __repr__(self) -> str:
        return f"PluginConfig(plugin_name={self.plugin_name!r}, keys={sorted(self.values)})"


EMPTY = PluginConfig(plugin_name="")


def parse_plugin_config_file(path: Path) -> dict[str, dict[str, Any]]:
    """Read ``<plugin> = { key = value }`` tables from a config.toml file."""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginConfigError(f"Cannot read {path}: {exc}") from exc
    tables: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            raise PluginConfigError(
                f"{path}: [{key}] must be a table of settings, got {type(value).__name__}"
            )
        tables[str(key)] = value
    return tables


class PluginConfigStore:
    """Reads the operator's per-plugin overrides once, at startup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._overrides = parse_plugin_config_file(path)

    @property
    def overridden(self) -> tuple[str, ...]:
        return tuple(sorted(self._overrides))

    def resolve(self, manifest: PluginManifest) -> PluginConfig:
        merged: dict[str, Any] = dict(manifest.config)
        sources: list[str] = ["plugin.toml"]
        override = self._overrides.get(manifest.name)
        if override:
            for key, value in override.items():
                if key in merged and not _compatible(merged[key], value):
                    raise PluginConfigError(
                        f"plugin {manifest.name!r}: override for {key!r} is "
                        f"{_type_name(value)} but the default is "
                        f"{_type_name(merged[key])}"
                    )
                merged[key] = value
            sources.append(str(self.path))
        return PluginConfig(
            plugin_name=manifest.name, values=MappingProxyType(merged), sources=tuple(sources)
        )


def _type_name(value: Any) -> str:
    return "boolean" if isinstance(value, bool) else type(value).__name__


def _compatible(default: Any, override: Any) -> bool:
    """Whether an override may replace a default.

    Same type is fine, and any two numbers are interchangeable. Everything else
    is refused: ``enabled = 1`` against a ``false`` default and ``limit = "10"``
    against an integer default both mean the operator misunderstood the setting,
    and failing here beats failing somewhere inside the plugin later.
    """
    if isinstance(default, bool) or isinstance(override, bool):
        return isinstance(default, bool) and isinstance(override, bool)
    if isinstance(default, (int, float)) and isinstance(override, (int, float)):
        return True
    return type(default) is type(override)
