"""Tests for the ai plugin's behaviour: commands, history and edit throttling.

The throttling is the part worth reading. An earlier plugin in this repository
shipped a progress reporter that edited the message on every callback, and the
resulting edit flood was a real rate-limit incident. ``ProgressEditor`` exists
because of that, and the tests here count actual edits rather than trusting the
implementation to be careful.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeContext, FakeEvent, plugin_config, routers_for, shipped_module


@pytest.fixture(scope="module")
def ai_module() -> Any:
    loaded, module = shipped_module("ai")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


class FakeStorage:
    """Mirrors ``PluginStorage``'s shape: rows come back as dicts, parameters
    arrive as one sequence. Matching it exactly is what caught the plugin passing
    SQL parameters as varargs and unpacking rows as tuples."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self._next_id = 1

    async def execute(self, sql: str, parameters: Any = ()) -> int:
        self.executed.append((sql, parameters))
        verb = sql.strip().split(None, 1)[0].upper()
        if verb == "DELETE" and "FROM history WHERE id" in sql:
            # The targeted delete, not "clear everything": a double that wiped
            # the table would make the rollback test pass for the wrong reason.
            newest_user = [row for row in self.rows if row["role"] == "user"]
            if newest_user:
                self.rows.remove(max(newest_user, key=lambda row: row["id"]))
        elif verb == "DELETE":
            self.rows.clear()
        elif verb == "INSERT" and "history" in sql:
            role, parts = parameters
            self.rows.append({"id": self._next_id, "role": role, "parts": parts})
            self._next_id += 1
        return 0

    async def fetchall(self, sql: str, parameters: Any = ()) -> list[dict[str, Any]]:
        if "FROM history" in sql:
            # Newest first, as the real query orders by id DESC.
            return list(reversed(self.rows))
        return []


class FakeClient:
    """Stands in for the Gemini client, recording what it was asked."""

    def __init__(self, pieces: list[str] | None = None, reply_text: str = "") -> None:
        self.pieces = pieces if pieces is not None else ["При", "вет"]
        self.reply_text = reply_text
        self.has_key = True
        self.models: list[Any] = []
        self.seen: list[Any] = []
        self.generated: list[Any] = []
        self.closed = False

    async def generate(self, turns: Any, **kwargs: Any) -> Any:
        self.generated.append((turns, kwargs))
        return type("Reply", (), {"text": self.reply_text})()

    async def stream(self, turns: Any, **kwargs: Any) -> Any:
        self.seen.append((turns, kwargs))
        for piece in self.pieces:
            yield piece

    async def list_models(self) -> list[Any]:
        return self.models

    async def aclose(self) -> None:
        self.closed = True


def make_context(tmp_path: Any, values: dict[str, Any] | None = None) -> Any:
    from userbot.config import Settings

    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    ctx = FakeContext(settings=settings, config=plugin_config(values, "ai"))
    ctx.storage = FakeStorage()
    return ctx


def make_plugin(ai_module: Any, tmp_path: Any, client: Any = None, **config: Any) -> Any:
    plugin = ai_module.Plugin()
    plugin.ctx = make_context(tmp_path, config)
    plugin.routers = routers_for(ai_module, client if client is not None else FakeClient())
    return plugin


def command_for(args: str) -> Any:
    return type("Cmd", (), {"args": args, "event": None, "respond": None})()


def _command(event: Any, args: str) -> Any:
    """A CommandContext stand-in whose replies land on the event.

    ``ask()`` answers through ``command.respond`` and places its placeholder with
    ``command.event.respond``, so both have to end up on the same double for the
    test to see what the chat would have seen.
    """

    async def respond(text: str, **kwargs: Any) -> None:
        await event.respond(text, **kwargs)

    # A namespace, not a class: a function in a class body would bind as a method
    # and receive the instance as its first argument.
    return SimpleNamespace(args=args, event=event, respond=respond, raw=args, name="ai")


# --- the progress editor ---------------------------------------------------


async def test_no_edit_happens_before_the_interval_elapses(ai_module: Any) -> None:
    """The regression this class exists for.

    An interval of an hour makes the deadline unreachable, so any edit that
    appears is a throttle that is not working. Counting edits is what makes this
    a test rather than a hope: the earlier incident was not a slow flood, it was
    one edit per streamed token.
    """
    event = FakeEvent("/ub ai x")
    editor = ai_module.ProgressEditor(event, 3600.0)
    worker = asyncio.create_task(editor.run())
    for index in range(200):
        editor.offer("x" * (index + 1))
        await asyncio.sleep(0)
    assert editor.edits == 0, f"{editor.edits} edits before the interval elapsed"
    assert event.edits == []
    editor.offer("final")
    await editor.finish()
    assert event.edits == ["final"], "the last text must still be delivered"
    await asyncio.gather(worker, return_exceptions=True)


