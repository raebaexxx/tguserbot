"""Summarise the last messages in a chat.

The command is owner-only like every other, but it reads what other people wrote:
a request that leaves this machine sends a conversation to a third party. Media
is therefore **off by default** and the default is the point -- turn it on
per-plugin for chats where that is acceptable.

The shape of the work is: read the recent messages, render the text into a
transcript, download whatever attachments are allowed, send both, report what the
model did not get. Nothing here is clever; what matters is that every part that
can quietly go wrong -- an unknown mime type, a file over the cap, a transcript
too long to send -- leaves a trace the user can see instead of a summary that is
subtly wrong.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from userbot.gemini import (
    DEFAULT_BASE_URL,
    GeminiError,
    MissingKeyError,
    ModelRouter,
    Part,
    Turn,
    keys_from_env,
)
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext

from ._collect import (
    Collected,
    MediaItem,
    SkippedMedia,
    build_transcript,
    classify,
    mime_for,
    take_media,
)
from ._prompt import SUMMARY_SYSTEM, build_request_parts

LOGGER = logging.getLogger(__name__)

USAGE = (
    "Использование:\n"
    "/ub sum — последние 10 сообщений в этом чате\n"
    "/ub sum 25 — последние 25\n"
    "/ub sum 10 media — вместе с голосовыми и кружками\n"
    "\n"
    "Медиа по умолчанию выключено: текст чужих сообщений уходит\n"
    "Google. Включается в plugin-config.toml: [sum] include_media = true"
)


class Plugin(BasePlugin):
    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.router: ModelRouter | None = None

    # -- lifecycle --------------------------------------------------------

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        names = tuple(
            part.strip()
            for part in ctx.config.str_value("api_key_env", "TGUSERBOT_GEMINI_API_KEY").split(",")
            if part.strip()
        )
        self.router = ModelRouter(
            _chain(ctx),
            base_url=ctx.config.str_value("base_url", DEFAULT_BASE_URL),
            # Longer than the ai plugin's default: a summary carries a transcript
            # and, when allowed, media, and a request that times out halfway is
            # charged for and produces nothing.
            timeout=ctx.config.int_value("timeout_seconds", 180),
            key_getter=lambda: keys_from_env(*names),
        )
        ctx.register_command(
            "sum",
            self.handle,
            help_text=USAGE,
            aliases=("summary", "сводка"),
        )
        if not self.router.has_key:
            LOGGER.warning("sum: no API key; /ub sum will say which variable is missing")
        if not ctx.config.bool_value("include_media", False):
            LOGGER.info("sum: media attachments are off (set [sum] include_media = true)")

    async def stop(self) -> None:
        if self.router is not None:
            await self.router.aclose()
            self.router = None

    def require_router(self) -> ModelRouter:
        if self.router is None or not self.router.has_key:
            raise MissingKeyError(
                "Ключ Gemini не задан. Добавьте TGUSERBOT_GEMINI_API_KEY в "
                "/etc/tguserbot/userbot.env и перезапустите сервис."
            )
        return self.router

    # -- argument parsing --------------------------------------------------

    def parse_args(self, ctx: PluginContext, text: str) -> tuple[int, bool] | None:
        """Read ``[count] [media|text]``. ``None`` means the usage should be sent.

        A count that is not a number is a usage error rather than something to
        guess at: silently summarising 10 when someone asked for 100 is the kind
        of quiet wrong answer this plugin tries to avoid.
        """
        default = ctx.config.int_value("message_count", 10)
        ceiling = ctx.config.int_value("max_message_count", 50)
        want_media = ctx.config.bool_value("include_media", False)
        count = default
        for word in text.split():
            lowered = word.lower()
            if lowered in ("media", "медиа"):
                want_media = True
            elif lowered in ("text", "текст", "no-media"):
                want_media = False
            elif lowered.isdigit():
                count = int(word)
            else:
                return None
        return max(1, min(count, ceiling)), want_media

    # -- command ------------------------------------------------------------

    async def handle(self, command: Any) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        text = (command.args or "").strip()
        parsed = self.parse_args(ctx, text)
        if parsed is None:
            await command.respond(USAGE)
            return
        count, want_media = parsed
        try:
            async with ctx.rate_limiter.slot("sum"):
                collected = await self.collect(ctx, command.event, count, want_media)
                if collected.message_count == 0:
                    await command.respond("В этом чате нечего summarировать: сообщений не нашлось.")
                    return
                reply = await self.summarise(ctx, collected)
                await command.respond(self.compose(collected, reply))
                notice = self.router.notice() if self.router is not None else ""
                if notice:
                    await command.respond(notice)
        except (GeminiError, MissingKeyError) as exc:
            # The reason goes to the log as well as to the chat: see the note in
            # the ai plugin. Without it a refusal is only visible to whoever was
            # in the chat, and the journal shows a clean run.
            LOGGER.warning("sum: Gemini refused after reading %d messages: %s", count, exc)
            await command.respond(f"Gemini: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("sum failed")
            await command.respond(f"Ошибка: {exc}")

    # -- reading the chat ----------------------------------------------------

    async def collect(
        self, ctx: PluginContext, event: Any, count: int, want_media: bool
    ) -> Collected:
        """Read the recent messages and, if allowed, download their attachments."""
        client = ctx.client
        entity = await client.get_input_entity(event.chat_id)
        own_id = getattr(event, "id", None)

        entries: list[tuple[Any, str]] = []
        downloadable: list[Any] = []
        async for message in client.iter_messages(entity, limit=count + 1):
            if own_id is not None and getattr(message, "id", None) == own_id:
                # The command itself is not part of what was said.
                continue
            if getattr(message, "action", None) is not None:
                continue  # "X joined", pinned notices: not conversation
            text = (getattr(message, "message", None) or "").strip()
            kind = classify(message)
            if not text and kind is None:
                continue
            if kind is not None:
                downloadable.append(message)
            entries.append((message, text or f"({kind})"))
            if len(entries) >= count:
                break

        entries.reverse()  # iter_messages is newest-first
        transcript, truncated = build_transcript(
            entries, ctx.config.int_value("max_transcript_chars", 24000)
        )
        collected = Collected(
            transcript=transcript, message_count=len(entries), truncated_transcript=truncated
        )
        if not want_media:
            # Say so once, rather than leaving the user to wonder whether a voice
            # message was simply missed.
            if downloadable:
                collected.notes.append(
                    f"медиа не отправляются (include_media = false), "
                    f"вложений пропущено: {len(downloadable)}"
                )
            return collected

        collected.media, media_skipped = await self.download(ctx, downloadable)
        collected.skipped.extend(media_skipped)
        return collected

    async def download(
        self, ctx: PluginContext, messages: list[Any]
    ) -> tuple[list[MediaItem], list[SkippedMedia]]:
        """Fetch attachments newest-first, within the configured budget.

        Newest-first because that is the order the budget is spent in, and the
        recent attachments are the ones the summary is usually about.
        """
        per_file = ctx.config.int_value("max_media_bytes", 4 * 1024 * 1024)
        total = ctx.config.int_value("max_total_media_bytes", 15 * 1024 * 1024)
        ordered = list(reversed(messages))
        collected = Collected()
        skipped: list[SkippedMedia] = []
        with tempfile.TemporaryDirectory(prefix="sum-") as directory:
            for index, message in enumerate(ordered):
                kind = classify(message)
                if kind is None:
                    continue
                mime = mime_for(message, kind)
                if mime is None:
                    skipped.append(
                        SkippedMedia(
                            getattr(message, "id", 0),
                            f"{kind}: не удалось определить формат",
                        )
                    )
                    continue
                target = Path(directory) / f"{index}{Path(str(_name_of(message))).suffix}"
                try:
                    # telethon's download_media is a coroutine function. Running
                    # it in a thread and awaiting the result handed a coroutine
                    # object to Path(), which blew up on every attachment.
                    got = await ctx.client.download_media(message, file=str(target))
                except Exception as exc:
                    LOGGER.debug("could not download message %s: %s", index, exc)
                    skipped.append(SkippedMedia(getattr(message, "id", 0), f"{kind}: не скачалось"))
                    continue
                path = Path(got) if got else target
                if not path.is_file():
                    skipped.append(SkippedMedia(getattr(message, "id", 0), f"{kind}: не скачалось"))
                    continue
                take_media(
                    collected,
                    [
                        (
                            message,
                            MediaItem(
                                getattr(message, "id", 0), kind, mime, path, path.stat().st_size
                            ),
                        )
                    ],
                    max_file_bytes=per_file,
                    max_total_bytes=total,
                )
        return collected.media, skipped + collected.skipped

    # -- asking --------------------------------------------------------------

    async def summarise(self, ctx: PluginContext, collected: Collected) -> str:
        router = self.require_router()
        payload = await asyncio.to_thread(read_media, collected.media)
        turns = [
            Turn(
                "user",
                build_request_parts(collected.transcript, payload, collected.notices()),
            )
        ]
        reply = await router.generate(
            turns,
            system=SUMMARY_SYSTEM,
            max_output_tokens=ctx.config.int_value("max_output_tokens", 2046),
        )
        return reply.text

    @staticmethod
    def compose(collected: Collected, summary: str) -> str:
        """The reply, with the caveats attached rather than swallowed."""
        header = f"Сводка по последним {collected.message_count} сообщениям:"
        body = summary.strip() or "(модель не вернула текст)"
        notices = collected.notices()
        if not notices:
            return f"{header}\n\n{body}"
        lines = [header, "", body, "", "Не учтено:"]
        lines += [f"• {note}" for note in notices]
        return "\n".join(lines)


def _chain(ctx: PluginContext) -> list[str]:
    """The models to try, in order. The configured one first, then the chain."""
    chain = [ctx.config.str_value("chat_model", "gemini-3.8-flash")]
    for name in ctx.config.str_list("model_fallbacks", ()):
        if name not in chain:
            chain.append(name)
    return chain


def _name_of(message: Any) -> str:
    document = getattr(message, "file", None)
    name = getattr(document, "name", None) if document is not None else None
    return name if isinstance(name, str) else "attachment"


def read_media(items: list[MediaItem]) -> list[Part]:
    """Read attachments into inline parts.

    Off the event loop deliberately: these are blocking file reads, and a few
    megabytes of voice notes would otherwise stall every other plugin.
    """
    import base64

    parts: list[Part] = []
    for item in items:
        try:
            data = item.path.read_bytes()
        except OSError:
            continue
        parts.append(Part.media(item.mime, base64.b64encode(data).decode("ascii")))
    return parts
