from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HealthService:
    started_monotonic: float = field(default_factory=time.monotonic)
    telegram_connected: bool = False
    authorized: bool = False
    watcher_running: bool = False
    reload_count: int = 0
    last_error: str | None = None

    def mark_error(self, error: str | None) -> None:
        self.last_error = error

    def snapshot(self, *, plugin_count: int = 0, plugin_errors: int = 0) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 1),
            "telegram_connected": self.telegram_connected,
            "authorized": self.authorized,
            "watcher_running": self.watcher_running,
            "reload_count": self.reload_count,
            "last_error": self.last_error,
            "plugin_count": plugin_count,
            "plugin_errors": plugin_errors,
        }
