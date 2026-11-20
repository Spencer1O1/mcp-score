"""Duration edits are strict, explicitly negotiated, and never replayed/batched."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.duration_edit import ExpectedDurationEvent
from mcp_score.bridge.results import Duration
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    sent_payloads,
)

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext
    from tests.fakes import FakeBridge


def _arguments() -> dict:
    return {
        "measure": 1,
        "staff": 0,
        "voice": 0,
        "offset": {"numerator": 0, "denominator": 1},
        "duration": {"numerator": 1, "denominator": 8},
        "expected": [
            {
                "kind": "chord",
                "type": 93,
                "staff": 0,
                "voice": 0,
                "tick": 0,
                "offset": {"numerator": 0, "denominator": 1},
                "duration": {"numerator": 1, "denominator": 4},
                "actual_duration": {"numerator": 1, "denominator": 4},
                "notes": [
                    {
                        "pitch": 60,
                        "tpc": 14,
                        "written_tpc": 14,
                        "displayed_tpc": 14,
                        "name": "C4",
                    }
                ],
            }
        ],
    }


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measure", True),
        ("staff", 0.0),
        ("voice", "0"),
        ("duration", {"numerator": True, "denominator": 8}),
        ("offset", {"numerator": 0, "denominator": "1"}),
    ],
)
async def test_mcp_rejects_coercion_before_bridge(
    registry: BridgeRegistry,
    context: ScoreContext,
    connected_bridge: FakeBridge,
    field: str,
    value: object,
) -> None:
    arguments = _arguments()
    arguments[field] = value
    with pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "set_live_event_duration", arguments, context=context
        )
    assert connected_bridge.calls == []


@pytest.mark.anyio()
@pytest.mark.parametrize("case", ["missing", "lost", "sequence"])
async def test_duration_requires_capability_and_never_replays_or_batches(
    registry: BridgeRegistry,
    case: str,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if case == "missing":
        handshake["result"]["commands"].remove("setEventDuration")
    replies = [handshake]
    if case == "lost":
        replies.append(ConnectionClosed(None, None))
    connection = fake_connection(*replies)
    connect = AsyncMock(return_value=connection)
    args = _arguments()
    bridge = registry.musescore
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(BridgeError) as caught:
        if case == "sequence":
            await bridge.process_sequence([{"action": "setEventDuration"}])
        else:
            await bridge.set_event_duration(
                1,
                0,
                0,
                Duration(numerator=0, denominator=1),
                Duration(numerator=1, denominator=8),
                [ExpectedDurationEvent.model_validate(args["expected"][0])],
            )
    commands = [payload["command"] for payload in sent_payloads(connection)]
    assert commands == (
        ["getCapabilities", "setEventDuration"]
        if case == "lost"
        else ["getCapabilities"]
    )
    if case == "lost":
        assert caught.value.details["code"] == "outcome_unknown"
    assert connect.await_count == 1


@pytest.mark.anyio()
async def test_tool_sends_explicit_snapshot_in_one_command_and_returns_native_readback(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    arguments = _arguments()
    events = arguments["expected"]
    reply = {
        "result": {
            "measure": 1,
            "staff": 0,
            "voice": 0,
            "before": events,
            "after": events,
            "verified": True,
            "mutationStatus": "unchanged",
            "snapshotScope": "target_voice_measure",
            "selectionStatus": "unchanged",
            "limitations": ["no global revision"],
        }
    }
    connection = fake_connection(MUSESCORE_HANDSHAKE, reply)
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.musescore.connect()
        registry.active = registry.musescore
        result = await create_server(registry).call_tool(
            "set_live_event_duration", arguments, context=context
        )
    assert result is not None
    payloads = sent_payloads(connection)
    assert [payload["command"] for payload in payloads] == [
        "getCapabilities",
        "setEventDuration",
    ]
    assert payloads[-1]["params"]["policy"] == "shorten_fill_rest"
    assert payloads[-1]["params"]["expected"][0]["notes"][0]["writtenTpc"] == 14
    assert payloads[-1]["params"]["offset"] == arguments["offset"]