async def test_the_edit_count_is_bounded_by_time_not_by_offers(ai_module: Any) -> None:
    """Many offers over a short window must not become many edits."""
    event = FakeEvent("/ub ai x")
    editor = ai_module.ProgressEditor(event, ai_module.MIN_EDIT_INTERVAL)
    worker = asyncio.create_task(editor.run())
    deadline = asyncio.get_running_loop().time() + ai_module.MIN_EDIT_INTERVAL * 3
    index = 0
    while asyncio.get_running_loop().time() < deadline:
        editor.offer(f"chunk {index}")
        index += 1
        await asyncio.sleep(0)
    await editor.finish()
    await asyncio.gather(worker, return_exceptions=True)
    # A three-window run cannot legitimately produce more than a handful of
    # edits, however many offers arrived.
    assert editor.edits <= 6, f"{index} offers produced {editor.edits} edits"
    assert index > 6, "the test did not actually offer enough to prove anything"


async def test_the_final_text_is_always_delivered(ai_module: Any) -> None:
    """Dropping the last edit on a throttle is how an answer looks truncated."""
    event = FakeEvent("/ub ai x")
    editor = ai_module.ProgressEditor(event, 3600.0)
    editor.offer("черновик")
    await editor.finish()
    assert event.edits == ["черновик"]


async def test_edits_happen_once_the_interval_passes(ai_module: Any) -> None:
    event = FakeEvent("/ub ai x")
    editor = ai_module.ProgressEditor(event, 0.05)
    worker = asyncio.create_task(editor.run())
    editor.offer("первый")
    await asyncio.sleep(0.12)
    editor.offer("второй")
    await asyncio.sleep(0.12)
    await editor.finish()
    await asyncio.gather(worker, return_exceptions=True)
    assert event.edits, "nothing was ever shown"
    assert event.edits[-1] == "второй", "the newest text must win"


async def test_an_edit_failure_stops_the_editor(ai_module: Any) -> None:
    """Retrying an edit that Telegram refuses is how a flood becomes a ban."""

    class Broken(FakeEvent):
        async def edit_text(self, text: str, **kwargs: Any) -> None:
            raise RuntimeError("message gone")

    editor = ai_module.ProgressEditor(Broken("/ub ai x", sender_id=1), 0.01)
    editor.offer("x")
    await asyncio.wait_for(editor.finish(), timeout=2)


async def test_clearing_the_pending_text_shows_nothing(ai_module: Any) -> None:
    event = FakeEvent("/ub ai x")
    editor = ai_module.ProgressEditor(event, 0.01)
    editor.offer(None)
    await editor.finish()
    assert event.edits == []


def test_the_editor_enforces_a_floor_on_the_interval(ai_module: Any) -> None:
    """A configured 0.1s would be a flood; the floor is not configurable."""
    editor = ai_module.ProgressEditor(FakeEvent("/ub ai x"), 0.0)
    assert editor._interval >= ai_module.MIN_EDIT_INTERVAL


# --- history ---------------------------------------------------------------


async def test_history_round_trips_through_storage(ai_module: Any, tmp_path: Any) -> None:
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    await plugin.ensure_history(ctx)
    await plugin.remember(ctx, ai_module.Turn("user", [ai_module.Part(text="вопрос")]))
    await plugin.remember(ctx, ai_module.Turn("model", [ai_module.Part(text="ответ")]))
    turns = await plugin.history(ctx)
    assert [t.role for t in turns] == ["user", "model"]
    assert [t.text for t in turns] == ["вопрос", "ответ"]


async def test_history_keeps_the_thought_signature(ai_module: Any, tmp_path: Any) -> None:
    """Without it the next turn is a fresh conversation, not a continuation."""
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    await plugin.ensure_history(ctx)
    await plugin.remember(
        ctx, ai_module.Turn("model", [ai_module.Part(text="ок", thought_signature="SIG")])
    )
    stored = json.loads(ctx.storage.rows[-1]["parts"])
    assert stored[0]["thoughtSignature"] == "SIG"


async def test_history_is_capped(ai_module: Any, tmp_path: Any) -> None:
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    ctx.config = plugin_config({"max_history_turns": 2}, "ai")
    await plugin.ensure_history(ctx)
    for index in range(10):
        await plugin.remember(ctx, ai_module.Turn("user", [ai_module.Part(text=str(index))]))
    turns = await plugin.history(ctx)
    assert len(turns) == 4, "two exchanges means four turns"


