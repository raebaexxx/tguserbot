"""Tests for the delivery path of a streamed answer.

The live failure this pins down: ``/ub ai привет`` produced the ``…`` placeholder
and then nothing. Every way the answer could reach the user -- the progress
edits, the final text, even the "no text" notice -- went through
``placeholder.edit_text()``, and when that call raises, all of them fail
together. The exceptions were swallowed at DEBUG, so the journal was empty and
there was no trace of it anywhere.

The property these tests encode: the answer is delivered by a path that does not
depend on the placeholder being editable, and a failure to edit is reported
rather than absorbed.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from conftest import FakeEvent, plugin_config, shipped_module
from userbot.config import Settings


@pytest.fixture(scope="module")
def ai_module() -> Any:
    loaded, module = shipped_module("ai")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


class FakeStorage:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def execute(self, sql: str, parameters: Any = ()) -> int:
        verb = sql.strip().split(None, 1)[0].upper()
        if verb == "DELETE":
            self.rows.clear()
        elif verb == "INSERT" and "history" in sql:
            role, parts = parameters
            self.rows.append({"id": len(self.rows) + 1, "role": role, "parts": parts})
        return 0

    async def fetchall(self, sql: str, parameters: Any = ()) -> list[dict[str, Any]]:
        if "FROM history" in sql:
            return list(reversed(self.rows))
        return []


class Uneditable:
    """A placeholder whose edits always raise, as the live one did."""

    def __init__(self) -> None:
        self.edit_attempts = 0

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edit_attempts += 1
        raise RuntimeError("MessageIdInvalidError")

    async def delete(self) -> None:
        return None


class NoEditMethod:
    """respond() returned an object with no edit support at all."""

    def __init__(self) -> None:
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class DeliveryEvent(FakeEvent):
    """Records everything sent as a *new* message, which is the path that works."""

    def __init__(self, placeholder: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder
        self.sent: list[str] = []
        self.placeholders: list[Any] = []

    async def respond(self, text: str | None = None, **kwargs: Any) -> Any:
        self.sent.append(text or "")
        self.placeholders.append(self.placeholder)
        return self.placeholder

    async def delete(self) -> None:
        self.deleted = True


class RecordingCommand:
    """A CommandContext whose respond() goes through the event, as the real one
    does. Recording it separately would have hidden the whole bug."""

    def __init__(self, args: str, event: Any) -> None:
        self.name = "ai"
        self.args = args
        self.raw = f"/ub ai {args}"
        self.event = event
        self.replies: list[str] = []

    async def respond(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)
        await self.event.respond(text, **kwargs)


class Streaming:
    def __init__(self, pieces: list[str] | None = None) -> None:
        self.pieces = pieces if pieces is not None else ["При", "вет"]
        self.has_key = True
        self.seen: list[Any] = []

    async def stream(self, turns: Any, **kwargs: Any) -> Any:
        self.seen.append((turns, kwargs))
        for piece in self.pieces:
            yield piece

    async def generate(self, turns: Any, **kwargs: Any) -> Any:
        raise AssertionError("not used here")

    async def list_models(self) -> list[Any]:
        return []

    async def aclose(self) -> None:
        return None


class Ctx:
    """The slice of PluginContext that the ai plugin touches."""

    def __init__(self, tmp_path: Any, values: dict[str, Any]) -> None:
        from userbot.rate_limit import RateLimiter

        self.logger = logging.getLogger("test.ai")
        self.rate_limiter = RateLimiter(min_interval=0)
        self.settings = Settings(
            root_dir=tmp_path,
            data_dir=tmp_path / "data",
            plugin_dir=tmp_path / "plugins",
            log_dir=tmp_path / "data" / "logs",
            api_id=1,
            api_hash="test",
        )
        self.config = plugin_config(values, "ai")
        self.storage = FakeStorage()

    @staticmethod
    def is_owner(sender_id: Any) -> bool:
        return sender_id == 1


def build(
    ai_module: Any,
    tmp_path: Any,
    event: Any,
    client: Any,
    args: str = "привет",
    **config: Any,
) -> Any:
    """A plugin wired to a command, without a real manager or database."""
    plugin = ai_module.Plugin()
    plugin.ctx = Ctx(tmp_path, config)
    plugin.client = client
    return plugin, RecordingCommand(args, event)


# --- the answer must arrive even when editing is impossible ---------------


async def test_the_answer_arrives_when_the_placeholder_cannot_be_edited(
    ai_module: Any, tmp_path: Any
) -> None:
    """The live failure, reproduced.

    Every delivery path went through edit_text. With it broken, the user got the
    placeholder and nothing else.
    """
    event = DeliveryEvent(Uneditable())
    client = Streaming()
    plugin, command = build(ai_module, tmp_path, event, client)

    await plugin.ask(plugin.ctx, command, "привет", kind="chat")

    assert any("Привет" in text for text in event.sent), (
        f"the answer never reached the user: sent={event.sent!r}"
    )


async def test_the_answer_arrives_when_respond_returns_something_unusable(
    ai_module: Any, tmp_path: Any
) -> None:
    """respond() returned an object with no edit_text at all."""
    event = DeliveryEvent(NoEditMethod())
    plugin, command = build(ai_module, tmp_path, event, Streaming())

    await plugin.ask(plugin.ctx, command, "привет", kind="chat")

    assert any("Привет" in text for text in event.sent), event.sent


async def test_an_empty_answer_says_so_by_a_working_path(ai_module: Any, tmp_path: Any) -> None:
    """The "no text" notice used to go through the same broken edit."""
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming(pieces=[]))

    await plugin.ask(plugin.ctx, command, "привет", kind="chat")

    assert any("не вернула" in text for text in event.sent), event.sent


async def test_the_history_records_the_answer_even_if_delivery_failed(
    ai_module: Any, tmp_path: Any
) -> None:
    """Losing the answer must not also lose the conversation."""
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming())
    ctx = plugin.ctx
    await plugin.ask(ctx, command, "привет", kind="chat")
    roles = [row["role"] for row in ctx.storage.rows]
    assert roles == ["user", "model"], roles


# --- a failure to edit is reported, not absorbed ---------------------------


async def test_a_failed_edit_is_logged_at_a_visible_level(
    ai_module: Any, tmp_path: Any, caplog: Any
) -> None:
    """This is why the journal was empty and the bug was invisible.

    The exceptions were swallowed at DEBUG, which systemd's default level does
    not show. Anything that means "the user may not get their answer" belongs at
    WARNING.
    """
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming())
    with caplog.at_level("WARNING"):
        await plugin.ask(plugin.ctx, command, "привет", kind="chat")
    assert any(record.levelname == "WARNING" for record in caplog.records), [
        r.levelname for r in caplog.records
    ]


async def test_the_edit_failure_is_logged_once_not_once_per_attempt(
    ai_module: Any, tmp_path: Any, caplog: Any
) -> None:
    """A log line per token would be its own flood."""
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming(pieces=["a"] * 200))
    with caplog.at_level("WARNING"):
        await plugin.ask(plugin.ctx, command, "x" * 200, kind="chat")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) <= 2, f"{len(warnings)} warnings for one failure"


# --- the placeholder is cleaned up ----------------------------------------


async def test_the_placeholder_is_deleted_once_the_answer_is_sent(
    ai_module: Any, tmp_path: Any
) -> None:
    placeholder = NoEditMethod()
    event = DeliveryEvent(placeholder)
    plugin, command = build(ai_module, tmp_path, event, Streaming())
    await plugin.ask(plugin.ctx, command, "привет", kind="chat")
    assert placeholder.deleted, "the placeholder was left behind"


async def test_a_failing_delete_does_not_break_delivery(ai_module: Any, tmp_path: Any) -> None:
    class Stubborn(NoEditMethod):
        async def delete(self) -> None:
            raise RuntimeError("cannot delete")

    event = DeliveryEvent(Stubborn())
    plugin, command = build(ai_module, tmp_path, event, Streaming())
    await plugin.ask(plugin.ctx, command, "привет", kind="chat")
    assert any("Привет" in text for text in event.sent), event.sent


# --- the question is passed on intact --------------------------------------


async def test_the_whole_question_is_sent(ai_module: Any, tmp_path: Any) -> None:
    """A question that starts with a mode word must not be split on it.

    "/ub ai flash привет" is a typo for "fast", but silently sending the question
    as "flash привет" is worse than saying the mode was not recognised.
    """
    event = DeliveryEvent(Uneditable())
    client = Streaming()
    plugin, command = build(ai_module, tmp_path, event, client)
    await plugin.ask(plugin.ctx, command, "flash привет", kind="chat")
    turns, _kwargs = client.seen[0]
    assert "flash привет" in turns[-1].text


async def test_an_unrecognised_mode_word_is_reported(ai_module: Any, tmp_path: Any) -> None:
    """A mode that does not exist must not be silently swallowed."""
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming(), args="fas привет")
    await plugin.handle(command)
    assert any("fast" in text for text in event.sent), (
        f"the user was not told which modes exist: {event.sent!r}"
    )
    # And the question is still answered, not consumed by the hint.
    assert any("Привет" in text for text in event.sent), event.sent


# --- progress still throttles when it works --------------------------------


async def test_progress_edits_happen_when_editing_works(ai_module: Any, tmp_path: Any) -> None:
    """The streaming nicety must survive the fix, not be replaced by it."""
    event = DeliveryEvent(FakeEvent("/ub ai x", sender_id=1))
    plugin, command = build(
        ai_module, tmp_path, event, Streaming(pieces=["a", "b"]), min_edit_interval=0.5
    )
    await plugin.ask(plugin.ctx, command, "привет", kind="chat")
    assert any("ab" in text for text in event.sent), event.sent


# --- typo detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("fas", "fast"),  # a single missing keystroke
        ("fst", "fast"),
        ("faast", "fast"),  # a repeated keystroke
        ("prro", "pro"),
        ("fast", "fast"),
        ("pro", "pro"),
        ("last", "fast"),  # one edit away, and rare as a question opener
        # Deliberate non-suggestions. At a distance of two, "list", "note",
        # "code" and "more" each match a mode -- flagging "/ub ai list ..." on
        # an ordinary question is worse than staying quiet.
        ("flash", None),
        ("list", None),
        ("note", None),
        ("code", None),
        ("more", None),
        ("how", None),
        ("the", None),
        ("what", None),
        ("привет", None),
        ("x", None),
    ],
)
def test_mode_typos_are_recognised(ai_module: Any, word: str, expected: str | None) -> None:
    assert ai_module.nearest_mode(word) == expected


def test_the_typo_threshold_is_one_not_two(ai_module: Any) -> None:
    """Precision beats recall for a hint: a wrong hint costs the user's trust."""
    assert ai_module.MODE_TYPO_DISTANCE == 1


async def test_a_typo_never_changes_the_question(ai_module: Any, tmp_path: Any) -> None:
    """A false positive must cost one line, not the answer."""
    event = DeliveryEvent(Uneditable())
    client = Streaming()
    plugin, command = build(ai_module, tmp_path, event, client, args="last friday")
    await plugin.handle(command)
    assert any("last friday" in turns[-1].text for turns, _ in client.seen)


async def test_a_mode_word_is_not_reported_as_a_typo(ai_module: Any, tmp_path: Any) -> None:
    event = DeliveryEvent(Uneditable())
    plugin, command = build(ai_module, tmp_path, event, Streaming(), args="fast привет")
    await plugin.handle(command)
    assert not any("похоже на" in text for text in event.sent), event.sent
