from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("userbot.tasks")


async def run_uninterruptible(
    coroutine: Coroutine[Any, Any, Any],
    *,
    max_attempts: int = 8,
) -> None:
    """Run ``coroutine`` to completion even while the caller is being cancelled.

    Cleanup paths that restore manager consistency (deactivating a context that
    was already replaced, rolling a half-applied migration back) must not be
    interrupted half-way, otherwise the process is left with a live context that
    nothing tracks. ``asyncio.wait_for`` and ``Task.cancel`` deliver exactly one
    ``CancelledError``, so re-awaiting the shielded task lets the cleanup finish
    and the caller still observes the cancellation afterwards.
    """
    task = asyncio.ensure_future(coroutine)
    attempts = 0
    while not task.done():
        attempts += 1
        if attempts > max_attempts:
            task.cancel()
            break
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            logger.exception("uninterruptible cleanup failed")
            break
    if task.done() and not task.cancelled():
        exception = task.exception()
        if exception is not None:
            logger.error("uninterruptible cleanup raised %r", exception)


class TaskGroup:
    """Owns background tasks created by a plugin so reload can cancel them.

    Uses :meth:`asyncio.wait` rather than ``wait_for(gather(...))`` so a timeout
    reports which tasks survived instead of abandoning them silently, and so
    every task's exception is retrieved before the group is discarded.
    """

    def __init__(self, *, name: str | None = None) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._name = name

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(task for task in self._tasks if not task.done())

    def spawn(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("Cannot spawn a task in a closed task group")
        task = asyncio.create_task(coroutine, name=name or self._name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("background task %s failed", task.get_name(), exc_info=exception)

    async def cancel_all(self, timeout_seconds: float = 10.0) -> bool:
        """Cancel every task and report whether all of them actually stopped."""
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
        for task in pending:
            # A second cancel gives well-behaved tasks a chance to unwind from
            # their own cleanup; anything still here is reported to the caller.
            task.cancel()
        if pending:
            logger.error(
                "%d background task(s) did not stop within %.1fs: %s",
                len(pending),
                timeout_seconds,
                ", ".join(sorted(task.get_name() for task in pending)),
            )
        return not pending
