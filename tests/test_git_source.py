"""Tests for the Git plugin source.

This is the most security-relevant module in the project: it runs ``git``
against operator-supplied URLs and executes whatever the repository contains.
It previously had no tests at all.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from userbot.config import Settings
from userbot.git_source import (
    GIT_ENV_OVERRIDES,
    GitPluginSource,
    GitSourceError,
    canonical_repo_url,
)

pytestmark = pytest.mark.git


def have_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


requires_git = pytest.mark.skipif(not have_git(), reason="git is not installed")


@pytest.fixture
def local_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitPluginSource:
    """A source whose URL policy is bypassed so a local repo can be fetched.

    The allow-list policy itself is covered by the pure-function tests; this
    fixture exercises the clone/stage/prune pipeline against a real repository.
    ``file`` is added to the allowed transports because ``GIT_ALLOW_PROTOCOL``
    correctly refuses to clone a local path in production.
    """
    import userbot.git_source as module

    monkeypatch.setitem(module.GIT_ENV_OVERRIDES, "GIT_ALLOW_PROTOCOL", "https:ssh:file")
    monkeypatch.setattr(GitPluginSource, "_validate_url", lambda _self, url: url)
    return GitPluginSource(make_settings(tmp_path))


def make_settings(tmp_path: Path, repos: tuple[str, ...] = ()) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
        git_allowed_repositories=repos,
    )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A local git repository holding one valid plugin."""
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True)
    plugin = tmp_path / "demo"
    plugin.mkdir()
    (plugin / "plugin.toml").write_text(
        'name = "demo"\nversion = "0.1.0"\napi = "1"\nentrypoint = "plugin:Plugin"\n',
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "plugin.py").write_text(
        "from userbot.plugin_api import Plugin\nclass Plugin(Plugin):\n    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


# --- URL canonicalisation --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://github.com/org/repo", "https://github.com:443/org/repo"),
        ("https://github.com/org/repo/", "https://github.com:443/org/repo"),
        ("https://github.com/org/repo.git", "https://github.com:443/org/repo"),
        ("https://GitHub.com/Org/Repo", "https://github.com:443/org/repo"),
        ("  https://github.com/org/repo  ", "https://github.com:443/org/repo"),
        ("ssh://git@github.com/org/repo.git", "ssh://git@github.com/org/repo"),
        ("git@github.com:org/repo.git", "ssh://git@github.com/org/repo"),
    ],
)
def test_canonical_repo_url(raw: str, expected: str) -> None:
    assert canonical_repo_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "ext::sh -c 'touch /tmp/pwned'",
        "https://user:pass@github.com/org/repo",
        "https://github.com:8443/org/repo",
        "https://github.com",
    ],
)
def test_canonical_repo_url_rejects_unsafe_input(raw: str) -> None:
    with pytest.raises(GitSourceError):
        canonical_repo_url(raw)


def test_allowlist_comparison_is_case_and_suffix_insensitive(tmp_path: Path) -> None:
    source = GitPluginSource(make_settings(tmp_path, ("https://github.com/Org/Repo.git",)))
    assert source._validate_url("https://github.com/org/repo") == (
        "https://github.com:443/org/repo"
    )
    assert source._validate_url("https://GitHub.com/ORG/REPO/") == (
        "https://github.com:443/org/repo"
    )


def test_allowlist_rejects_a_different_repository(tmp_path: Path) -> None:
    source = GitPluginSource(make_settings(tmp_path, ("https://github.com/org/allowed",)))
    with pytest.raises(GitSourceError, match="not in TGUSERBOT_GIT_ALLOWED_REPOS"):
        source._validate_url("https://github.com/evil/other")


def test_empty_allowlist_refuses_everything(tmp_path: Path) -> None:
    source = GitPluginSource(make_settings(tmp_path))
    with pytest.raises(GitSourceError, match="No Git repositories are allowed"):
        source._validate_url("https://github.com/org/repo")


# --- ref validation --------------------------------------------------------


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "feature/x", "a" * 100])
def test_valid_refs_are_accepted(ref: str) -> None:
    assert GitPluginSource._validate_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "  ",
        "--upload-pack=evil",
        "-x",
        "..",
        "a..b",
        "with space",
        "refs/heads/../../x",
        "x" * 300,
    ],
)
def test_invalid_refs_are_rejected(ref: str) -> None:
    with pytest.raises(GitSourceError, match="Invalid Git ref"):
        GitPluginSource._validate_ref(ref)


# --- subpath validation ----------------------------------------------------


@pytest.mark.parametrize("subpath", [None, "", "   ", ".", "plugins/demo", "a/b/c"])
def test_valid_subpaths(subpath: str | None) -> None:
    result = GitPluginSource._validate_subpath(subpath)
    assert result in (None, "plugins/demo", "a/b/c")


@pytest.mark.parametrize("subpath", ["/abs", "../escape", "a/../../b", "a/..", "..", "C:/win"])
def test_invalid_subpaths_are_rejected(subpath: str) -> None:
    with pytest.raises(GitSourceError, match="Invalid Git plugin subpath"):
        GitPluginSource._validate_subpath(subpath)


# --- git environment hardening ---------------------------------------------


