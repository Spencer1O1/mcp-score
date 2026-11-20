"""Strict batch tool boundaries, negotiated coverage and no automatic replay."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import _camel_case_keys
from mcp_score.bridge.pitch import ExpectedChord, PitchEdit, PitchEditRequest
from mcp_score.bridge.pitch_batch import PitchBatchEdit, PitchBatchRequest
from mcp_score.bridge.receipts import GuardedPitchBatchMutation, ReceiptExecution
from mcp_score.bridge.results import Duration
from mcp_score.server import create_server
from mcp_score.tools import ToolError
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


def _batch() -> PitchBatchRequest:
    before = _chord()
    other = before.model_copy(
        update={"tick": 0, "offset": Duration(numerator=0, denominator=1)}
    )
    return PitchBatchRequest(
        edits=[
            PitchEditRequest(
                measure=1,
                staff=0,
                voice=0,
                offset=event.offset.model_dump(),
                note_index=0,
                pitch=67,
                tpc=15,
                expected=ExpectedChord.model_validate(event.model_dump()),
            )
            for event in [before, other]
        ]
    )


def _result() -> PitchBatchEdit:
    edits = []
    for request in _batch().edits:
        before = _chord().model_copy(
            update={"tick": request.expected.tick, "offset": request.expected.offset}
        )
        after = before.model_copy(deep=True)
        after.notes[0] = after.notes[0].model_copy(
            update={
                "pitch": 67,
                "tpc": 15,
                "written_tpc": 15,
                "displayed_tpc": 15,
                "name": "G4",
            }
        )
        edits.append(
            PitchEdit(
                measure=1,
                note_index_before=0,
                note_index_after=0,
                before=before,
                after=after,
                verified=True,
                mutation_status="applied",
                snapshot_scope="target_chord",
                limitations=[],
            )
        )
    return PitchBatchEdit(
        edits=edits,
        verified=True,
        mutation_status="applied",
        snapshot_scope="target_chords",
        limitations=["target chords only"],
    )


@pytest.mark.anyio()
async def test_mcp_batch_delivers_all_explicit_targets_in_one_command(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    result = _result()
    connection = fake_connection(
        MUSESCORE_HANDSHAKE, {"result": _camel_case_keys(result.model_dump())}
    )
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        output = await create_server(registry).call_tool(
            "set_live_note_pitches", _batch().model_dump(), context=context
        )
    assert output.structured_content == result.model_dump()
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities",
        "setNotePitches",
    ]
    assert (
        sent_payloads(connection)[-1]["params"]["edits"][1]["expected"]["notes"][0][
            "writtenTpc"
        ]
        == 14
    )


@pytest.mark.anyio()
async def test_mcp_receipted_batch_retains_actual_command_and_typed_variant(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    receipt = {
        "pluginSession": "test-plugin-session",
        "operationId": "batch-id",
        "command": "setNotePitches",
        "status": "completed",
        "cached": False,
        "historical": True,
        "reason": None,
        "originalReply": {"result": _camel_case_keys(_result().model_dump())},
    }
    connection = fake_connection(MUSESCORE_HANDSHAKE, {"result": receipt})
    request = ReceiptExecution(
        operation_id="batch-id",
        expected_plugin_session="test-plugin-session",
        mutation=GuardedPitchBatchMutation(kind="set_note_pitches", request=_batch()),
    )
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        output = await create_server(registry).call_tool(
            "execute_mutation_once", request.model_dump(), context=context
        )
    assert output.structured_content is not None
    assert output.structured_content["command"] == "setNotePitches"
    assert (
        sent_payloads(connection)[-1]["params"]["mutation"]["kind"]
        == "set_note_pitches"
    )


@pytest.mark.anyio()
@pytest.mark.parametrize("receipted", [False, True])
async def test_older_plugin_batch_coverage_rejects_before_dispatch(
    registry: BridgeRegistry,
    receipted: bool,
) -> None:
    old = deepcopy(MUSESCORE_HANDSHAKE)
    old["result"]["commands"].remove("setNotePitches")
    old["result"]["receipts"]["supportedMutations"] = ["setNotePitch"]
    connection = fake_connection(old)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="does not support"),
    ):
        if receipted:
            await registry.musescore.execute_mutation_once(
                ReceiptExecution(
                    operation_id="retained-id",
                    expected_plugin_session="test-plugin-session",
                    mutation=GuardedPitchBatchMutation(
                        kind="set_note_pitches", request=_batch()
                    ),
                )
            )
        else:
            await registry.musescore.set_note_pitches(_batch())
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "change", ["too_many", "empty", "alias", "bool", "float", "coerced_pitch"]
)
async def test_invalid_batch_rejects_at_mcp_boundary_before_native_access(
    registry: BridgeRegistry,
    context: ScoreContext,
    change: str,
) -> None:
    payload = _batch().model_dump()
    if change == "too_many":
        payload["edits"] *= 17
    elif change == "empty":
        payload["edits"] = []
    elif change == "alias":
        payload["edits"][1] = deepcopy(payload["edits"][0])
        payload["edits"][1]["offset"] = {"numerator": 2, "denominator": 8}
        payload["edits"][1]["note_index"] = 1
    elif change == "bool":
        payload["edits"][1]["note_index"] = True
    elif change == "float":
        payload["edits"][1]["offset"]["numerator"] = 0.0
    else:
        payload["edits"][1]["pitch"] = "67"
    connect = AsyncMock()
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "set_live_note_pitches", payload, context=context
        )
    connect.assert_not_awaited()


@pytest.mark.anyio()
async def test_batch_is_neither_sequence_step_nor_automatically_replayed(
    registry: BridgeRegistry,
) -> None:
    connection = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    connect = AsyncMock(return_value=connection)
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(BridgeError) as lost:
        await registry.musescore.set_note_pitches(_batch())
    assert lost.value.details["replayed"] is False and connect.await_count == 1
    assert len(sent_payloads(connection)) == 2
    fresh = fake_connection(MUSESCORE_HANDSHAKE)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=fresh)),
        pytest.raises(BridgeError, match="sequence step"),
    ):
        await registry.musescore.process_sequence(
            [{"action": "setNotePitches", "params": {}}]
        )
    assert len(sent_payloads(fresh)) == 1
