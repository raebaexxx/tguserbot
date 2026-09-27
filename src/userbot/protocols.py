"""Structural types for the values handed to plugins.

``PluginContext`` is the entire surface a plugin sees, and every field used to
be ``Any``. mypy reported success while checking nothing at the plugin boundary,
which is exactly where a mistake is most expensive: a plugin is third-party code
running in this process.

The protocols here describe the shape the core actually provides. They are
``runtime_checkable`` and verified against the real objects in
``tests/test_plugin_api.py``, so a core refactor that breaks the contract fails
the suite instead of surfacing as a ``AttributeError`` inside a plugin.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SupportsRespond(Protocol):
    """The event object a command callback or handler receives."""

    async def respond(self, text: str | None = None, **kwargs: Any) -> Any: ...

    async def edit_text(self, text: str, **kwargs: Any) -> Any: ...


@runtime_checkable
class EventDispatcher(Protocol):
    """The subset of ``CommandDispatcher`` plugins and the core share."""

    owner_ids: set[int]

    def register(
        self,
        name: str,
        callback: Any,
        *,
        help_text: str = "",
        aliases: tuple[str, ...] = (),
        plugin_name: str = "core",
    ) -> None: ...

    def unregister(self, name: str, *, plugin_name: str) -> None: ...

    def commands(self) -> list[Any]: ...

    def is_owner(self, sender_id: int | None) -> bool: ...


@runtime_checkable
class TelegramClientLike(Protocol):
    """The subset of the Telethon client plugins are expected to use."""

    def add_event_handler(self, callback: Any, event: Any) -> None: ...

    def remove_event_handler(self, callback: Any, event: Any | None = None) -> Any: ...

    def is_connected(self) -> bool: ...


@runtime_checkable
class PluginHost(Protocol):
    """The manager operations a plugin may perform on other plugins."""

    def active_count(self) -> int: ...

    def active_names(self) -> list[str]: ...

    def get_runtime(self, name: str) -> Any: ...

    def has_plugin(self, name: str) -> bool: ...

    def is_disabled(self, name: str) -> bool: ...

    async def error_count(self) -> int: ...


__all__ = [
    "EventDispatcher",
    "PluginHost",
    "SupportsRespond",
    "TelegramClientLike",
]
