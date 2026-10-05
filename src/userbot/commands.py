from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from telethon import events

from .topics import TopicAwareEvent

#: Telegram rejects text messages longer than this, counted in UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096

#: Matches a `/ub` or `.ub` command at the very start of a message. Telethon
#: applies the pattern with ``re.match`` (anchored), so ``(?m)`` would be
#: misleading here and is deliberately absent.
COMMAND_PATTERN = r"^(?:/ub|\.ub)(?:\s+|$)"

#: Fallback parser used when a command-looking message arrives without a
#: ``bot_command`` entity (for example a message typed on another client).
_COMMAND_BODY = re.compile(r"^(?:/ub|\.ub)(?:\s+(?P<body>[\s\S]*))?$")

logger = logging.getLogger("userbot.commands")


@dataclass(slots=True)
class CommandContext:
    name: str
    args: str
    raw: str
    event: Any

    async def respond(self, text: str, **kwargs: Any) -> Any:
        return await self.event.respond(_clip(text), **kwargs)


@dataclass(slots=True)
class CommandRegistration:
    name: str
    callback: Any
    help_text: str
    aliases: tuple[str, ...]
    plugin_name: str


def _utf16_units(text: str) -> int:
    """Length in the units Telegram counts.

    Telegram measures a message in UTF-16 code units, not code points. Every
    character outside the BMP is one code point here and two units there, so
    ``len()`` understated an emoji-heavy message by a factor of two -- and the
    clipped text was still over the limit, and refused.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _clip(text: str) -> str:
    """Trim to Telegram's limit, measured the way Telegram measures it."""
    if _utf16_units(text) <= MAX_MESSAGE_LENGTH:
        return text
    budget = MAX_MESSAGE_LENGTH - 1  # room for the ellipsis
    # Built a code point at a time so a surrogate pair is never cut in half: half
    # an emoji is not text, it is a replacement character.
    out: list[str] = []
    used = 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > budget:
            break
        out.append(character)
        used += width
    return "".join(out) + "…"


#: Sub-commands accepted by ``/ub plugin``.
PLUGIN_ACTIONS = ("list", "reload", "enable", "disable", "install", "update", "adopt")


@dataclass(frozen=True, slots=True)
class PluginAction:
    action: str
    name: str | None = None
    #: First positional argument after the action; a URL for ``install``.
    ref: str | None = None
    #: Second positional argument: a Git ref for ``install``/``update``.
    commit: str | None = None
    subpath: str | None = None


def parse_plugin_action(text: str) -> PluginAction | None:
    """Parse ``/ub plugin ...`` arguments.

    Returns ``None`` when there is no recognisable sub-command so the caller can
    print usage instead of guessing. An explicit parsing function replaces the
    hand-rolled ``split(maxsplit=3)`` chain, which silently mis-assigned
    arguments for some shapes.
    """
    parts = text.strip().split()
    if not parts:
        return None
    action = parts[0].lower()
    if action not in PLUGIN_ACTIONS:
        return None
    rest = parts[1:]
    if action == "list":
        return PluginAction(action="list")
    if action == "adopt":
        if not rest:
            return None
        return PluginAction(action="adopt", name=rest[0])
    if action in {"reload", "enable", "disable"}:
        if not rest:
            return None
        return PluginAction(action=action, name=rest[0])
    if action == "install":
        if len(rest) < 2:
            return None
        # A sub-path never contains whitespace, so the remainder is a single token.
        subpath = rest[2] if len(rest) > 2 else None
        if subpath is not None and (len(rest) > 3 or any(char.isspace() for char in subpath)):
            return None
        return PluginAction(action="install", ref=rest[0], commit=rest[1], subpath=subpath)
    if not rest:
        return None
    return PluginAction(action="update", name=rest[0], commit=rest[1] if len(rest) > 1 else None)


