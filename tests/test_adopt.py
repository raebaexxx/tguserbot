"""Tests for adopting a generated plugin: the second, explicit half of creation.

Generation writes to a staging directory and nothing more. These tests cover the
gate that decides whether that code ever runs, so the properties that matter are
the negative ones: a staged plugin stays invisible, and code that reaches for a
shell or a socket is refused rather than merely warned about.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import FakeClient, Settings, Storage
from userbot.commands import CommandDispatcher, parse_plugin_action
from userbot.health import HealthService
from userbot.loader import PluginLoadError
from userbot.manager import PluginManager
from userbot.rate_limit import RateLimiter

#: Shaped exactly like a shipped plugin: the entrypoint is ``plugin:Plugin``, so
#: the class has to be called ``Plugin`` and extend ``BasePlugin``.
CLEAN_PLUGIN = """
from userbot.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    async def setup(self, ctx):
        self.ctx = ctx
"""


def manifest_for(name: str, version: str = "0.1.0", description: str = "test") -> str:
    """A plugin.toml in the shape the loader actually reads (flat, no section)."""
    return (
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        'api = "1"\n'
        'entrypoint = "plugin:Plugin"\n'
        f'description = "{description}"\n'
        "schema_version = 1\n"
    )


def stage(
    settings: Settings,
    name: str,
    body: str = CLEAN_PLUGIN,
    *,
    version: str = "0.1.0",
    manifest_name: str | None = None,
) -> Path:
    """Write a plugin into the staging area, as the generator would."""
    target = settings.staging_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.toml").write_text(
        manifest_for(manifest_name or name, version=version), encoding="utf-8"
    )
    (target / "plugin.py").write_text(textwrap.dedent(body), encoding="utf-8")
    # The loader requires both files; the package __init__ re-exports the class.
    (target / "__init__.py").write_text(
        'from .plugin import Plugin\n\n__all__ = ["Plugin"]\n', encoding="utf-8"
    )
    return target


def readonly_manager(settings: Settings) -> PluginManager:
    """A manager for queries only: discovery never touches the database.

    Constructed without initialising storage, so nothing here can accidentally
    pass by writing state.
    """
    manager = PluginManager(
        settings=settings,
        client=FakeClient(),
        storage=cast(Any, None),
        dispatcher=cast(Any, None),
        rate_limiter=RateLimiter(min_interval=0),
        health=HealthService(),
    )
    return manager


@pytest.fixture
async def wired(settings: Settings) -> tuple[Settings, Storage, PluginManager]:
    """A manager wired the way the app wires one, with a real database."""
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
    return settings, storage, manager


# --- argument parsing -------------------------------------------------------


def test_adopt_is_parsed() -> None:
    parsed = parse_plugin_action("adopt myplugin")
    assert parsed is not None
    assert parsed.action == "adopt"
    assert parsed.name == "myplugin"


def test_adopt_without_a_name_is_rejected() -> None:
    assert parse_plugin_action("adopt") is None


def test_adopt_is_not_mistaken_for_update() -> None:
    parsed = parse_plugin_action("adopt notes")
    assert parsed is not None
    assert parsed.action == "adopt"
    assert parsed.name == "notes"


# --- staging is invisible ---------------------------------------------------


def test_a_staged_plugin_is_never_discovered(settings: Settings) -> None:
    """The whole design rests on this: nothing generated runs without adoption."""
    stage(settings, "generated")
    manager = readonly_manager(settings)
    assert "generated" not in manager.discover_local_names()
    assert manager.local_path("generated") is None


async def test_a_staged_plugin_cannot_be_loaded_by_name(wired: tuple[Any, ...]) -> None:
    """/ub plugin reload must not reach into staging either."""
    settings, storage, manager = wired
    stage(settings, "generated")
    try:
        with pytest.raises(PluginLoadError, match="не найден"):
            await manager.load_local("generated")
        assert manager.active_count() == 0
    finally:
        await manager.shutdown()
        await storage.close()


def test_staging_lives_outside_both_plugin_roots(settings: Settings) -> None:
    """If staging were a root, discovery would pick generated code up by itself."""
    roots = readonly_manager(settings).plugin_roots()
    assert settings.staging_dir not in roots
    assert not settings.staging_dir.is_relative_to(settings.plugin_dir)


# --- listing ----------------------------------------------------------------


def test_list_staged_reports_a_clean_plugin(settings: Settings) -> None:
    stage(settings, "good")
    items = readonly_manager(settings).list_staged()
    assert [item["name"] for item in items] == ["good"]
    assert items[0]["report"].ok
    assert items[0]["manifest"].version == "0.1.0"
    assert "plugin.py" in items[0]["files"]


def test_list_staged_is_empty_when_nothing_is_staged(settings: Settings) -> None:
    assert readonly_manager(settings).list_staged() == []


def test_list_staged_skips_a_directory_without_a_manifest(settings: Settings) -> None:
    half = settings.staging_dir / "half-written"
    half.mkdir(parents=True)
    (half / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    assert readonly_manager(settings).list_staged() == []


def test_list_staged_surfaces_the_review_before_adoption(settings: Settings) -> None:
    """The owner has to see what they are agreeing to before typing the command."""
    stage(settings, "risky", body="import os\n\n\ndef f():\n    return os.system('id')\n")
    report = readonly_manager(settings).list_staged()[0]["report"]
    assert not report.ok
    assert "system" in report.summary()


# --- adoption ---------------------------------------------------------------


async def test_adopt_installs_and_loads(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    stage(settings, "good")
    try:
        runtime = await manager.install_local("good")
        assert runtime.name == "good"
        assert manager.get_runtime("good") is not None
        # It went to the writable root, not the shipped tree.
        assert runtime.path == settings.installed_plugin_dir / "good"
        assert (runtime.path / "plugin.py").is_file()
    finally:
        await manager.shutdown()
        await storage.close()


async def test_adopt_refuses_code_that_reaches_for_a_shell(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    stage(settings, "evil", body="import os\n\n\ndef f():\n    return os.system('id')\n")
    try:
        with pytest.raises(PluginLoadError, match="безопасност"):
            await manager.install_local("evil")
        assert not (settings.installed_plugin_dir / "evil").exists()
        assert not manager.has_plugin("evil")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_adopt_refuses_a_network_client(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    stage(
        settings,
        "net",
        body=CLEAN_PLUGIN
        + "\nimport requests\n\n\ndef f():\n    return requests.get('http://x')\n",
    )
    try:
        with pytest.raises(PluginLoadError, match="безопасност"):
            await manager.install_local("net")
        assert not (settings.installed_plugin_dir / "net").exists()
    finally:
        await manager.shutdown()
        await storage.close()


async def test_adopt_refuses_a_manifest_that_lies_about_its_name(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    stage(settings, "outer", manifest_name="inner")
    try:
        with pytest.raises(PluginLoadError, match="не совпадает"):
            await manager.install_local("outer")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_adopt_reports_a_missing_manifest(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    (settings.staging_dir / "nothing").mkdir(parents=True)
    try:
        with pytest.raises(PluginLoadError, match="plugin.toml"):
            await manager.install_local("nothing")
    finally:
        await manager.shutdown()
        await storage.close()


async def test_a_failed_load_leaves_nothing_half_installed(wired: tuple[Any, ...]) -> None:
    settings, storage, manager = wired
    stage(settings, "broken", body="# no Plugin class at all\n")
    try:
        with pytest.raises(PluginLoadError):
            await manager.install_local("broken")
        assert not (settings.installed_plugin_dir / "broken").exists(), (
            "a rejected plugin must leave nothing behind that could load by accident"
        )
    finally:
        await manager.shutdown()
        await storage.close()


async def test_regenerating_and_readopting_replaces_the_installed_copy(
    wired: tuple[Any, ...],
) -> None:
    settings, storage, manager = wired
    stage(settings, "good", version="0.1.0")
    try:
        first = await manager.install_local("good")
        assert first.manifest.version == "0.1.0"
        # Regenerating into staging must not require unloading by hand.
        stage(settings, "good", body=CLEAN_PLUGIN + "\nEXTRA = 1\n", version="0.2.0")
        second = await manager.install_local("good")
        assert second.manifest.version == "0.2.0"
        assert manager.active_count() == 1
    finally:
        await manager.shutdown()
        await storage.close()


async def test_the_staged_copy_survives_adoption(wired: tuple[Any, ...]) -> None:
    """The reviewed copy is the record of what was approved."""
    settings, storage, manager = wired
    stage(settings, "good")
    try:
        await manager.install_local("good")
        assert (settings.staging_dir / "good" / "plugin.py").is_file()
    finally:
        await manager.shutdown()
        await storage.close()


async def test_the_review_is_refused_by_default_and_overridable_on_purpose(
    wired: tuple[Any, ...],
) -> None:
    """The honest boundary: advisory for an owner who read the code, a hard
    stop for everyone else. A default of ``True`` would make it advisory by
    accident, which is why the flag is keyword-only and undefaulted in spirit.
    """
    settings, storage, manager = wired
    stage(
        settings,
        "wants_shell",
        body=CLEAN_PLUGIN + "\nimport subprocess\n\n\ndef run():\n    return subprocess\n",
    )
    try:
        with pytest.raises(PluginLoadError, match="безопасност"):
            await manager.install_local("wants_shell")
        assert not (settings.installed_plugin_dir / "wants_shell").exists()

        runtime = await manager.install_local("wants_shell", allow_blocking=True)
        assert runtime.name == "wants_shell"
        assert manager.active_count() == 1
    finally:
        await manager.shutdown()
        await storage.close()


# --- the two roots ----------------------------------------------------------


def test_installed_plugins_are_discovered(settings: Settings) -> None:
    stage(settings, "good")
    installed = settings.installed_plugin_dir / "good"
    installed.mkdir(parents=True)
    (installed / "plugin.toml").write_text(manifest_for("good"), encoding="utf-8")
    (installed / "plugin.py").write_text(CLEAN_PLUGIN, encoding="utf-8")
    manager = readonly_manager(settings)
    assert "good" in manager.discover_local_names()
    assert manager.local_path("good") == installed


def test_shipped_plugins_are_still_discovered(settings: Settings) -> None:
    shipped = settings.plugin_dir / "builtin"
    shipped.mkdir(parents=True)
    (shipped / "plugin.toml").write_text(manifest_for("builtin"), encoding="utf-8")
    manager = readonly_manager(settings)
    assert "builtin" in manager.discover_local_names()
    assert manager.local_path("builtin") == shipped


def test_the_two_roots_do_not_produce_duplicates(settings: Settings) -> None:
    for root in (settings.plugin_dir, settings.installed_plugin_dir):
        path = root / "same"
        path.mkdir(parents=True)
        (path / "plugin.toml").write_text(manifest_for("same"), encoding="utf-8")
    assert readonly_manager(settings).discover_local_names().count("same") == 1


def test_the_installed_root_is_writable_and_the_shipped_root_is_not_assumed_to_be(
    settings: Settings,
) -> None:
    """They must differ, or the read-only assumption in production is wrong."""
    assert settings.installed_plugin_dir != settings.plugin_dir
    assert settings.installed_plugin_dir.is_relative_to(settings.data_dir)


def test_a_missing_root_is_skipped_without_error(settings: Settings) -> None:
    assert readonly_manager(settings).discover_local_names() == []


# --- reading a tree for review ----------------------------------------------


def test_read_tree_sources_skips_symlinks(tmp_path: Path) -> None:
    """A symlink out of the tree would smuggle in unreviewed code."""
    from userbot.safety import read_tree_sources

    (tmp_path / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("import os\nos.system('id')\n", encoding="utf-8")
    try:
        (tmp_path / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert set(read_tree_sources(tmp_path)) == {"plugin.py"}


def test_read_tree_sources_skips_binary_and_session_files(tmp_path: Path) -> None:
    from userbot.safety import read_tree_sources

    (tmp_path / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "session.session").write_bytes(b"\x00binary")
    (tmp_path / "big.bin").write_bytes(b"\x00" * 10)
    assert set(read_tree_sources(tmp_path)) == {"plugin.py"}


def test_read_tree_sources_respects_a_size_cap(tmp_path: Path) -> None:
    from userbot.safety import read_tree_sources

    (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("y = 2\n" * 1000, encoding="utf-8")
    assert set(read_tree_sources(tmp_path, max_bytes=50)) == {"small.py"}
