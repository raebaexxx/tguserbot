from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Keep the per-plugin error ring buffer bounded; ``/ub status`` only needs a
#: recent view, and an unbounded map would grow for the life of the process.
MAX_TRACKED_ERRORS = 64


@dataclass(slots=True)
class PluginError:
    plugin: str
    message: str
    at: float

    def age_seconds(self, now: float | None = None) -> float:
        return round((now if now is not None else time.time()) - self.at, 1)


@dataclass(slots=True)
class HealthService:
    started_monotonic: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)
    telegram_connected: bool = False
    authorized: bool = False
    watcher_running: bool = False
    reload_count: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    plugin_errors: dict[str, PluginError] = field(default_factory=dict)

    def mark_error(self, error: str | None, *, plugin: str | None = None) -> None:
        """Record or clear an error.

        ``plugin=None`` records a process-level error. A plugin-scoped error is
        tracked separately so that one healthy plugin reloading successfully can
        no longer erase the failure of an unrelated one.
        """
        if error is None:
            if plugin is None:
                self.last_error = None
                self.last_error_at = None
            else:
                self.plugin_errors.pop(plugin, None)
            return
        now = time.time()
        if plugin is None:
            self.last_error = error
            self.last_error_at = now
            return
        self.plugin_errors[plugin] = PluginError(plugin=plugin, message=error, at=now)
        while len(self.plugin_errors) > MAX_TRACKED_ERRORS:
            oldest = min(self.plugin_errors.values(), key=lambda item: item.at)
            self.plugin_errors.pop(oldest.plugin, None)

    def clear_plugin_error(self, plugin: str) -> None:
        self.plugin_errors.pop(plugin, None)

    def set_telegram_state(self, *, connected: bool, authorized: bool) -> None:
        self.telegram_connected = connected
        self.authorized = authorized

    def heartbeat_age_seconds(self) -> float | None:
        path = getattr(self, "_heartbeat_path", None)
        if path is None:
            return None
        try:
            return round(time.time() - path.stat().st_mtime, 1)
        except OSError:
            return None

    @property
    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    def snapshot(self, *, plugin_count: int = 0, plugin_errors: int = 0) -> dict[str, Any]:
        now = time.time()
        return {
            "uptime_seconds": self.uptime_seconds,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started_wall)),
            "telegram_connected": self.telegram_connected,
            "authorized": self.authorized,
            "watcher_running": self.watcher_running,
            "reload_count": self.reload_count,
            "last_error": self.last_error,
            "last_error_age_seconds": (
                None if self.last_error_at is None else round(now - self.last_error_at, 1)
            ),
            "plugin_count": plugin_count,
            "plugin_errors": plugin_errors,
            "plugin_error_details": [
                {
                    "plugin": item.plugin,
                    "message": item.message,
                    "age_seconds": item.age_seconds(now),
                }
                for item in sorted(self.plugin_errors.values(), key=lambda entry: -entry.at)
            ],
        }
