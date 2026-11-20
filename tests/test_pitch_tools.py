"""The guarded pitch tool sends one explicit, negotiated, non-replayable edit."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError
from mcp_score.bridge.passage import PassageEvent, PassageNote
from mcp_score.bridge.pitch import PitchEdit
from mcp_score.bridge.results import Duration
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from mcp_score.tools.manipulation import set_live_note_pitch
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


def _chord() -> PassageEvent:
    return PassageEvent(
        kind="chord",
        type=93,
        staff=0,
        voice=0,
        tick=480,
        offset=Duration(numerator=1, denominator=4),
        duration=Duration(numerator=3, denominator=4),
        actual_duration=Duration(numerator=3, denominator=4),
        notes=[
            PassageNote(pitch=60, tpc=14, written_tpc=14, displayed_tpc=14, name="C4")
        ],
    )


@pytest.mark.anyio()
async def test_tool_delivers_explicit_snapshot_in_one_command_without_navigation(
    registry: BridgeRegistry,
    context: ScoreContext,
) -> None:
    before = _chord()
    after = before.model_copy(deep=True)
    after.notes[0] = PassageNote(
        pitch=67, tpc=15, written_tpc=15, displayed_tpc=15, name="G4"
    )
    result = PitchEdit(
        measure=1,
        note_index_before=0,
        note_index_after=0,
        before=before,
        after=after,
        verified=True,
        mutation_status="applied",
        snapshot_scope="target_chord",
        limitations=["no global revision"],
    )
    connection = fake_connection(MUSESCORE_HANDSHAKE, {"result": result.model_dump()})
    bridge = registry.musescore
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await bridge.connect()
    registry.active = bridge

    actual = await set_live_note_pitch(
        context, 1, 0, 0, before.offset, 0, 67, 15, before
    )

    assert actual == result
    messages = sent_payloads(connection)
    assert [message["command"] for message in messages] == [
        "getCapabilities",
        "setNotePitch",
    ]
    params = messages[-1]["params"]
    assert params["expected"]["actualDuration"] == {"numerator": 3, "denominator": 4}
    assert params["expected"]["notes"][0]["writtenTpc"] == 14
    assert params["noteIndex"] == 0
    assert params["offset"] == {"numerator": 1, "denominator": 4}


@pytest.mark.anyio()
async def test_new_edit_requires_advertised_capability_before_sending(
    registry: BridgeRegistry,
) -> None:
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    handshake["result"]["commands"].remove("setNotePitch")
    connection = fake_connection(handshake)
    bridge = registry.musescore
    expected = _chord()
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="does not support command .*setNotePitch"),
    ):
        await bridge.set_note_pitch(1, 0, 0, expected.offset, 0, 67, 15, expected)
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
async def test_guarded_edit_is_not_replayed_when_its_reply_is_lost(
    registry: BridgeRegistry,
) -> None:
    connection = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
    connect = AsyncMock(return_value=connection)
    bridge = registry.musescore
    expected = _chord()
    with (
        patch(WEBSOCKETS_CONNECT, connect),
        pytest.raises(BridgeError) as error,
    ):
        await bridge.set_note_pitch(1, 0, 0, expected.offset, 0, 67, 15, expected)
    assert error.value.details["code"] == "outcome_unknown"
    assert connect.await_count == 1
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities",
        "setNotePitch",
    ]


@pytest.mark.anyio()
async def test_guarded_edit_cannot_be_embedded_in_a_mutation_sequence(
    registry: BridgeRegistry,
) -> None:
    connection = fake_connection(MUSESCORE_HANDSHAKE)
    with (
        patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
        pytest.raises(BridgeError, match="does not support sequence step"),
    ):
        await registry.musescore.process_sequence([{"action": "setNotePitch"}])
    assert [message["command"] for message in sent_payloads(connection)] == [
        "getCapabilities"
    ]


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("staff", "voice", "index", "pitch", "tpc", "offset"),
    [
        (-1, 0, 0, 67, 15, Duration(numerator=1, denominator=4)),
        (0, 4, 0, 67, 15, Duration(numerator=1, denominator=4)),
        (0, 0, -1, 67, 15, Duration(numerator=1, denominator=4)),
        (0, 0, 0, 128, 15, Duration(numerator=1, denominator=4)),
        (0, 0, 0, 67, 34, Duration(numerator=1, denominator=4)),
        (0, 0, 0, 67, 15, Duration(numerator=-1, denominator=4)),
    ],
)
async def test_invalid_edit_is_rejected_before_bridge_access(
    connected_bridge: FakeBridge,
    context: ScoreContext,
    staff: int,
    voice: int,
    index: int,
    pitch: int,
    tpc: int,
    offset: Duration,
) -> None:
    with pytest.raises(ToolError):
        await set_live_note_pitch(
            context, 1, staff, voice, offset, index, pitch, tpc, _chord()
        )
    assert connected_bridge.calls == []


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measure", True),
        ("staff", 0.0),
        ("voice", "0"),
        ("note_index", False),
        ("pitch", "67"),
        ("tpc", 15.0),
        ("offset_numerator", True),
        ("offset_denominator", "4"),
        ("snapshot_pitch", 60.0),
        ("snapshot_written_tpc", True),
    ],
)
async def test_mcp_rejects_coerced_coordinates_and_snapshot_numbers_before_bridge(
    registry: BridgeRegistry,
    context: ScoreContext,
    connected_bridge: FakeBridge,
    field: str,
    value: object,
) -> None:
    arguments = {
        "measure": 1,
        "staff": 0,
        "voice": 0,
        "offset": {"numerator": 1, "denominator": 4},
        "note_index": 0,
        "pitch": 67,
        "tpc": 15,
        "expected": _chord().model_dump(),
    }
    if field.startswith("offset_"):
        arguments["offset"][field.removeprefix("offset_")] = value
    elif field.startswith("snapshot_"):
        arguments["expected"]["notes"][0][field.removeprefix("snapshot_")] = value
    else:
        arguments[field] = value
    server = create_server(registry)

    with pytest.raises(ToolError):
        await server.call_tool("set_live_note_pitch", arguments, context=context)

    assert connected_bridge.calls == []
