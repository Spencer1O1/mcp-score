"""Lost replies must not cause a second application of a score mutation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from mcp_score.bridge import BridgeError
from mcp_score.bridge.dorico import DoricoBridge
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    REMOTE_CONTROL_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    sent_payloads,
)

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("appendMeasures", {"count": 1}),
        ("transpose", {"semitones": 2}),
        ("undo", None),
        ("goToMeasure", {"measure": 2}),
        (
            "processSequence",
            {"sequence": [{"action": "appendMeasures", "params": {"count": 1}}]},
        ),
        ("futureEdit", {"value": "unknown operation"}),
    ],
)
async def test_applied_musescore_mutation_with_lost_reply_is_never_replayed(
    command: str, params: dict[str, Any] | None
) -> None:
    state = {"applications": 0}
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    handshake["result"]["commands"].append("futureEdit")
    dropped = fake_connection(handshake, ConnectionClosed(None, None))
    replacement = fake_connection(handshake, {"result": {}})

    async def apply(message: str) -> None:
        payload = json.loads(message)
        if payload.get("command") == command:
            state["applications"] += 1

    dropped.send.side_effect = apply
    replacement.send.side_effect = apply
    connect = AsyncMock(side_effect=[dropped, replacement])
    bridge = MuseScoreBridge()

    with (
        patch(WEBSOCKETS_CONNECT, connect),
        pytest.raises(BridgeError) as error,
    ):
        await bridge.send_command(command, params)

    assert state["applications"] == 1
    assert connect.await_count == 1
    assert sent_payloads(replacement) == []
    dropped.close.assert_awaited_once()
    assert not bridge.is_connected
    assert error.value.details["code"] == "outcome_unknown"
    assert error.value.details["mutation_status"] == "unknown"
    assert error.value.details["operation"] == command
    assert error.value.details["target"] == params
    assert error.value.details["replayed"] is False
    assert (
        str(UUID(error.value.details["request_id"]))
        == error.value.details["request_id"]
    )
    assert error.value.details["request_id_scope"] == "client_correlation_only"


@pytest.mark.anyio()
@pytest.mark.parametrize("failure", ["malformed", "binary", "timeout", "send_failure"])
async def test_unsafe_request_failure_discards_connection_without_resending(
    failure: str,
) -> None:
    reply: Any = {"result": {}}
    if failure == "malformed":
        reply = "not JSON"
    elif failure == "binary":
        reply = b"bytes"
    elif failure == "timeout":
        reply = TimeoutError("reply lost")
    dropped = fake_connection(MUSESCORE_HANDSHAKE, reply)
    bridge = MuseScoreBridge()
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=dropped)):
        assert await bridge.connect()
    if failure == "send_failure":
        dropped.send.side_effect = ConnectionClosed(None, None)

    with pytest.raises(BridgeError) as error:
        await bridge.append_measures(1)

    assert error.value.details["code"] == "outcome_unknown"
    assert (
        len(
            [
                item
                for item in sent_payloads(dropped)
                if item["command"] == "appendMeasures"
            ]
        )
        == 1
    )
    dropped.close.assert_awaited_once()


@pytest.mark.anyio()
async def test_not_sent_request_is_distinguished_from_unknown_outcome() -> None:
    dropped = fake_connection(MUSESCORE_HANDSHAKE)
    bridge = MuseScoreBridge()
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=dropped)):
        assert await bridge.connect()
    # The peer closes after the bridge's is_connected check but before request
    # obtains its connection. No send occurs in this race.
    with (
        patch.object(type(bridge), "is_connected", property(lambda _: True)),
        pytest.raises(BridgeError) as error,
    ):
        dropped.state = State.CLOSED
        await bridge.append_measures(1)

    assert error.value.details["code"] == "request_not_sent"
    assert error.value.details["mutation_status"] == "not_applied"
    assert [item["command"] for item in sent_payloads(dropped)] == ["getCapabilities"]


@pytest.mark.anyio()
async def test_safe_read_reconnects_and_renegotiates_before_one_retry() -> None:
    dropped = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    replacement = fake_connection(MUSESCORE_HANDSHAKE, {"result": "pong"})
    connect = AsyncMock(side_effect=[dropped, replacement])
    bridge = MuseScoreBridge()

    with patch(WEBSOCKETS_CONNECT, connect):
        assert await bridge.ping()

    assert connect.await_count == 2
    assert sent_payloads(replacement) == [
        {"command": "getCapabilities", "params": {"protocolVersion": 1}},
        {"command": "ping"},
    ]


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "command", ["AddRehearsalMark", "Edit.Undo", "Future.Mutation"]
)
async def test_dorico_applied_command_with_lost_reply_is_not_replayed(
    command: str,
) -> None:
    state = {"applications": 0}
    dropped = fake_connection(*REMOTE_CONTROL_HANDSHAKE, ConnectionClosed(None, None))
    replacement = fake_connection({"message": "response", "code": "kConnected"})

    async def apply(message: str) -> None:
        if json.loads(message).get("commandName") == command:
            state["applications"] += 1

    dropped.send.side_effect = apply
    replacement.send.side_effect = apply
    connect = AsyncMock(side_effect=[dropped, replacement])
    bridge = DoricoBridge()

    with (
        patch(WEBSOCKETS_CONNECT, connect),
        pytest.raises(BridgeError) as error,
    ):
        await bridge.send_command(command)

    assert state["applications"] == 1
    assert connect.await_count == 1
    assert error.value.details["code"] == "outcome_unknown"
    assert error.value.details["operation"] == command
    assert not bridge.is_connected


@pytest.mark.anyio()
async def test_dorico_safe_status_read_reconnects_with_session_handshake() -> None:
    dropped = fake_connection(*REMOTE_CONTROL_HANDSHAKE, ConnectionClosed(None, None))
    replacement = fake_connection(
        {"message": "response", "code": "kConnected"},
        {"message": "status", "name": "Fixture"},
    )
    bridge = DoricoBridge()

    with patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[dropped, replacement])):
        reply = await bridge.get_status()

    assert reply == {"message": "status", "name": "Fixture"}
    assert [item["message"] for item in sent_payloads(replacement)] == [
        "connect",
        "getstatus",
    ]


@pytest.mark.anyio()
async def test_unknown_dorico_protocol_message_is_conservatively_not_replayed() -> None:
    dropped = fake_connection(*REMOTE_CONTROL_HANDSHAKE, ConnectionClosed(None, None))
    connect = AsyncMock(return_value=dropped)
    bridge = DoricoBridge()

    with (
        patch(WEBSOCKETS_CONNECT, connect),
        pytest.raises(BridgeError) as error,
    ):
        await bridge.send_message("futureMessage")

    assert connect.await_count == 1
    assert error.value.details["operation"] == "futureMessage"
    assert error.value.details["code"] == "outcome_unknown"


@pytest.mark.anyio()
async def test_mcp_tool_error_preserves_unknown_outcome_and_request_correlation(
    registry: BridgeRegistry, context: ScoreContext
) -> None:
    state = {"measures_added": 0}
    dropped = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))

    async def apply(message: str) -> None:
        payload = json.loads(message)
        if payload.get("command") == "appendMeasures":
            state["measures_added"] += payload["params"]["count"]

    dropped.send.side_effect = apply
    bridge = registry.musescore
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=dropped)):
        assert await bridge.connect()
    registry.active = bridge
    server = create_server(registry)

    with pytest.raises(ToolError) as error:
        await server.call_tool("append_live_measures", {"count": 1}, context=context)

    # The SDK may prepend the tool name; the JSON error envelope survives.
    text = str(error.value)
    envelope = json.loads(text[text.index("{") :])
    assert state["measures_added"] == 1
    assert envelope["details"]["code"] == "outcome_unknown"
    assert envelope["details"]["mutation_status"] == "unknown"
    assert envelope["details"]["replayed"] is False
    assert envelope["details"]["target"] == {"count": 1}
    assert envelope["details"]["request_id"]
    assert "Inspect the score" in envelope["error"]
