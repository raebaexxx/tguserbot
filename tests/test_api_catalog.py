"""Tests for the key catalogue: ingest, capability heuristics, and ranking.

The catalogue is where the autopilot's promises live: flags are OR-ed across
posts (a later listing must not un-say an earlier "works"), cooldowns exclude
rather than deprioritise, and the ranking is pinned > provider priority >
verified > marked-working. Each of those is a decision, and each gets a test.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import shipped_submodule


@pytest.fixture(scope="module")
def api_modules() -> Any:
    loaded, _entry = shipped_submodule("api", "plugin")
    package = loaded.module.__name__
    namespace = SimpleNamespace(
        catalog=importlib.import_module(f"{package}._catalog"),
        channel=importlib.import_module(f"{package}._channel"),
    )
    yield namespace
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


@pytest.fixture
async def catalog(api_modules: Any, tmp_path: Any) -> Any:
    from userbot.storage import create_plugin_storage

    storage = await create_plugin_storage(tmp_path, "api")
    store = api_modules.catalog.Catalog(storage)
    await store.ensure()
    try:
        yield store
    finally:
        await storage.close()


def parsed(
    api_modules: Any,
    *,
    base: str = "https://api.groq.com/openai/v1",
    key: str = "key-1",
    working: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
    listed: tuple[str, ...] = (),
    post_date: str | None = "2026-10-16T07:06:00+00:00",
) -> Any:
    return api_modules.channel.ParsedPost(
        base_url=base,
        api_key=key,
        post_date=post_date,
        tier=None,
        working=tuple(api_modules.channel.ModelMention(name) for name in working),
        denied=tuple(api_modules.channel.ModelMention(name) for name in denied),
        listed=tuple(api_modules.channel.ModelMention(name) for name in listed),
        listed_truncated=False,
    )


# --- ingest -----------------------------------------------------------------


async def test_upsert_is_idempotent_and_flags_are_monotonic(api_modules: Any, catalog: Any) -> None:
    endpoint_id, created = await catalog.upsert_post(
        parsed(api_modules, working=("m-one",)), post_id=1
    )
    assert created is True
    again, created_again = await catalog.upsert_post(
        parsed(api_modules, listed=("m-one",)), post_id=2
    )
    assert again == endpoint_id
    assert created_again is False
    rows = await catalog.model_rows()
    assert len(rows) == 1
    assert rows[0]["working"] == 1
    assert rows[0]["listed"] == 1
    assert await catalog.endpoint_count() == 1


async def test_a_denied_model_stays_denied_and_is_never_a_candidate(
    api_modules: Any, catalog: Any
) -> None:
    await catalog.upsert_post(parsed(api_modules, working=("m-two",), denied=("m-two",)), post_id=1)
    candidates = await catalog.candidates("chat", provider_priority=(), pinned="")
    assert candidates == []


async def test_the_cursor_only_moves_forward(catalog: Any) -> None:
    await catalog.advance_cursor(10)
    await catalog.advance_cursor(4)
    assert await catalog.get_meta("cursor") == "10"
    await catalog.advance_cursor(11)
    assert await catalog.get_meta("cursor") == "11"


# --- capabilities -----------------------------------------------------------


def test_capability_heuristics_match_the_live_listings(api_modules: Any) -> None:
    capabilities = api_modules.catalog.capabilities
    assert capabilities("whisper-large-v3-turbo") == frozenset({"stt"})
    assert "vision" in capabilities("meta/llama-3.2-11b-vision-instruct")
    assert "chat" in capabilities("meta/llama-3.2-11b-vision-instruct")
    assert capabilities("agnes-image-2.0-flash") == frozenset({"image"})
    assert capabilities("agnes-video-2.5-flash") == frozenset({"video"})
    assert capabilities("mistral-ocr-4-1") == frozenset({"ocr"})
    assert capabilities("mistral-embed") == frozenset({"aux"})
    assert capabilities("canopylabs/orpheus-v1-english") == frozenset({"aux"})
    assert capabilities("meta-llama/llama-prompt-guard-2-86m") == frozenset({"aux"})
    # "safeguard" contains "guard", and gpt-oss-safeguard is a working chat
    # model on the live channel; the aux rule must not swallow it.
    assert capabilities("openai/gpt-oss-safeguard-20b") == frozenset({"chat"})


# --- ranking ----------------------------------------------------------------


async def test_provider_priority_orders_equal_candidates(api_modules: Any, catalog: Any) -> None:
    await catalog.upsert_post(
        parsed(api_modules, base="https://api.mistral.ai/v1", key="k-m", working=("m-mistral",)),
        post_id=1,
    )
    await catalog.upsert_post(parsed(api_modules, working=("m-groq",)), post_id=2)
    candidates = await catalog.candidates("chat", provider_priority=("groq", "mistral"), pinned="")
    assert [candidate.model for candidate in candidates] == ["m-groq", "m-mistral"]


async def test_a_pinned_model_comes_first_regardless_of_priority(
    api_modules: Any, catalog: Any
) -> None:
    await catalog.upsert_post(parsed(api_modules, working=("m-groq",)), post_id=1)
    await catalog.upsert_post(
        parsed(api_modules, base="https://api.mistral.ai/v1", key="k-m", working=("m-mistral",)),
        post_id=2,
    )
    candidates = await catalog.candidates(
        "chat", provider_priority=("groq", "mistral"), pinned="m-mistral"
    )
    assert candidates[0].model == "m-mistral"
    assert candidates[-1].model == "m-groq"


async def test_a_verified_endpoint_outranks_an_unverified_one(
    api_modules: Any, catalog: Any
) -> None:
    fresh, _ = await catalog.upsert_post(parsed(api_modules, working=("m-new",)), post_id=1)
    old, _ = await catalog.upsert_post(
        parsed(api_modules, key="key-2", working=("m-old",)), post_id=2
    )
    await catalog.mark_checked(old, alive=True, api_models=["m-old"])
    assert not await catalog.is_checked(fresh)
    candidates = await catalog.candidates("chat", provider_priority=("groq",), pinned="")
    assert [candidate.model for candidate in candidates] == ["m-old", "m-new"]


async def test_cooldowns_exclude_rather_than_deprioritise(api_modules: Any, catalog: Any) -> None:
    endpoint_id, _ = await catalog.upsert_post(
        parsed(api_modules, working=("m-a", "m-b")), post_id=1
    )
    await catalog.cooldown_model(endpoint_id, "m-a", 3600)
    candidates = await catalog.candidates("chat", provider_priority=(), pinned="")
    assert [candidate.model for candidate in candidates] == ["m-b"]
    await catalog.record_success(endpoint_id, "m-a")
    candidates = await catalog.candidates("chat", provider_priority=(), pinned="")
    assert {candidate.model for candidate in candidates} == {"m-a", "m-b"}
    await catalog.cooldown_endpoint(endpoint_id, 3600)
    assert await catalog.candidates("chat", provider_priority=(), pinned="") == []


async def test_roles_filter_by_capability(api_modules: Any, catalog: Any) -> None:
    await catalog.upsert_post(
        parsed(api_modules, working=("m-chat", "whisper-x", "vision-x")), post_id=1
    )
    chat = await catalog.candidates("chat", provider_priority=(), pinned="")
    assert {candidate.model for candidate in chat} == {"m-chat", "vision-x"}
    stt = await catalog.candidates("stt", provider_priority=(), pinned="")
    assert {candidate.model for candidate in stt} == {"whisper-x"}


async def test_model_and_provider_lookups(api_modules: Any, catalog: Any) -> None:
    await catalog.upsert_post(parsed(api_modules, working=("m-known",)), post_id=1)
    assert await catalog.model_exists("m-known")
    assert not await catalog.model_exists("m-unknown")
    assert await catalog.provider_exists("groq")
    assert not await catalog.provider_exists("nothing")


async def test_endpoint_models_scores_working_and_starred_first(
    api_modules: Any, catalog: Any
) -> None:
    mention = api_modules.channel.ModelMention
    post = api_modules.channel.ParsedPost(
        base_url="https://api.groq.com/openai/v1",
        api_key="key-1",
        post_date=None,
        tier=None,
        working=(mention("m-plain"), mention("m-star", True)),
        denied=(mention("m-denied"),),
        listed=(mention("m-listed"),),
        listed_truncated=False,
    )
    endpoint_id, _created = await catalog.upsert_post(post, post_id=1)
    names = await catalog.endpoint_models(endpoint_id, role="chat")
    assert names[0] == "m-star"
    assert set(names) == {"m-star", "m-plain", "m-listed"}
    # A role no listed model satisfies comes back empty rather than wrong.
    assert await catalog.endpoint_models(endpoint_id, role="stt") == []
