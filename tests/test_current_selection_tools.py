"""Selection tools negotiate native reads without falling back to cursor metadata."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.current_selection import CurrentSelection
from mcp_score.tools import ToolError
from mcp_score.tools.analysis import read_current_selection
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    FakeBridge,
    fake_connection,
    sent_payloads,
)

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext


def _empty() -> CurrentSelection:
    return CurrentSelection(
        kind="none",
        range=None,
        elements=[],
        elements_complete=True,
        observed_voices=[],
        unsupported=["filter state not exposed"],
    )


@pytest.mark.anyio()
async def test_selection_tool_returns_native_result_without_navigation(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    expected = _empty()
    connection = fake_connection(MUSESCORE_HANDSHAKE, {"result": expected.model_dump()})
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        actual = await read_current_selection(context)
    assert actual == expected
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities",
        "readCurrentSelection",
    ]


@pytest.mark.anyio()
async def test_missing_selection_capability_rejects_without_cursor_fallback(
    registry: BridgeRegistry,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    handshake["result"]["commands"].remove("readCurrentSelection")
    connection = fake_connection(handshake)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="does not support command"),
    ):
        await registry.musescore.read_current_selection()
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
async def test_retry_observes_new_native_selection_on_fresh_connection(
    registry: BridgeRegistry,
) -> None:
    first = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    second = fake_connection(MUSESCORE_HANDSHAKE, {"result": _empty().model_dump()})
    with patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[first, second])):
        assert await registry.musescore.read_current_selection() == _empty()
    assert [message["command"] for message in sent_payloads(first)] == [
        "getCapabilities",
        "readCurrentSelection",
    ]
    assert [message["command"] for message in sent_payloads(second)] == [
        "getCapabilities",
        "readCurrentSelection",
    ]


@pytest.mark.anyio()
async def test_other_application_cannot_substitute_logical_cursor(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    bridge = FakeBridge("Other")
    registry.active = bridge
    with pytest.raises(ToolError, match="actual UI selection"):
        await read_current_selection(context)
    assert bridge.calls == []
