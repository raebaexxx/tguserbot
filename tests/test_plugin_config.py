"""Tests for per-plugin configuration.

Plugins previously hardcoded every tunable, so changing the TikTok size limit
meant editing code and triggering a reload. `[config]` in plugin.toml now
carries the shipped defaults and `<data_dir>/plugin-config.toml` overrides
them per installation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_plugin
from userbot.loader import PluginLoadError, PluginManifest
from userbot.plugin_config import (
    PluginConfig,
    PluginConfigError,
    PluginConfigStore,
    parse_plugin_config_file,
)

# --- manifest defaults -----------------------------------------------------


def test_manifest_reads_a_config_table(plugin_root: Path) -> None:
    write_plugin(
        plugin_root,
        "cfg",
        manifest_extra='[config]\nlimit = 10\nname_hint = "hello"\nenabled = true\n',
        body="",
    )
    manifest = PluginManifest.from_path(plugin_root / "cfg")
    assert manifest.config == {"limit": 10, "name_hint": "hello", "enabled": True}


def test_manifest_without_a_config_table(plugin_root: Path) -> None:
    write_plugin(plugin_root, "cfg", body="")
    assert PluginManifest.from_path(plugin_root / "cfg").config == {}


def test_manifest_config_must_be_a_table(plugin_root: Path) -> None:
    write_plugin(plugin_root, "cfg", manifest_extra="config = 5\n", body="")
    with pytest.raises(PluginLoadError, match=r"\[config\].*must be a table"):
        PluginManifest.from_path(plugin_root / "cfg")


def test_manifest_config_is_read_only(plugin_root: Path) -> None:
    write_plugin(plugin_root, "cfg", manifest_extra="[config]\nlimit = 1\n", body="")
    manifest = PluginManifest.from_path(plugin_root / "cfg")
    with pytest.raises(TypeError):
        manifest.config["limit"] = 2  # type: ignore[index]


# --- override file ---------------------------------------------------------


def test_parse_a_config_file(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.toml"
    path.write_text("[tiktok]\nmax_file_mib = 100\n\n[notes]\npage_size = 5\n", encoding="utf-8")
    assert parse_plugin_config_file(path) == {
        "tiktok": {"max_file_mib": 100},
        "notes": {"page_size": 5},
    }


def test_a_missing_config_file_is_empty(tmp_path: Path) -> None:
    assert parse_plugin_config_file(tmp_path / "absent.toml") == {}


def test_a_malformed_config_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.toml"
    path.write_text("[tiktok\nbroken = 1\n", encoding="utf-8")
    with pytest.raises(PluginConfigError, match="Cannot read"):
        parse_plugin_config_file(path)


def test_a_non_table_section_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.toml"
    path.write_text("tiktok = 5\n", encoding="utf-8")
    with pytest.raises(PluginConfigError, match="must be a table"):
        parse_plugin_config_file(path)


def test_store_lists_the_overridden_plugins(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.toml"
    path.write_text("[b]\nx = 1\n\n[a]\ny = 2\n", encoding="utf-8")
    store = PluginConfigStore(path)
    assert store.overridden == ("a", "b")


# --- merging ---------------------------------------------------------------


def make_manifest(plugin_root: Path, name: str, extra: str) -> PluginManifest:
    write_plugin(plugin_root, name, manifest_extra=extra, body="")
    return PluginManifest.from_path(plugin_root / name)


def test_defaults_apply_without_an_override(plugin_root: Path, tmp_path: Path) -> None:
    manifest = make_manifest(plugin_root, "cfg", "[config]\nlimit = 10\n")
    store = PluginConfigStore(tmp_path / "absent.toml")
    config = store.resolve(manifest)
    assert config.int_value("limit", 0) == 10
    assert config.sources == ("plugin.toml",)


def test_an_override_wins_over_the_default(plugin_root: Path, tmp_path: Path) -> None:
    manifest = make_manifest(plugin_root, "cfg", "[config]\nlimit = 10\n")
    path = tmp_path / "plugin-config.toml"
    path.write_text("[cfg]\nlimit = 99\n", encoding="utf-8")
    config = PluginConfigStore(path).resolve(manifest)
    assert config.int_value("limit", 0) == 99
    assert config.sources == ("plugin.toml", str(path))


def test_an_override_may_introduce_a_new_key(plugin_root: Path, tmp_path: Path) -> None:
    manifest = make_manifest(plugin_root, "cfg", "[config]\nlimit = 10\n")
    path = tmp_path / "plugin-config.toml"
    path.write_text("[cfg]\nextra = 'yes'\n", encoding="utf-8")
    config = PluginConfigStore(path).resolve(manifest)
    assert config.str_value("extra", "") == "yes"


def test_an_override_of_the_wrong_type_is_rejected(plugin_root: Path, tmp_path: Path) -> None:
    """Silently accepting limit = "big" would fail later, far from the cause."""
    manifest = make_manifest(plugin_root, "cfg", "[config]\nlimit = 10\n")
    path = tmp_path / "plugin-config.toml"
    path.write_text('[cfg]\nlimit = "big"\n', encoding="utf-8")
    with pytest.raises(PluginConfigError, match="override for 'limit' is str"):
        PluginConfigStore(path).resolve(manifest)


def test_a_bool_default_rejects_an_int_override(plugin_root: Path, tmp_path: Path) -> None:
    manifest = make_manifest(plugin_root, "cfg", "[config]\nenabled = false\n")
    path = tmp_path / "plugin-config.toml"
    path.write_text("[cfg]\nenabled = 1\n", encoding="utf-8")
    with pytest.raises(PluginConfigError, match="boolean"):
        PluginConfigStore(path).resolve(manifest)


def test_overrides_for_other_plugins_are_ignored(plugin_root: Path, tmp_path: Path) -> None:
    manifest = make_manifest(plugin_root, "cfg", "[config]\nlimit = 10\n")
    path = tmp_path / "plugin-config.toml"
    path.write_text("[somebody_else]\nlimit = 1\n", encoding="utf-8")
    config = PluginConfigStore(path).resolve(manifest)
    assert config.int_value("limit", 0) == 10
    assert config.sources == ("plugin.toml",)


# --- typed accessors -------------------------------------------------------


def test_get_and_require() -> None:
    config = PluginConfig(plugin_name="p", values={"a": 1})
    assert config.get("a") == 1
    assert config.get("missing", "fallback") == "fallback"
    assert config.require("a") == 1
    with pytest.raises(PluginConfigError, match="no configuration key"):
        config.require("missing")


def test_int_value_rejects_junk() -> None:
    config = PluginConfig(plugin_name="p", values={"a": "nope"})
    with pytest.raises(PluginConfigError, match="must be a number"):
        config.int_value("a", 0)


def test_int_value_accepts_a_numeric_string() -> None:
    # An operator override quoted in TOML is still usable.
    assert PluginConfig(plugin_name="p", values={"a": "42"}).int_value("a", 0) == 42


def test_bool_value() -> None:
    assert PluginConfig(plugin_name="p", values={"a": True}).bool_value("a", False) is True
    with pytest.raises(PluginConfigError, match="true or false"):
        PluginConfig(plugin_name="p", values={"a": "yes"}).bool_value("a", False)


def test_str_value() -> None:
    assert PluginConfig(plugin_name="p", values={"a": "x"}).str_value("a", "") == "x"
    with pytest.raises(PluginConfigError, match="must be a string"):
        PluginConfig(plugin_name="p", values={"a": 1}).str_value("a", "")


def test_str_list() -> None:
    config = PluginConfig(plugin_name="p", values={"a": ["x", "y"]})
    assert config.str_list("a") == ("x", "y")
    with pytest.raises(PluginConfigError, match="list of strings"):
        PluginConfig(plugin_name="p", values={"a": "x"}).str_list("a")
    with pytest.raises(PluginConfigError, match="list of strings"):
        PluginConfig(plugin_name="p", values={"a": 1}).str_list("a")


def test_as_dict_is_a_copy() -> None:
    config = PluginConfig(plugin_name="p", values={"a": 1})
    snapshot = config.as_dict()
    snapshot["a"] = 2
    assert config.get("a") == 1


def test_repr_lists_the_keys_not_the_values() -> None:
    text = repr(PluginConfig(plugin_name="p", values={"secret": "value"}))
    assert "secret" in text
    assert "value" not in text


# --- shipped plugins -------------------------------------------------------


def test_shipped_tiktok_defaults_are_declared() -> None:
    manifest = PluginManifest.from_path(Path("plugins") / "tiktok")
    assert manifest.config["max_file_mib"] == 50
    assert manifest.config["allowed_domains"] == ["tiktok.com", "tiktokv.com"]


def test_shipped_status_has_no_config_section() -> None:
    """The smallest shipped plugin declares no settings.

    Worth pinning while it is true: it is the one plugin a new author copies, and a
    ``[config]`` block it does not read is exactly the "settings that do nothing"
    defect the manifest review is for.
    """
    manifest = PluginManifest.from_path(Path("plugins") / "status")
    assert manifest.config == {}


def test_shipped_tiktok_honours_an_override(
    plugin_root: Path, tmp_path: Path, real_plugin_dir: Path
) -> None:
    """An operator retunes the plugin without touching its code."""
    import shutil

    shutil.copytree(real_plugin_dir / "tiktok", plugin_root / "tiktok")
    path = tmp_path / "plugin-config.toml"
    path.write_text(
        '[tiktok]\nmax_file_mib = 4\nallowed_domains = ["example.com"]\n',
        encoding="utf-8",
    )
    manifest = PluginManifest.from_path(plugin_root / "tiktok")
    config = PluginConfigStore(path).resolve(manifest)
    assert config.int_value("max_file_mib", 0) == 4
    assert config.str_list("allowed_domains") == ("example.com",)


async def test_manager_survives_a_broken_override_file(plugin_root: Path, tmp_path: Path) -> None:
    """A bad config file must be loud but must not stop the bot from booting."""
    from conftest import FakeClient, make_noop_plugin
    from userbot.commands import CommandDispatcher
    from userbot.config import Settings
    from userbot.health import HealthService
    from userbot.manager import PluginManager
    from userbot.rate_limit import RateLimiter
    from userbot.storage import Storage

    write_plugin(plugin_root, "alpha", body=make_noop_plugin("alpha"))
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="t",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.plugin_config_path.write_text("[broken\n", encoding="utf-8")
    storage = Storage(settings.database_path)
    await storage.initialize()
    manager = PluginManager(
        settings=settings,
        client=FakeClient(),
        storage=storage,
        dispatcher=CommandDispatcher({1}),
        rate_limiter=RateLimiter(min_interval=0),
        health=HealthService(),
    )
    try:
        assert manager.config_store.overridden == ()
        await manager.load_all_local()
        assert manager.active_count() == 1
        runtime = manager.get_runtime("alpha")
        assert runtime is not None
        assert runtime.context.config.get("anything") is None
    finally:
        await manager.shutdown()
        await storage.close()
