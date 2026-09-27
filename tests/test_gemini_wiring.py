"""The plugins must work with the real ``ModelRouter``, not only with a double.

This file exists because a test double hid a break.

The plugins were wired to a router so a spent quota could fall through to
another model. The tests were then updated to set ``plugin.router = ...`` with a
stand-in that forwards straight to a fake client. That stand-in accepts whatever
the plugin passes, including a ``model=`` keyword -- and the real
``ModelRouter.generate`` binds ``model`` itself for each attempt, so passing one
as well raises ``TypeError: got multiple values for keyword argument 'model'``.

Every plugin test passed. Both ``/ub ai`` modes -- and with them the entire point
of the change -- were broken in production code, one layer below anything the
suite could see. Deploying it would have left ``/ub ai`` dead while the log
looked healthy.

The rule these tests exist to enforce: anything crossing a boundary that a
double replaces gets exercised with the real object. Here the real plugin runs
against a real ``ModelRouter`` over a mock HTTP transport, and the only thing
faked is the network.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx

from conftest import FakeClient, plugin_config, shipped_module, shipped_submodule
from userbot.config import Settings
from userbot.gemini import DEFAULT_BASE_URL, GeminiClient, ModelRouter, Turn
from userbot.rate_limit import RateLimiter

KEY = "test-key-abcdefghijklmnop"

#: The body Gemini returns for a spent daily allowance, captured live.
QUOTA_BODY = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information, see "
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}


def model_of(request: httpx.Request) -> str:
    return request.url.path.split("/models/")[1].split(":")[0]


def text_frame(text: str) -> bytes:
    """One server-sent-events frame carrying a complete answer."""
    payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def answering(text: str = "Привет", *, dead: str | None = None) -> tuple[Any, list[Any]]:
    """A transport answering every model, with an optional dead one.

    Returns the handler and the list it records requests into.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if dead is not None and model_of(request) == dead:
            return httpx.Response(429, json=QUOTA_BODY)
        if request.url.path.endswith(":streamGenerateContent"):
            return httpx.Response(200, content=text_frame(text))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]},
        )

    return handler, seen


def real_router(models: list[str], handler: Any) -> ModelRouter:
    """The genuine article: a router, over a client, over a mock transport."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GeminiClient(api_keys=[KEY], client=http, base_url=DEFAULT_BASE_URL, backoff=0.001)
    return ModelRouter(models, client=client)


class Event:
    """A stand-in for a telethon event that records what was sent to the user.

    ``respond`` returns a new event, as telethon's does. Returning ``None`` is
    not a harmless simplification: the caller keeps the return value to edit
    later, and code that checks it for ``None`` then takes a different path.
    """

    def __init__(self) -> None:
        self.id = 1
        self.chat_id = 1
        self.sender_id = 1
        self.out = False
        self.responses: list[str] = []
        self.edits: list[str] = []
        self.files: list[str] = []
        self.deleted = False

    async def respond(self, text: str | None = None, *, file: str | None = None, **_: Any) -> Event:
        if file is not None:
            self.files.append(file)
        if text is not None:
            self.responses.append(text)
        sent = Event()
        sent.chat_id = self.chat_id
        return sent

    async def reply(self, text: str | None = None, **kwargs: Any) -> Event:
        return await self.respond(text, **kwargs)

    async def edit_text(self, text: str, **_: Any) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted = True


def make_event() -> Event:
    return Event()


def make_context(tmp_path: Any, name: str, config: dict[str, Any], **overrides: Any) -> Any:
    settings = Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )
    values: dict[str, Any] = {
        "logger": logging.getLogger(f"test.wiring.{name}"),
        "rate_limiter": RateLimiter(min_interval=0),
        "client": FakeClient(),
        "storage": Storage(),
        "settings": settings,
        "config": plugin_config(config, name),
        "is_owner": lambda sender_id: sender_id == 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Storage:
    """An in-memory history, since the real one needs a connection to open.

    Deliberately minimal: this file is about the HTTP boundary, and a storage
    double that pretends to be SQL would only hide the next thing.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, str]] = []

    async def execute(self, sql: str, parameters: Any = ()) -> int:
        verb = sql.strip().split(None, 1)[0].upper()
        if verb == "DELETE":
            self.rows.clear()
        elif verb == "INSERT":
            self.rows.append((len(self.rows) + 1, *tuple(parameters)))
        return 0

    async def fetchall(self, sql: str, parameters: Any = ()) -> list[dict[str, Any]]:
        if "FROM history" in sql:
            return [{"id": row[0], "role": row[1], "parts": row[2]} for row in reversed(self.rows)]
        return []