class CommandDispatcher:
    """Dispatches owner-only userbot commands and owns plugin command unregistering.

    The dispatcher is the single security boundary for the ``/ub`` prefix, so it
    also enforces the operational limits that keep a misbehaving plugin from
    wedging the process: a per-command timeout, coalescing of duplicate
    invocations, and a small per-sender cooldown.
    """

    def __init__(
        self,
        owner_ids: set[int] | frozenset[int] = frozenset(),
        *,
        command_timeout: float = 120.0,
        cooldown: float = 0.0,
        max_cooldown_entries: int = 512,
    ) -> None:
        self.owner_ids = set(owner_ids)
        self._commands: dict[str, CommandRegistration] = {}
        self._handler: Any = None
        self._client: Any = None
        self._command_timeout = command_timeout
        self._cooldown = cooldown
        self._max_cooldown_entries = max(1, max_cooldown_entries)
        self._cooldowns: OrderedDict[tuple[int, str], float] = OrderedDict()
        self._running: set[tuple[int, str]] = set()

    # -- configuration ------------------------------------------------------

    def set_command_timeout(self, seconds: float) -> None:
        self._command_timeout = max(0.0, seconds)

    def set_cooldown(self, seconds: float) -> None:
        self._cooldown = max(0.0, seconds)

    def set_max_cooldown_entries(self, entries: int) -> None:
        self._max_cooldown_entries = max(1, entries)

    def cooldown_entry_count(self) -> int:
        return len(self._cooldowns)

    def add_owner(self, user_id: int) -> None:
        self.owner_ids.add(user_id)

    # -- registry -----------------------------------------------------------

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
            dict.fromkeys(
                self._normalize(alias)
                for alias in aliases
                if self._normalize(alias) and self._normalize(alias) != normalized
            )
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

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id in self.owner_ids

    def commands(self) -> list[CommandRegistration]:
        unique: dict[str, CommandRegistration] = {}
        for registration in self._commands.values():
            unique.setdefault(registration.name, registration)
        return sorted(unique.values(), key=lambda item: item.name)

    def help_text(self) -> str:
        lines = ["Команды userbot:"]
        for registration in self.commands():
            suffix = f" — {registration.help_text}" if registration.help_text else ""
            lines.append(f"/ub {registration.name}{suffix}")
        return "\n".join(lines)

    # -- client attachment --------------------------------------------------

    def attach_client(self, client: Any) -> None:
        if client is None:
            raise RuntimeError("A Telegram client is required to dispatch commands")
        self._client = client
        if self._handler is not None:
            return
        self._handler = self.handle_event
        client.add_event_handler(
            self._handler,
            events.NewMessage(pattern=COMMAND_PATTERN),
        )

    def detach_client(self) -> None:
        if self._client is not None and self._handler is not None:
            self._client.remove_event_handler(self._handler)
        self._client = None
        self._handler = None

    # -- dispatch -----------------------------------------------------------

    @staticmethod
    def extract_command_body(message: Any) -> str | None:
        """Return the text after ``/ub``/``.ub``, or ``None`` if this is not a command.

        A line that merely *contains* ``/ub`` (inside a quote, a note body, or a
        plugin's own reply) must not be treated as a command, so the check is
        anchored to the start of the message and prefers Telegram's own
        ``bot_command`` entity when the client provides one.
        """
        if message is None:
            return None
        raw = getattr(message, "raw_text", None) or getattr(message, "text", None) or ""
        if not raw:
            return None
        entities = getattr(message, "entities", None)
        if entities:
            for entity in entities:
                if getattr(entity, "offset", None) != 0:
                    continue
                bot_command = getattr(entity, "bot_command", None)
                if bot_command is None:
                    continue
                prefix = "/" + bot_command
                if raw.startswith(prefix):
                    return raw[len(prefix) :].strip()
        match = _COMMAND_BODY.match(raw)
        if match is None:
            return None
        return (match.group("body") or "").strip()

    async def handle_event(self, event: Any) -> None:
        # Wrapped once, here, before any of the early replies below. Anything
        # that answers before the command is even resolved -- an unknown
        # command, a cooldown -- is still a reply, and it still belongs in the
        # topic the question was asked in.
        event = TopicAwareEvent(event)
        sender_id = getattr(event, "sender_id", None)
        if sender_id not in self.owner_ids:
            return
        message = getattr(event, "message", None)
        body = self.extract_command_body(message if message is not None else event)
        if body is None:
            return
        pieces = body.split(maxsplit=1)
        if not pieces:
            return
        command_name = self._normalize(pieces[0])
        args = pieces[1].strip() if len(pieces) == 2 else ""
        registration = self._commands.get(command_name)
        if registration is None:
            await event.respond("Неизвестная команда. Отправьте /ub help")
            return
        # Telegram never delivers more than this, so clipping is a safety net
        # rather than a real limit; it keeps a plugin from being handed an
        # unbounded buffer.
        args = _clip(args)
        key = (int(sender_id), registration.name)
        if self._cooldown > 0 and not self._claim_cooldown(key):
            await event.respond("Слишком часто. Повторите позже.")
            return
        if key in self._running:
            return  # a double tap must not run the same command twice
        self._running.add(key)
        try:
            raw = getattr(event, "raw_text", "") or ""
            await self._invoke(
                registration,
                CommandContext(registration.name, args, raw, event),
            )
        finally:
            self._running.discard(key)

    async def _invoke(self, registration: CommandRegistration, command: CommandContext) -> None:
        try:
            result = registration.callback(command)
            if not inspect.isawaitable(result):
                return
            if self._command_timeout <= 0:
                await result
                return
            await asyncio.wait_for(result, timeout=self._command_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error("command /%s timed out", registration.name)
            await self._notify(command, f"Команда /{registration.name} превысила лимит времени.")
        except Exception:
            logger.exception(
                "command /%s from plugin %s failed", registration.name, registration.plugin_name
            )
            await self._notify(command, "Ошибка выполнения команды; подробности записаны в лог.")

    async def _notify(self, command: CommandContext, text: str) -> None:
        try:
            await command.respond(text)
        except Exception:
            logger.debug("could not deliver the command failure notice", exc_info=True)

    def _claim_cooldown(self, key: tuple[int, str]) -> bool:
        now = time.monotonic()
        previous = self._cooldowns.get(key)
        if previous is not None and now - previous < self._cooldown:
            self._cooldowns.move_to_end(key)
            return False
        self._cooldowns[key] = now
        self._cooldowns.move_to_end(key)
        while len(self._cooldowns) > self._max_cooldown_entries:
            self._cooldowns.popitem(last=False)
        return True

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().lstrip("/.")
