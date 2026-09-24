from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RateLimiter:
    """A small per-key concurrency limiter and minimum-spacing guard."""

    def __init__(self, min_interval: float = 0.5, max_concurrency: int = 4):
        self.min_interval = max(0.0, min_interval)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._lock = asyncio.Lock()
        self._last_started: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, key: str | int) -> AsyncIterator[None]:
        normalized_key = str(key)
        await self._semaphore.acquire()
        try:
            loop = asyncio.get_running_loop()
            async with self._lock:
                now = loop.time()
                previous = self._last_started.get(normalized_key, now)
                delay = self.min_interval - (now - previous)
                if delay > 0:
                    await asyncio.sleep(delay)
                    now = loop.time()
                self._last_started[normalized_key] = now
            yield
        finally:
            self._semaphore.release()

    async def wait(self, key: str | int) -> None:
        async with self.slot(key):
            return None
