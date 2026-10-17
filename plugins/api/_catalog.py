"""The key catalogue: storage, capability heuristics, and the ranking.

Everything the channel publishes lands in two tables. ``endpoints`` is one row
per (base_url, key) pair; ``models`` is one row per (endpoint, model name) with
flags OR-ed across posts, because a name the channel once called working must
not be un-said by a later post that only lists it.

The ranking is the autopilot. It orders candidates by what the operator said
matters -- a pinned choice first, then provider priority -- and then by what
was actually observed: validated endpoints before untested ones, channel-marked
working models before merely listed ones. Cooldowns are applied by exclusion
rather than by deprioritising, because asking a key that just returned 401
"again, but a bit later" spends the request that could have reached a working
one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ._channel import ModelMention, ParsedPost, provider_for

#: A revoked key or a denied model is not coming back this hour; re-checking it
#: is pure waste. Six hours keeps a revoked key from being retried all day while
#: still noticing a key that was re-enabled.
AUTH_COOLDOWN_SECONDS = 6 * 3600.0
MODEL_DENIED_SECONDS = 6 * 3600.0
MODEL_MISSING_SECONDS = 24 * 3600.0
RATE_COOLDOWN_SECONDS = 60.0
SERVER_COOLDOWN_SECONDS = 120.0

#: Only wait for a rate limit the API expected to clear this fast. A minute
#: inside a command is forever; switching is faster, and the switched-to model
#: answers.
RETRY_INLINE_SECONDS = 10.0

#: Tags that remove a model from every candidate list. Embedding and moderation
#: models are real, listed, and of no use to a chat command.
AUX_MARKERS = ("embed", "moderation", "prompt-guard", "-guard-", "rerank", "orpheus")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cooldown_until(seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=max(1.0, seconds))
    return moment.isoformat(timespec="seconds")


def capabilities(name: str) -> frozenset[str]:
    """What a model name says it can do.

    A heuristic, and said out loud: the channel's listings carry names, not
    modality metadata, and probing every model would burn the quota this plugin
    exists to spend on questions. Names are checked against the live listings
    (``whisper-``, ``-vision-``, ``-image-`` all appear in them), and a wrong
    guess costs one failed request that the cooldown then remembers.
    """
    lowered = name.lower()
    if any(marker in lowered for marker in AUX_MARKERS):
        return frozenset({"aux"})
    if "whisper" in lowered or "transcribe" in lowered:
        return frozenset({"stt"})
    if "ocr" in lowered:
        # Mistral's OCR models are a separate endpoint, not chat-completions.
        return frozenset({"ocr"})
    if "image" in lowered or "dall-e" in lowered:
        return frozenset({"image"})
    if "video" in lowered:
        return frozenset({"video"})
    tags = {"chat"}
    if any(marker in lowered for marker in ("vision", "fuyu", "kosmos", "deplot", "pixtral")):
        tags.add("vision")
    return frozenset(tags)


@dataclass(frozen=True, slots=True)
class Candidate:
    endpoint_id: int
    base_url: str
    api_key: str
    provider: str
    model: str
    alive: bool
    working: bool
    starred: bool
    post_date: str | None
    fail_count: int
    tags: frozenset[str]


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: int
    base_url: str
    api_key: str
    provider: str


class Catalog:
    """Everything this plugin remembers, behind one storage handle."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    async def ensure(self) -> None:
        await self.storage.execute(
            "CREATE TABLE IF NOT EXISTS endpoints ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "base_url TEXT NOT NULL, "
            "api_key TEXT NOT NULL, "
            "provider TEXT NOT NULL, "
            "tier TEXT, "
            "post_id INTEGER, "
            "post_date TEXT, "
            "added_at TEXT NOT NULL, "
            "last_checked TEXT, "
            "alive INTEGER NOT NULL DEFAULT 0, "
            "fail_count INTEGER NOT NULL DEFAULT 0, "
            "cooldown_until TEXT, "
            "UNIQUE(base_url, api_key))"
        )
        await self.storage.execute(
            "CREATE TABLE IF NOT EXISTS models ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "endpoint_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, "
            "working INTEGER NOT NULL DEFAULT 0, "
            "denied INTEGER NOT NULL DEFAULT 0, "
            "listed INTEGER NOT NULL DEFAULT 0, "
            "starred INTEGER NOT NULL DEFAULT 0, "
            "cooldown_until TEXT, "
            "UNIQUE(endpoint_id, name))"
        )
        await self.storage.execute(
            "CREATE TABLE IF NOT EXISTS history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "role TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )

    # -- meta -----------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        return await self.storage.get_value(key)

    async def set_meta(self, key: str, value: str) -> None:
        await self.storage.set_value(key, value)

    async def advance_cursor(self, post_id: int) -> None:
        if post_id <= 0:
            return
        current = await self.get_meta("cursor")
        if current and current.isdigit() and int(current) >= post_id:
            return
        await self.set_meta("cursor", str(post_id))

    # -- ingest ---------------------------------------------------------------

    async def upsert_post(self, parsed: ParsedPost, *, post_id: int | None) -> tuple[int, bool]:
        """Store one parsed post. Returns (endpoint id, whether it is new)."""
        base_url = parsed.base_url
        provider = provider_for(base_url)
        existing = await self.storage.fetchone(
            "SELECT id FROM endpoints WHERE base_url = ? AND api_key = ?",
            (base_url, parsed.api_key),
        )
        created = existing is None
        if created:
            await self.storage.execute(
                "INSERT INTO endpoints "
                "(base_url, api_key, provider, tier, post_id, post_date, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    base_url,
                    parsed.api_key,
                    provider,
                    parsed.tier,
                    post_id,
                    parsed.post_date,
                    now_iso(),
                ),
            )
            found = await self.storage.fetchone(
                "SELECT id FROM endpoints WHERE base_url = ? AND api_key = ?",
                (base_url, parsed.api_key),
            )
            if found is None:  # pragma: no cover - the insert just succeeded
                raise RuntimeError("endpoint vanished after insert")
            endpoint_id = int(found["id"])
        else:
            endpoint_id = int(existing["id"])
            await self.storage.execute(
                "UPDATE endpoints SET "
                "provider = ?, "
                "tier = COALESCE(?, tier), "
                "post_id = MAX(COALESCE(post_id, 0), COALESCE(?, 0)), "
                "post_date = COALESCE(?, post_date) "
                "WHERE id = ?",
                (provider, parsed.tier, post_id, parsed.post_date, endpoint_id),
            )
        await self._mark_models(endpoint_id, parsed.working, flag="working")
        await self._mark_models(endpoint_id, parsed.denied, flag="denied")
        await self._mark_models(endpoint_id, parsed.listed, flag="listed")
        return endpoint_id, created

    async def _mark_models(
        self, endpoint_id: int, mentions: Sequence[ModelMention], *, flag: str
    ) -> None:
        if not mentions:
            return
        working = 1 if flag == "working" else 0
        denied = 1 if flag == "denied" else 0
        listed = 1 if flag == "listed" else 0
        for mention in mentions:
            await self.storage.execute(
                "INSERT INTO models (endpoint_id, name, starred, working, denied, listed) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint_id, name) DO UPDATE SET "
                "starred = MAX(models.starred, excluded.starred), "
                "working = MAX(models.working, excluded.working), "
                "denied = MAX(models.denied, excluded.denied), "
                "listed = MAX(models.listed, excluded.listed)",
                (
                    endpoint_id,
                    mention.name,
                    1 if mention.starred else 0,
                    working,
                    denied,
                    listed,
                ),
            )

    # -- validation -----------------------------------------------------------

    async def endpoint(self, endpoint_id: int) -> Endpoint | None:
        row = await self.storage.fetchone(
            "SELECT id, base_url, api_key, provider FROM endpoints WHERE id = ?",
            (endpoint_id,),
        )
        if row is None:
            return None
        return Endpoint(
            id=int(row["id"]),
            base_url=str(row["base_url"]),
            api_key=str(row["api_key"]),
            provider=str(row["provider"]),
        )

    async def is_checked(self, endpoint_id: int) -> bool:
        row = await self.storage.fetchone(
            "SELECT last_checked FROM endpoints WHERE id = ?", (endpoint_id,)
        )
        return bool(row and row["last_checked"])

    async def unchecked_endpoint_ids(self) -> list[int]:
        """Endpoints whose validation never completed.

        A validation that crashed mid-way leaves no marker, so without this
        sweep those keys would sit unverified forever: nothing re-validates an
        endpoint that already exists in the catalogue.
        """
        rows = await self.storage.fetchall(
            "SELECT id FROM endpoints WHERE last_checked IS NULL ORDER BY id"
        )
        return [int(row["id"]) for row in rows]

    async def mark_checked(
        self, endpoint_id: int, *, alive: bool, api_models: Sequence[str] | None
    ) -> None:
        await self.storage.execute(
            "UPDATE endpoints SET last_checked = ?, alive = ?, "
            "fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END "
            "WHERE id = ?",
            (now_iso(), 1 if alive else 0, 1 if alive else 0, endpoint_id),
        )
        if api_models is not None:
            for name in api_models:
                await self.storage.execute(
                    "INSERT INTO models (endpoint_id, name, listed) VALUES (?, ?, 1) "
                    "ON CONFLICT(endpoint_id, name) DO UPDATE SET "
                    "listed = 1",
                    (endpoint_id, name),
                )

    async def endpoint_models(self, endpoint_id: int, *, role: str, limit: int = 3) -> list[str]:
        rows = await self.storage.fetchall(
            "SELECT name, working, denied, listed, starred FROM models WHERE endpoint_id = ?",
            (endpoint_id,),
        )
        scored: list[tuple[int, str]] = []
        for row in rows:
            name = str(row["name"])
            if int(row["denied"]) or role not in capabilities(name):
                continue
            score = (
                (2 if row["working"] else 0)
                + (1 if row["starred"] else 0)
                + (1 if row["listed"] else 0)
            )
            scored.append((-score, name))
        scored.sort()
        return [name for _score, name in scored[:limit]]

    # -- cooldowns ------------------------------------------------------------

    async def cooldown_endpoint(self, endpoint_id: int, seconds: float) -> None:
        await self.storage.execute(
            "UPDATE endpoints SET cooldown_until = ?, fail_count = fail_count + 1 WHERE id = ?",
            (cooldown_until(seconds), endpoint_id),
        )

    async def cooldown_model(self, endpoint_id: int, model: str, seconds: float) -> None:
        await self.storage.execute(
            "UPDATE models SET cooldown_until = ? WHERE endpoint_id = ? AND name = ?",
            (cooldown_until(seconds), endpoint_id, model),
        )

    async def record_success(self, endpoint_id: int, model: str) -> None:
        await self.storage.execute(
            "UPDATE endpoints SET fail_count = 0 WHERE id = ?", (endpoint_id,)
        )
        await self.storage.execute(
            "UPDATE models SET cooldown_until = NULL WHERE endpoint_id = ? AND name = ?",
            (endpoint_id, model),
        )

    # -- candidates -----------------------------------------------------------

    async def candidates(
        self, role: str, *, provider_priority: Sequence[str], pinned: str
    ) -> list[Candidate]:
        """Ranked candidates for one role. Excluded entirely: cooldowns, denied."""
        now = now_iso()
        rows = await self.storage.fetchall(
            "SELECT e.id AS endpoint_id, e.base_url, e.api_key, e.provider, e.alive, "
            "e.post_date, e.fail_count, e.cooldown_until, "
            "m.name AS model, m.working, m.denied, m.listed, m.starred, "
            "m.cooldown_until AS model_cooldown "
            "FROM endpoints e JOIN models m ON m.endpoint_id = e.id "
            "WHERE m.denied = 0 AND (m.working = 1 OR m.listed = 1)"
        )
        priority = {name: index for index, name in enumerate(provider_priority)}
        results: list[Candidate] = []
        for row in rows:
            if role not in capabilities(str(row["model"])):
                continue
            if _active(row["cooldown_until"], now) or _active(row["model_cooldown"], now):
                continue
            results.append(
                Candidate(
                    endpoint_id=int(row["endpoint_id"]),
                    base_url=str(row["base_url"]),
                    api_key=str(row["api_key"]),
                    provider=str(row["provider"]),
                    model=str(row["model"]),
                    alive=bool(row["alive"]),
                    working=bool(row["working"]),
                    starred=bool(row["starred"]),
                    post_date=row["post_date"],
                    fail_count=int(row["fail_count"]),
                    tags=capabilities(str(row["model"])),
                )
            )
        results.sort(key=lambda item: _rank(item, priority, pinned))
        return results

    # -- reporting ------------------------------------------------------------

    async def keys_summary(self) -> list[dict[str, Any]]:
        now = now_iso()
        return await self.storage.fetchall(
            "SELECT provider, COUNT(*) AS total, SUM(alive) AS alive_count, "
            "SUM(CASE WHEN cooldown_until IS NOT NULL AND cooldown_until > ? "
            "THEN 1 ELSE 0 END) AS cooling "
            "FROM endpoints GROUP BY provider ORDER BY provider",
            (now,),
        )

    async def model_rows(self) -> list[dict[str, Any]]:
        return await self.storage.fetchall(
            "SELECT e.provider AS provider, m.name AS name, m.starred AS starred, "
            "m.working AS working, m.denied AS denied, m.listed AS listed, "
            "m.cooldown_until AS model_cooldown, e.alive AS alive "
            "FROM models m JOIN endpoints e ON e.id = m.endpoint_id"
        )

    async def endpoint_count(self) -> int:
        row = await self.storage.fetchone("SELECT COUNT(*) AS total FROM endpoints")
        return int(row["total"]) if row else 0

    async def model_exists(self, name: str) -> bool:
        row = await self.storage.fetchone(
            "SELECT 1 AS present FROM models WHERE name = ? LIMIT 1", (name,)
        )
        return row is not None

    async def provider_exists(self, provider: str) -> bool:
        row = await self.storage.fetchone(
            "SELECT 1 AS present FROM endpoints WHERE provider = ? LIMIT 1", (provider,)
        )
        return row is not None


def _active(stamp: Any, now: str) -> bool:
    return isinstance(stamp, str) and stamp > now


def _rank(candidate: Candidate, priority: dict[str, int], pinned: str) -> tuple[Any, ...]:
    if pinned:
        pin_rank = 0 if (candidate.model == pinned or candidate.provider == pinned) else 1
    else:
        pin_rank = 0
    return (
        pin_rank,
        priority.get(candidate.provider, len(priority)),
        0 if candidate.alive else 1,
        0 if candidate.working else 1,
        0 if candidate.starred else 1,
        _newest_first(candidate.post_date),
        candidate.fail_count,
        candidate.endpoint_id,
        candidate.model,
    )


def _newest_first(post_date: str | None) -> float:
    """Smaller sorts first: negate the epoch so the newest date wins."""
    if not post_date:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(post_date)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return -parsed.timestamp()


__all__ = [
    "AUTH_COOLDOWN_SECONDS",
    "MODEL_DENIED_SECONDS",
    "MODEL_MISSING_SECONDS",
    "RATE_COOLDOWN_SECONDS",
    "RETRY_INLINE_SECONDS",
    "SERVER_COOLDOWN_SECONDS",
    "Candidate",
    "Catalog",
    "Endpoint",
    "capabilities",
]
