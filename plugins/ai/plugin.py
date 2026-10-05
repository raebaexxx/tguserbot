"""Gemini chat, and on-demand plugin generation.

Two capabilities, deliberately separated by a step the model cannot take. Chat
writes nothing. Generation writes only into the staging directory, which is not a
plugin root, so nothing it produces can run until ``/ub plugin adopt`` is typed
by the owner -- and adoption runs a static review that refuses a shell, a
socket, or a handle on the session file.

The progress reporter is throttled, and that is a lesson rather than a
preference. The TikTok plugin shipped an edit-per-progress-callback reporter and
that was the cause of a rate-limit incident: Telegram counts message edits, so a
streaming answer is edited at most once every ``min_edit_interval`` seconds
however fast the tokens arrive.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from userbot.messaging import delete_message, edit_message
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext
from userbot.safety import format_findings, read_tree_sources, review_tree

from ._codegen import (
    PLUGIN_SCHEMA,
    GeneratedFileError,
    GeneratedPlugin,
    parse_generated,
    write_plugin,
)
from ._prompts import CHAT_SYSTEM, CODE_SYSTEM

LOGGER = logging.getLogger(__name__)

USAGE = (
    "Использование:\n"
    "/ub ai <вопрос> — спросить Gemini\n"
    "/ub ai pro <вопрос> — gemini-3.1-pro-preview (медленнее, не бесплатно)\n"
    "/ub ai fast <вопрос> — gemini-3.5-flash-lite (быстро и дёшево)\n"
    "/ub ai new <имя> <описание> — сгенерировать плагин\n"
    "/ub ai list — что ждёт в staging\n"
    "/ub ai show <имя> — файлы и отчёт безопасности\n"
    "/ub ai models — модели, доступные ключу\n"
    "/ub ai reset — забыть историю диалога"
)

#: Model roles, and the manifest key each one reads.
MODELS = {
    "chat": ("chat_model", "gemini-3.8-flash"),
    "code": ("code_model", "gemini-3.8-flash"),
    "fast": ("fast_model", "gemini-3.5-flash-lite"),
    "pro": ("pro_model", "gemini-3.1-pro-preview"),
}

#: Sub-commands that are not a question.
SUBCOMMANDS = frozenset({"list", "show", "new", "models", "reset"})

#: Prefixes that select a model rather than being part of the question.
MODES = frozenset({"pro", "fast"})

#: Prefix on the streaming message, so a half-written answer is not mistaken
#: for a finished one.
STREAMING_MARK = "…"

#: Telegram rejects edits faster than this; the real floor is much higher.
MIN_EDIT_INTERVAL = 0.5


#: How far a word may be from a mode name and still count as a typo for it.
#:
#: One, deliberately, not two. At two, ordinary question openers get flagged --
#: "list", "note", "code" and "more" are each two edits from "fast" or "pro" --
#: and a chat assistant that comments on "list my notes" is worse than one that
#: quietly answers it. A missed hint costs a retype; a wrong hint costs trust.
MODE_TYPO_DISTANCE = 1

#: Below this length a near-match is coincidence: "how" is two edits from "pro".
MODE_MIN_LENGTH = 3


def nearest_mode(word: str) -> str | None:
    """The mode ``word`` was probably meant to be, or ``None``.

    Used only to *add a note*, never to change the question: a false positive
    costs the user one extra line, whereas swallowing the word silently means
    they get an answer to a question they did not ask.
    """
    target = word.lower()
    if target in MODES:
        return target
    if len(target) < MODE_MIN_LENGTH:
        return None
    scored = sorted((_edit_distance(target, mode), mode) for mode in MODES)
    best, mode = scored[0]
    if best > MODE_TYPO_DISTANCE:
        return None
    if len(scored) > 1 and scored[1][0] == best:
        return None  # equidistant from both: no basis for guessing
    return mode


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, for words short enough that the cost is trivial."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return previous[-1]


class ProgressEditor:
    """Edits a message at most once per interval, and always delivers the last text.

    A throttle that simply drops intermediate states loses the final answer if the
    request fails mid-stream, which reads as a plugin that hangs. So the pending
    text is kept, and :meth:`finish` flushes whatever is left regardless of the
    clock.
    """

    def __init__(self, event: Any, interval: float) -> None:
        self._event = event
        self._interval = max(MIN_EDIT_INTERVAL, interval)
        self._pending: str | None = None
        self._stop = asyncio.Event()
        self._edits = 0
        #: Set once editing is known to be impossible, so it is not retried on
        #: every single token.
        self._broken = False

    @property
    def edits(self) -> int:
        """How many edits were actually sent. Asserted on in the tests."""
        return self._edits

    def offer(self, text: str | None) -> None:
        """Record the text to show next, or ``None`` to show nothing more."""
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
        self._edits += 1
        if not await edit_message(self._event, text):
            # Telethon's Message has `edit` and no `edit_text`, so calling
            # edit_text directly raised AttributeError on every progress update
            # and streaming did nothing in production. edit_message knows the
            # name; this only has to notice that it failed.
            self._broken = True
            self._stop.set()
            LOGGER.warning(
                "ai: cannot edit the progress message; answers will be sent as new messages"
            )

    async def finish(self) -> None:
        self._stop.set()
        if self._pending is not None:
            await self._flush()


#: Every role gets its own chain, because the head of the chain is that role's
#: configured model and the router is what binds it: one router for everything
#: would answer code requests with the chat model and quietly ignore
#: ``code_model``.
#:
#: Derived from ``MODELS`` rather than written out. It was a literal
#: ``("chat", "code")`` once, and ``/ub ai pro`` and ``/ub ai fast`` then reported
#: a missing API key on every call — a hand-written copy of a mapping, drifting.
ROLES = tuple(MODELS)


class UnknownRoleError(RuntimeError):
    """A role was requested that this plugin does not route.

    Deliberately not a ``MissingKeyError``. A role with no router is a mistake in
    this file, and reporting it as "the API key is not set" sent the operator to
    a configuration file that was already correct.
    """


class Plugin(BasePlugin):
    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.routers: dict[str, ModelRouter] = {}
        self.generated: dict[str, GeneratedPlugin] = {}

    # -- lifecycle ---------------------------------------------------------

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        keys = keys_from_env(*key_env_names(ctx))
        base_url = ctx.config.str_value("base_url", DEFAULT_BASE_URL)
        timeout = ctx.config.int_value("timeout_seconds", 120)
        for kind in ROLES:
            self.routers[kind] = ModelRouter(
                _chain(ctx, kind),
                base_url=base_url,
                timeout=timeout,
                key_getter=lambda: keys,
            )
        ctx.register_command(
            "ai",
            self.handle,
            help_text=USAGE,
            aliases=("gemini",),
        )
        if not keys:
            # Not fatal: the plugin loads so /ub ai can explain what is missing,
            # rather than the bot starting with one fewer plugin and no clue.
            LOGGER.warning(
                "ai: no API key found in %s; /ub ai will report it",
                ", ".join(key_env_names(ctx)),
            )

    async def stop(self) -> None:
        for router in self.routers.values():
            await router.aclose()
        self.routers.clear()

    # -- helpers -----------------------------------------------------------

    def require_router(self, kind: str = "chat") -> ModelRouter:
        """The router for one role, or why there is not one.

        The two reasons are kept apart because they send the reader to different
        places. "No key" is fixed in ``/etc/tguserbot/userbot.env``; "no such
        role" is a defect in this file, and answering it with the first was how
        ``/ub ai pro`` spent a day blaming a key that was present and working.
        """
        router = self.routers.get(kind)
        if router is None:
            raise UnknownRoleError(
                f"в плагине ai нет маршрута для роли {kind!r}; "
                f"известные роли: {', '.join(sorted(self.routers))}"
            )
        if not router.has_key:
            raise MissingKeyError(
                "Ключ Gemini не задан. Добавьте TGUSERBOT_GEMINI_API_KEY в "
                "/etc/tguserbot/userbot.env и перезапустите сервис."
            )
        return router

    def model_for(self, ctx: PluginContext, kind: str) -> str:
        key, default = MODELS[kind]
        return ctx.config.str_value(key, default)

    @staticmethod
    def staged(ctx: PluginContext) -> list[Any]:
        root = ctx.settings.staging_dir
        if not root.is_dir():
            return []
        return [
            path
            for path in sorted(root.iterdir())
            if path.is_dir() and (path / "plugin.toml").is_file()
        ]

    # -- history -----------------------------------------------------------

    async def ensure_history(self, ctx: PluginContext) -> None:
        await ctx.storage.execute(
            "CREATE TABLE IF NOT EXISTS history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, parts TEXT NOT NULL)"
        )

    async def history(self, ctx: PluginContext) -> list[Turn]:
        """The most recent exchanges, oldest first, ready to be resent.

        Every turn is replayed on the next request, so this window is the real
        cost driver rather than the number of messages -- which is why it is
        configuration and not a hardcoded slice.
        """
        rows = await ctx.storage.fetchall(
            "SELECT role, parts FROM history ORDER BY id DESC LIMIT 200"
        )
        # Rows arrive newest first, so the window is the head, not the tail.
        # Slicing the tail would keep the oldest turns and drop the current
        # conversation, which looks exactly like the bot forgetting.
        limit = ctx.config.int_value("max_history_turns", 20)
        if limit < 1:
            limit = 1
        turns: list[Turn] = []
        for row in rows[: 2 * limit]:
            try:
                stored = json.loads(row["parts"])
            except (ValueError, KeyError, TypeError):
                continue
            turn = Turn.from_wire({"role": row.get("role"), "parts": stored})
            if turn.text.strip():
                turns.append(turn)
        turns.reverse()
        return turns

    async def remember(self, ctx: PluginContext, turn: Turn) -> None:
        await ctx.storage.execute(
            "INSERT INTO history (role, parts) VALUES (?, ?)",
            (turn.role, json.dumps([part.to_wire() for part in turn.parts])),
        )

    # -- command dispatch --------------------------------------------------

    async def handle(self, command: Any) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        text = (command.args or "").strip()
        if not text:
            await command.respond(USAGE)
            return
        head, _, rest = text.partition(" ")
        head, rest = head.lower(), rest.strip()
        try:
            # One slot for the whole plugin: a second question while the first is
            # still streaming should queue, not race the same conversation.
            async with ctx.rate_limiter.slot("ai"):
                if head in SUBCOMMANDS:
                    await self.subcommand(ctx, command, head, rest)
                elif head in MODES:
                    await self.ask(ctx, command, rest, kind=head)
                else:
                    guess = nearest_mode(head) if rest else None
                    if guess is not None:
                        await command.respond(
                            f"Режима {head!r} нет — похоже на {guess!r}. "
                            f"Отвечаю на текст как на вопрос; для {guess}: "
                            f"/ub ai {guess} {rest}"
                        )
                    await self.ask(ctx, command, text, kind="chat")
        except (GeminiError, MissingKeyError, UnknownRoleError) as exc:
            # Also to the log, with the reason. The user is told, and then the
            # reason is gone: nothing reaches the journal, so the next "it says
            # something went wrong" can only be answered by asking the user to
            # screenshot the chat. The router logs the quota retries, so this
            # line is what covers everything it passes straight through.
            LOGGER.warning("ai: Gemini refused: %s", exc)
            await command.respond(f"Gemini: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("ai command failed")
            await command.respond(f"Ошибка: {exc}")

    async def subcommand(self, ctx: PluginContext, command: Any, head: str, rest: str) -> None:
        if head == "reset":
            await self.ensure_history(ctx)
            await ctx.storage.execute("DELETE FROM history")
            await command.respond("История диалога очищена.")
        elif head == "models":
            await self.show_models(ctx, command)
        elif head == "list":
            await self.list_staged(ctx, command)
        elif head == "show":
            await self.show_staged(ctx, command, rest)
        elif head == "new":
            await self.generate(ctx, command, rest)

    # -- chat --------------------------------------------------------------

    async def ask(self, ctx: PluginContext, command: Any, text: str, *, kind: str) -> None:
        if not text:
            await command.respond(USAGE)
            return
        router = self.require_router(kind)
        await self.ensure_history(ctx)
        # The question goes into the conversation only once there is an answer to
        # put next to it. It used to be stored before the request, so a refusal --
        # an expired key, a spent quota -- left a question with nothing after it:
        # replayed on every later turn, displacing real history inside
        # max_history_turns, and asking the model to continue a reply that does
        # not exist. A failed exchange now leaves no trace.
        question = Turn("user", [Part(text=text)])
        await self.remember(ctx, question)
        turns = await self.history(ctx)

        placeholder = await command.event.respond(STREAMING_MARK)
        editor = ProgressEditor(placeholder, ctx.config.float_value("min_edit_interval", 3.0))
        worker = asyncio.create_task(editor.run())
        pieces: list[str] = []
        try:
            async for delta in router.stream(turns, system=CHAT_SYSTEM):
                pieces.append(delta)
                editor.offer("".join(pieces))
            await editor.finish()
        except asyncio.CancelledError:
            editor.offer(None)
            await self._discard(placeholder)
            await self._forget(ctx, question)
            raise
        except Exception:
            await editor.finish()
            # Both of these used to live after the block, so a refusal skipped
            # them: the chat kept a lone "…" that nothing would ever remove, and
            # the history kept the unanswered question.
            await self._discard(placeholder)
            await self._forget(ctx, question)
            raise
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        answer = "".join(pieces).strip()
        # The answer goes out as a new message, not as an edit.
        #
        # This is deliberate. Editing the placeholder is a nicety, and it turned
        # out to be the only delivery path: when `respond()` hands back something
        # that cannot be edited -- which is what happened live -- the answer, the
        # "no text" notice and every progress update all failed together and the
        # user got the placeholder and nothing else. Sending is the operation
        # that is known to work, so it carries the answer; the placeholder is
        # cleaned up afterwards.
        if not answer:
            await command.respond("Модель не вернула текст.")
            # Nothing came back, so there is no exchange to remember.
            await self._forget(ctx, question)
        else:
            await command.respond(answer)
            # Keep the signature so the next turn is a real continuation.
            await self.remember(ctx, Turn("model", [Part(text=answer)]))
        await self._discard(placeholder)
        notice = router.notice()
        if notice:
            # Only when the configured model is not the one that answered, so a
            # normal run stays silent and a fallback is never mistaken for it.
            await command.respond(notice)

    @staticmethod
    async def _forget(ctx: PluginContext, question: Turn) -> None:
        """Take back the question that was stored ahead of an answer.

        Deletes the newest ``user`` row rather than matching on the text: the same
        question asked twice is two rows, and removing both would also drop the
        earlier exchange that has a real answer after it.
        """
        try:
            await ctx.storage.execute(
                "DELETE FROM history WHERE id = ("
                "  SELECT id FROM history WHERE role = 'user' ORDER BY id DESC LIMIT 1"
                ")"
            )
        except Exception:
            # A stale question is not worth failing the command over; the next
            # successful exchange appends after it, and /ub ai reset clears it.
            LOGGER.warning("ai: could not remove the unanswered question from the history")

    @staticmethod
    async def _discard(placeholder: Any) -> None:
        """Remove the placeholder once the real answer is out."""
        await delete_message(placeholder)

    # -- generation --------------------------------------------------------

    async def generate(self, ctx: PluginContext, command: Any, rest: str) -> None:
        name, _, description = rest.partition(" ")
        name, description = name.strip().lower(), description.strip()
        if not name or not description:
            await command.respond("Использование: /ub ai new <имя> <описание>")
            return
        router = self.require_router("code")
        reply = await router.generate(
            [Turn("user", [Part(text=f"Создай плагин {name!r}.\n\n{description}")])],
            system=CODE_SYSTEM,
            max_output_tokens=ctx.config.int_value("code_output_tokens", 16384),
            response_schema=PLUGIN_SCHEMA,
        )
        try:
            plugin = parse_generated(reply.text, expected_name=name)
        except GeneratedFileError as exc:
            await command.respond(
                f"Модель вернула непригодный результат: {exc}\n"
                "Ничего не записано. Попробуйте переформулировать задачу."
            )
            return
        destination = write_plugin(ctx.settings.staging_dir, plugin)
        self.generated[name] = plugin

        report = review_tree(plugin.as_mapping())
        lines = [plugin.summary or "(без описания)", ""]
        lines += [f"• {item.path}" for item in plugin.files]
        if report.findings:
            lines += ["", format_findings(report.findings)]
        lines += ["", f"Записано в {destination}", f"Установить: /ub plugin adopt {name}"]
        await command.respond("\n".join(lines))

    # -- staging -----------------------------------------------------------

    async def show_models(self, ctx: PluginContext, command: Any) -> None:
        self.require_router()
        models = await self.require_router("chat").list_models()
        if not models:
            await command.respond("Модели не вернулись.")
            return
        lines = [
            f"{item.name}"
            + (f" — {item.display_name}" if item.display_name else "")
            + (f", контекст {item.input_limit // 1024}K" if item.input_limit else "")
            for item in models
        ]
        await command.respond("Доступно вашему ключу:\n" + "\n".join(lines[:40]))

    async def list_staged(self, ctx: PluginContext, command: Any) -> None:
        staged = self.staged(ctx)
        if not staged:
            await command.respond(
                "В staging ничего нет.\nСгенерировать: /ub ai new <имя> <описание>"
            )
            return
        lines = []
        for path in staged:
            report = review_tree(read_tree_sources(path))
            verdict = (
                "проблемы" if not report.ok else ("предупреждения" if report.findings else "чисто")
            )
            lines.append(f"{path.name}: проверка — {verdict}")
        await command.respond("Ждут установки:\n" + "\n".join(lines))

    async def show_staged(self, ctx: PluginContext, command: Any, name: str) -> None:
        if not name:
            await command.respond("Использование: /ub ai show <имя>")
            return
        target = ctx.settings.staging_dir / name
        if not target.is_dir():
            await command.respond(f"В staging нет {name!r}.")
            return
        sources = read_tree_sources(target)
        report = review_tree(sources)
        body = format_findings(report.findings) if report.findings else "Замечаний нет."
        await command.respond(
            f"{name}: {', '.join(sorted(sources))}\n\n{body}\n\nУстановить: /ub plugin adopt {name}"
        )


def _chain(ctx: PluginContext, kind: str) -> list[str]:
    """The models to try, in order, for one role.

    The role's own model first, then the configured chain. Quota is counted per
    model on the free tier, so having a second one is the difference between a
    slower answer and none at all.
    """
    key, default = MODELS[kind]
    primary = ctx.config.str_value(key, default)
    chain = [primary]
    for name in ctx.config.str_list("model_fallbacks", ()):
        if name not in chain:
            chain.append(name)
    return chain


def key_env_names(ctx: PluginContext) -> tuple[str, ...]:
    """Variables to read the key from, in order.

    The name is configuration because a proxied or self-hosted endpoint will not
    call it ``TGUSERBOT_GEMINI_API_KEY``, and a user with several keys for
    rotation needs a different variable name than the first one.
    """
    raw = ctx.config.str_value("api_key_env", "TGUSERBOT_GEMINI_API_KEY")
    return tuple(part.strip() for part in raw.split(",") if part.strip())
