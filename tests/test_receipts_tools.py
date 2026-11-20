"""Caller identities survive MCP validation, transport loss and session changes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.pitch import ExpectedChord, PitchEditRequest
from mcp_score.bridge.receipts import (
    GuardedPitchMutation,
    ReceiptExecution,
)
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from mcp_score.tools.receipts import execute_mutation_once
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    sent_payloads,
)
from tests.test_pitch_tools import _chord

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext


def _request() -> ReceiptExecution:
    chord = _chord()
    return ReceiptExecution(
        operation_id="retained-before-send",
        expected_plugin_session="test-plugin-session",
        mutation=GuardedPitchMutation(
            kind="set_note_pitch",
            request=PitchEditRequest(
                measure=1,
                staff=0,
                voice=0,
                offset=chord.offset.model_dump(),
                note_index=0,
                pitch=67,
                tpc=15,
                expected=ExpectedChord.model_validate(chord.model_dump()),
            ),
        ),
    )


def _reply(*, cached: bool = False) -> dict[str, object]:
    return {
        "result": {
            "pluginSession": "test-plugin-session",
            "operationId": "retained-before-send",
            "command": "setNotePitch",
            "status": "completed",
            "cached": cached,
            "historical": True,
            "originalReply": {"result": {"mutationStatus": "applied"}},
            "reason": None,
        }
    }


@pytest.mark.anyio()
async def test_execute_preserves_identity_and_original_native_reply_keys(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    request = _request()
    connection = fake_connection(MUSESCORE_HANDSHAKE, _reply())
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        receipt = await execute_mutation_once(
            context,
            request.operation_id,
            request.expected_plugin_session,
            request.mutation,
        )
    assert receipt.original_reply == {"result": {"mutationStatus": "applied"}}
    params = sent_payloads(connection)[-1]["params"]
    assert params["operationId"] == request.operation_id
    assert params["expectedPluginSession"] == request.expected_plugin_session
    assert params["receiptVersion"] == 1
    assert params["mutation"]["request"]["expected"]["notes"][0]["writtenTpc"] == 14


@pytest.mark.anyio()
async def test_lost_reply_does_not_auto_replay_and_explicit_retry_retains_identity(
    registry: BridgeRegistry,
) -> None:
    request = _request()
    first = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    second = fake_connection(MUSESCORE_HANDSHAKE, _reply(cached=True))
    connect = AsyncMock(side_effect=[first, second])
    with patch(WEBSOCKETS_CONNECT, connect):
        with pytest.raises(BridgeError) as error:
            await registry.musescore.execute_mutation_once(request)
        assert connect.await_count == 1
        assert error.value.details["operation_id"] == request.operation_id
        assert (
            error.value.details["expected_plugin_session"]
            == request.expected_plugin_session
        )
        assert error.value.details["replayed"] is False
        assert error.value.details["mutation_status"] == "unknown"
        result = await registry.musescore.execute_mutation_once(request)
    assert result.cached and result.historical
    assert sent_payloads(first)[-1] == sent_payloads(second)[-1]


@pytest.mark.anyio()
@pytest.mark.parametrize("lookup", [False, True])
async def test_restarted_plugin_never_substitutes_session_before_retry_or_lookup(
    registry: BridgeRegistry,
    lookup: bool,
) -> None:
    request = _request()
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    handshake["result"]["receipts"]["pluginSession"] = "restarted-instance"
    connection = fake_connection(handshake)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError) as error,
    ):
        if lookup:
            await registry.musescore.read_mutation_receipt(request)
        else:
            await registry.musescore.execute_mutation_once(request)
    assert error.value.details["code"] == "stale_plugin_session"
    assert error.value.details["operation_id"] == request.operation_id
    assert (
        error.value.details["expected_plugin_session"]
        == request.expected_plugin_session
    )
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
async def test_lookup_safe_read_reconnect_revalidates_original_session(
    registry: BridgeRegistry,
) -> None:
    request = _request()
    first = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    changed = deepcopy(MUSESCORE_HANDSHAKE)
    changed["result"]["receipts"]["pluginSession"] = "new-session"
    second = fake_connection(changed)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[first, second])),
        pytest.raises(BridgeError) as error,
    ):
        await registry.musescore.read_mutation_receipt(request)
    assert error.value.details["code"] == "stale_plugin_session"
    assert [message["command"] for message in sent_payloads(second)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
@pytest.mark.parametrize("version", [None, 2])
async def test_missing_or_incompatible_receipt_protocol_fails_before_mutation(
    registry: BridgeRegistry,
    version: int | None,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if version is None:
        del handshake["result"]["receipts"]
    else:
        handshake["result"]["receipts"]["protocolVersion"] = version
    connection = fake_connection(handshake)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="receipt protocol") as error,
    ):
        await registry.musescore.execute_mutation_once(_request())
    assert error.value.details["operation_id"] == _request().operation_id
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
async def test_cancellation_keeps_caller_identity_and_discards_the_socket(
    registry: BridgeRegistry,
) -> None:
    receiving = asyncio.Event()
    connection = fake_connection(MUSESCORE_HANDSHAKE)
    import json

    count = 0

    async def receive() -> str:
        nonlocal count
        count += 1
        if count == 1:
            return json.dumps(MUSESCORE_HANDSHAKE)
        receiving.set()
        await asyncio.Event().wait()
        return ""

    connection.recv.side_effect = receive
    request = _request()
    async with asyncio.timeout(5):
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            task = asyncio.create_task(
                registry.musescore.execute_mutation_once(request)
            )
            await receiving.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    connection.close.assert_awaited_once()
    assert (
        sent_payloads(connection)[-1]["params"]["operationId"] == request.operation_id
    )


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "change", ["operation_id", "session", "kind", "pitch", "extra"]
)
async def test_mcp_rejects_invalid_identity_or_nested_mutation_before_connection(
    registry: BridgeRegistry,
    context: ScoreContext,
    change: str,
) -> None:
    arguments = _request().model_dump()
    if change == "operation_id":
        arguments["operation_id"] = True
    elif change == "session":
        arguments["expected_plugin_session"] = 1
    elif change == "kind":
        arguments["mutation"]["kind"] = "undo"
    elif change == "pitch":
        arguments["mutation"]["request"]["pitch"] = "67"
    else:
        arguments["mutation"]["extra"] = True
    connect = AsyncMock()
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "execute_mutation_once", arguments, context=context
        )
    connect.assert_not_awaited()


@pytest.mark.anyio()
@pytest.mark.parametrize("change", ["operationId", "pluginSession", "originalReply"])
async def test_wrong_or_inconsistent_receipt_does_not_claim_a_verified_outcome(
    registry: BridgeRegistry,
    change: str,
) -> None:
    reply = _reply()
    reply["result"][change] = (
        None if change == "originalReply" else "different-identity"
    )
    connection = fake_connection(MUSESCORE_HANDSHAKE, reply)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError) as error,
    ):
        await registry.musescore.execute_mutation_once(_request())
    assert error.value.details["code"] == "invalid_mutation_receipt"
    assert error.value.details["mutation_status"] == "unknown"
    assert error.value.details["operation_id"] == _request().operation_id
    assert len(sent_payloads(connection)) == 2


@pytest.mark.anyio()
async def test_inconsistent_receipt_advertisement_rejects_handshake(
    registry: BridgeRegistry,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    handshake["result"]["commands"].remove("executeMutationOnce")
    connection = fake_connection(handshake)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="advertise missing commands"),
    ):
        await registry.musescore.execute_mutation_once(_request())
    assert [payload["command"] for payload in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
async def test_mcp_execute_and_lookup_publish_structured_historical_native_reply(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    connection = fake_connection(MUSESCORE_HANDSHAKE, _reply(), _reply(cached=True))
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        server = create_server(registry)
        executed = await server.call_tool(
            "execute_mutation_once", _request().model_dump(), context=context
        )
        looked_up = await server.call_tool(
            "read_mutation_receipt",
            {
                "operation_id": _request().operation_id,
                "expected_plugin_session": _request().expected_plugin_session,
            },
            context=context,
        )
    assert executed.structured_content is not None
    assert looked_up.structured_content is not None
    assert executed.structured_content["original_reply"] == {
        "result": {"mutationStatus": "applied"}
    }
    assert looked_up.structured_content["historical"] is True
    assert looked_up.structured_content["cached"] is True
    assert (
        looked_up.structured_content["original_reply"]
        == executed.structured_content["original_reply"]
    )
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities",
        "executeMutationOnce",
        "readMutationReceipt",
    ]