class Command:
    """Just enough of a CommandContext to deliver a reply."""

    def __init__(self, args: str, event: Any) -> None:
        self.name = "ai"
        self.args = args
        self.raw = f"/ub ai {args}"
        self.event = event
        self.replies: list[str] = []

    async def respond(self, text: str, **_: Any) -> None:
        self.replies.append(text)
        await self.event.respond(text)


def wire_ai(tmp_path: Any, config: dict[str, Any], handler: Any) -> Any:
    """The plugin with a real router per role, sharing one mock transport.

    The chain comes from the configuration, the way a real deployment gets it,
    rather than being handed in -- otherwise the wiring under test is not the
    wiring that ships.
    """
    _loaded, module = shipped_module("ai")
    plugin = module.Plugin()
    plugin.ctx = make_context(tmp_path, "ai", config)
    plugin.routers = {
        kind: real_router(module._chain(plugin.ctx, kind), handler) for kind in module.ROLES
    }
    return module, plugin


#: A default model whose allowance is spent, with one working model behind it.
#: Configured the way a real deployment configures it, so the chain under test
#: is the chain that ships.
SPENT = {
    "chat_model": "dead",
    "code_model": "dead",
    "model_fallbacks": ["alive"],
}


# --- the break, as it would have shipped ------------------------------------


async def test_chat_survives_the_real_router(tmp_path: Any) -> None:
    """``ask`` was handing the router a model the router was already choosing.

    The router binds ``model`` per attempt in order to decide the fallback, so a
    second one is not a preference, it is a ``TypeError``. The fake client in the
    other tests swallowed it, which is precisely why the suite stayed green.
    """
    handler, _seen = answering("Привет")
    _module, plugin = wire_ai(tmp_path, {"chat_model": "gemini-3.8-flash"}, handler)
    event = make_event()

    await plugin.ask(plugin.ctx, Command("привет", event), "привет", kind="chat")

    assert any("Привет" in text for text in event.responses), (
        f"the answer never arrived: {event.responses!r}"
    )


async def test_plugin_generation_survives_the_real_router(tmp_path: Any) -> None:
    """The same mistake in the code path, which had it in two places."""
    handler, seen = answering("сделано")
    _module, plugin = wire_ai(tmp_path, {"code_model": "gemini-3.8-flash"}, handler)
    event = make_event()
    command = Command("new demo сделать", event)

    # The canned reply is not a valid plugin, so generation says so and writes
    # nothing. What must not happen is failing earlier, on the call itself.
    await plugin.generate(plugin.ctx, command, "demo сделать")

    assert [model_of(r) for r in seen] == ["gemini-3.8-flash"], seen
    assert command.replies, "the user was told nothing at all"
    assert not any("multiple values" in t for t in command.replies), command.replies


async def test_summary_survives_the_real_router(tmp_path: Any) -> None:
    """``sum`` too, for the same reason, in case the shape changes again."""
    _loaded, module = shipped_module("sum")
    plugin = module.Plugin()
    ctx = make_context(tmp_path, "sum", {"chat_model": "gemini-3.8-flash"})
    plugin.ctx = ctx
    handler, _seen = answering("Коротко: поговорили.")
    plugin.router = real_router(["gemini-3.8-flash"], handler)

    _loaded_sum, collect_module = shipped_submodule("sum", "_collect")
    collected = collect_module.Collected(transcript="— привет\n— пока", message_count=2)
    text = await plugin.summarise(ctx, collected)

    assert "поговорили" in text, text


