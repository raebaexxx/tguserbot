"""Every configurable role has to be routable.

`/ub ai pro` and `/ub ai fast` are in `USAGE`, in the docs, and in `MODELS`.
They stopped working when a single Gemini client was replaced by a router per
role and the list of roles was written out by hand:

    MODELS roles: ['chat', 'code', 'fast', 'pro']
    ROLES built:  ['chat', 'code']
    /ub ai pro -> "Ключ Gemini не задан. Добавьте TGUSERBOT_GEMINI_API_KEY…"

The key was present and working. `require_router` could not tell "no router for
this role" from "no key configured", so a programming mistake was reported to the
user as a configuration one — and the reply pointed them at a file that was
already correct.

Two things are asserted here: that the role list cannot drift from the
configuration, and that the two conditions are reported as what they are.

Run with: pytest tests/test_ai_roles.py -q --no-cov
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeEvent, plugin_config, shipped_module
from userbot.config import Settings
from userbot.rate_limit import RateLimiter

logging.basicConfig(level=logging.CRITICAL)


def settings_in(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        plugin_dir=tmp_path / "plugins",
        log_dir=tmp_path / "data" / "logs",
        api_id=1,
        api_hash="test",
    )


class Storage:
    """An in-memory history.

    The real one needs a connection opened before it answers, and opening one
    here would make this test about sqlite rather than about which roles exist.
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
    def __init__(self, args: str = "") -> None:
        self.name = "ai"
        self.args = args
        self.raw = f"/ub ai {args}"
        self.event = FakeEvent()
        self.replies: list[str] = []

    async def respond(self, text: str, **_: Any) -> None:
        self.replies.append(text)
        await self.event.respond(text)


def plugin_with_key(ai_module: Any, tmp_path: Path, monkeypatch: Any, **config: Any) -> Any:
    """A plugin set up as if a key were configured."""
    monkeypatch.setattr(ai_module, "keys_from_env", lambda *names: ["a-key"])
    monkeypatch.setattr(ai_module, "key_env_names", lambda ctx: ("TGUSERBOT_GEMINI_API_KEY",))
    plugin = ai_module.Plugin()
    plugin.ctx = SimpleNamespace(
        settings=settings_in(tmp_path),
        config=plugin_config(config, "ai"),
        storage=Storage(),
        rate_limiter=RateLimiter(min_interval=0),
        logger=logging.getLogger("test.ai"),
        register_command=lambda *a, **k: None,
    )
    asyncio.run(plugin.setup(plugin.ctx))
    return plugin


# --- the role list cannot drift from the configuration ----------------------


def test_every_configurable_role_gets_a_router(tmp_path: Any, monkeypatch: Any) -> None:
    """Derived, not written out.

    A hand-written list is exactly how two of the four roles ended up with no
    router while the configuration still advertised them.
    """
    _loaded, module = shipped_module("ai")
    plugin = plugin_with_key(module, tmp_path, monkeypatch)

    assert sorted(plugin.routers) == sorted(module.MODELS), (
        f"roles without a router: {sorted(set(module.MODELS) - set(plugin.routers))}"
    )


def test_the_role_list_is_derived_from_the_models(tmp_path: Any) -> None:
    """The relationship is the thing worth protecting, not the contents."""
    _loaded, module = shipped_module("ai")
    assert tuple(module.ROLES) == tuple(module.MODELS), (
        f"ROLES is a second copy of MODELS: {module.ROLES} vs {tuple(module.MODELS)}"
    )


# --- the two failures are told apart ----------------------------------------


def test_an_unknown_role_is_not_reported_as_a_missing_key(tmp_path: Any, monkeypatch: Any) -> None:
    """A bug in this plugin should not send the operator to a config file."""
    from userbot.gemini import MissingKeyError

    _loaded, module = shipped_module("ai")
    plugin = plugin_with_key(module, tmp_path, monkeypatch)

    with pytest.raises(Exception) as info:
        plugin.require_router("no-such-role")
    assert not isinstance(info.value, MissingKeyError), (
        f"an unknown role was blamed on the API key: {info.value!r}"
    )


def test_a_missing_key_is_still_reported_as_one(tmp_path: Any, monkeypatch: Any) -> None:
    """The fix must not turn every router problem into a key problem."""
    from userbot.gemini import MissingKeyError

    _loaded, module = shipped_module("ai")
    monkeypatch.setattr(module, "keys_from_env", lambda *names: [])
    plugin = module.Plugin()
    plugin.ctx = SimpleNamespace(
        settings=settings_in(tmp_path),
        config=plugin_config({}, "ai"),
        register_command=lambda *a, **k: None,
    )
    asyncio.run(plugin.setup(plugin.ctx))

    with pytest.raises(MissingKeyError, match="TGUSERBOT_GEMINI_API_KEY"):
        plugin.require_router("chat")


# --- and the advertised modes actually reach a model -----------------------


@pytest.mark.parametrize("mode", ["pro", "fast"])
def test_a_mode_word_does_not_report_a_missing_key(
    tmp_path: Any, monkeypatch: Any, mode: str
) -> None:
    """The regression, end to end through the command.

    The reply must not blame the key. What the model answers is not asserted
    here -- the router is a stand-in with no network -- only that the request
    gets as far as the router at all.
    """
    _loaded, module = shipped_module("ai")
    plugin = plugin_with_key(module, tmp_path, monkeypatch)
    asked: list[tuple[str, list[str]]] = []

    class Router:
        has_key = True
        models = ("gemini-test",)

        def notice(self) -> str:
            return ""

        def _get_client(self) -> Any:
            return self

        async def generate(self, turns: Any, **kwargs: Any) -> Any:
            asked.append(("generate", [t.role for t in turns]))
            return SimpleNamespace(text="ответ")

        async def stream(self, turns: Any, **kwargs: Any) -> Any:
            asked.append(("stream", [t.role for t in turns]))
            yield "ответ"

        async def aclose(self) -> None:
            return None

    plugin.routers = {kind: Router() for kind in module.ROLES}
    command = Command(f"{mode} скажи что-нибудь")

    asyncio.run(plugin.handle(command))

    joined = "\n".join(command.replies)
    assert "Ключ Gemini не задан" not in joined, (
        f"/ub ai {mode} blamed the key while it was configured: {joined!r}"
    )
    assert asked, f"/ub ai {mode} never reached a model; replies were {command.replies!r}"


def test_the_mode_model_is_the_configured_one(tmp_path: Any, monkeypatch: Any) -> None:
    """`pro_model` and `fast_model` are separate settings for a reason."""
    _loaded, module = shipped_module("ai")
    ctx = SimpleNamespace(
        config=plugin_config(
            {
                "pro_model": "gemini-3.1-pro-preview",
                "fast_model": "gemini-3.5-flash-lite",
                "model_fallbacks": ["gemini-3.7-flash"],
            },
            "ai",
        )
    )
    # The configured model leads its chain; the fallback is behind it, not in
    # front of it.
    assert module._chain(ctx, "pro") == ["gemini-3.1-pro-preview", "gemini-3.7-flash"]
    assert module._chain(ctx, "fast") == ["gemini-3.5-flash-lite", "gemini-3.7-flash"]
