"""Which application the server is talking to right now.

The server keeps one bridge per supported application and at most one of
them active. Connecting to an application deactivates the previous one,
so a tool never has to ask which application it is operating on. The
server owns the registry for its lifetime and hands it to the tools
through their context.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp_score.bridge.dorico import DoricoBridge
from mcp_score.bridge.locking import TaskLock
from mcp_score.bridge.musescore import MuseScoreBridge

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mcp_score.bridge.base import ScoreBridge

__all__ = ["BridgeRegistry"]


class BridgeRegistry:
    """Holds the bridges and tracks the active one."""

    def __init__(
        self,
        musescore: MuseScoreBridge | None = None,
        dorico: DoricoBridge | None = None,
    ) -> None:
        self.musescore = musescore if musescore is not None else MuseScoreBridge()
        self.dorico = dorico if dorico is not None else DoricoBridge()
        self.active: ScoreBridge | None = None
        self._workflow_lock = TaskLock()

    @asynccontextmanager
    async def operation(self) -> AsyncGenerator[None]:
        """Own one complete tool workflow, including any connection changes.

        Nested registry operations in the owning task share the scope. The
        active bridge is captured at entry: activate it before beginning a
        standalone cursor workflow. Direct bridge users must also own a scope.
        """
        async with self._workflow_lock.hold():
            active = self.active
            if active is None:
                yield
            else:
                async with active.operation():
                    yield

    def connected(self) -> ScoreBridge | None:
        """The active bridge if it is connected, otherwise ``None``."""
        active = self.active
        if active is not None and active.is_connected:
            return active
        return None

    async def activate(self, bridge: ScoreBridge) -> bool:
        """Connect *bridge* and make it the active one.

        Any other active bridge is disconnected first. Returns whether the
        connection succeeded; on failure nothing is active.
        """
        async with self.operation():
            if self.active is not None and self.active is not bridge:
                await self.deactivate(self.active)
            try:
                connected = await bridge.connect()
            finally:
                if self.active is bridge and not bridge.is_connected:
                    self.active = None
            if not connected:
                self.active = None
                return False
            self.active = bridge
            return True

    async def deactivate(self, bridge: ScoreBridge) -> None:
        """Disconnect *bridge*; it stops being active if it was."""
        async with self.operation():
            try:
                await bridge.disconnect()
            finally:
                if self.active is bridge and not bridge.is_connected:
                    self.active = None
