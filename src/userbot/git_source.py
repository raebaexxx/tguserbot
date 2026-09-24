from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from .config import Settings
from .loader import PluginManifest


class GitSourceError(RuntimeError):
    pass


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _select_source_root(
    repository_root: Path,
    subpath: str | None,
) -> tuple[Path, str | None]:
    if subpath is not None:
        source_root = repository_root / subpath
        if not source_root.is_dir():
            raise GitSourceError(f"Git subpath {subpath!r} does not exist")
        return source_root, subpath
    if (repository_root / "plugin.toml").is_file():
        return repository_root, None
    candidates = sorted(
        child
        for child in repository_root.iterdir()
        if child.is_dir() and (child / "plugin.toml").is_file()
    )
    if len(candidates) != 1:
        raise GitSourceError("Repository contains multiple plugins; specify a subpath")
    selected = candidates[0]
    return selected, selected.relative_to(repository_root).as_posix()


@dataclass(frozen=True, slots=True)
class GitPackage:
    name: str
    path: Path
    commit: str
    url: str
    subpath: str | None


class GitPluginSource:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _validate_url(self, url: str) -> str:
        normalized = url.strip().rstrip("/")
        if not normalized:
            raise GitSourceError("Git URL must not be empty")
        parsed = urlparse(normalized)
        if parsed.username or parsed.password:
            raise GitSourceError("Credentials must not be embedded in Git URLs")
        allowed_ssh = normalized.startswith("git@") and ":" in normalized
        if not allowed_ssh and parsed.scheme not in {"https", "ssh"}:
            raise GitSourceError("Only HTTPS and SSH Git URLs are supported")
        allowed = self.settings.git_allowed_repositories
        if not allowed:
            raise GitSourceError(
                "No Git repositories are allowed; set TGUSERBOT_GIT_ALLOWED_REPOS first"
            )
        if normalized not in allowed:
            raise GitSourceError("Git repository is not in TGUSERBOT_GIT_ALLOWED_REPOS")
        return normalized

    @staticmethod
    def _validate_subpath(subpath: str | None) -> str | None:
        if subpath is None or not subpath.strip() or subpath.strip() == ".":
            return None
        normalized = subpath.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise GitSourceError("Invalid Git plugin subpath")
        return path.as_posix()

    async def _run_git(self, *args: str, cwd: Path) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise GitSourceError(f"Git command failed: {detail or 'unknown error'}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def fetch(
        self,
        *,
        url: str,
        ref: str,
        subpath: str | None = None,
    ) -> GitPackage:
        normalized_url = self._validate_url(url)
        normalized_subpath = self._validate_subpath(subpath)
        normalized_ref = ref.strip()
        if not normalized_ref or normalized_ref.startswith("-") or ".." in normalized_ref:
            raise GitSourceError("Invalid Git ref")

        self.settings.git_plugin_dir.mkdir(parents=True, exist_ok=True)
        clone_path = Path(tempfile.mkdtemp(prefix=".clone-", dir=self.settings.git_plugin_dir))
        package_path: Path | None = None
        try:
            await self._run_git("init", "--quiet", cwd=clone_path)
            await self._run_git("remote", "add", "origin", normalized_url, cwd=clone_path)
            await self._run_git(
                "fetch",
                "--quiet",
                "--depth=1",
                "origin",
                normalized_ref,
                cwd=clone_path,
            )
            await self._run_git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=clone_path)
            commit = await self._run_git("rev-parse", "FETCH_HEAD", cwd=clone_path)
            source_root, normalized_subpath = await asyncio.to_thread(
                _select_source_root,
                clone_path,
                normalized_subpath,
            )
            manifest = await asyncio.to_thread(PluginManifest.from_path, source_root)
            if manifest.api != "1":
                raise GitSourceError(f"Unsupported plugin API version: {manifest.api}")

            package_path = Path(
                tempfile.mkdtemp(prefix=".package-", dir=self.settings.git_plugin_dir)
            )
            await asyncio.to_thread(
                shutil.copytree,
                source_root,
                package_path,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            final_parent = self.settings.git_plugin_dir / manifest.name
            final_parent.mkdir(parents=True, exist_ok=True)
            final_path = final_parent / commit
            if final_path.exists():
                await asyncio.to_thread(_remove_tree, package_path)
            else:
                await asyncio.to_thread(shutil.move, str(package_path), str(final_path))
            package_path = None
            return GitPackage(
                name=manifest.name,
                path=final_path,
                commit=commit,
                url=normalized_url,
                subpath=normalized_subpath,
            )
        finally:
            if package_path is not None:
                await asyncio.to_thread(_remove_tree, package_path)
            await asyncio.to_thread(_remove_tree, clone_path)