# --- a role's own model leads its own chain ---------------------------------


async def test_chat_and_code_use_their_own_configured_model(tmp_path: Any) -> None:
    """The two roles are configured separately, so they are routed separately.

    Collapsing them onto one router would answer code requests with the chat
    model and quietly ignore ``code_model``.
    """
    _loaded, module = shipped_module("ai")
    ctx = make_context(
        tmp_path,
        "ai",
        {
            "chat_model": "gemini-3.7-flash",
            "code_model": "gemini-3.6-flash",
            "model_fallbacks": ["gemini-3.5-flash-lite"],
        },
    )

    assert module._chain(ctx, "chat") == [
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
    ], module._chain(ctx, "chat")
    assert module._chain(ctx, "code") == [
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
    ], module._chain(ctx, "code")

    # And each chain really requests its own head.
    handler, seen = answering()
    for kind, expected in (("chat", "gemini-3.7-flash"), ("code", "gemini-3.6-flash")):
        seen.clear()
        router = real_router(module._chain(ctx, kind), handler)
        await router.generate([Turn("user", [])])
        assert model_of(seen[0]) == expected, f"{kind}: {seen[0].url.path}"


# --- fallback, end to end, with no doubles in the middle --------------------


async def test_a_spent_quota_reaches_the_user_from_the_fallback(tmp_path: Any) -> None:
    """The whole point, run the way it will actually run.

    The default model's daily allowance is spent -- which is the real state of
    the free tier right now -- and the user must still get an answer, and a line
    saying which model is speaking.
    """
    handler, seen = answering("Привет из резервной модели", dead="dead")
    _module, plugin = wire_ai(tmp_path, dict(SPENT), handler)
    event = make_event()

    await plugin.ask(plugin.ctx, Command("привет", event), "привет", kind="chat")

    assert any("Привет из резервной модели" in t for t in event.responses), event.responses
    assert any("alive" in t for t in event.responses), (
        f"the user was not told which model answered: {event.responses!r}"
    )
    assert [model_of(r) for r in seen][:2] == ["dead", "alive"], "the chain should move on"


async def test_a_spent_quota_does_not_return_on_the_next_question(tmp_path: Any) -> None:
    """The dead model is probed once, then remembered as dead."""
    handler, seen = answering("да", dead="dead")
    _module, plugin = wire_ai(tmp_path, dict(SPENT), handler)

    for _ in range(2):
        await plugin.routers["chat"].generate([Turn("user", [])])

    assert [model_of(r) for r in seen] == ["dead", "alive", "alive"], (
        f"the dead model was probed again: {[model_of(r) for r in seen]}"
    )


