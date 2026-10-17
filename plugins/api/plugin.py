"""Free OpenAI-compatible APIs from the @zk_ichinokata channel.

The channel publishes one offer per post: a base URL, a key, and the channel's
own probe of which models the key can reach. This plugin turns that feed into
a working assistant:

* posts are parsed as they arrive, and the last N are read on first start and
  on ``/ub api refresh``;
* every new key is validated with one ``/models`` call and one one-token chat
  request -- the only *active* probe, because deep-probing media models would
  spend the free quota this plugin exists to spend on questions;
* questions route through an autopilot that prefers what the operator pinned,
  then provider priority, then what was actually verified; failures earn
  cooldowns and the next candidate answers;
* voice notes transcribe, videos summarise (speech track + sampled frames,
  merged), photos answer questions, and ``/ub api img`` generates images.

Media is on by default here, unlike the ``sum`` plugin, and the first media
reply says once where the file went: these are third-party endpoints, and that
is a fact the user should be told rather than protected from.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
from collections.abc import Callable, Coroutine
from functools import partial
from pathlib import Path
from shutil import rmtree
from typing import Any, cast

import httpx
from telethon import events
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import InputChannel, InputPeerChannel

from userbot.commands import CommandContext
from userbot.messaging import delete_message, edit_message
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext

from ._catalog import (
    AUTH_COOLDOWN_SECONDS,
    MODEL_DENIED_SECONDS,
    MODEL_MISSING_SECONDS,
    RATE_COOLDOWN_SECONDS,
    RETRY_INLINE_SECONDS,
    SERVER_COOLDOWN_SECONDS,
    Candidate,
    Catalog,
    capabilities,
    now_iso,
)
from ._channel import parse_post
from ._media import (
    MediaError,
    download,
    extract_audio,
    extract_frames,
    file_mime,
    has_ffmpeg,
    media_kind,
)
from ._openai import (
    ApiError,
    chat_once,
    chat_stream,
    generate_image,
    list_models,
    transcribe,
)
from ._prompts import (
    CHAT_SYSTEM,
    FRAME_PROMPT,
    PRIVACY_NOTICE,
    VIDEO_MERGE_SYSTEM,
    video_merge_message,
)

LOGGER = logging.getLogger(__name__)

USAGE = (
    "Использование:\n"
    "/ub api <вопрос> — спросить свободную модель из канала\n"
    "/ub api img <промпт> — сгенерировать изображение\n"
    "/ub api (ответом) — расшифровка голосового, пересказ видео, разбор фото\n"
    "/ub api models [фильтр] — что есть в каталоге\n"
    "/ub api keys — состояние ключей по провайдерам\n"
    "/ub api use <модель|провайдер> — закрепить выбор (use auto — вернуть автопилот)\n"
    "/ub api refresh [N] — перечитать последние посты канала\n"
    "/ub api test <модель> — живой тест модели\n"
    "/ub api reset — очистить историю диалога"
)

#: Sub-commands, checked before the text is treated as a question.
SUBCOMMANDS = frozenset({"models", "keys", "use", "refresh", "test", "img", "reset"})

#: How many candidates one chat request may burn before giving up. Eight is
#: enough to cross a handful of dead keys; more would turn a working day into
#: a silent queue of requests against a broken afternoon.
MAX_CHAT_ATTEMPTS = 8

#: Fewer for the media passes: each is a heavier request on a free key.
MAX_MEDIA_ATTEMPTS = 5

#: Progress edits, throttled. The lesson from the ai plugin applies here as it
#: does everywhere Telegram counts edits.
MIN_EDIT_INTERVAL = 0.5
STREAMING_MARK = "…"


class _ProgressEditor:
    """Edits a message at most once per interval, and flushes the last text."""

    def __init__(self, event: Any, interval: float, logger: logging.Logger) -> None:
        self._event = event
        self._interval = max(MIN_EDIT_INTERVAL, interval)
        self._logger = logger
        self._pending: str | None = None
        self._stop = asyncio.Event()
        self._broken = False

    def offer(self, text: str | None) -> None:
        self._pending = text

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            if self._stop.is_set():
                break
            if self._pending is not None:
                await self._flush()

    async def _flush(self) -> None:
        text = self._pending
        self._pending = None
        if text is None or self._broken:
            return
        if not await edit_message(self._event, text):
            self._broken = True
            self._stop.set()
            self._logger.warning("api: cannot edit the progress message; final answers still send")

    async def finish(self) -> None:
        self._stop.set()
        if self._pending is not None:
            await self._flush()


class Plugin(BasePlugin):
    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.catalog: Catalog | None = None
        self.http: httpx.AsyncClient | None = None
        self._channel = ""
        self._entity: Any = None
        self._validate_sem: asyncio.Semaphore | None = None

    # -- lifecycle ---------------------------------------------------------

    async def migrate(self, storage: Any) -> None:
        await Catalog(storage).ensure()

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.catalog = Catalog(ctx.storage)
        await self.catalog.ensure()
        self.http = httpx.AsyncClient(follow_redirects=True)
        concurrency = max(1, ctx.config.int_value("validation_concurrency", 3))
        self._validate_sem = asyncio.Semaphore(concurrency)
        self._channel = ctx.config.str_value("channel", "zk_ichinokata").strip().lstrip("@")
        ctx.register_command("api", self._command, help_text=USAGE)
        self._log.info(
            "api: канал %s, медиа %s", self._channel, "вкл" if self._media_enabled() else "выкл"
        )
        ctx.spawn(self._bootstrap(), name="api:bootstrap")

    async def stop(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    def _media_enabled(self) -> bool:
        return bool(self.ctx and self.ctx.config.bool_value("media_enabled", True))

    @property
    def _log(self) -> logging.Logger:
        """The namespaced logger the manager configured for this plugin.

        The module-level logger resolves to this module's import name, which
        has no handler attached in production -- every INFO line was silently
        dropped until validation failures showed up through the task registry
        instead. Messages must go through ``ctx.logger``
        (``userbot.plugin.api``), which is wired to the journal and the file.
        """
        if self.ctx is not None:
            return self.ctx.logger
        return LOGGER

    # -- channel -----------------------------------------------------------

    async def _bootstrap(self) -> None:
        """Resolve the channel, join it, listen, and read the backlog once.

        Kept off the setup path so a slow or unreachable Telegram does not turn
        into a plugin that fails to load; every failure here degrades to
        ``/ub api refresh`` and is said in the journal.
        """
        ctx = self.ctx
        if ctx is None:
            return
        delay = 15.0
        for _ in range(20):
            try:
                self._entity = await ctx.client.get_input_entity(self._channel)
                break
            except Exception as exc:
                self._log.warning("api: канал %s пока не резолвится: %s", self._channel, exc)
                self._entity = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300.0)
        if self._entity is None:
            self._log.error(
                "api: %s недоступен; команды работают, свежие посты подтянет /ub api refresh",
                self._channel,
            )
            return
        if ctx.config.bool_value("join_on_setup", True):
            await self._ensure_member(self._entity)
        ctx.register_handler(self._on_channel_post, events.NewMessage(chats=[self._entity]))
        self._log.info("api: слушаю канал %s", self._channel)
        if self.catalog is None or await self.catalog.get_meta("cursor") is not None:
            await self._validate_unchecked()
            return
        limit = ctx.config.int_value("initial_scan", 100)
        try:
            posts, created = await self._scan(self._entity, limit=limit)
        except Exception as exc:
            self._log.warning("api: первичный скан канала не удался: %s", exc)
            await self._validate_unchecked()
            return
        self._log.info("api: первичный скан: %s постов, %s новых ключей", posts, created)
        await self._validate_unchecked()

    async def _validate_unchecked(self) -> int:
        """Queue validation for every key whose check never finished."""
        if self.catalog is None or self.ctx is None:
            return 0
        ids = await self.catalog.unchecked_endpoint_ids()
        for endpoint_id in ids:
            self.ctx.spawn(self._validate_endpoint(endpoint_id), name="api:validate")
        if ids:
            self._log.info("api: ставлю на проверку %s ключей", len(ids))
        return len(ids)

    async def _ensure_member(self, entity: Any) -> None:
        client = cast(Any, self.ctx.client if self.ctx else None)
        if isinstance(entity, InputPeerChannel):
            channel: Any = InputChannel(
                channel_id=entity.channel_id, access_hash=entity.access_hash
            )
        elif isinstance(entity, InputChannel):
            channel = entity
        else:
            self._log.warning("api: %r не похож на канал; реалтайм не включаю", entity)
            return
        try:
            await client(JoinChannelRequest(channel))
            self._log.info("api: подписался на канал")
        except UserAlreadyParticipantError:
            pass
        except Exception as exc:
            self._log.warning(
                "api: не удалось подписаться (%s); реалтайм недоступен, остаётся /ub api refresh",
                exc,
            )

    async def _resolve_channel(self) -> Any:
        if self._entity is None:
            if self.ctx is None:
                raise RuntimeError("плагин не запущен")
            self._entity = await self.ctx.client.get_input_entity(self._channel)
        return self._entity

    async def _on_channel_post(self, event: Any) -> None:
        message = getattr(event, "message", None)
        text = getattr(message, "raw_text", None) or getattr(message, "message", None)
        if not isinstance(text, str) or not text:
            return
        await self._ingest(text, post_id=int(getattr(message, "id", 0) or 0))

    async def _ingest(self, text: str, *, post_id: int) -> None:
        """One channel message: store the offer, validate it if it is new."""
        if self.ctx is None or self.catalog is None:
            return
        parsed = parse_post(text)
        if parsed is None:
            return
        endpoint_id, created = await self.catalog.upsert_post(parsed, post_id=post_id or None)
        await self.catalog.advance_cursor(post_id)
        if created:
            self.ctx.spawn(self._validate_endpoint(endpoint_id), name="api:validate")

    async def _scan(self, entity: Any, *, limit: int) -> tuple[int, int]:
        """Read the last ``limit`` posts, oldest first. Returns (posts, new keys)."""
        if self.ctx is None or self.catalog is None:
            return 0, 0
        messages = [message async for message in self.ctx.client.iter_messages(entity, limit=limit)]
        messages.reverse()
        posts = 0
        created = 0
        for message in messages:
            text = getattr(message, "raw_text", None) or getattr(message, "message", None)
            if not isinstance(text, str) or not text:
                continue
            parsed = parse_post(text)
            if parsed is None:
                continue
            posts += 1
            message_id = int(getattr(message, "id", 0) or 0)
            endpoint_id, is_new = await self.catalog.upsert_post(parsed, post_id=message_id or None)
            await self.catalog.advance_cursor(message_id)
            if is_new:
                created += 1
                self.ctx.spawn(self._validate_endpoint(endpoint_id), name="api:validate")
        return posts, created

    # -- validation --------------------------------------------------------

    async def _validate_endpoint(self, endpoint_id: int) -> None:
        """One active probe per key: the /models list, then a one-token ping."""
        if self.ctx is None or self.catalog is None or self.http is None:
            return
        endpoint = await self.catalog.endpoint(endpoint_id)
        if endpoint is None:
            return
        semaphore = self._validate_sem
        if semaphore is None:
            return
        async with semaphore:
            if await self.catalog.is_checked(endpoint_id):
                return
            models: list[str] | None = None
            try:
                models = await list_models(
                    self.http, endpoint.base_url, endpoint.api_key, request_timeout=25
                )
            except ApiError as exc:
                self._log.info("api: /models у %s не ответил: %s", endpoint.provider, exc)
                if exc.kind == "auth":
                    await self.catalog.cooldown_endpoint(endpoint_id, AUTH_COOLDOWN_SECONDS)
                    await self.catalog.mark_checked(endpoint_id, alive=False, api_models=None)
                    return
            ping_models = await self.catalog.endpoint_models(endpoint_id, role="chat", limit=3)
            if not ping_models and models:
                ping_models = [name for name in models if "chat" in capabilities(name)][:3]
            alive = False
            if not ping_models:
                # Reachable but nothing to ask: a key for embeddings, or a
                # provider whose /models succeeded and lists no chat model.
                alive = models is not None
            for name in ping_models:
                try:
                    await chat_once(
                        self.http,
                        endpoint.base_url,
                        endpoint.api_key,
                        name,
                        [{"role": "user", "content": "ping"}],
                        max_tokens=4,
                        request_timeout=40,
                    )
                    alive = True
                    break
                except ApiError as exc:
                    if exc.kind == "auth":
                        await self.catalog.cooldown_endpoint(endpoint_id, AUTH_COOLDOWN_SECONDS)
                        break
                    if exc.kind == "denied":
                        await self.catalog.cooldown_model(endpoint_id, name, MODEL_DENIED_SECONDS)
                    continue
            await self.catalog.mark_checked(endpoint_id, alive=alive, api_models=models)
            self._log.info(
                "api: проверка %s: жив=%s, моделей от API: %s",
                endpoint.provider,
                alive,
                "нет" if models is None else len(models),
            )

    # -- command entry -----------------------------------------------------

    async def _command(self, command: CommandContext) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        text = (command.args or "").strip()
        head, _, rest = text.partition(" ")
        head_l, rest = head.strip().lower(), rest.strip()
        try:
            # One slot for the plugin, like the ai plugin: a second question
            # queues behind the first instead of racing the same conversation.
            async with ctx.rate_limiter.slot("api"):
                if head_l in SUBCOMMANDS:
                    await self._subcommand(command, head_l, rest)
                    return
                media = await self._replied_media(command)
                if media is not None:
                    await self._media_flow(command, media[0], media[1], text)
                elif text:
                    await self._chat(command, text)
                else:
                    await command.respond(USAGE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.exception("api: команда упала")
            await command.respond(f"Ошибка: {exc}")

    async def _subcommand(self, command: CommandContext, head: str, rest: str) -> None:
        if head == "reset":
            await self._reset(command)
        elif head == "models":
            await self._models(command, rest)
        elif head == "keys":
            await self._keys(command)
        elif head == "use":
            await self._use(command, rest)
        elif head == "refresh":
            await self._refresh(command, rest)
        elif head == "test":
            await self._test(command, rest)
        elif head == "img":
            await self._img(command, rest)

    async def _replied_media(self, command: CommandContext) -> tuple[Any, str] | None:
        event = getattr(command, "event", None)
        message = getattr(event, "message", None)
        if message is None:
            return None
        try:
            reply = await message.get_reply_message()
        except Exception:
            return None
        if reply is None:
            return None
        kind = media_kind(reply)
        if kind is None:
            return None
        return reply, kind

    # -- history -----------------------------------------------------------

    async def _history(self) -> list[dict[str, Any]]:
        ctx = self.ctx
        if ctx is None:
            return []
        rows = await ctx.storage.fetchall(
            "SELECT role, content FROM history ORDER BY id DESC LIMIT 200"
        )
        limit = max(1, ctx.config.int_value("max_history_turns", 12))
        rows = rows[: 2 * limit]
        rows.reverse()
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in rows
            if str(row["content"]).strip()
        ]

    async def _remember(self, role: str, content: str) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        await ctx.storage.execute(
            "INSERT INTO history (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, now_iso()),
        )
        # A ceiling, not a feature: without it the file grows forever and the
        # only way to notice would be the disk.
        await ctx.storage.execute(
            "DELETE FROM history WHERE id <= (SELECT MAX(id) FROM history) - 400"
        )

    async def _forget_question(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        try:
            await ctx.storage.execute(
                "DELETE FROM history WHERE id = ("
                "  SELECT id FROM history WHERE role = 'user' ORDER BY id DESC LIMIT 1"
                ")"
            )
        except Exception:
            self._log.warning("api: не удалось убрать неотвеченный вопрос из истории")

    async def _reset(self, command: CommandContext) -> None:
        if self.ctx is None:
            return
        await self.ctx.storage.execute("DELETE FROM history")
        await command.respond("История диалога очищена.")

    # -- candidate plumbing ------------------------------------------------

    async def _role_candidates(self, role: str) -> list[Candidate]:
        ctx = self.ctx
        if ctx is None or self.catalog is None:
            return []
        priority = ctx.config.str_list(
            "provider_priority", ("groq", "nvidia", "mistral", "minimax", "agnes")
        )
        pinned = (await self.catalog.get_meta("pinned_model")) or ""
        return await self.catalog.candidates(role, provider_priority=priority, pinned=pinned)

    async def _penalize(self, candidate: Candidate, exc: ApiError) -> None:
        if self.catalog is None:
            return
        if exc.kind == "auth":
            await self.catalog.cooldown_endpoint(candidate.endpoint_id, AUTH_COOLDOWN_SECONDS)
        elif exc.kind == "denied":
            await self.catalog.cooldown_model(
                candidate.endpoint_id, candidate.model, MODEL_DENIED_SECONDS
            )
        elif exc.kind == "not_found":
            await self.catalog.cooldown_model(
                candidate.endpoint_id, candidate.model, MODEL_MISSING_SECONDS
            )
        elif exc.kind == "rate":
            await self.catalog.cooldown_model(
                candidate.endpoint_id,
                candidate.model,
                max(exc.retry_after or RATE_COOLDOWN_SECONDS, RATE_COOLDOWN_SECONDS),
            )
        elif exc.kind in ("server", "network"):
            await self.catalog.cooldown_endpoint(candidate.endpoint_id, SERVER_COOLDOWN_SECONDS)
        # bad_request is not remembered: the shape was wrong for this model,
        # and the next candidate deserves the request.
        self._log.info("api: %s (%s) — %s", candidate.model, candidate.provider, exc)

    async def _try_role(
        self,
        role: str,
        call: Callable[[Candidate], Coroutine[Any, Any, Any]],
        *,
        attempts: int = MAX_MEDIA_ATTEMPTS,
    ) -> tuple[Any, Candidate]:
        candidates = await self._role_candidates(role)
        if not candidates:
            raise ApiError(
                "network", f"нет доступных моделей для роли {role}; сделай /ub api refresh"
            )
        last: ApiError | None = None
        for candidate in candidates[:attempts]:
            try:
                result = await call(candidate)
            except ApiError as exc:
                last = exc
                await self._penalize(candidate, exc)
                continue
            if self.catalog is not None:
                await self.catalog.record_success(candidate.endpoint_id, candidate.model)
            return result, candidate
        raise last or ApiError("network", f"для роли {role} нет живых моделей")

    def _timeout(self) -> float:
        if self.ctx is None:
            return 180.0
        return float(self.ctx.config.int_value("timeout_seconds", 180))

    # -- chat --------------------------------------------------------------

    async def _chat(self, command: CommandContext, question: str) -> None:
        ctx = self.ctx
        http = self.http
        if ctx is None or http is None:
            return
        candidates = await self._role_candidates("chat")
        if not candidates:
            await command.respond("Каталог пуст или все ключи в кулдауне. Сделай /ub api refresh.")
            return
        await self._remember("user", question)
        messages = [{"role": "system", "content": CHAT_SYSTEM}] + await self._history()
        first = candidates[0]

        placeholder = await command.event.respond(STREAMING_MARK)
        editor = _ProgressEditor(
            placeholder, ctx.config.float_value("min_edit_interval", 3.0), ctx.logger
        )
        worker = asyncio.create_task(editor.run())
        content: list[str] = []
        reasoning: list[str] = []
        answered: Candidate | None = None
        last_error: str | None = None
        attempted = 0
        try:
            for candidate in candidates:
                if attempted >= MAX_CHAT_ATTEMPTS or content:
                    break
                attempted += 1
                retried = False
                while True:
                    try:
                        async for kind, delta in chat_stream(
                            http,
                            candidate.base_url,
                            candidate.api_key,
                            candidate.model,
                            messages,
                            request_timeout=self._timeout(),
                        ):
                            if kind == "content":
                                content.append(delta)
                                editor.offer("".join(content))
                            else:
                                reasoning.append(delta)
                    except ApiError as exc:
                        last_error = f"{candidate.model} ({candidate.provider}): {exc}"
                        await self._penalize(candidate, exc)
                        if content:
                            break
                        short_rate = (
                            exc.kind == "rate"
                            and not retried
                            and exc.retry_after is not None
                            and exc.retry_after <= RETRY_INLINE_SECONDS
                        )
                        if short_rate:
                            retried = True
                            await asyncio.sleep(exc.retry_after or 0)
                            continue
                        break
                    if content or reasoning:
                        answered = candidate
                    break
                if answered is not None or content:
                    break
            await editor.finish()
        except asyncio.CancelledError:
            editor.offer(None)
            await delete_message(placeholder)
            await self._forget_question()
            raise
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        answer = "".join(content).strip() or "".join(reasoning).strip()
        if not answer:
            detail = f" Последняя ошибка: {last_error}" if last_error else ""
            await command.respond(f"Ответа нет ({attempted} попыток).{detail}")
            await self._forget_question()
            await delete_message(placeholder)
            return
        if answered is None and last_error:
            answer += f"\n\n_(поток оборвался: {last_error})_"
        elif answered is not None and self.catalog is not None:
            await self.catalog.record_success(answered.endpoint_id, answered.model)
        await command.respond(answer)
        await self._remember("assistant", "".join(content).strip() or answer)
        if answered is not None and (answered.endpoint_id, answered.model) != (
            first.endpoint_id,
            first.model,
        ):
            await command.respond(f"Ответила {answered.model} ({answered.provider}).")
        await delete_message(placeholder)

    # -- media -------------------------------------------------------------

    async def _media_flow(
        self, command: CommandContext, message: Any, kind: str, text: str
    ) -> None:
        if not self._media_enabled():
            await command.respond(
                "Медиа выключено. Включи [api] media_enabled в plugin-config.toml."
            )
            return
        if kind in ("voice", "audio"):
            await self._transcribe_flow(command, message, text)
        elif kind in ("video", "video_note"):
            await self._video_flow(command, message, text)
        elif kind == "photo":
            await self._photo_flow(command, message, text)

    async def _media_footer(self) -> str:
        if self.ctx is None:
            return ""
        if await self.ctx.storage.get_value("media_notice_shown"):
            return ""
        await self.ctx.storage.set_value("media_notice_shown", "1")
        return PRIVACY_NOTICE

    async def _download_or_report(
        self, command: CommandContext, message: Any, limit: int
    ) -> bytes | None:
        ctx = self.ctx
        if ctx is None:
            return None
        try:
            return await download(ctx.client, message, max_bytes=limit)
        except MediaError as exc:
            await command.respond(f"Не получилось скачать: {exc}")
            return None

    async def _transcribe_flow(self, command: CommandContext, message: Any, text: str) -> None:
        ctx = self.ctx
        http = self.http
        if ctx is None or http is None:
            return
        limit = ctx.config.int_value("audio_max_bytes", 20 * 1024 * 1024)
        placeholder = await command.event.respond("🎙 Расшифровываю…")
        data = await self._download_or_report(command, message, limit)
        if data is None:
            await delete_message(placeholder)
            return
        mime = file_mime(message) or "audio/ogg"
        filename = "voice.ogg" if mime.endswith("ogg") else "audio.bin"
        try:
            transcript, candidate = await self._try_role(
                "stt",
                lambda c: transcribe(
                    http,
                    c.base_url,
                    c.api_key,
                    c.model,
                    filename=filename,
                    data=data,
                    mime=mime,
                    request_timeout=self._timeout(),
                ),
            )
        except ApiError as exc:
            await command.respond(f"Расшифровка недоступна: {exc}")
            await delete_message(placeholder)
            return
        if text:
            # A question about the recording goes through the normal chat,
            # with the transcript attached to the question.
            await delete_message(placeholder)
            await self._chat(command, f"{text}\n\n[Расшифровка голосового]\n{transcript}")
            return
        footer = await self._media_footer()
        await command.respond(
            f"🎙 Расшифровка ({candidate.model}, {candidate.provider}):\n{transcript}{footer}"
        )
        await delete_message(placeholder)

    async def _photo_flow(self, command: CommandContext, message: Any, question: str) -> None:
        ctx = self.ctx
        http = self.http
        if ctx is None or http is None:
            return
        limit = ctx.config.int_value("image_max_bytes", 10 * 1024 * 1024)
        placeholder = await command.event.respond("👁 Смотрю изображение…")
        data = await self._download_or_report(command, message, limit)
        if data is None:
            await delete_message(placeholder)
            return
        mime = file_mime(message) or "image/jpeg"
        payload = base64.b64encode(data).decode("ascii")
        content: list[dict[str, Any]] = [
            {"type": "text", "text": question or FRAME_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload}"}},
        ]
        try:
            answer, candidate = await self._try_role(
                "vision",
                lambda c: chat_once(
                    http,
                    c.base_url,
                    c.api_key,
                    c.model,
                    [{"role": "user", "content": content}],
                    request_timeout=self._timeout(),
                ),
            )
        except ApiError as exc:
            await command.respond(f"Не получилось посмотреть: {exc}")
            await delete_message(placeholder)
            return
        footer = await self._media_footer()
        await command.respond(f"👁 {answer}\n\n_({candidate.model}, {candidate.provider})_{footer}")
        await delete_message(placeholder)

    async def _video_flow(self, command: CommandContext, message: Any, focus: str) -> None:
        ctx = self.ctx
        http = self.http
        if ctx is None or http is None:
            return
        if not has_ffmpeg():
            await command.respond("ffmpeg не установлен — пересказ видео недоступен.")
            return
        limit = ctx.config.int_value("video_max_bytes", 50 * 1024 * 1024)
        placeholder = await command.event.respond("🎬 Разбираю видео: кадры и звук…")
        data = await self._download_or_report(command, message, limit)
        if data is None:
            await delete_message(placeholder)
            return
        workdir = Path(tempfile.mkdtemp(prefix="api-video-"))
        try:
            source = workdir / "video.bin"
            source.write_bytes(data)
            loop = asyncio.get_running_loop()
            frames = await loop.run_in_executor(
                None,
                partial(
                    extract_frames,
                    source,
                    workdir,
                    count=ctx.config.int_value("video_frames", 6),
                    width=ctx.config.int_value("frame_width", 512),
                ),
            )
            audio = await loop.run_in_executor(None, extract_audio, source, workdir)

            stages: list[str] = []
            transcript: str | None = None
            if audio is not None:
                audio_bytes = audio.read_bytes()
                try:
                    transcript, stt_candidate = await self._try_role(
                        "stt",
                        lambda c: transcribe(
                            http,
                            c.base_url,
                            c.api_key,
                            c.model,
                            filename="audio.mp3",
                            data=audio_bytes,
                            mime="audio/mpeg",
                            request_timeout=self._timeout(),
                        ),
                    )
                    if transcript:
                        stages.append(f"🎙 {stt_candidate.model} ({stt_candidate.provider})")
                    else:
                        transcript = None
                except ApiError as exc:
                    self._log.info("api: STT для видео недоступен: %s", exc)
            frame_notes: list[tuple[float, str]] = []
            vision_used: str | None = None
            for moment, jpg in frames:
                payload = base64.b64encode(jpg).decode("ascii")
                content: list[dict[str, Any]] = [
                    {"type": "text", "text": FRAME_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{payload}"},
                    },
                ]

                async def ask_vision(
                    candidate: Candidate, content: list[dict[str, Any]] = content
                ) -> str:
                    return await chat_once(
                        http,
                        candidate.base_url,
                        candidate.api_key,
                        candidate.model,
                        [{"role": "user", "content": content}],
                        request_timeout=self._timeout(),
                    )

                try:
                    description, vision_candidate = await self._try_role("vision", ask_vision)
                except ApiError as exc:
                    self._log.info("api: vision для видео недоступен: %s", exc)
                    break
                if description and description.strip():
                    frame_notes.append((moment, description.strip()))
                    vision_used = f"{vision_candidate.model} ({vision_candidate.provider})"
            if vision_used:
                stages.append(f"👁 {vision_used}")

            if not transcript and not frame_notes:
                await command.respond(
                    "Не удалось собрать данные о видео: ни речь, ни кадры не распознали."
                )
                await delete_message(placeholder)
                return
            merge = video_merge_message(
                transcript=transcript, frame_notes=frame_notes, focus=focus or None
            )
            try:
                summary, chat_candidate = await self._try_role(
                    "chat",
                    lambda c: chat_once(
                        http,
                        c.base_url,
                        c.api_key,
                        c.model,
                        [
                            {"role": "system", "content": VIDEO_MERGE_SYSTEM},
                            {"role": "user", "content": merge},
                        ],
                        request_timeout=self._timeout(),
                    ),
                )
            except ApiError as exc:
                fallback = transcript or "\n".join(
                    f"— {moment:.0f} c: {text}" for moment, text in frame_notes
                )
                await command.respond(
                    f"Пересказ не получился ({exc}); вот что удалось собрать:\n{fallback}"
                )
                await delete_message(placeholder)
                return
            stages.append(f"📝 {chat_candidate.model} ({chat_candidate.provider})")
            footer = await self._media_footer()
            await command.respond(f"🎬 {summary}\n\n_Источники: {' · '.join(stages)}_{footer}")
            await delete_message(placeholder)
        finally:
            rmtree(workdir, ignore_errors=True)

    async def _img(self, command: CommandContext, prompt: str) -> None:
        http = self.http
        if self.ctx is None or http is None:
            return
        if not prompt:
            await command.respond("Использование: /ub api img <промпт>")
            return
        placeholder = await command.event.respond("🖼 Генерирую изображение…")
        try:
            data, candidate = await self._try_role(
                "image",
                lambda c: generate_image(
                    http,
                    c.base_url,
                    c.api_key,
                    c.model,
                    prompt,
                    request_timeout=self._timeout() * 2,
                ),
            )
        except ApiError as exc:
            await command.respond(f"Не получилось сгенерировать: {exc}")
            await delete_message(placeholder)
            return
        suffix = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            path = Path(handle.name)
        try:
            await command.event.respond(
                file=str(path),
                caption=f"🖼 {candidate.model} ({candidate.provider})\n{prompt[:200]}",
            )
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        await delete_message(placeholder)

    # -- subcommands -------------------------------------------------------

    async def _models(self, command: CommandContext, rest: str) -> None:
        if self.ctx is None or self.catalog is None:
            return
        needle = rest.strip().lower()
        rows = await self.catalog.model_rows()
        endpoints = await self.catalog.endpoint_count()
        if not rows:
            await command.respond("Каталог пуст. Сделай /ub api refresh.")
            return
        aggregated: dict[tuple[str, str], dict[str, int]] = {}
        for row in rows:
            name = str(row["name"])
            provider = str(row["provider"])
            if needle and needle not in name.lower() and needle not in provider.lower():
                continue
            entry = aggregated.setdefault(
                (provider, name),
                {"total": 0, "alive": 0, "working": 0, "starred": 0, "cooling": 0},
            )
            entry["total"] += 1
            entry["alive"] += 1 if row["alive"] else 0
            entry["working"] += 1 if row["working"] else 0
            entry["starred"] += 1 if row["starred"] else 0
            entry["cooling"] += (
                1
                if isinstance(row["model_cooldown"], str) and row["model_cooldown"] > now_iso()
                else 0
            )
        if not aggregated:
            await command.respond(f"Ничего не нашлось по фильтру {rest!r}.")
            return
        by_provider: dict[str, list[tuple[str, dict[str, int]]]] = {}
        for (provider, name), entry in aggregated.items():
            by_provider.setdefault(provider, []).append((name, entry))
        lines = [f"Каталог: {endpoints} ключей, моделей {len(aggregated)}."]
        shown = 0
        hidden = 0
        for provider in sorted(by_provider):
            entries = sorted(by_provider[provider], key=lambda item: (-item[1]["alive"], item[0]))
            lines.append(f"— {provider} —")
            for name, entry in entries:
                if shown >= 60:
                    hidden += 1
                    continue
                shown += 1
                tags = " ".join(sorted(capabilities(name)))
                mark = "⭐" if entry["starred"] else ""
                work = "✅" if entry["working"] else "·"
                lines.append(
                    f"{work} {mark}{name} [{tags}] живых {entry['alive']}/{entry['total']}"
                )
        if hidden:
            lines.append(f"…и ещё {hidden} — сузь фильтром: /ub api models <текст>")
        await command.respond("\n".join(lines))

    async def _keys(self, command: CommandContext) -> None:
        if self.catalog is None:
            return
        rows = await self.catalog.keys_summary()
        if not rows:
            await command.respond("Каталог пуст. Сделай /ub api refresh.")
            return
        lines = [f"Ключи из канала @{self._channel}:"]
        for row in rows:
            lines.append(
                f"• {row['provider']}: ключей {row['total']}, "
                f"живых {row['alive_count'] or 0}, в кулдауне {row['cooling'] or 0}"
            )
        await command.respond("\n".join(lines))

    async def _use(self, command: CommandContext, rest: str) -> None:
        if self.ctx is None or self.catalog is None:
            return
        value = rest.strip()
        if not value:
            current = await self.catalog.get_meta("pinned_model")
            if current:
                await command.respond(f"Закреплено: {current}. Вернуть автопилот: /ub api use auto")
            else:
                await command.respond("Автопилот. Закрепить: /ub api use <модель|провайдер>")
            return
        if value.lower() == "auto":
            await self.catalog.set_meta("pinned_model", "")
            await command.respond("Автопилот включён.")
            return
        if await self.catalog.model_exists(value) or await self.catalog.provider_exists(
            value.lower()
        ):
            await self.catalog.set_meta("pinned_model", value)
            await command.respond(f"Закреплено: {value}. Вернуть автопилот: /ub api use auto")
            return
        await command.respond(f"{value!r} нет в каталоге. Посмотреть: /ub api models")

    async def _refresh(self, command: CommandContext, rest: str) -> None:
        if self.ctx is None:
            return
        limit = (
            int(rest) if rest.strip().isdigit() else self.ctx.config.int_value("initial_scan", 100)
        )
        placeholder = await command.event.respond(f"📡 Читаю последние {limit} постов канала…")
        try:
            entity = await self._resolve_channel()
            posts, created = await self._scan(entity, limit=limit)
        except Exception as exc:
            self._log.warning("api: refresh не удался: %s", exc)
            await command.respond(f"Не получилось прочитать канал: {exc}")
            await delete_message(placeholder)
            return
        await command.respond(
            f"Разобрано постов: {posts}, новых ключей: {created}. "
            "Проверка новых идёт в фоне — /ub api keys покажет итог."
        )
        await self._validate_unchecked()
        await delete_message(placeholder)

    async def _test(self, command: CommandContext, rest: str) -> None:
        http = self.http
        if self.ctx is None or self.catalog is None or http is None:
            return
        query = rest.strip()
        if not query:
            await command.respond("Использование: /ub api test <модель|часть имени>")
            return
        seen: dict[tuple[int, str], Candidate] = {}
        for role in ("chat", "vision", "stt", "image"):
            for candidate in await self._role_candidates(role):
                seen.setdefault((candidate.endpoint_id, candidate.model), candidate)
        needle = query.lower()
        matches = [candidate for candidate in seen.values() if needle in candidate.model.lower()]
        if not matches:
            await command.respond(
                f"Живых кандидатов по {query!r} нет. /ub api models покажет каталог."
            )
            return
        lines: list[str] = []
        for candidate in matches[:4]:
            started = time.monotonic()
            try:
                answer = await chat_once(
                    http,
                    candidate.base_url,
                    candidate.api_key,
                    candidate.model,
                    [{"role": "user", "content": "ping"}],
                    max_tokens=8,
                    request_timeout=60,
                )
            except ApiError as exc:
                await self._penalize(candidate, exc)
                lines.append(f"❌ {candidate.model} ({candidate.provider}) — {exc}")
                continue
            await self.catalog.record_success(candidate.endpoint_id, candidate.model)
            elapsed = time.monotonic() - started
            tail = f", ответ: {answer.strip()[:60]!r}" if answer.strip() else ", пустой ответ"
            lines.append(f"✅ {candidate.model} ({candidate.provider}) — {elapsed:.1f} c{tail}")
        await command.respond("\n".join(lines))


__all__ = ["Plugin"]
