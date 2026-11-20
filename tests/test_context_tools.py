"""Strict public context requests, negotiated capability, and safe read reconnect."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.staff_context import StaffContext
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from mcp_score.tools.analysis import read_staff_context
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    sent_payloads,
)
from tests.test_context_plugin import _run

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext
    from tests.fakes import FakeBridge


@pytest.mark.anyio()
async def test_public_context_tool_reconnects_read_without_navigation(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    first = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    second = fake_connection(MUSESCORE_HANDSHAKE, _run()["reply"])
    with patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[first, second])):
        assert await registry.activate(registry.musescore)
        result = await read_staff_context(context, 2, 3)
    assert isinstance(result, StaffContext) and result.sampling_complete
    assert result.timelines[1].start.native_transposition.chromatic == -12
    for connection in (first, second):
        assert [message["command"] for message in sent_payloads(connection)] == [
            "getCapabilities",
            "readStaffContext",
        ]
        assert sent_payloads(connection)[1]["params"] == {
            "startMeasure": 2,
            "endMeasure": 3,
            "staff": None,
        }


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_measure", True),
        ("end_measure", 2.0),
        ("staff", "0"),
        ("staff", -1),
    ],
)
async def test_mcp_rejects_coerced_context_coordinates_before_bridge(
    registry: BridgeRegistry,
    context: ScoreContext,
    connected_bridge: FakeBridge,
    field: str,
    value: object,
) -> None:
    arguments = {"start_measure": 1, "end_measure": 2, "staff": 0}
    arguments[field] = value
    with pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "read_staff_context", arguments, context=context
        )
    assert connected_bridge.calls == []


@pytest.mark.anyio()
@pytest.mark.parametrize("sequence", [False, True])
async def test_context_requires_top_level_negotiated_capability(
    registry: BridgeRegistry,
    sequence: bool,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if not sequence:
        handshake["result"]["commands"].remove("readStaffContext")
    connection = fake_connection(handshake)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError),
    ):
        if sequence:
            await registry.musescore.process_sequence([{"action": "readStaffContext"}])
        else:
            await registry.musescore.read_staff_context(1, 1)
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]
