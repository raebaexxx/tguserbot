from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any

from telethon import events


@dataclass(slots=True)
class CommandContext:
    name: str
    args: str
    raw: str
    event: Any

    async def respond(self, text: str, **kwargs: Any) -> Any:
        return await self.event.respond(text, **kwargs)


@dataclass(slots=True)
class CommandRegistration:
    name: str
    callback: Any
    help_text: str
    aliases: tuple[str, ...]
    plugin_name: str


class CommandDispatcher:
    """Dispatches owner-only userbot commands and owns plugin command unregistering."""

    def __init__(self, owner_ids: set[int] | frozenset[int] = frozenset()) -> None:
        self.owner_ids = set(owner_ids)
        self._commands: dict[str, CommandRegistration] = {}
        self._handler: Any = None
        self._client: Any = None

    def add_owner(self, user_id: int) -> None:
        self.owner_ids.add(user_id)

    def register(
        self,
        name: str,
        callback: Any,
        *,
        help_text: str = "",
        aliases: tuple[str, ...] = (),
        plugin_name: str = "core",
    ) -> None:
        normalized = self._normalize(name)
        if not normalized:
            raise ValueError("Command name must not be empty")
        if normalized in self._commands:
            raise ValueError(f"Command /{normalized} is already registered")
        if not callable(callback):
            raise TypeError("Command callback must be callable")
        normalized_aliases = tuple(
            dict.fromkeys(self._normalize(alias) for alias in aliases if self._normalize(alias))
        )
        for candidate in (normalized, *normalized_aliases):
            if candidate in self._commands:
                raise ValueError(f"Command /{candidate} is already registered")
        self._commands[normalized] = CommandRegistration(
            name=normalized,
            callback=callback,
            help_text=help_text,
            aliases=normalized_aliases,
            plugin_name=plugin_name,
        )
        for alias in normalized_aliases:
            self._commands[alias] = CommandRegistration(
                name=normalized,
                callback=callback,
                help_text=help_text,
                aliases=(),
                plugin_name=plugin_name,
            )

    def unregister(self, name: str, *, plugin_name: str) -> None:
        normalized = self._normalize(name)
        registration = self._commands.get(normalized)
        if registration is None or registration.plugin_name != plugin_name:
            return
        self._commands.pop(normalized, None)
        for candidate in (registration.name, *registration.aliases):
            current = self._commands.get(candidate)
            if current is not None and current.plugin_name == plugin_name:
                self._commands.pop(candidate, None)

    def commands(self) -> list[CommandRegistration]:
        unique: dict[str, CommandRegistration] = {}
        for registration in self._commands.values():
            unique.setdefault(registration.name, registration)
        return sorted(unique.values(), key=lambda item: item.name)

    def attach_client(self, client: Any) -> None:
        self._client = client
        if self._handler is not None:
            return
        self._handler = self.handle_event
        client.add_event_handler(
            self._handler,
            events.NewMessage(pattern=r"(?m)^(?:/ub|\.ub)(?:\s+|$)"),
        )

    def detach_client(self) -> None:
        if self._client is not None and self._handler is not None:
            self._client.remove_event_handler(self._handler)
        self._client = None
        self._handler = None

    async def handle_event(self, event: Any) -> None:
        sender_id = getattr(event, "sender_id", None)
        if sender_id not in self.owner_ids:
            return
        raw = getattr(event, "raw_text", None) or getattr(event, "text", "") or ""
        match = re.match(r"^\s*(?:/ub|\.ub)(?:\s+|$)(.*)$", raw, flags=re.DOTALL)
        if match is None:
            return
        body = match.group(1).strip()
        if not body:
            return
        pieces = body.split(maxsplit=1)
        command_name = self._normalize(pieces[0])
        args = pieces[1].strip() if len(pieces) == 2 else ""
        registration = self._commands.get(command_name)
        if registration is None:
            await event.respond("Неизвестная команда. Отправьте /ub help")
            return
        try:
            result = registration.callback(
                CommandContext(name=registration.name, args=args, raw=raw, event=event)
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._log_exception(registration)
            await event.respond("Ошибка выполнения команды; подробности записаны в лог.")

    def help_text(self) -> str:
        lines = ["Команды userbot:"]
        for registration in self.commands():
            suffix = f" — {registration.help_text}" if registration.help_text else ""
            lines.append(f"/ub {registration.name}{suffix}")
        return "\n".join(lines)

    def _log_exception(self, registration: CommandRegistration) -> None:
        import logging

        logging.getLogger(f"userbot.command.{registration.plugin_name}").exception(
            "command callback failed"
        )

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().lstrip("/.")