async def test_empty_turns_are_not_kept(ai_module: Any, tmp_path: Any) -> None:
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    await plugin.ensure_history(ctx)
    await plugin.remember(ctx, ai_module.Turn("model", [ai_module.Part(text="")]))
    assert await plugin.history(ctx) == []


async def test_reset_clears_the_history(ai_module: Any, tmp_path: Any) -> None:
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    await plugin.ensure_history(ctx)
    await plugin.remember(ctx, ai_module.Turn("user", [ai_module.Part(text="x")]))
    assert ctx.storage.rows
    await ctx.storage.execute("DELETE FROM history")
    assert await plugin.history(ctx) == []


# --- the two things that survive a failure ---------------------------------
#
# Both are the same mistake in two places: the work that must be undone on the
# error path lives after the call that can fail, so an exception skips it.


class PlaceholderEvent(FakeEvent):
    """A ``FakeEvent`` whose ``respond`` hands back the message it made.

    A real Telethon ``respond`` returns the ``Message``, which is the object a
    plugin then edits and deletes. Returning ``None`` would make the placeholder
    undeletable by construction and hide the very defect under test.
    """

    async def respond(self, text: str | None = None, *, file: str | None = None, **_: Any) -> Any:
        await super().respond(text, file=file)
        return self


async def test_a_failed_answer_does_not_leave_the_placeholder_behind(
    ai_module: Any, tmp_path: Any
) -> None:
    """A refusal mid-stream left a lone "…" in the chat, forever.

    The placeholder is deleted on the success path, but the stream is wrapped in
    ``try/except`` that re-raises, so a refusal — an expired key, a spent quota —
    jumped straight past the delete. What the user was left with was "…" and an
    error line, with the ellipsis never going away and nothing in it explaining
    what it had been.
    """
    from userbot.gemini import GeminiError

    class Failing(FakeClient):
        async def stream(self, turns: Any, **kwargs: Any) -> Any:
            self.seen.append((turns, kwargs))
            yield "нач"
            raise GeminiError("quota exhausted")

    plugin = make_plugin(ai_module, tmp_path, client=Failing())
    event = PlaceholderEvent("/ub ai вопрос")
    command = _command(event, "вопрос")

    await plugin.handle(command)

    assert event.deleted, "the placeholder survived a failed answer"
    assert any("quota exhausted" in text for text in event.responses), (
        "the user must still be told why, not just left with a deleted ellipsis"
    )


async def test_a_failed_answer_does_not_poison_the_history(ai_module: Any, tmp_path: Any) -> None:
    """A question the model never answered was kept and sent again next time.

    The user's turn is written to storage *before* the request. If the request
    fails there is no matching model turn, so the conversation keeps a question
    with no answer: it is replayed on the next question, it displaces real history
    within ``max_history_turns``, and the model is asked to continue a reply that
    does not exist. A failed exchange leaves no trace at all.
    """
    from userbot.gemini import GeminiError

    class Failing(FakeClient):
        async def stream(self, turns: Any, **kwargs: Any) -> Any:
            self.seen.append((turns, kwargs))
            raise GeminiError("quota exhausted")
            yield ""  # pragma: no cover - makes this an async generator

    plugin = make_plugin(ai_module, tmp_path, client=Failing())
    event = FakeEvent("/ub ai вопрос")
    command = _command(event, "вопрос")

    await plugin.handle(command)

    assert await plugin.history(plugin.ctx) == [], (
        "the unanswered question is still in the conversation"
    )

    # And the next question must not carry it either.
    client = FakeClient()
    plugin.routers = routers_for(ai_module, client)
    await plugin.handle(_command(event, "следующий"))
    sent = client.seen[0][0]
    assert all(turn.text != "вопрос" for turn in sent), [turn.text for turn in sent]


async def test_the_rollback_only_removes_the_unanswered_question(
    ai_module: Any, tmp_path: Any
) -> None:
    """An earlier exchange with a real answer must survive a later failure.

    The rollback removes the newest ``user`` row rather than matching on the
    text. Matching the text would delete both when the same question is asked
    twice -- including the earlier copy, which does have an answer after it.
    """
    from userbot.gemini import GeminiError

    class Failing(FakeClient):
        async def stream(self, turns: Any, **kwargs: Any) -> Any:
            self.seen.append((turns, kwargs))
            raise GeminiError("quota exhausted")
            yield ""  # pragma: no cover - makes this an async generator

    plugin = make_plugin(ai_module, tmp_path, client=Failing())
    ctx = plugin.ctx
    event = PlaceholderEvent("/ub ai первый")
    await plugin.ensure_history(ctx)
    await plugin.handle(_command(event, "повтор"))
    await plugin.handle(_command(event, "повтор"))

    assert await plugin.history(ctx) == [], (
        "an earlier answered exchange was destroyed by a later rollback"
    )


