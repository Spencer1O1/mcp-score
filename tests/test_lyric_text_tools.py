"""Public MCP strict validation, capability gates, receipt coverage and no replay."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError, BridgeRegistry
from mcp_score.bridge.lyric_text import LyricTextRequest
from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.receipts import GuardedLyricTextMutation, ReceiptExecution
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
from tests.test_lyric_text_plugin import _run


def _request() -> LyricTextRequest:
    observed = _run()
    return LyricTextRequest(
        measure=1,
        staff=0,
        voice=0,
        offset={"numerator": 0, "denominator": 1},
        lyric_index=0,
        plain_text="café & < > \" '🙂",
        expected=_snake_case_keys(observed["reply"]["result"]["before"]),
    )


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "field,value",
    [
        ("measure", True),
        ("staff", 0.0),
        ("voice", "0"),
        ("lyric_index", False),
        ("plain_text", 42),
        ("plain_text", ""),
        ("plain_text", "a\nb"),
        ("plain_text", "\ud800"),
        ("plain_text", "\ufdd0"),
        ("plain_text", "x" * 513),
        ("offset", {"numerator": 0, "denominator": "1"}),
    ],
)
async def test_public_sdk_rejects_bad_text_and_coerced_coordinates_before_bridge(
    field: str, value: object
) -> None:
    registry = BridgeRegistry()
    bridge = FakeBridge()
    registry.active = bridge
    arguments = _request().model_dump()
    arguments[field] = value
    with pytest.raises(ToolError):
        await create_server(registry).call_tool(
            "set_live_lyric_text", arguments, context=score_context(registry)
        )
    assert bridge.calls == []


@pytest.mark.anyio()
@pytest.mark.parametrize("receipted", [False, True])
async def test_public_sdk_uses_one_command_and_typed_native_reply(
    receipted: bool,
) -> None:
    request = _request()
    reply = _run()["reply"]
    registry = BridgeRegistry()
    arguments = request.model_dump()
    tool = "set_live_lyric_text"
    if receipted:
        reply = {
            "result": {
                "pluginSession": "test-plugin-session",
                "operationId": "retained",
                "status": "completed",
                "command": "setLyricText",
                "cached": False,
                "historical": True,
                "originalReply": reply,
                "reason": None,
            }
        }
        arguments = {
            "operation_id": "retained",
            "expected_plugin_session": "test-plugin-session",
            "mutation": {"kind": "set_lyric_text", "request": arguments},
        }
        tool = "execute_mutation_once"
    connection = fake_connection(MUSESCORE_HANDSHAKE, reply)
    with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
        assert await registry.activate(registry.musescore)
        result = await create_server(registry).call_tool(
            tool, arguments, context=score_context(registry)
        )
    assert result.structured_content is not None
    assert [p["command"] for p in sent_payloads(connection)] == [
        "getCapabilities",
        "executeMutationOnce" if receipted else "setLyricText",
    ]


@pytest.mark.anyio()
@pytest.mark.parametrize("case", ["lost", "sequence", "command", "receipt_coverage"])
async def test_protocol_requires_command_and_receipt_coverage_without_replay(
    case: str,
) -> None:
    request = _request()
    handshake = deepcopy(MUSESCORE_HANDSHAKE)
    if case == "command":
        handshake["result"]["commands"].remove("setLyricText")
    if case == "receipt_coverage":
        handshake["result"]["receipts"]["supportedMutations"].remove("setLyricText")
    connection = fake_connection(handshake, ConnectionClosed(None, None))
    connect = AsyncMock(return_value=connection)
    registry = BridgeRegistry()
    with patch(WEBSOCKETS_CONNECT, connect), pytest.raises(BridgeError) as caught:
        if case == "sequence":
            await registry.musescore.process_sequence([{"action": "setLyricText"}])
        elif case == "receipt_coverage":
            await registry.musescore.execute_mutation_once(
                ReceiptExecution(
                    operation_id="retained",
                    expected_plugin_session="test-plugin-session",
                    mutation=GuardedLyricTextMutation(
                        kind="set_lyric_text", request=request
                    ),
                )
            )
        else:
            await registry.musescore.set_lyric_text(request)
    assert [p["command"] for p in sent_payloads(connection)] == (
        ["getCapabilities", "setLyricText"] if case == "lost" else ["getCapabilities"]
    )
    assert connect.await_count == 1
    if case == "lost":
        assert caught.value.details["code"] == "outcome_unknown"
