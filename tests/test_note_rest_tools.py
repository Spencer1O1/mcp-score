"""Public strict inputs and conservative protocol safety for replacements."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError, BridgeRegistry
from mcp_score.bridge.note_rest import FillRestRequest, ReplaceNoteRequest
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    FakeBridge,
    fake_connection,
    score_context,
    sent_payloads,
)


def _arguments() -> dict:
    return {
        "measure": 1,
        "staff": 0,
        "voice": 0,
        "offset": {"numerator": 0, "denominator": 1},
        "duration": {"numerator": 1, "denominator": 4},
        "pitch": 60,
        "tpc": 14,
        "expected": [
            {
                "kind": "rest",
                "type": 25,
                "staff": 0,
                "voice": 0,
                "tick": 0,
                "offset": {"numerator": 0, "denominator": 1},
                "duration": {"numerator": 1, "denominator": 1},
                "actual_duration": {"numerator": 1, "denominator": 1},
                "notes": [],
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
        ("pitch", False),
        ("tpc", 14.0),
        ("offset", {"numerator": 0, "denominator": "1"}),
        ("duration", {"numerator": True, "denominator": 4}),
    ],
)
async def test_mcp_strict_inputs_cannot_silently_change_target(
    field: str, value: object
) -> None:
    registry = BridgeRegistry()
    bridge = FakeBridge()
    registry.active = bridge
    args = _arguments()
    args[field] = value
    with pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "fill_live_rest", args, context=score_context(registry)
        )
    assert bridge.calls == []


@pytest.mark.anyio()
@pytest.mark.parametrize("fill", [True, False])
@pytest.mark.parametrize("case", ["missing", "lost", "sequence"])
async def test_capability_required_mutations_never_replay_or_run_in_sequence(
    fill: bool, case: str
) -> None:
    command = "fillRestWithNote" if fill else "replaceNoteWithRest"
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if case == "missing":
        handshake["result"]["commands"].remove(command)
    connection = fake_connection(handshake, ConnectionClosed(None, None))
    connect = AsyncMock(return_value=connection)
    registry = BridgeRegistry()
    bridge = registry.musescore
    args = _arguments()
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(BridgeError) as caught:
        if case == "sequence":
            await bridge.process_sequence([{"action": command}])
        elif fill:
            await bridge.fill_rest_with_note(FillRestRequest.model_validate(args))
        else:
            args = {
                key: value
                for key, value in args.items()
                if key not in {"duration", "pitch", "tpc"}
            }
            await bridge.replace_note_with_rest(ReplaceNoteRequest.model_validate(args))
    assert [p["command"] for p in sent_payloads(connection)] == (
        ["getCapabilities", command] if case == "lost" else ["getCapabilities"]
    )
    if case == "lost":
        assert caught.value.details["code"] == "outcome_unknown"
    assert connect.await_count == 1


@pytest.mark.anyio()
@pytest.mark.parametrize("fill", [True, False])
async def test_public_tools_send_complete_snapshot_and_publish_actual_readback(
    fill: bool,
) -> None:
    args = _arguments()
    if not fill:
        args = {
            key: value
            for key, value in args.items()
            if key not in {"duration", "pitch", "tpc"}
        }
    before = args["expected"]
    after = deepcopy(before)
    after[0]["kind"] = "chord" if fill else "rest"
    reply = {
        "result": {
            "measure": 1,
            "staff": 0,
            "voice": 0,
            "before": before,
            "after": after,
            "verified": True,
            "mutationStatus": "applied",
            "snapshotScope": "target_voice_measure",
            "selectionStatus": "not_preserved",
            "limitations": ["no full engraving guarantee"],
        }
    }
    connection = fake_connection(MUSESCORE_HANDSHAKE, reply)
    registry = BridgeRegistry()
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.musescore.connect()
        registry.active = registry.musescore
        result = await create_server(registry).call_tool(
            "fill_live_rest" if fill else "replace_live_note_with_rest",
            args,
            context=score_context(registry),
        )
    assert result is not None
    sent = sent_payloads(connection)
    assert (
        sent[-1]["params"]["expected"][0]["actualDuration"]
        == before[0]["actual_duration"]
    )
    assert sent[-1]["command"] == (
        "fillRestWithNote" if fill else "replaceNoteWithRest"
    )
