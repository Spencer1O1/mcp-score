"""A restarted disposable plugin must reject an old retained operation ticket."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import websockets

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import MuseScoreBridge, _camel_case_keys
from mcp_score.bridge.receipts import ReceiptExecution

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_RECEIPT_RESTART_FIXTURE") != "1",
        reason="requires isolated restart with a retained old ticket",
    ),
]


@pytest.mark.anyio()
async def test_restart_changes_native_session_and_old_request_never_executes() -> None:
    ticket = ReceiptExecution.model_validate_json(
        Path(os.environ["MCP_SCORE_RECEIPT_RESTART_STATE"]).read_text()
    )
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 1)
        capabilities = bridge.diagnostics().receipts
        assert capabilities is not None
        assert capabilities.plugin_session != ticket.expected_plugin_session
        for operation in [bridge.execute_mutation_once, bridge.read_mutation_receipt]:
            with pytest.raises(BridgeError) as error:
                await operation(ticket)
            assert error.value.details["code"] == "stale_plugin_session"
        # Independently bypass Python's preflight to prove native session enforcement.
        params = _camel_case_keys(ticket.model_dump())
        assert isinstance(params, dict)
        params["receiptVersion"] = 1
        async with websockets.connect("ws://localhost:8765") as connection:
            for command in ["executeMutationOnce", "readMutationReceipt"]:
                await connection.send(
                    json.dumps({"command": command, "params": params})
                )
                reply = json.loads(await connection.recv())
                assert reply["code"] == "stale_plugin_session"
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        await bridge.disconnect()
