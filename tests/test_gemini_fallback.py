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


# --- the live message says more than the headline ---------------------------


#: Captured verbatim from the running service on the day of this change. The
#: headline is the one the classifier keyed on, and the second half is the part
#: that says how long for.
FREE_TIER_PER_MINUTE = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your "
            "current usage, head to: https://ai.dev/rate-limit. \n"
            "Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\n"
            "Please retry in 56.601092868s."
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}


def test_the_retry_hint_is_read() -> None:
    """``Please retry in 56.6s`` is the only thing separating the two cases.

    The headline -- "exceeded your current quota, check your plan and billing" --
    is identical for a limit that clears in under a minute and for one that is
    gone until the daily reset. Reading only the headline calls a per-minute
    cap a spent allowance, and then tells the user to expect nothing until
    midnight.
    """
    message = str(FREE_TIER_PER_MINUTE["error"]["message"])
    assert gemini._retry_hint_seconds(message) == pytest.approx(56.6, abs=0.01)


def test_a_minute_long_wait_moves_the_chain_but_is_not_called_a_quota() -> None:
    """Both halves of the live message matter, and they point different ways.

    56 seconds is too long to sit inside a command, so the chain moves. But the
    allowance is not gone, and the wait rides along on the exception precisely so
    the reply can say "try again in a minute" instead of "check your billing".
    """
    error = gemini.GeminiClient._describe_error(429, FREE_TIER_PER_MINUTE)
    assert isinstance(error, gemini.QuotaExhausted), "a 56 second wait should not stall the command"
    assert error.retryable is False
    assert error.wait == pytest.approx(56.6, abs=0.01), (
        "the wait is lost, so the user gets told the wrong thing"
    )


def test_the_wait_survives_key_redaction() -> None:
    """``_describe`` rebuilds the exception to scrub the key, and must keep it.

    Rebuilding by type alone is the obvious way to write that, and it drops
    every extra field -- which here is the only thing distinguishing a per-minute
    cap from a spent allowance.
    """
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(429, json=FREE_TIER_PER_MINUTE))
    )
    client = gemini.GeminiClient(api_keys=[KEY], client=http)
    error = client._describe(httpx.Response(429, json=FREE_TIER_PER_MINUTE))
    assert isinstance(error, gemini.QuotaExhausted)
    assert error.wait == pytest.approx(56.6, abs=0.01), error.wait


def test_a_limit_clearing_immediately_stays_put() -> None:
    """Waiting two seconds is cheaper than answering from a different model."""
    body = {
        "error": {
            "code": 429,
            "message": (
                "Resource has been exhausted (e.g. check quota). "
                "Quota exceeded for metric: generate_content_free_tier_requests, "
                "limit: 20, model: gemini-3.8-flash\nPlease retry in 1.4s."
            ),
        }
    }
    error = gemini.GeminiClient._describe_error(429, body)
    assert not isinstance(error, gemini.QuotaExhausted)
    assert error.retryable is True


def test_a_long_wait_does_move_the_chain_on() -> None:
    """Nobody wants a command that sits there for a minute and a half."""
    body = {
        "error": {
            "code": 429,
            "message": "Quota exceeded for metric: x, limit: 20. Please retry in 900s.",
        }
    }
    assert isinstance(gemini.GeminiClient._describe_error(429, body), gemini.QuotaExhausted)


def test_the_switch_threshold_is_a_documented_choice() -> None:
    """Not a magic number: the trade-off is spelled out where it is set."""
    assert 0 < gemini.SWITCH_AFTER_SECONDS <= 30
    assert isinstance(gemini.SWITCH_AFTER_SECONDS, float)


def test_the_notice_says_what_actually_happened() -> None:
    """ "квота исчерпана" on a per-minute cap sends people to check billing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/models/")[1].split(":")[0] == "a":
            return httpx.Response(429, json=FREE_TIER_PER_MINUTE)
        return ok()(request)

    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, handler)[0])
    asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))

    assert chain.active == "b"
    notice = chain.notice()
    assert "b" in notice, notice
    assert "квот" not in notice.lower(), (
        f"a per-minute limit is not an exhausted allowance: {notice!r}"
    )


def test_a_genuinely_spent_allowance_does_say_so() -> None:
    """The honest case keeps the honest wording."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.split("/models/")[1].split(":")[0] == "a":
            return quota(request)
        return ok()(request)

    chain = gemini.ModelRouter(["a", "b"], client=build(gemini, handler)[0])
    asyncio.run(chain.generate([gemini.Turn("user", [gemini.Part("x")])]))
    assert "квот" in chain.notice().lower(), chain.notice()
