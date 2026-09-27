"""Tests for switching models when one runs out of quota.

Measured against the live API, this is not hypothetical. The free-tier daily
quota on the configured default had run out, and the failure mode was bad in two
ways at once:

* ``429`` was classified as retryable, so the client spent four attempts with
  backoff on a condition that cannot recover before midnight. "Exceeded your
  current quota, check your plan and billing" is not a rate limit and retrying it
  only delays the real answer by a minute.
* There was nowhere to go next. One model was configured, so a dead quota meant no
  answer at all.

The chain fixes both, and remembers which model worked so the dead one is not
tried again on every subsequent question.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from userbot import gemini

KEY = "test-key-abcdefghijklmnop"

#: The body Gemini returns for a spent daily quota, captured live.
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

#: A per-minute rate limit: this one does clear, so it must be retried.
RATE_BODY = {
    "error": {
        "code": 429,
        "message": "Resource has been exhausted (e.g. check quota).",
        "status": "RESOURCE_EXHAUSTED",
    }
}


def build(module: Any, handler: Any, **kwargs: Any) -> tuple[Any, Any]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return module.GeminiClient(api_keys=[KEY], client=http, backoff=0.001, **kwargs), calls


def ok(text: str = "ок") -> Any:
    return lambda _r: httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
    )


def quota(_r: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json=QUOTA_BODY)


# --- the two 429s are not the same thing ------------------------------------


def test_a_spent_quota_is_not_retryable() -> None:
    """Four attempts with backoff on a midnight-bound condition is just a wait."""
    instance, calls = build(gemini, quota, attempts=4)
    with pytest.raises(gemini.QuotaExhausted):
        asyncio.run(instance.generate([gemini.Turn("user", [gemini.Part("привет")])]))
    assert len(calls) == 1, "a spent quota must not be retried"


def test_a_rate_limit_still_is() -> None:
    """This one clears, so it belongs in the retry path."""
    instance, calls = build(gemini, quota, attempts=2)
    calls_count = {"n": 0}

    def rate_then_ok(request: httpx.Request) -> httpx.Response:
        calls_count["n"] += 1
        if calls_count["n"] == 1:
            return httpx.Response(429, json=RATE_BODY)
        return ok()(request)

    instance, calls = build(gemini, rate_then_ok, attempts=3)
    reply = asyncio.run(instance.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert reply.text == "ок"
    assert len(calls) == 2


def test_the_two_conditions_differ() -> None:
    quota_error = gemini.GeminiClient._describe_error(429, QUOTA_BODY)
    rate_error = gemini.GeminiClient._describe_error(429, RATE_BODY)
    assert isinstance(quota_error, gemini.QuotaExhausted)
    assert not isinstance(rate_error, gemini.QuotaExhausted)
    assert rate_error.retryable is True


def test_a_billing_message_also_counts() -> None:
    """Billing is the same condition with different words."""
    body = {"error": {"code": 429, "message": "Check billing for project foo"}}
    assert isinstance(gemini.GeminiClient._describe_error(429, body), gemini.QuotaExhausted)


def test_a_free_tier_message_counts_too() -> None:
    body = {"error": {"code": 429, "message": "Free tier quota exceeded for this model"}}
    assert isinstance(gemini.GeminiClient._describe_error(429, body), gemini.QuotaExhausted)


# --- the fallback chain -----------------------------------------------------


def test_the_chain_tries_the_next_model() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        seen.append(model)
        if model == "dead":
            return quota(request)
        return ok("живой")(request)

    chain = gemini.ModelRouter(["dead", "alive"], client=build(gemini, handler)[0])
    reply = asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert reply.text == "живой"
    assert seen == ["dead", "alive"]


def test_the_working_model_is_remembered() -> None:
    """Otherwise every question re-probes the dead model and waits for a 429."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        seen.append(model)
        return quota(request) if model == "dead" else ok()(request)

    chain = gemini.ModelRouter(["dead", "alive"], client=build(gemini, handler)[0])
    run = lambda: chain.generate([gemini.Turn("user", [gemini.Part("x")])])  # noqa: E731
    asyncio.run(run())
    asyncio.run(run())
    # The dead model is probed once, the working one answers both times.
    assert seen == ["dead", "alive", "alive"], seen
    assert chain.active == "alive"


