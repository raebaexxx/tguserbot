"""The channel's post format, and everything that reads it.

Posts look like this; every field after the URL is optional::

    🌐 Base URL: https://api.groq.com/openai/v1

    🔑 Ключ: gsk_...

    📅 Дата: 2026-10-16 07:06 UTC
    📊 Тир: 8k tok/min (мелкий)

    🧩 Модели:

    ✅ Работают: openai/gpt-oss-120b
    🔒 Нет доступа (403): ⭐ claude-sonnet-5
    📋 Листятся: allam-2-7b, whisper-large-v3 … (+14 ещё)

    @zk_ichinokata

A post without a base URL is not an offer: "SOMETHING HAS BEEN HERE!" is what
an edited post degrades to, and it was seen live. Such posts are skipped, not
guessed at.

The parser is deliberately forgiving about layout -- extra blank lines, missing
emoji -- and deliberately strict about meaning: no scheme, no key, no offer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

#: Host -> provider id. The three MiniMax domains are one provider: keys are
#: found for all of them, and the chat endpoint behaves the same.
PROVIDER_HOSTS = {
    "api.groq.com": "groq",
    "integrate.api.nvidia.com": "nvidia",
    "api.mistral.ai": "mistral",
    "api.minimax.chat": "minimax",
    "api.minimaxi.com": "minimax",
    "api.minimax.cn": "minimax",
    "apihub.agnes-ai.com": "agnes",
}

_BASE_RE = re.compile(r"(?:🌐\s*)?Base URL:\s*(\S+)")
_KEY_RE = re.compile(r"(?:🔑\s*)?Ключ:\s*(\S+)")
_DATE_RE = re.compile(r"(?:📅\s*)?Дата:\s*([^\n]+)")
_TIER_RE = re.compile(r"(?:📊\s*)?Тир:\s*([^\n]+)")
_WORKING_RE = re.compile(r"✅\s*Работают:\s*([^\n]+)")
_DENIED_RE = re.compile(r"🔒\s*Нет доступа[^:]*:\s*([^\n]+)")
_LISTED_RE = re.compile(r"📋\s*Листятся:\s*([^\n]+)")

#: The channel truncates long model lists as "… (+14 ещё)". The number is the
#: count already shown, not the amount missing.
_TRUNCATED_RE = re.compile(r"\s*[.…]{1,3}\s*\(\+\d+\s*ещё\)\s*$")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M UTC",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M UTC",
    "%d.%m.%Y %H:%M",
)


@dataclass(frozen=True, slots=True)
class ModelMention:
    """One model name as the channel wrote it, with the star kept."""

    name: str
    starred: bool = False


@dataclass(frozen=True, slots=True)
class ParsedPost:
    base_url: str
    api_key: str
    #: ISO 8601 UTC, when the channel said when the key was made. ``None`` when
    #: the field is missing or written in a shape this parser does not know --
    #: unknown beats invented, because the date is used for ordering.
    post_date: str | None
    tier: str | None
    working: tuple[ModelMention, ...]
    denied: tuple[ModelMention, ...]
    listed: tuple[ModelMention, ...]
    listed_truncated: bool


def normalize_base_url(url: str) -> str:
    """Strip the shapes that make two spellings of one endpoint differ."""
    return url.strip().rstrip("/")


def provider_for(base_url: str) -> str:
    """The provider a base URL belongs to, or its host when it is new.

    An unknown host is not an error: the channel adds providers, and a new one
    should work before this table learns about it.
    """
    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return "unknown"
    return PROVIDER_HOSTS.get(host, host)


def parse_post(text: str) -> ParsedPost | None:
    """One channel post, or ``None`` when it is not an API offer."""
    if not text:
        return None
    base = _match(_BASE_RE, text)
    key = _match(_KEY_RE, text)
    if not base or not key:
        return None
    if not base.startswith(("http://", "https://")):
        return None
    if any(character.isspace() for character in key):
        return None
    listed_raw = _match(_LISTED_RE, text)
    truncated = bool(listed_raw and _TRUNCATED_RE.search(listed_raw))
    if listed_raw:
        listed_raw = _TRUNCATED_RE.sub("", listed_raw)
    return ParsedPost(
        base_url=normalize_base_url(base),
        api_key=key,
        post_date=_parse_date(_match(_DATE_RE, text)),
        tier=_match(_TIER_RE, text) or None,
        working=_mentions(_match(_WORKING_RE, text)),
        denied=_mentions(_match(_DENIED_RE, text)),
        listed=_mentions(listed_raw),
        listed_truncated=truncated,
    )


def _match(pattern: re.Pattern[str], text: str) -> str:
    found = pattern.search(text)
    return found.group(1).strip() if found else ""


def _mentions(raw: str) -> tuple[ModelMention, ...]:
    if not raw:
        return ()
    mentions: list[ModelMention] = []
    for piece in raw.split(","):
        name = piece.strip()
        starred = name.startswith("⭐")
        name = name.lstrip("⭐").strip()
        if not name:
            continue
        mentions.append(ModelMention(name=name, starred=starred))
    return tuple(mentions)


def _parse_date(raw: str) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC).isoformat(timespec="seconds")
    return None


__all__ = [
    "ModelMention",
    "ParsedPost",
    "normalize_base_url",
    "parse_post",
    "provider_for",
]
