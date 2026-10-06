"""Every knob a plugin ships must be a knob it reads.

A setting in a manifest is a promise to the operator: change this line and
something happens. Three promises in this repository did nothing at all --
``max_output_tokens`` in the ``ai`` manifest, ``require_confirm``, and the
``TGUSERBOT_INSTALLED_PLUGIN_DIR`` override -- and each was invisible, because a
default nobody reads produces exactly the same behaviour as one nobody sets, and
the value happened to match the code's own default.

The check is deliberately blunt: every key under ``[config]`` must appear
somewhere in the plugin's own source. That catches the whole class, not the three
instances, and it costs one pass over four small files.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins"


def manifest_keys(manifest: dict[str, object]) -> list[str]:
    config = manifest.get("config")
    if not isinstance(config, dict):
        return []
    return sorted(str(key) for key in config)


def plugin_source(name: str) -> str:
    """Every source file of one plugin, concatenated.

    A key may legitimately be read in a helper module (``_collect``, ``_codegen``),
    so one file is not enough to decide that a setting is dead.
    """
    parts: list[str] = []
    for path in sorted((PLUGIN_DIR / name).glob("*.py")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.mark.parametrize(
    "manifest",
    sorted(PLUGIN_DIR.glob("*/plugin.toml")),
    ids=lambda path: path.parent.name,
)
def test_every_shipped_setting_is_read(manifest: Path) -> None:
    name = manifest.parent.name
    declared = manifest_keys(tomllib.loads(manifest.read_text(encoding="utf-8")))
    if not declared:
        pytest.skip(f"{name} declares no settings")

    source = plugin_source(name)
    dead = [key for key in declared if f'"{key}"' not in source and f"'{key}'" not in source]
    assert not dead, (
        f"{name}/plugin.toml declares {dead} but never reads "
        f"{dead[0] if len(dead) == 1 else 'them'}: an operator changing that line "
        "would see nothing happen, which is indistinguishable from a setting that "
        "is working"
    )