def test_the_dead_model_is_put_back_only_after_everyone_fails() -> None:
    """If the quota resets, the chain should notice rather than stay degraded."""

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        return ok(model)(request) if model == "dead" else quota(request)

    chain = gemini.ModelRouter(["dead", "alive"], client=build(gemini, handler)[0])
    run = lambda: chain.generate([gemini.Turn("user", [gemini.Part("x")])])  # noqa: E731
    assert asyncio.run(run()).text == "dead"
    # The remembered model is exhausted, so the chain probes from the top again.
    assert chain.active == "dead"


def test_every_model_exhausted_reports_the_last_reason() -> None:
    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, quota)[0])
    with pytest.raises(gemini.QuotaExhausted) as info:
        asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert "a" in str(info.value) and "b" in str(info.value), (
        "the reply must name the models that were tried"
    )


def test_a_non_quota_error_stops_the_chain() -> None:
    """A 404 for one model is not a reason to try a different one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return handler(request)

    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, recording)[0])
    with pytest.raises(gemini.GeminiError, match="not found"):
        asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert len(seen) == 1, "only a spent quota should move the chain on"


def test_a_streaming_request_switches_too() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = request.url.path.split("/models/")[1].split(":")[0]
        seen.append(model)
        if model == "dead":
            return quota(request)
        frame = 'data: {"candidates":[{"content":{"parts":[{"text":"поток"}]}}]}\n\n'
        return httpx.Response(200, content=frame.encode("utf-8"))

    chain = gemini.ModelRouter(["dead", "alive"], client=build(gemini, handler)[0])

    async def run() -> str:
        out = ""
        async for piece in chain.stream([gemini.Turn("user", [gemini.Part("x")])]):
            out += piece
        return out

    assert asyncio.run(run()) == "поток"
    assert seen == ["dead", "alive"]


# --- configuration ----------------------------------------------------------


def test_the_default_chain_survives_a_dead_default() -> None:
    """The configured default had its quota spent; the chain below it works."""
    from userbot.loader import PluginManifest

    root = Path(__file__).resolve().parent.parent
    for plugin in ("ai", "sum"):
        manifest = PluginManifest.from_path(root / "plugins" / plugin)
        chain = list(manifest.config["model_fallbacks"])
        assert manifest.config["chat_model"] in chain, plugin
        assert len(chain) >= 2, f"{plugin}: a single model is not a fallback"
        assert len(set(chain)) == len(chain), f"{plugin}: duplicate models waste a probe"


def test_the_default_chain_only_contains_models_that_exist() -> None:
    """A typo in the chain is a model that can never answer."""
    from userbot.loader import PluginManifest

    root = Path(__file__).resolve().parent.parent
    known = {"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"}
    for plugin in ("ai", "sum"):
        manifest = PluginManifest.from_path(root / "plugins" / plugin)
        unknown = set(manifest.config["model_fallbacks"]) - known
        assert not unknown, f"{plugin}: unknown models in the chain: {unknown}"


def test_the_router_reports_which_model_answered() -> None:
    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, ok())[0])
    asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert chain.active == "a"
    assert chain.degraded is False


def test_a_degraded_router_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/models/")[1].split(":")[0] == "a":
            return quota(request)
        return ok()(request)

    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, handler)[0])
    asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert chain.degraded is True
    assert "b" in chain.notice(), chain.notice()
    assert "a" in chain.notice(), "the user should know which model ran out"


def test_an_empty_chain_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="model"):
        gemini.ModelRouter([], client=build(gemini, ok())[0])


def test_duplicates_are_collapsed() -> None:
    """Probing the same dead model twice is a wasted request."""
    chain = gemini.ModelRouter(["a", "a", "b"], client=build(gemini, ok())[0])
    assert chain.models == ("a", "b")


# --- options that used to be honoured and quietly stopped being read ---------


def test_a_configured_timeout_reaches_the_client() -> None:
    """``timeout_seconds`` was in both manifests and read by the plugins.

    Building the client inside the router dropped it: the connection started
    using the default of 120 seconds, and ``sum`` with media -- which is allowed
    180 for a reason -- would start timing out mid-request with nothing in the
    configuration to say why.
    """
    router = gemini.ModelRouter(["a"], key_getter=lambda: [KEY], timeout=42)
    assert router.timeout == 42
    assert router._get_client().timeout == 42


def test_the_default_is_the_clients_own_default() -> None:
    """Not a second copy of 120 to drift out of step with the client."""
    router = gemini.ModelRouter(["a"], key_getter=lambda: [KEY])
    assert router._get_client().timeout == gemini.GeminiClient(api_keys=[KEY]).timeout
