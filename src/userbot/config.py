from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse a small dotenv-style file into a mapping.

    Supports ``KEY=value``, an optional ``export`` prefix, surrounding single or
    double quotes, blank lines, and ``#`` comments. It never mutates
    ``os.environ``: leaking parsed values into the process environment made a
    second ``from_env`` call silently reuse the first file's secrets.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
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
            values[key] = value
    return values


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a dotenv file and return its key/value pairs without side effects."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    return _parse_env_text(text)


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
    watch_enabled: bool = True
    watch_interval: float = 2.0
    command_timeout: float = 120.0
    log_json: bool = False

    @property
    def session_path(self) -> Path:
        return self.data_dir / "session"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "userbot.sqlite3"

    @property
    def git_plugin_dir(self) -> Path:
        return self.data_dir / "git-plugins"

    @property
    def plugin_data_dir(self) -> Path:
        """Root of the per-plugin SQLite sandbox (see ``Storage.plugin_store``)."""
        return self.data_dir / "plugin-data"

    @property
    def installed_plugin_dir(self) -> Path:
        """Root for plugins the operator installed at runtime.

        Deliberately *not* ``plugin_dir``, and deliberately not configurable. The
        shipped tree is root-owned and made read-only by ``ProtectSystem=strict``,
        so writing an adopted plugin there fails in production while succeeding in
        development. Keeping runtime-installed plugins under the writable data
        directory means the same code path works in both, and the shipped code
        stays untamperable.

        ``TGUSERBOT_INSTALLED_PLUGIN_DIR`` used to override this, and was the worst
        kind of setting: undocumented, untested, and able only to break the one
        invariant the directory exists to hold. Point it somewhere the service user
        cannot write and adoption fails; point it somewhere nothing scans and a
        plugin is adopted and then never loads. Removed.
        """
        return self.data_dir / "local-plugins"

    @property
    def staging_dir(self) -> Path:
        """Where generated plugins wait for review before being adopted.

        Not a plugin root: a subdirectory here has no ``plugin.toml`` of its
        own, so neither discovery nor the watcher ever sees it.
        """
        return self.data_dir / "plugin-staging"

    @property
    def plugin_config_path(self) -> Path:
        """Operator overrides for per-plugin settings."""
        return self.data_dir / "plugin-config.toml"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    def validate(self) -> None:
        if self.api_id <= 0:
            raise RuntimeError(
                "TGUSERBOT_API_ID is missing or invalid; obtain credentials at "
                "https://my.telegram.org/apps"
            )
        if not self.api_hash.strip():
            raise RuntimeError("TGUSERBOT_API_HASH is missing")
        if self.flood_sleep_threshold < 0:
            raise RuntimeError(
                "TGUSERBOT_FLOOD_THRESHOLD must not be negative; a negative value "
                "turns every FloodWait into an exception instead of a sleep"
            )
        if self.min_request_interval < 0:
            raise RuntimeError("TGUSERBOT_MIN_INTERVAL must not be negative")
        if self.watch_interval <= 0:
            raise RuntimeError("TGUSERBOT_WATCH_INTERVAL must be positive")
        if self.command_timeout <= 0:
            raise RuntimeError("TGUSERBOT_COMMAND_TIMEOUT must be positive")
        bad_owners = sorted(value for value in self.owner_ids if value <= 0)
        if bad_owners:
            raise RuntimeError(f"TGUSERBOT_OWNER_IDS must be positive user IDs: {bad_owners}")

    @classmethod
    def from_env(
        cls,
        root_dir: Path | None = None,
        env_file: Path | None = None,
    ) -> Settings:
        root = (root_dir or Path(os.environ.get("TGUSERBOT_ROOT", Path.cwd()))).resolve()
        dotenv_path = env_file or (root / ".env")
        if not dotenv_path.is_absolute():
            dotenv_path = root / dotenv_path
        # Real environment variables win over the file, but nothing is written
        # back into os.environ, so repeated calls stay independent.
        env: dict[str, str] = parse_env_file(dotenv_path)
        env.update(os.environ)

        raw_api_id = env.get("TGUSERBOT_API_ID", "").strip()
        try:
            api_id = int(raw_api_id or "0")
        except ValueError as exc:
            raise RuntimeError("TGUSERBOT_API_ID must be an integer") from exc

        data_dir = _as_path(root, env.get("TGUSERBOT_DATA_DIR"), root / "var")
        plugin_dir = _as_path(root, env.get("TGUSERBOT_PLUGIN_DIR"), root / "plugins")
        log_dir = _as_path(root, env.get("TGUSERBOT_LOG_DIR"), data_dir / "logs")
        owner_ids: set[int] = set()
        for raw_id in _csv(env.get("TGUSERBOT_OWNER_IDS", "")):
            try:
                owner_ids.add(int(raw_id))
            except ValueError as exc:
                raise RuntimeError(f"Invalid owner ID: {raw_id!r}") from exc

        try:
            flood_sleep_threshold = float(env.get("TGUSERBOT_FLOOD_THRESHOLD", "60"))
            min_request_interval = float(env.get("TGUSERBOT_MIN_INTERVAL", "0.5"))
        except ValueError as exc:
            raise RuntimeError("Flood and request interval settings must be numbers") from exc

        watch_enabled = env.get("TGUSERBOT_WATCH", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        try:
            watch_interval = float(env.get("TGUSERBOT_WATCH_INTERVAL", "2.0"))
        except ValueError as exc:
            raise RuntimeError("TGUSERBOT_WATCH_INTERVAL must be a number") from exc

        repos = tuple(repo.rstrip("/") for repo in _csv(env.get("TGUSERBOT_GIT_ALLOWED_REPOS", "")))
        disabled = frozenset(_csv(env.get("TGUSERBOT_DISABLED_PLUGINS", "")))
        phone = env.get("TGUSERBOT_PHONE") or None

        return cls(
            root_dir=root,
            data_dir=data_dir,
            plugin_dir=plugin_dir,
            log_dir=log_dir,
            api_id=api_id,
            api_hash=env.get("TGUSERBOT_API_HASH", ""),
            phone=phone,
            owner_ids=frozenset(owner_ids),
            disabled_plugins=disabled,
            git_allowed_repositories=repos,
            log_level=env.get("TGUSERBOT_LOG_LEVEL", "INFO").upper(),
            flood_sleep_threshold=flood_sleep_threshold,
            min_request_interval=min_request_interval,
            device_model=env.get("TGUSERBOT_DEVICE_MODEL", "tguserbot"),
            app_version=env.get("TGUSERBOT_APP_VERSION", "0.1.0"),
            watch_enabled=watch_enabled,
            watch_interval=watch_interval,
            command_timeout=float(env.get("TGUSERBOT_COMMAND_TIMEOUT", "120")),
            log_json=env.get("TGUSERBOT_LOG_JSON", "0").strip().lower()
            in {"1", "true", "yes", "on"},
        )
