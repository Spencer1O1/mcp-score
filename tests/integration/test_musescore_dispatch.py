"""Raw wire probes against the existing disposable CI MuseScore instance."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from mcp_score.bridge.musescore import DEFAULT_HOST, DEFAULT_PORT
from tests.dispatch_cases import INVALID_ENVELOPES

pytestmark = pytest.mark.integration
_REPLY_TIMEOUT_SECONDS = 5


@pytest.mark.anyio()
async def test_invalid_envelopes_return_errors_and_keep_same_socket_usable() -> None:
    async with websockets.connect(f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}") as connection:
        for message, error in INVALID_ENVELOPES:
            await connection.send(message)
            reply = json.loads(
                await asyncio.wait_for(connection.recv(), _REPLY_TIMEOUT_SECONDS)
            )
            assert isinstance(reply, dict), (message, reply)
            assert isinstance(reply.get("error"), str), (message, reply)
            assert error in reply["error"], (message, reply)
            assert "result" not in reply, (message, reply)

            await connection.send('{"command":"ping"}')
            followup = json.loads(
                await asyncio.wait_for(connection.recv(), _REPLY_TIMEOUT_SECONDS)
            )
            assert followup == {"result": "pong"}, (message, followup)

        await connection.send('{"command":"ping","params":{}}')
        assert json.loads(
            await asyncio.wait_for(connection.recv(), _REPLY_TIMEOUT_SECONDS)
        ) == {"result": "pong"}
