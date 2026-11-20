"""Negotiation gates every command on each MuseScore connection."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeError, BridgeRegistry
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.tools import ToolError
from mcp_score.tools.connection import connect_to_musescore, get_musescore_bridge_info
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    fake_connection,
    score_context,
    sent_payloads,
)

if TYPE_CHECKING:
    from mcp_score.context import ScoreContext

HANDSHAKE_REQUEST = {"command": "getCapabilities", "params": {"protocolVersion": 1}}


def capabilities_reply(**changes: Any) -> dict[str, Any]:
    reply = deepcopy(MUSESCORE_HANDSHAKE)
    reply["result"].update(changes)
    return reply


class TestNegotiation:
    @pytest.mark.anyio()
    async def test_connect_returns_negotiated_diagnostics_without_reading_score(
        self,
    ) -> None:
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        registry = BridgeRegistry()
        context = score_context(registry)
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            result = await connect_to_musescore(context)
            diagnostics = await get_musescore_bridge_info(context)

        assert diagnostics == result.bridge
        assert diagnostics.plugin_version == "0.2.0-test"
        assert diagnostics.server_version
        assert diagnostics.protocol_version == 1
        assert "addNote" in diagnostics.commands
        assert "addNote" in diagnostics.sequence_commands
        assert "getScore" not in diagnostics.sequence_commands
        assert sent_payloads(connection) == [HANDSHAKE_REQUEST]

    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            ({"error": "Unknown command: getCapabilities"}, "negotiation failed"),
            (capabilities_reply(protocolVersion=2), "Incompatible"),
            (capabilities_reply(protocolVersion="1"), "invalid capabilities"),
            (capabilities_reply(pluginVersion=""), "invalid capabilities"),
            (capabilities_reply(commands=["ping"]), "missing required"),
            (capabilities_reply(sequenceCommands=["futureCommand"]), "absent"),
            ({"result": []}, "invalid capabilities"),
            ({"result": {"protocolVersion": 1}}, "invalid capabilities"),
        ],
    )
    async def test_incompatible_handshake_closes_connection_and_explains_upgrade(
        self, reply: dict[str, Any], reason: str
    ) -> None:
        connection = fake_connection(reply)
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            connected = await bridge.connect()

        assert connected is False
        assert bridge.is_connected is False
        assert bridge.connection_error is not None
        assert reason in bridge.connection_error
        assert "install-plugin" in bridge.connection_error
        assert sent_payloads(connection) == [HANDSHAKE_REQUEST]
        connection.close.assert_awaited_once()
        with pytest.raises(BridgeError, match="no active negotiated connection"):
            bridge.diagnostics()

    @pytest.mark.anyio()
    async def test_connect_tool_preserves_legacy_plugin_upgrade_cause(self) -> None:
        connection = fake_connection({"error": "Unknown command: getCapabilities"})
        registry = BridgeRegistry()
        with (
            patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
            pytest.raises(ToolError, match="install-plugin") as error,
        ):
            await connect_to_musescore(score_context(registry))
        assert "Unknown command: getCapabilities" in str(error.value)
        assert registry.active is None

    @pytest.mark.anyio()
    async def test_disconnect_invalidates_diagnostics(self) -> None:
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            assert await bridge.connect()
        await bridge.disconnect()
        with pytest.raises(BridgeError, match="no active negotiated connection"):
            bridge.diagnostics()

    @pytest.mark.anyio()
    async def test_diagnostics_cannot_change_negotiated_capabilities(self) -> None:
        connection = fake_connection(MUSESCORE_HANDSHAKE, {"result": "pong"})
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            assert await bridge.connect()
        bridge.diagnostics().commands.clear()
        assert await bridge.ping()

    @pytest.mark.anyio()
    async def test_diagnostics_without_connection_reports_not_connected(
        self, context: ScoreContext
    ) -> None:
        with pytest.raises(ToolError, match="Not connected"):
            await get_musescore_bridge_info(context)


class TestCommandPreflight:
    @pytest.mark.anyio()
    @pytest.mark.parametrize(
        ("action", "params"),
        [
            ("addNote", {"pitch": 60}),
            ("processSequence", {"sequence": [{"action": "addNote"}]}),
            ("processSequence", {"sequence": [{"action": "getScore"}]}),
        ],
    )
    async def test_unsupported_command_is_not_sent(
        self, action: str, params: dict[str, Any]
    ) -> None:
        connection = fake_connection(
            capabilities_reply(
                commands=["getCapabilities", "ping", "getScore", "processSequence"],
                sequenceCommands=["ping"],
            )
        )
        bridge = MuseScoreBridge()
        with (
            patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)),
            pytest.raises(BridgeError, match="does not support"),
        ):
            await bridge.send_command(action, params)
        assert sent_payloads(connection) == [HANDSHAKE_REQUEST]

    @pytest.mark.anyio()
    async def test_lazy_connection_negotiates_before_sending_command(self) -> None:
        connection = fake_connection(MUSESCORE_HANDSHAKE, {"result": "pong"})
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            assert await bridge.ping()
        assert sent_payloads(connection) == [HANDSHAKE_REQUEST, {"command": "ping"}]

    @pytest.mark.anyio()
    @pytest.mark.parametrize("incompatible", [False, True])
    async def test_reconnect_never_sends_mutation_to_unsupported_plugin(
        self, incompatible: bool
    ) -> None:
        dropped = fake_connection(MUSESCORE_HANDSHAKE, ConnectionClosed(None, None))
        reply = (
            capabilities_reply(protocolVersion=2)
            if incompatible
            else capabilities_reply(
                commands=["getCapabilities", "ping"], sequenceCommands=[]
            )
        )
        replacement = fake_connection(reply)
        bridge = MuseScoreBridge()
        with (
            patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[dropped, replacement])),
            pytest.raises(BridgeError),
        ):
            await bridge.send_command("addNote", {"pitch": 60})
        assert sent_payloads(dropped) == [
            HANDSHAKE_REQUEST,
            {"command": "addNote", "params": {"pitch": 60}},
        ]
        assert sent_payloads(replacement) == [HANDSHAKE_REQUEST]
        if incompatible:
            replacement.close.assert_awaited_once()
            assert bridge.is_connected is False

    @pytest.mark.anyio()
    async def test_reconnecting_legacy_plugin_clears_previous_diagnostics(self) -> None:
        first = fake_connection(MUSESCORE_HANDSHAKE)
        legacy = fake_connection({"error": "Unknown command: getCapabilities"})
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[first, legacy])):
            assert await bridge.connect()
            assert not await bridge.connect()
        first.close.assert_awaited_once()
        legacy.close.assert_awaited_once()
        with pytest.raises(BridgeError, match="no active negotiated connection"):
            bridge.diagnostics()
