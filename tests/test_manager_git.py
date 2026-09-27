"""End-to-end tests for installing and updating Git plugins.

A real local repository stands in for the remote, so the whole
fetch → stage → load → record → prune pipeline is exercised.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClient, make_command_plugin, write_plugin
from userbot.commands import CommandDispatcher
from userbot.config import Settings
from userbot.git_source import GitSourceError
from userbot.health import HealthService
from userbot.loader import PluginLoadError
from userbot.manager import GIT_KEEP_REVISIONS, PluginManager
from userbot.rate_limit import RateLimiter
from userbot.storage import Storage

pytestmark = pytest.mark.git


def have_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


requires_git = pytest.mark.skipif(not have_git(), reason="git is not installed")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def git_async(*args: str, cwd: Path) -> None:
    import asyncio

    await asyncio.to_thread(git, *args, cwd=cwd)


def commit_plugin(
    origin: Path,
    name: str,
    *,
    version: str = "0.1.0",
    command: str | None = None,
) -> str:
    plugin = origin / name
    plugin.mkdir(exist_ok=True)
    (plugin / "plugin.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\napi = "1"\n'
        'entrypoint = "plugin:Plugin"\ndescription = "git plugin"\n',
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    if command:
        (plugin / "plugin.py").write_text(
            textwrap.dedent(make_command_plugin(command)), encoding="utf-8"
        )
    else:
        (plugin / "plugin.py").write_text(
            "from userbot.plugin_api import Plugin\nclass Plugin(Plugin):\n    pass\n",
            encoding="utf-8",
        )
    git("add", "-A", cwd=origin)
    git("commit", "--quiet", "-m", f"{name} {version}", cwd=origin)
    return git_out(origin, "rev-parse", "HEAD")


def git_out(origin: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=origin, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=tmp_path, check=True)
    git("config", "user.email", "t@example.com", cwd=tmp_path)
    git("config", "user.name", "Tester", cwd=tmp_path)
    return tmp_path


async def build_manager(tmp_path: Path, plugin_root: Path) -> Any:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=plugin_root,
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    storage = Storage(settings.database_path)
    await storage.initialize()
    client = FakeClient()
    dispatcher = CommandDispatcher({1})
    manager = PluginManager(
        settings=settings,
        client=client,
        storage=storage,
        dispatcher=dispatcher,
        rate_limiter=RateLimiter(min_interval=0),
        health=HealthService(),
    )
    # The URL policy is exercised in test_git_source; here the pipeline is the
    # subject, so the local path is accepted and the transports relaxed.
    manager._git_source.allowed = {"local"}
    return manager, storage, client, dispatcher


@pytest.fixture(autouse=True)
def _allow_local_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import userbot.git_source as module

    monkeypatch.setitem(module.GIT_ENV_OVERRIDES, "GIT_ALLOW_PROTOCOL", "https:ssh:file")
    monkeypatch.setattr(module.GitPluginSource, "_validate_url", lambda _self, url: url)


@requires_git
async def test_install_a_git_plugin(tmp_path: Path, origin: Path, plugin_root: Path) -> None:
    commit = commit_plugin(origin, "remote_demo", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        runtime = await manager.install_git(str(origin), "main")
        assert runtime.name == "remote_demo"
        assert runtime.source == "git"
        assert runtime.source_ref == commit
        assert manager.active_count() == 1
        assert "rtdemo" in {item.name for item in dispatcher.commands()}
        state = await storage.get_plugin_state("remote_demo")
        assert state is not None
        assert state["status"] == "active"
        assert state["source"] == "git"
        assert state["source_ref"] == commit
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_a_git_plugin_survives_a_restart(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit = commit_plugin(origin, "remote_demo", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.install_git(str(origin), "main")
    await manager.shutdown()
    await storage.close()

    manager2, storage2, client2, dispatcher2 = await build_manager(tmp_path, plugin_root)
    try:
        await manager2.initialize_state()
        await manager2.load_all_local()
        assert manager2.get_runtime("remote_demo") is not None
        assert "rtdemo" in {item.name for item in dispatcher2.commands()}
        assert manager2.get_runtime("remote_demo").source_ref == commit
    finally:
        await manager2.shutdown()
        await storage2.close()


@requires_git
async def test_install_refuses_to_shadow_a_local_plugin(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "alpha")
    write_plugin(plugin_root, "alpha", body="")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        with pytest.raises(PluginLoadError, match="конфликтует"):
            await manager.install_git(str(origin), "main")
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_update_moves_to_a_new_commit(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    first = commit_plugin(origin, "remote_demo", version="0.1.0", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.install_git(str(origin), "main")
        second = commit_plugin(origin, "remote_demo", version="0.2.0", command="rtdemo")
        updated = await manager.update_git("remote_demo")
        assert updated.source_ref == second
        assert updated.manifest.version == "0.2.0"
        assert first != second
        state = await storage.get_plugin_state("remote_demo")
        assert state is not None
        assert state["source_ref"] == second
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_update_to_a_specific_ref_allows_rollback(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", version="0.1.0", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        first = commit_plugin(origin, "remote_demo", version="0.1.0", command="rtdemo")
        await manager.install_git(str(origin), "main")
        commit_plugin(origin, "remote_demo", version="0.2.0", command="rtdemo")
        await manager.update_git("remote_demo")
        assert manager.get_runtime("remote_demo").manifest.version == "0.2.0"
        rolled_back = await manager.update_git("remote_demo", first)
        assert rolled_back.manifest.version == "0.1.0"
        assert rolled_back.source_ref == first
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_update_rejects_a_manifest_rename(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.install_git(str(origin), "main")
        git("mv", "remote_demo", "renamed", cwd=origin)
        git("add", "-A", cwd=origin)
        git("commit", "--quiet", "-m", "rename", cwd=origin)
        # The recorded sub-path no longer resolves, so the update is refused.
        with pytest.raises(GitSourceError):
            await manager.update_git("remote_demo")
        # The original keeps running after the refused update.
        assert manager.get_runtime("remote_demo") is not None
        assert "rtdemo" in {item.name for item in dispatcher.commands()}
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_update_requires_an_active_git_plugin(tmp_path: Path, plugin_root: Path) -> None:
    write_plugin(plugin_root, "alpha", body="")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.load_all_local()
        with pytest.raises(PluginLoadError, match="Git-плагином"):
            await manager.update_git("alpha")
        with pytest.raises(PluginLoadError, match="Git-плагином"):
            await manager.update_git("absent")
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_old_revisions_are_pruned(tmp_path: Path, origin: Path, plugin_root: Path) -> None:
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        commit_plugin(origin, "remote_demo", version="0.0.0", command="rtdemo")
        await manager.install_git(str(origin), "main")
        for index in range(GIT_KEEP_REVISIONS + 3):
            # Change the tree each round so every commit has a distinct SHA.
            (origin / "remote_demo" / "notes.txt").write_text(
                f"revision {index}\n", encoding="utf-8"
            )
            commit_plugin(origin, "remote_demo", version=f"0.0.{index}", command="rtdemo")
            await manager.update_git("remote_demo")
        staged = tmp_path / "data" / "git-plugins" / "remote_demo"
        revisions = [path.name for path in staged.iterdir() if path.is_dir()]
        assert len(revisions) == GIT_KEEP_REVISIONS
        current = manager.get_runtime("remote_demo").source_ref
        assert current in revisions, "the active revision must never be pruned"
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_a_failed_update_keeps_the_previous_revision(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", version="0.1.0", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.install_git(str(origin), "main")
        good = manager.get_runtime("remote_demo").source_ref
        # Ship a broken revision, then try to move onto it.
        (origin / "remote_demo" / "plugin.py").write_text("class Plugin(:\n", encoding="utf-8")
        git("add", "-A", cwd=origin)
        git("commit", "--quiet", "-m", "broken", cwd=origin)
        with pytest.raises(PluginLoadError):
            await manager.update_git("remote_demo")
        # The last good revision is still the active one.
        assert manager.get_runtime("remote_demo").source_ref == good
        assert manager.get_runtime("remote_demo").manifest.version == "0.1.0"
        assert "rtdemo" in {item.name for item in dispatcher.commands()}
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_a_git_plugin_can_be_disabled_and_re_enabled(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.install_git(str(origin), "main")
        await manager.disable("remote_demo")
        assert manager.get_runtime("remote_demo") is None
        assert dispatcher.commands() == []
        await manager.enable("remote_demo")
        assert manager.get_runtime("remote_demo") is not None
        assert "rtdemo" in {item.name for item in dispatcher.commands()}
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_reload_of_a_git_plugin_fetches_a_new_commit(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", version="0.1.0", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    try:
        await manager.install_git(str(origin), "main")
        commit_plugin(origin, "remote_demo", version="0.3.0", command="rtdemo")
        updated = await manager.update_git("remote_demo")
        assert updated.manifest.version == "0.3.0"
    finally:
        await manager.shutdown()
        await storage.close()


@requires_git
async def test_a_missing_staged_revision_is_reported_not_fatal(
    tmp_path: Path, origin: Path, plugin_root: Path
) -> None:
    commit_plugin(origin, "remote_demo", command="rtdemo")
    manager, storage, client, dispatcher = await build_manager(tmp_path, plugin_root)
    await manager.install_git(str(origin), "main")
    await manager.shutdown()
    await storage.close()

    # Simulate someone wiping the staged checkout.
    import shutil

    shutil.rmtree(tmp_path / "data" / "git-plugins" / "remote_demo")

    manager2, storage2, client2, dispatcher2 = await build_manager(tmp_path, plugin_root)
    try:
        await manager2.initialize_state()
        await manager2.load_all_local()
        assert manager2.get_runtime("remote_demo") is None
        listed = {item["name"]: item for item in await manager2.list_plugins()}
        assert listed["remote_demo"]["status"] == "unloaded"
    finally:
        await manager2.shutdown()
        await storage2.close()
    assert client is not None