def test_git_environment_disables_config_and_prompts() -> None:
    assert GIT_ENV_OVERRIDES["GIT_CONFIG_GLOBAL"] == os.devnull
    assert GIT_ENV_OVERRIDES["GIT_CONFIG_NOSYSTEM"] == "1"
    assert GIT_ENV_OVERRIDES["GIT_TERMINAL_PROMPT"] == "0"
    # Only the two transports we support; blocks ext:: helper RCE via insteadOf.
    assert GIT_ENV_OVERRIDES["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert "BatchMode=yes" in GIT_ENV_OVERRIDES["GIT_SSH_COMMAND"]


@requires_git
async def test_missing_git_binary_is_reported_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GitPluginSource(make_settings(tmp_path, ("https://github.com/org/repo",)))
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args: str, **kwargs: object):
        if args and args[0] == "git":
            raise FileNotFoundError("git")
        return await real_exec(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(GitSourceError, match="git.*not found"):
        await source._run_git("status", cwd=tmp_path)


@requires_git
async def test_git_command_timeout_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import userbot.git_source as module

    source = GitPluginSource(make_settings(tmp_path, ("https://github.com/org/repo",)))
    real_exec = asyncio.create_subprocess_exec
    calls: list[str] = []

    async def fake_exec(*args: str, **kwargs: object):
        calls.append(args[0] if args else "")
        return await real_exec(
            "sleep",
            "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(module, "GIT_TIMEOUT", 0.1)
    with pytest.raises(GitSourceError, match="timed out"):
        await source._run_git("status", cwd=tmp_path)
    assert calls == ["git"], "the stub must replace git, not sleep"


@requires_git
async def test_git_command_failure_includes_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GitPluginSource(make_settings(tmp_path, ("https://github.com/org/repo",)))
    with pytest.raises(GitSourceError, match="Git command failed"):
        await source._run_git("rev-parse", "--verify", "no-such-ref", cwd=tmp_path)


# --- fetching --------------------------------------------------------------


@requires_git
async def test_fetch_a_single_plugin_repository(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    package = await local_source.fetch(url=str(origin), ref="main")
    assert package.name == "demo"
    assert len(package.commit) == 40
    assert package.path.is_dir()
    assert (package.path / "plugin.toml").is_file()
    assert not (package.path / ".git").exists()
    assert package.path.parent == local_source.settings.git_plugin_dir / "demo"
    # No temporary clone directories are left behind.
    assert not list(local_source.settings.git_plugin_dir.glob(".clone-*"))
    assert not list(local_source.settings.git_plugin_dir.glob(".package-*"))


@requires_git
async def test_fetch_with_a_subpath(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    package = await local_source.fetch(url=str(origin), ref="main", subpath="demo")
    assert package.subpath == "demo"
    assert package.path.name == package.commit


@requires_git
async def test_fetch_rejects_a_bad_subpath(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    with pytest.raises(GitSourceError, match="does not exist"):
        await local_source.fetch(url=str(origin), ref="main", subpath="missing")


async def git(*args: str, cwd: Path) -> None:
    """Run git off the event loop; these are test-fixture mutations."""
    await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@requires_git
async def test_fetch_rejects_a_repo_without_a_plugin(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    await git("rm", "-r", "--quiet", "demo", cwd=origin)
    (origin / "README.md").write_text("nothing here\n", encoding="utf-8")
    await git("add", "-A", cwd=origin)
    await git("commit", "--quiet", "-m", "empty", cwd=origin)
    with pytest.raises(GitSourceError, match="multiple plugins|specify a subpath"):
        await local_source.fetch(url=str(origin), ref="main")


@requires_git
async def test_fetch_rejects_an_unsupported_api_version(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    (origin / "demo" / "plugin.toml").write_text(
        'name = "demo"\nversion = "0.1.0"\napi = "9"\nentrypoint = "plugin:Plugin"\n',
        encoding="utf-8",
    )
    await git("add", "-A", cwd=origin)
    await git("commit", "--quiet", "-m", "api", cwd=origin)
    with pytest.raises(GitSourceError, match="Unsupported plugin API"):
        await local_source.fetch(url=str(origin), ref="main")


@requires_git
async def test_fetch_reuses_an_already_staged_revision(
    tmp_path: Path, origin: Path, local_source: GitPluginSource
) -> None:
    first = await local_source.fetch(url=str(origin), ref="main")
    marker = first.path / "marker.txt"
    marker.write_text("kept\n", encoding="utf-8")
    second = await local_source.fetch(url=str(origin), ref="main")
    assert second.commit == first.commit
    assert marker.exists(), "an existing staged revision must not be clobbered"


@requires_git
async def test_stale_temp_directories_are_reaped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.git_plugin_dir.mkdir(parents=True, exist_ok=True)
    stale = settings.git_plugin_dir / ".clone-abandoned"
    stale.mkdir()
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = settings.git_plugin_dir / ".clone-inflight"
    fresh.mkdir()
    source = GitPluginSource(settings)
    await source._remove_stale_temp_dirs()
    assert not stale.exists()
    assert fresh.exists(), "a directory younger than the TTL must be left alone"


def test_guard_sandbox_sql_reexported_for_reference() -> None:
    """The SQL guard lives in storage; keep the import surface honest."""
    from userbot.storage import SandboxedSqlError, guard_sandbox_sql

    guard_sandbox_sql("SELECT 1")
    for bad in ("ATTACH DATABASE 'x' AS y", "pragma journal_mode=WAL", "VACUUM"):
        with pytest.raises(SandboxedSqlError):
            guard_sandbox_sql(bad)
    with pytest.raises(SandboxedSqlError, match="single SQL statement"):
        guard_sandbox_sql("SELECT 1; DROP TABLE t")
