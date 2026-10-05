"""Tests for the Gemini client.

The wire formats here are not invented: they were captured from the live API
while building this, and each one contradicts what the documentation implies.
The routine 503s, the ``alt=sse`` requirement, the ``thoughtSignature`` echo and
the absence of ``streamGenerateContent`` from the model list are all reproduced
as fixtures, so a refactor cannot quietly break them.

HTTP goes through ``httpx.MockTransport`` rather than a hand-rolled fake, so the
request the client actually builds is the request that gets asserted on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

KEY = "test-key-abcdefghijklmnop"


@pytest.fixture(scope="module")
def client() -> Any:
    """The shared Gemini client, now part of the core rather than one plugin.

    It is imported rather than loaded through the plugin loader because two
    plugins use it now, and a test that went through one of them would fail for
    no reason when the other changed.
    """
    from userbot import gemini

    return gemini


@pytest.fixture
def run() -> Callable[[Any], Any]:
    """Run a coroutine or collect an async generator, and return the result."""

    def _run(awaitable: Any) -> Any:
        async def collect() -> Any:
            if hasattr(awaitable, "__aiter__"):
                return [item async for item in awaitable]
            return await awaitable

        return asyncio.run(collect())

    return _run


def build(client: Any, handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> Any:
    """A client whose HTTP goes through the mock transport."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client.GeminiClient(api_keys=[KEY], client=http, **kwargs)


def user_turn(client: Any, text: str = "hi") -> Any:
    return client.Turn("user", [client.Part(text)])


def turns(client: Any, text: str = "hi") -> list[Any]:
    return [user_turn(client, text)]


async def drain(stream: AsyncIterator[str]) -> list[str]:
    return [piece async for piece in stream]


# --- key handling -----------------------------------------------------------


def test_keys_are_split_and_trimmed(client: Any) -> None:
    assert client.parse_keys("a, b ,c,") == ["a", "b", "c"]
    assert client.parse_keys("") == []
    assert client.parse_keys(None) == []


def test_no_key_means_a_clear_error_not_a_401(client: Any) -> None:
    """Better to say which variable is missing than to send an empty key."""
    instance = client.GeminiClient(api_keys=[])
    assert instance.has_key is False
    with pytest.raises(client.MissingKeyError, match="TGUSERBOT_GEMINI_API_KEY"):
        instance._next_key()


def test_keys_rotate_between_calls(client: Any) -> None:
    instance = client.GeminiClient(api_keys=["one", "two"])
    assert [instance._next_key() for _ in range(5)] == [
        "one",
        "two",
        "one",
        "two",
        "one",
    ]


def test_the_key_travels_in_a_header_not_the_url(client: Any) -> None:
    instance = client.GeminiClient(api_keys=[KEY])
    assert instance._headers(KEY)["x-goog-api-key"] == KEY
    assert KEY not in instance.base_url


def test_key_count_is_reported(client: Any) -> None:
    assert client.GeminiClient(api_keys=[KEY, "b"]).key_count == 2
    assert client.GeminiClient(api_keys=[]).key_count == 0


# --- request building -------------------------------------------------------


def test_the_body_carries_contents_and_config(client: Any) -> None:
    body = client.GeminiClient._body(turns(client), system="be brief", max_output_tokens=100)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert body["generationConfig"]["maxOutputTokens"] == 100
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}


