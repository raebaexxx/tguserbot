from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class TaskGroup:
    """Owns background tasks created by a plugin so reload can cancel them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def spawn(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("Cannot spawn a task in a closed task group")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_all(self, timeout_seconds: float = 10.0) -> bool:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return False
        return True
