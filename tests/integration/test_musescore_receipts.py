"""Receipted guarded edits against the disposable native pitch fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import websockets

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.pitch import ExpectedChord, PitchEditRequest
from mcp_score.bridge.receipts import GuardedPitchMutation, ReceiptExecution

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_RECEIPT_FIXTURE") != "1",
        reason="requires disposable pitch fixture and receipt restart state path",
    ),
]


async def _request(bridge: MuseScoreBridge, *, noop: bool = False) -> ReceiptExecution:
    passage = await bridge.read_passage(1, 1, 0, 1)
    event = next(event for event in passage.measures[0].events if event.kind == "chord")
    receipt_capabilities = bridge.diagnostics().receipts
    assert receipt_capabilities is not None
    return ReceiptExecution(
        operation_id=str(uuid4()),
        expected_plugin_session=receipt_capabilities.plugin_session,
        mutation=GuardedPitchMutation(
            kind="set_note_pitch",
            request=PitchEditRequest(
                measure=1,
                staff=0,
                voice=1,
                offset=event.offset.model_dump(),
                note_index=0,
                pitch=event.notes[0].pitch if noop else 67,
                tpc=event.notes[0].tpc if noop else 15,
                expected=ExpectedChord.model_validate(event.model_dump()),
            ),
        ),
    )


def _applied(receipt: Any) -> bool:
    return (
        receipt.status == "completed"
        and receipt.original_reply is not None
        and receipt.original_reply.get("result", {}).get("mutationStatus") == "applied"
    )


@pytest.mark.anyio()
async def test_native_duplicate_and_post_undo_receipts_are_historical() -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        baseline = await bridge.read_passage(1, 1)
        request = await _request(bridge)
        # The caller retains both identity and exact payload before the first send.
        path = Path(os.environ["MCP_SCORE_RECEIPT_RESTART_STATE"])
        path.write_text(request.model_dump_json())
        receipt = await bridge.execute_mutation_once(request)
        applied = _applied(receipt)
        assert applied
        repeat = await bridge.execute_mutation_once(request)
        assert repeat.cached and repeat.original_reply == receipt.original_reply
        changed = request.model_copy(deep=True)
        changed.mutation.request.pitch = 69
        changed.mutation.request.tpc = 17
        with pytest.raises(BridgeError) as conflict:
            await bridge.execute_mutation_once(changed)
        assert conflict.value.details["code"] == "operation_id_conflict"
        await bridge.undo()
        applied = False
        historical = await bridge.execute_mutation_once(request)
        restored = await bridge.read_passage(1, 1)
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert await bridge.read_mutation_receipt(request) == historical
        assert restored == baseline
    finally:
        if applied:
            await bridge.undo()
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_dropped_reply_explicit_retry_does_not_edit_twice() -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        baseline = await bridge.read_passage(1, 1)
        request = await _request(bridge)
        connections = 0

        async def proxy(client: Any) -> None:
            nonlocal connections
            connections += 1
            async with websockets.connect("ws://localhost:8765") as upstream:
                async for message in client:
                    await upstream.send(message)
                    reply = await upstream.recv()
                    if json.loads(message).get("command") == "executeMutationOnce":
                        # Drop after the plugin executed and retained its reply.
                        await client.close()
                        return
                    await client.send(reply)

        async with websockets.serve(proxy, "127.0.0.1", 0) as server:
            proxy_bridge = MuseScoreBridge(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            try:
                with pytest.raises(BridgeError) as lost:
                    await proxy_bridge.execute_mutation_once(request)
                assert connections == 1  # No automatic mutation replay/reconnect.
                assert lost.value.details["operation_id"] == request.operation_id
            finally:
                await proxy_bridge.disconnect()
        receipt = await bridge.execute_mutation_once(request)
        applied = _applied(receipt)
        assert applied and receipt.cached
        assert receipt.original_reply is not None
        assert receipt.original_reply["result"]["before"]["notes"][0]["pitch"] == 60
        assert receipt.original_reply["result"]["after"]["notes"][-1]["pitch"] == 67
        await bridge.undo()
        applied = False
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        if applied:
            await bridge.undo()
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_full_ledger_keeps_retained_receipt_and_rejects_new_ids() -> None:
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 1)
        request = await _request(bridge, noop=True)
        first = await bridge.execute_mutation_once(request)
        assert first.status == "completed"
        limits = bridge.diagnostics().receipts
        assert limits is not None
        full = None
        for _ in range(limits.capacity + 1):
            next_request = request.model_copy(update={"operation_id": str(uuid4())})
            try:
                await bridge.execute_mutation_once(next_request)
            except BridgeError as error:
                full = error
                break
        assert full is not None and full.details["code"] == "receipt_ledger_full"
        assert (
            await bridge.execute_mutation_once(request)
        ).original_reply == first.original_reply
        assert (
            await bridge.read_mutation_receipt(request)
        ).original_reply == first.original_reply
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        await bridge.disconnect()