def test_structured_output_is_requested_through_the_config(client: Any) -> None:
    """Asking for JSON in the prompt is not the same as constraining the model to it."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    body = client.GeminiClient._body(turns(client), response_schema=schema)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"] is schema


def test_temperature_is_omitted_unless_asked(client: Any) -> None:
    assert "temperature" not in client.GeminiClient._body(turns(client))["generationConfig"]
    warm = client.GeminiClient._body(turns(client), temperature=0.4)
    assert warm["generationConfig"]["temperature"] == 0.4


def test_a_thought_signature_is_carried_back_to_the_api(client: Any) -> None:
    """Verified against the live API: dropping it degrades the next turn."""
    turn = client.Turn("model", [client.Part("hi", thought_signature="SIG==")])
    assert turn.to_wire()["parts"][0] == {"text": "hi", "thoughtSignature": "SIG=="}
    # And it survives a round trip through storage, which is where it would
    # otherwise be lost between messages.
    assert client.Turn.from_wire(turn.to_wire()).parts[0].thought_signature == "SIG=="


def test_a_part_without_a_signature_sends_no_empty_key(client: Any) -> None:
    assert client.Part("hi").to_wire() == {"text": "hi"}


def test_a_turns_text_concatenates_its_parts(client: Any) -> None:
    turn = client.Turn("model", [client.Part("a"), client.Part("b")])
    assert turn.text == "ab"


# --- reply parsing ----------------------------------------------------------

#: Captured from gemini-3.8-flash answering "Say OK". Note thoughtsTokenCount: 97
#: against candidatesTokenCount: 1 -- almost the whole budget went on thinking.
REAL_REPLY: dict[str, Any] = {
    "candidates": [
        {
            "content": {
                "parts": [{"text": "OK", "thoughtSignature": "Et0DCtoDAWkUfRMpnAe5WwP..."}],
                "role": "model",
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 6,
        "candidatesTokenCount": 1,
        "totalTokenCount": 104,
        "thoughtsTokenCount": 97,
        "serviceTier": "standard",
    },
    "modelVersion": "gemini-3.8-flash",
}


def test_a_real_reply_is_parsed(client: Any) -> None:
    reply = client.GeminiClient.parse_reply(REAL_REPLY)
    assert reply.text == "OK"
    assert reply.finish_reason == "STOP"
    assert (reply.input_tokens, reply.output_tokens, reply.thoughts_tokens) == (6, 1, 97)
    assert reply.total_tokens == 104
    assert reply.truncated is False


def test_thinking_tokens_are_counted_but_not_billed_as_output(client: Any) -> None:
    """The account is charged for candidate tokens, not thinking tokens."""
    reply = client.GeminiClient.parse_reply(REAL_REPLY)
    assert reply.output_tokens == 1, "a 97-token thought must not show as output"
    assert reply.thoughts_tokens == 97


def test_a_thought_only_reply_does_not_crash(client: Any) -> None:
    """Possible while the model is still thinking."""
    reply = client.GeminiClient.parse_reply(
        {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    )
    assert reply.text == ""
    assert reply.truncated is True


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "MAX_TOKENS_TRUNCATED"])
def test_truncation_is_reported(client: Any, reason: str) -> None:
    reply = client.GeminiClient.parse_reply(
        {"candidates": [{"content": {"parts": [{"text": "cut"}]}, "finishReason": reason}]}
    )
    assert reply.truncated is True


def test_an_empty_candidate_list_raises(client: Any) -> None:
    with pytest.raises(client.GeminiError, match="пустой"):
        client.GeminiClient.parse_reply({"candidates": []})


def test_a_blocked_prompt_names_the_reason(client: Any) -> None:
    with pytest.raises(client.GeminiError, match="SAFETY"):
        client.GeminiClient.parse_reply(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )


def test_a_malformed_part_is_skipped_not_fatal(client: Any) -> None:
    turn = client.Turn.from_wire(
        {"role": "model", "parts": ["junk", {"no": "text"}, {"text": "ok"}]}
    )
    assert turn.text == "ok"


# --- errors and retries -----------------------------------------------------


def test_a_503_is_retried_then_succeeds(client: Any, run: Any) -> None:
    """The first call of a session failing on capacity is routine, not fatal."""
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(
                503,
                json={"error": {"message": "This model is currently experiencing high demand."}},
            )
        return httpx.Response(200, json=REAL_REPLY)

    instance = build(client, handler, backoff=0.001)
    assert run(instance.generate(turns(client))).text == "OK"
    assert len(attempts) == 3


def test_a_persistent_503_gives_up_with_the_api_message(client: Any, run: Any) -> None:
    instance = build(
        client,
        lambda _r: httpx.Response(503, json={"error": {"message": "high demand"}}),
        attempts=2,
        backoff=0.001,
    )
    with pytest.raises(client.GeminiError, match="high demand"):
        run(instance.generate(turns(client)))


def test_a_403_is_not_retried(client: Any, run: Any) -> None:
    """An invalid key will still be invalid in four seconds."""
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"error": {"message": "API key not valid"}})

    instance = build(client, handler, backoff=0.001)
    with pytest.raises(client.GeminiError, match="not valid") as info:
        run(instance.generate(turns(client)))
    assert info.value.status == 403
    assert info.value.retryable is False
    assert len(calls) == 1


def test_a_429_is_retried(client: Any, run: Any) -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=REAL_REPLY)

    instance = build(client, handler, backoff=0.001)
    assert run(instance.generate(turns(client))).text == "OK"
    assert len(calls) == 2


def test_a_network_failure_is_reported_as_such(client: Any, run: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    instance = build(client, handler, attempts=2, backoff=0.001)
    with pytest.raises(client.GeminiError, match="сеть"):
        run(instance.generate(turns(client)))


def test_the_api_message_is_shown_verbatim(client: Any, run: Any) -> None:
    """A paraphrase of the error is worse than the error."""
    instance = build(
        client,
        lambda _r: httpx.Response(400, json={"error": {"message": "Token limit exceeded"}}),
    )
    with pytest.raises(client.GeminiError, match="Token limit exceeded"):
        run(instance.generate(turns(client)))


def test_a_non_json_error_body_does_not_crash(client: Any, run: Any) -> None:
    instance = build(client, lambda _r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(client.GeminiError):
        run(instance.generate(turns(client)))


# --- the key must never leak ------------------------------------------------


def test_the_key_never_appears_in_an_error_message(client: Any, run: Any) -> None:
    """The most likely way a key escapes: an error body that echoes it back."""
    instance = build(
        client, lambda _r: httpx.Response(400, json={"error": {"message": f"key {KEY} is bad"}})
    )
    with pytest.raises(client.GeminiError) as info:
        run(instance.generate(turns(client)))
    assert KEY not in str(info.value)
    assert "<redacted>" in str(info.value)


def test_redaction_handles_several_keys(client: Any) -> None:
    assert client._redact("a SECRET b", ["SECRET"]) == "a <redacted> b"
    assert client._redact("nothing here", []) == "nothing here"
    assert client._redact("x", [""]) == "x"


def test_the_key_is_not_logged_on_retry(client: Any, run: Any, caplog: Any) -> None:
    instance = build(
        client,
        lambda _r: httpx.Response(503, json={"error": {"message": "busy"}}),
        attempts=2,
        backoff=0.001,
    )
    with caplog.at_level("DEBUG", logger="userbot.gemini"):
        with pytest.raises(client.GeminiError):
            run(instance.generate(turns(client)))
    assert KEY not in caplog.text


# --- streaming --------------------------------------------------------------


def sse(*chunks: dict[str, Any]) -> bytes:
    """The ``alt=sse`` framing the live endpoint actually returns."""
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()


def chunk(text: str) -> dict[str, Any]:
    return {"candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}]}


def test_streaming_requires_alt_sse(client: Any, run: Any) -> None:
    """Without it the endpoint sends one pretty-printed array at the end."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=sse(chunk("a"), chunk("b")))

    instance = build(client, handler)
    assert run(instance.stream(turns(client))) == ["a", "b"]
    assert seen[0].url.params["alt"] == "sse"


