"""Tests for the channel post parser.

The fixtures are shaped exactly like the live posts (keys replaced), including
the two traps found on the real channel: model lists truncated with "… (+14
ещё)", and edited posts that degrade to "SOMETHING HAS BEEN HERE!" and must be
skipped rather than guessed at.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import shipped_submodule

FULL_POST = """\
🌐 Base URL: https://api.groq.com/openai/v1

🔑 Ключ: gsk_TESTKEY0123456789

📅 Дата: 2026-10-16 07:06 UTC
📊 Тир: 8k tok/min (мелкий)

🧩 Модели:

✅ Работают: openai/gpt-oss-120b

🔒 Нет доступа (403): ⭐ claude-sonnet-5, ⭐ gpt-5.5

📋 Листятся: allam-2-7b, whisper-large-v3, whisper-large-v3-turbo … (+14 ещё)

@zk_ichinokata
"""

MINIMAL_POST = """\
🌐 Base URL: https://api.mistral.ai/v1/
🔑 Ключ: mstrl_TESTKEY0123
"""


@pytest.fixture(scope="module")
def channel() -> Any:
    loaded, module = shipped_submodule("api", "_channel")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def test_a_full_post_parses_every_field(channel: Any) -> None:
    parsed = channel.parse_post(FULL_POST)
    assert parsed is not None
    assert parsed.base_url == "https://api.groq.com/openai/v1"
    assert parsed.api_key == "gsk_TESTKEY0123456789"
    assert parsed.post_date == "2026-10-16T07:06:00+00:00"
    assert parsed.tier == "8k tok/min (мелкий)"
    assert [mention.name for mention in parsed.working] == ["openai/gpt-oss-120b"]
    assert [mention.name for mention in parsed.denied] == ["claude-sonnet-5", "gpt-5.5"]
    assert [mention.starred for mention in parsed.denied] == [True, True]
    assert [mention.starred for mention in parsed.working] == [False]


def test_a_truncated_list_is_marked_and_the_names_kept(channel: Any) -> None:
    parsed = channel.parse_post(FULL_POST)
    assert parsed is not None
    assert parsed.listed_truncated is True
    assert [mention.name for mention in parsed.listed] == [
        "allam-2-7b",
        "whisper-large-v3",
        "whisper-large-v3-turbo",
    ]


def test_a_minimal_post_parses_without_the_optional_fields(channel: Any) -> None:
    parsed = channel.parse_post(MINIMAL_POST)
    assert parsed is not None
    assert parsed.base_url == "https://api.mistral.ai/v1"
    assert parsed.post_date is None
    assert parsed.tier is None
    assert parsed.working == ()
    assert parsed.denied == ()
    assert parsed.listed == ()


def test_an_edited_post_is_skipped_not_guessed_at(channel: Any) -> None:
    assert channel.parse_post("SOMETHING HAS BEEN HERE!") is None


def test_a_post_without_a_key_is_skipped(channel: Any) -> None:
    text = "🌐 Base URL: https://api.example.com/v1\nпросто анонс"
    assert channel.parse_post(text) is None


def test_a_base_url_without_a_scheme_is_skipped(channel: Any) -> None:
    text = "🌐 Base URL: api.example.com/v1\n🔑 Ключ: abc"
    assert channel.parse_post(text) is None


def test_provider_maps_known_hosts_and_falls_back_to_the_host(channel: Any) -> None:
    assert channel.provider_for("https://api.groq.com/openai/v1") == "groq"
    assert channel.provider_for("https://api.minimax.cn/v1") == "minimax"
    assert channel.provider_for("https://api.minimaxi.com/v1") == "minimax"
    assert channel.provider_for("https://apihub.agnes-ai.com/v1") == "agnes"
    assert channel.provider_for("https://api.newcomer.example/v1") == "api.newcomer.example"


def test_a_trailing_slash_is_normalised_away(channel: Any) -> None:
    assert channel.normalize_base_url("https://api.mistral.ai/v1/") == "https://api.mistral.ai/v1"
    parsed = channel.parse_post(MINIMAL_POST)
    assert parsed is not None
    assert parsed.base_url == "https://api.mistral.ai/v1"
