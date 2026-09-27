from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urlunparse

from .config import Settings
from .loader import PluginManifest

#: Refuse to let git hang forever on a stalled network or a prompt.
GIT_TIMEOUT = 120.0

#: Temporary clone/package directories older than this are garbage.
STALE_TEMP_AGE = 3600.0

#: Environment applied to every git invocation: no config, no credential
#: prompts, and only the transports we intend to allow. Without these, an
#: operator's global ``url.*.insteadOf`` could rewrite an allow-listed HTTPS URL
#: into an ``ext::`` helper, which git executes.
GIT_ENV_OVERRIDES = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_ALLOW_PROTOCOL": "https:ssh",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}

_VALID_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class GitSourceError(RuntimeError):
    pass


class GitCommandError(GitSourceError):
    pass


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def canonical_repo_url(url: str) -> str:
    """Normalise a repository URL so the allow-list compares like with like.

    The previous exact string match made the allow-list unusable in practice:
    ``https://GitHub.com/Org/Repo.git`` and ``https://github.com/org/repo``
    are the same repository but compared unequal, which invites operators to
    widen ``TGUSERBOT_GIT_ALLOWED_REPOS``.
    """
    candidate = url.strip()
    if not candidate:
        raise GitSourceError("Git URL must not be empty")
    scp_match = re.match(r"^(?:ssh://)?git@([^:/]+):(.+)$", candidate)
    if scp_match and "://" not in candidate:
        host, path = scp_match.group(1), scp_match.group(2)
        return f"ssh://git@{host.lower()}/{_normalize_repo_path(path)}"
    parsed = urlparse(candidate)
    if not parsed.scheme:
        raise GitSourceError(f"Unsupported Git URL: {url!r}")
    if parsed.username or parsed.password:
        raise GitSourceError("Credentials must not be embedded in Git URLs")
    if parsed.port not in (None, 443, 22):
        raise GitSourceError("Unexpected port in the Git URL")
    if parsed.scheme not in {"https", "ssh", "http"}:
        raise GitSourceError("Only HTTPS and SSH Git URLs are supported")
    path = _normalize_repo_path(parsed.path)
    netloc = parsed.hostname or ""
    if parsed.scheme == "http":
        netloc = f"{netloc}:80"
    elif parsed.scheme == "https":
        netloc = f"{netloc}:443"
    else:
        netloc = f"git@{netloc}"
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


def _normalize_repo_path(path: str) -> str:
    normalized = path.strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    if not normalized:
        raise GitSourceError("Git URL must include a repository path")
    # Comparison form: hosts we allow treat the path case-insensitively, and a
    # case-sensitive mismatch would only push operators to widen the allow-list.
    return normalized.lower()


def _select_source_root(
    repository_root: Path,
    subpath: str | None,
) -> tuple[Path, str | None]:
    if subpath is not None:
        source_root = repository_root / subpath
        # A symlink inside the repository could point anywhere on disk.
        resolved_root = repository_root.resolve()
        resolved = source_root.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise GitSourceError(f"Git subpath {subpath!r} escapes the repository")
        if not resolved.is_dir():
            raise GitSourceError(f"Git subpath {subpath!r} does not exist")
        return resolved, subpath
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
        self.allowed = {canonical_repo_url(item) for item in settings.git_allowed_repositories}

    def _validate_url(self, url: str) -> str:
        normalized = canonical_repo_url(url)
        if not self.allowed:
            raise GitSourceError(
                "No Git repositories are allowed; set TGUSERBOT_GIT_ALLOWED_REPOS first"
            )
        if normalized not in self.allowed:
            raise GitSourceError(
                f"Git repository {url!r} is not in TGUSERBOT_GIT_ALLOWED_REPOS"
            )
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

    @staticmethod
    def _validate_ref(ref: str) -> str:
        normalized = ref.strip()
        if not normalized:
            raise GitSourceError("Invalid Git ref")
        if not _VALID_REF.match(normalized) or ".." in normalized:
            raise GitSourceError(f"Invalid Git ref: {ref!r}")
        return normalized

    async def _run_git(self, *args: str, cwd: Path) -> str:
        env = os.environ.copy()
        env.update(GIT_ENV_OVERRIDES)
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise GitSourceError(
                "The 'git' executable was not found; install git to use Git plugins"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=GIT_TIMEOUT
            )
        except TimeoutError as exc:
            try:
                process.kill()
            except (ProcessLookupError, AttributeError):
                pass
            raise GitSourceError(
                f"git {' '.join(args[:2])} timed out after {GIT_TIMEOUT:.0f}s"
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise GitCommandError(f"Git command failed: {detail or 'unknown error'}")
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
        normalized_ref = self._validate_ref(ref)

        self.settings.git_plugin_dir.mkdir(parents=True, exist_ok=True)
        await self._remove_stale_temp_dirs()
        clone_path = Path(tempfile.mkdtemp(prefix=".clone-", dir=self.settings.git_plugin_dir))
        package_path: Path | None = None
        try:
            await self._run_git("init", "--quiet", cwd=clone_path)
            await self._run_git("remote", "add", "origin", normalized_url, cwd=clone_path)
            await self._run_git(
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "origin",
                normalized_ref,
                cwd=clone_path,
            )
            await self._run_git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=clone_path)
            commit = await self._run_git(
                "rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=clone_path
            )
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise GitSourceError(f"Unexpected commit identifier: {commit!r}")
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

    async def _remove_stale_temp_dirs(self) -> None:
        """Reap leftovers from a previous crash; ``mkdtemp`` alone leaks them."""
        import time

        now = time.time()
        try:
            entries = list(self.settings.git_plugin_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith((".clone-", ".package-")):
                continue
            try:
                if now - entry.stat().st_mtime < STALE_TEMP_AGE:
                    continue
            except OSError:
                continue
            await asyncio.to_thread(_remove_tree, entry)
