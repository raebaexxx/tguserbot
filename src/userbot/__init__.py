"""Stable, public surface of the tguserbot plugin API.

Plugin authors should import from here rather than from deep module paths::

    from userbot.plugin_api import Plugin, PluginContext

Everything reachable from :data:`__all__` is covered by the project's semantic
versioning; internal modules may change between releases.
"""

from __future__ import annotations

from .commands import MAX_MESSAGE_LENGTH, CommandContext
from .config import Settings
from .health import HealthService
from .plugin_api import (
    LIFECYCLE_HOOKS,
    Plugin,
    PluginContext,
    PluginContractError,
    validate_plugin_interface,
)
from .plugin_config import PluginConfig, PluginConfigError
from .rate_limit import RateLimiter
from .storage import PluginStorage, SandboxedSqlError, StorageError
from .task_registry import TaskGroup

__version__ = "0.1.0"

__all__ = [
    "LIFECYCLE_HOOKS",
    "MAX_MESSAGE_LENGTH",
    "CommandContext",
    "HealthService",
    "Plugin",
    "PluginConfig",
    "PluginConfigError",
    "PluginContext",
    "PluginContractError",
    "PluginStorage",
    "RateLimiter",
    "SandboxedSqlError",
    "Settings",
    "StorageError",
    "TaskGroup",
    "__version__",
    "validate_plugin_interface",
]
