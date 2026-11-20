"""Task-owned operation scopes for nested bridge and registry calls."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["TaskLock"]


class TaskLock:
    """Serialize tasks while allowing the owning task to enter nested scopes.

    Ownership follows the actual asyncio task, rather than a context variable:
    a child task must wait even when created inside an operation. Only the
    outermost scope releases the underlying lock, including on cancellation.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            yield
            return
        async with self._lock:
            self._owner = task
            try:
                yield
            finally:
                self._owner = None
