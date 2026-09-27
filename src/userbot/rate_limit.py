from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

#: Cap on remembered keys so a plugin that generates dynamic keys (chat ids,
#: message ids, ...) cannot grow the limiter without bound.
MAX_TRACKED_KEYS = 1024


class RateLimiter:
    """Per-key spacing guard with a global concurrency cap.

    The spacing delay is taken *outside* the bookkeeping lock. Holding one lock
    across ``asyncio.sleep`` serialised every key in the process, so three
    unrelated operations queued behind each other instead of merely being spaced.
    """

    def __init__(self, min_interval: float = 0.5, max_concurrency: int = 4):
        self.min_interval = max(0.0, min_interval)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._last_started: OrderedDict[str, float] = OrderedDict()

    @property
    def tracked_keys(self) -> int:
        return len(self._last_started)

    def _key_lock(self, key: str) -> asyncio.Lock:
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    async def _wait_turn(self, key: str) -> float:
        """Reserve this key's next start slot; return how long to wait for it.

        The slot is *reserved* before sleeping. Recording the current time and
        then sleeping instead would give every concurrent caller the same delay,
        so N same-key operations would all start together rather than queue.
        """
        async with self._key_lock(key):
            async with self._lock:
                now = time.monotonic()
                previous = self._last_started.get(key)
                reserved = now if previous is None else max(now, previous + self.min_interval)
                self._last_started[key] = reserved
                self._last_started.move_to_end(key)
                while len(self._last_started) > MAX_TRACKED_KEYS:
                    self._last_started.popitem(last=False)
                return max(0.0, reserved - now)

    @asynccontextmanager
    async def slot(self, key: str | int) -> AsyncIterator[None]:
        normalized_key = str(key)
        await self._semaphore.acquire()
        try:
            delay = await self._wait_turn(normalized_key)
            if delay > 0:
                await asyncio.sleep(delay)
            yield
        finally:
            self._semaphore.release()

    async def wait(self, key: str | int) -> None:
        async with self.slot(key):
            return None
