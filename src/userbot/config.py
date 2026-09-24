from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_dotenv(path: Path) -> None:
    """Load a small, dependency-free .env file without overriding real env vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _as_path(root: Path, value: str | None, default: Path) -> Path:
    candidate = Path(value) if value else default
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.expanduser().resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    root_dir: Path
    data_dir: Path
    plugin_dir: Path
    log_dir: Path
    api_id: int
    api_hash: str = field(repr=False)
    phone: str | None = field(default=None, repr=False)
    owner_ids: frozenset[int] = frozenset()
    disabled_plugins: frozenset[str] = frozenset()
    git_allowed_repositories: tuple[str, ...] = ()
    log_level: str = "INFO"
    flood_sleep_threshold: float = 60.0
    min_request_interval: float = 0.5
    device_model: str = "tguserbot"
    app_version: str = "0.1.0"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "userbot.sqlite3"

    @property
    def git_plugin_dir(self) -> Path:
        return self.data_dir / "git-plugins"

    def validate(self) -> None:
        if self.api_id <= 0:
            raise RuntimeError(
                "TGUSERBOT_API_ID is missing or invalid; obtain credentials at "
                "https://my.telegram.org/apps"
            )
        if not self.api_hash.strip():
            raise RuntimeError("TGUSERBOT_API_HASH is missing")

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> Settings:
        root = (root_dir or Path(os.environ.get("TGUSERBOT_ROOT", Path.cwd()))).resolve()
        _read_dotenv(root / ".env")

        try:
            api_id = int(os.environ.get("TGUSERBOT_API_ID", "0"))
        except ValueError as exc:
            raise RuntimeError("TGUSERBOT_API_ID must be an integer") from exc

        data_dir = _as_path(root, os.environ.get("TGUSERBOT_DATA_DIR"), root / "var")
        plugin_dir = _as_path(root, os.environ.get("TGUSERBOT_PLUGIN_DIR"), root / "plugins")
        log_dir = _as_path(root, os.environ.get("TGUSERBOT_LOG_DIR"), data_dir / "logs")
        owner_ids: set[int] = set()
        for raw_id in _csv(os.environ.get("TGUSERBOT_OWNER_IDS", "")):
            try:
                owner_ids.add(int(raw_id))
            except ValueError as exc:
                raise RuntimeError(f"Invalid owner ID: {raw_id!r}") from exc

        try:
            flood_sleep_threshold = float(os.environ.get("TGUSERBOT_FLOOD_THRESHOLD", "60"))
            min_request_interval = float(os.environ.get("TGUSERBOT_MIN_INTERVAL", "0.5"))
        except ValueError as exc:
            raise RuntimeError("Flood and request interval settings must be numbers") from exc

        repos = tuple(
            repo.rstrip("/") for repo in _csv(os.environ.get("TGUSERBOT_GIT_ALLOWED_REPOS", ""))
        )
        disabled = frozenset(_csv(os.environ.get("TGUSERBOT_DISABLED_PLUGINS", "")))
        phone = os.environ.get("TGUSERBOT_PHONE") or None

        return cls(
            root_dir=root,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            log_dir=log_dir,
            api_id=api_id,
            api_hash=os.environ.get("TGUSERBOT_API_HASH", ""),
            phone=phone,
            owner_ids=frozenset(owner_ids),
            disabled_plugins=disabled,
            git_allowed_repositories=repos,
            log_level=os.environ.get("TGUSERBOT_LOG_LEVEL", "INFO").upper(),
            flood_sleep_threshold=flood_sleep_threshold,
            min_request_interval=min_request_interval,
            device_model=os.environ.get("TGUSERBOT_DEVICE_MODEL", "tguserbot"),
            app_version=os.environ.get("TGUSERBOT_APP_VERSION", "0.1.0"),
        )