def refusing() -> Any:
    """A transport that refuses every model, as a spent key would."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=QUOTA_BODY)

    return handler


async def test_ai_logs_a_refusal_it_shows_the_user(tmp_path: Any, caplog: Any) -> None:
    """The reason belongs in the log as well as in Telegram.

    A refusal is shown to the user, which is right, and then discarded: nothing
    reaches the journal, so the next "why is it failing" has to be answered by
    asking the user to screenshot the chat. That is what made a broken attachment
    download so hard to find -- the visible error blamed the model while the
    actual defect sat in the log under an unrelated traceback.
    """
    _module, plugin = wire_ai(tmp_path, dict(SPENT), refusing())
    event = make_event()
    command = Command("привет", event)

    with caplog.at_level(logging.WARNING):
        await plugin.handle(command)

    assert event.responses, "the user was left with nothing at all"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "quota" in logged.lower(), f"the reason was not logged: {logged!r}"
    assert "dead" in logged and "alive" in logged, (
        f"the models that were tried are not recoverable from the log: {logged!r}"
    )


async def test_sum_logs_a_refusal_it_shows_the_user(tmp_path: Any, caplog: Any) -> None:
    """The same swallow, in the other plugin."""
    _loaded, module = shipped_module("sum")
    plugin = module.Plugin()
    telegram = FakeClient()
    # `message`, not `text`: that is the attribute telethon messages carry, and
    # reading anything else here would quietly collect nothing. The ids are above
    # the command's own id, which `collect` rightly skips.
    telegram.messages = [
        SimpleNamespace(id=10, sender_id=2, out=False, message="второе", action=None),
        SimpleNamespace(id=11, sender_id=2, out=False, message="третье", action=None),
    ]
    ctx = make_context(tmp_path, "sum", dict(SPENT), client=telegram)
    plugin.ctx = ctx
    plugin.router = real_router(module._chain(ctx), refusing())
    event = make_event()

    with caplog.at_level(logging.WARNING):
        await plugin.handle(Command("5", event))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "quota" in logged.lower(), f"the reason was not logged: {logged!r}"
    assert "dead" in logged, f"the exhausted model is not named: {logged!r}"


async def test_a_healthy_request_logs_nothing(tmp_path: Any, caplog: Any) -> None:
    """Otherwise the warnings become noise and everyone learns to skip them."""
    handler, _seen = answering("Привет")
    _module, plugin = wire_ai(tmp_path, {"chat_model": "gemini-3.8-flash"}, handler)
    event = make_event()

    with caplog.at_level(logging.WARNING):
        await plugin.ask(plugin.ctx, Command("привет", event), "привет", kind="chat")

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_non_quota_refusal_is_logged_too(tmp_path: Any, caplog: Any) -> None:
    """The router only logs what it retries; anything else it passes through.

    A 400, a bad key, a prompt the model refuses: the user is told, the log stays
    empty, and the next "it says something went wrong" has no answer at all.
    """

    def bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "API key not valid", "code": 400}})

    _module, plugin = wire_ai(tmp_path, {"chat_model": "gemini-3.8-flash"}, bad_request)
    event = make_event()

    with caplog.at_level(logging.WARNING):
        await plugin.handle(Command("привет", event))

    assert event.responses, "the user was left with nothing at all"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "not valid" in logged, f"the reason was not logged: {logged!r}"


# --- the options in the manifests must still be read -----------------------


async def test_a_configured_timeout_survives_the_rewrite(tmp_path: Any) -> None:
    """``timeout_seconds`` is in both manifests, so it has to be read.

    Building the client inside the router dropped it, which left the setting in
    the file doing nothing and quietly put ``sum`` -- which allows 180 seconds
    because it carries a transcript and media -- back to the 120 second default.
    """
    # setup() registers the command, which needs a real context; here the point
    # is the router it builds, so a no-op registration is the whole of it.
    silent = {"register_command": lambda *args, **kwargs: None}

    for name, default in (("ai", 120), ("sum", 180)):
        _loaded, module = shipped_module(name)
        plugin = module.Plugin()
        ctx = make_context(tmp_path, name, {"timeout_seconds": 300}, **silent)
        await plugin.setup(ctx)
        routers = list(plugin.routers.values()) if hasattr(plugin, "routers") else [plugin.router]
        for router in routers:
            assert router.timeout == 300, f"{name}: {router.timeout}"
        await plugin.stop()

        # And the shipped default, which differs per plugin on purpose.
        plugin2 = module.Plugin()
        await plugin2.setup(make_context(tmp_path, name, {}, **silent))
        routers2 = (
            list(plugin2.routers.values()) if hasattr(plugin2, "routers") else [plugin2.router]
        )
        assert routers2[0].timeout == default, f"{name}: {routers2[0].timeout}"
        await plugin2.stop()