def test_stream_deltas_concatenate_to_the_answer(client: Any, run: Any) -> None:
    instance = build(
        client, lambda _r: httpx.Response(200, content=sse(chunk("1, "), chunk("2, "), chunk("3")))
    )
    assert "".join(run(instance.stream(turns(client)))) == "1, 2, 3"


def test_a_malformed_stream_line_does_not_kill_the_stream(client: Any, run: Any) -> None:
    instance = build(
        client, lambda _r: httpx.Response(200, content=b"data: {not json}\n\n" + sse(chunk("ok")))
    )
    assert run(instance.stream(turns(client))) == ["ok"]


def test_a_stream_503_is_retried(client: Any, run: Any) -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, content=sse(chunk("ok")))

    instance = build(client, handler, backoff=0.001)
    assert run(instance.stream(turns(client))) == ["ok"]
    assert len(calls) == 2


def test_a_stream_error_raises_with_the_api_message(client: Any, run: Any) -> None:
    """A wrong model id must name itself, not arrive as an empty answer."""
    instance = build(
        client, lambda _r: httpx.Response(404, json={"error": {"message": "model not found"}})
    )
    with pytest.raises(client.GeminiError, match="not found"):
        run(instance.stream(turns(client)))


def test_an_empty_stream_yields_nothing(client: Any, run: Any) -> None:
    instance = build(client, lambda _r: httpx.Response(200, content=b""))
    assert run(instance.stream(turns(client))) == []