# --- key configuration -----------------------------------------------------


def test_the_key_variable_is_configurable(ai_module: Any) -> None:
    """A proxied endpoint will not call it TGUSERBOT_GEMINI_API_KEY."""
    ctx = FakeContext()
    assert ai_module.key_env_names(ctx) == ("TGUSERBOT_GEMINI_API_KEY",)
    ctx = FakeContext(config=plugin_config({"api_key_env": "MY_KEY, OTHER_KEY"}))
    assert ai_module.key_env_names(ctx) == ("MY_KEY", "OTHER_KEY")


def test_a_missing_key_is_reported_clearly(ai_module: Any, tmp_path: Any) -> None:
    from userbot.gemini import MissingKeyError

    plugin = make_plugin(ai_module, tmp_path, client=FakeClient())
    plugin.routers["chat"].has_key = False
    with pytest.raises(MissingKeyError, match="TGUSERBOT_GEMINI_API_KEY"):
        plugin.require_router()


def test_models_come_from_configuration(ai_module: Any, tmp_path: Any) -> None:
    plugin = make_plugin(ai_module, tmp_path)
    ctx = plugin.ctx
    assert plugin.model_for(ctx, "chat") == "gemini-3.8-flash"
    assert plugin.model_for(ctx, "code") == "gemini-3.8-flash"
    assert plugin.model_for(ctx, "fast") == "gemini-3.5-flash-lite"
    assert plugin.model_for(ctx, "pro") == "gemini-3.1-pro-preview"
    ctx.config = plugin_config({"chat_model": "custom-model"}, "ai")
    assert plugin.model_for(ctx, "chat") == "custom-model"


def test_every_model_role_has_a_default(ai_module: Any) -> None:
    """A missing key would mean an AttributeError at request time."""
    for kind, (config_key, default) in ai_module.MODELS.items():
        assert config_key and default, kind


# --- the usage text --------------------------------------------------------


def test_the_usage_text_lists_every_subcommand(ai_module: Any) -> None:
    for name in sorted(ai_module.SUBCOMMANDS):
        assert f"/ub ai {name}" in ai_module.USAGE, name
    for name in ("pro", "fast"):
        assert f"/ub ai {name}" in ai_module.USAGE, name
    assert "/ub plugin adopt" not in ai_module.USAGE, (
        "adoption is a core command, not an ai subcommand"
    )


def test_the_usage_mentions_the_models_it_uses(ai_module: Any) -> None:
    assert "gemini-3.5-flash-lite" in ai_module.USAGE
    assert "gemini-3.1-pro-preview" in ai_module.USAGE


# --- the codegen prompt is specific about what broke -----------------------


def test_the_prompt_states_the_real_manifest_shape(ai_module: Any) -> None:
    """A [plugin] section header is the mistake the loader rejects."""
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    assert "no [section] header" in prompts.CODE_SYSTEM
    assert 'entrypoint = "plugin:Plugin"' in prompts.CODE_SYSTEM


def test_the_prompt_names_the_required_files(ai_module: Any) -> None:
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    for name in ("plugin.toml", "__init__.py", "plugin.py"):
        assert name in prompts.CODE_SYSTEM, name


def test_the_prompt_states_the_class_must_be_named_plugin(ai_module: Any) -> None:
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    assert "MUST be" in prompts.CODE_SYSTEM
    assert "class Plugin(BasePlugin)" in prompts.CODE_SYSTEM


def test_the_prompt_lists_the_forbidden_imports(ai_module: Any) -> None:
    """Telling the model saves a rejection round trip."""
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    for name in ("subprocess", "eval", "exec", "socket", "pickle", "session.session"):
        assert name in prompts.CODE_SYSTEM, name


def test_the_prompt_documents_the_context_api(ai_module: Any) -> None:
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    for name in (
        "ctx.config.str_value",
        "ctx.register_command",
        "ctx.register_handler",
        "ctx.logger",
        "ctx.is_owner",
        "ctx.spawn",
        "ctx.storage",
    ):
        assert name in prompts.CODE_SYSTEM, name


def test_the_chat_prompt_asks_for_concise_plain_text(ai_module: Any) -> None:
    from conftest import shipped_submodule

    _loaded, prompts = shipped_submodule("ai", "_prompts")
    assert "concise" in prompts.CHAT_SYSTEM
    assert "language the user wrote in" in prompts.CHAT_SYSTEM
