"""Public SDK validation, negotiated command coverage and no replay for topology."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.chord_tone import AddChordToneRequest, RemoveChordToneRequest
from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.receipts import (
    GuardedAddChordToneMutation,
    GuardedRemoveChordToneMutation,
    ReceiptExecution,
)
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    sent_payloads,
)
from tests.test_chord_tone_plugin import _execute

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext


def _request(operation: str) -> AddChordToneRequest | RemoveChordToneRequest:
    observed = _execute(f"operation='{operation}';")
    arguments = dict(
        measure=1,
        staff=0,
        voice=0,
        offset={"numerator": 1, "denominator": 4},
        expected=_snake_case_keys(observed["before"]),
    )
    if operation == "add":
        return AddChordToneRequest(**arguments, pitch=67, tpc=15)
    return RemoveChordToneRequest(**arguments, note_index=0)


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("receipted", [False, True])
async def test_sdk_returns_typed_actual_events_in_one_native_call(
    registry: BridgeRegistry, context: ScoreContext, operation: str, receipted: bool
) -> None:
    request = _request(operation)
    observed = _execute(f"operation='{operation}';")
    command = operation + "ChordTone"
    reply = observed["reply"]
    if receipted:
        reply = {
            "result": {
                "pluginSession": "test-plugin-session",
                "operationId": "retained",
                "status": "completed",
                "command": command,
                "cached": False,
                "historical": True,
                "originalReply": reply,
                "reason": None,
            }
        }
    connection = fake_connection(MUSESCORE_HANDSHAKE, reply)
    arguments = request.model_dump()
    tool = operation + "_live_chord_tone"
    if receipted:
        arguments = {
            "operation_id": "retained",
            "expected_plugin_session": "test-plugin-session",
            "mutation": {"kind": operation + "_chord_tone", "request": arguments},
        }
        tool = "execute_mutation_once"
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        result = await create_server(registry).call_tool(
            tool, arguments, context=context
        )
    assert result.structured_content is not None
    messages = sent_payloads(connection)
    assert [message["command"] for message in messages] == [
        "getCapabilities",
        "executeMutationOnce" if receipted else command,
    ]
    if receipted:
        assert result.structured_content["command"] == command
        assert messages[-1]["params"]["mutation"]["kind"] == operation + "_chord_tone"
    else:
        assert result.structured_content["operation"] == operation
        assert result.structured_content["after"]["notes"]


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("case", ["missing", "receipt_coverage", "lost", "sequence"])
async def test_negotiated_topology_coverage_no_sequence_and_no_replay(
    registry: BridgeRegistry, operation: str, case: str
) -> None:
    command = operation + "ChordTone"
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if case == "missing":
        handshake["result"]["commands"].remove(command)
    if case == "receipt_coverage":
        handshake["result"]["receipts"]["supportedMutations"].remove(command)
    connection = fake_connection(
        handshake, *([ConnectionClosed(None, None)] if case == "lost" else [])
    )
    request = _request(operation)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError),
    ):
        if case == "sequence":
            await registry.musescore.process_sequence([{"action": command}])
        elif case == "receipt_coverage":
            mutation = (
                GuardedAddChordToneMutation(kind="add_chord_tone", request=request)
                if isinstance(request, AddChordToneRequest)
                else GuardedRemoveChordToneMutation(
                    kind="remove_chord_tone", request=request
                )
            )
            await registry.musescore.execute_mutation_once(
                ReceiptExecution(
                    operation_id="retained",
                    expected_plugin_session="test-plugin-session",
                    mutation=mutation,
                )
            )
        elif isinstance(request, AddChordToneRequest):
            await registry.musescore.add_chord_tone(request)
        else:
            await registry.musescore.remove_chord_tone(request)
    assert [message["command"] for message in sent_payloads(connection)] == (
        ["getCapabilities", command] if case == "lost" else ["getCapabilities"]
    )


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize(
    "change",
    [
        "missing_lyrics",
        "missing_articulations",
        "missing_text",
        "bool",
        "float",
        "huge",
    ],
)
async def test_strict_sdk_inputs_fail_before_connection(
    registry: BridgeRegistry, context: ScoreContext, operation: str, change: str
) -> None:
    arguments = _request(operation).model_dump()
    if change.startswith("missing_"):
        del arguments["expected"][change.removeprefix("missing_")]
    elif change == "bool":
        arguments["pitch" if operation == "add" else "note_index"] = True
    elif change == "float":
        arguments["offset"]["numerator"] = 1.0
    else:
        arguments["offset"]["numerator"] = 2**54 + 1
    connect = AsyncMock()
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(ToolError):
        await create_server(registry).call_tool(
            operation + "_live_chord_tone", arguments, context=context
        )
    connect.assert_not_awaited()