def finish_chunk(text: str, reason: str) -> dict[str, Any]:
    """A chunk that carries text *and* the reason the model stopped."""
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": reason,
            }
        ]
    }


def test_a_truncated_stream_says_so(client: Any, run: Any) -> None:
    """The reason a stream ended was thrown away, so a cut answer looked complete.

    ``_iter_sse`` pulled the text out of each chunk and ignored ``finishReason``
    entirely. Gemini ends a stream that hit ``maxOutputTokens`` the same way it
    ends a finished one -- the last chunk simply carries ``MAX_TOKENS`` -- so the
    user received half an answer with nothing marking the cut, and no way to tell
    it from a complete one. The client now records the reason it was given.
    """
    instance = build(
        client,
        lambda _r: httpx.Response(
            200, content=sse(chunk("начало"), finish_chunk(" конец", "MAX_TOKENS"))
        ),
    )
    assert run(instance.stream(turns(client))) == ["начало", " конец"]
    assert instance.last_finish_reason == "MAX_TOKENS"
    assert instance.truncated is True


def test_a_complete_stream_is_not_truncated(client: Any, run: Any) -> None:
    """The other side of the same assertion, so it cannot pass vacuously."""
    instance = build(client, lambda _r: httpx.Response(200, content=sse(chunk("готово"))))
    run(instance.stream(turns(client)))
    assert instance.truncated is False


def test_a_stream_that_never_says_why_it_stopped_is_not_a_failure(client: Any, run: Any) -> None:
    """Chunks without a finishReason are the normal case, not an error."""
    instance = build(client, lambda _r: httpx.Response(200, content=sse(chunk("ok"))))
    assert run(instance.stream(turns(client))) == ["ok"]
    assert instance.last_finish_reason == ""
    assert instance.truncated is False


# --- model listing ----------------------------------------------------------


def test_models_are_parsed_from_the_live_shape(client: Any) -> None:
    models = client.GeminiClient.parse_models(
        {
            "models": [
                {
                    "name": "models/gemini-3.8-flash",
                    "displayName": "Gemini 3.8 Flash",
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 65536,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                },
                {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": []},
            ]
        }
    )
    assert [m.name for m in models] == ["gemini-2.0-flash", "gemini-3.8-flash"]
    assert models[1].display_name == "Gemini 3.8 Flash"
    assert models[1].input_limit == 1048576


def test_a_model_with_no_name_is_skipped(client: Any) -> None:
    assert client.GeminiClient.parse_models({"models": [{"nope": 1}, "junk"]}) == []


def test_list_models_uses_a_get(client: Any, run: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"models": []})

    assert run(build(client, handler).list_models()) == []
    assert seen[0].method == "GET"


def test_list_models_reports_an_error(client: Any, run: Any) -> None:
    instance = build(client, lambda _r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(client.GeminiError, match="bad key"):
        run(instance.list_models())


# --- backoff ----------------------------------------------------------------


def test_backoff_is_capped(client: Any) -> None:
    """The cap is what stops a retry loop becoming a long sleep."""
    instance = client.GeminiClient(api_keys=[KEY], backoff=2.0, backoff_cap=5.0)
    assert 0 < instance._delay(0) < 5
    assert instance._delay(4) <= 5.0


def test_backoff_varies_between_attempts(client: Any) -> None:
    """No jitter means synchronised retries, which is worse on capacity errors."""
    instance = client.GeminiClient(api_keys=[KEY], backoff=2.0, backoff_cap=100.0)
    assert len({round(instance._delay(1), 6) for _ in range(5)}) > 1


# --- keys from the environment ---------------------------------------------


def test_keys_come_from_the_environment(client: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("TB_TEST_KEYS", "a, b")
    assert client.keys_from_env("TB_TEST_KEYS") == ["a", "b"]


def test_the_first_non_empty_variable_wins(client: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("TB_TEST_A", "")
    monkeypatch.setenv("TB_TEST_B", "only")
    assert client.keys_from_env("TB_TEST_A", "TB_TEST_B") == ["only"]


def test_no_variable_set_means_no_keys(client: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("TB_TEST_MISSING", raising=False)
    assert client.keys_from_env("TB_TEST_MISSING") == []
