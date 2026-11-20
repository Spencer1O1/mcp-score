"""Guarded batches and a fixed midbatch fault in the disposable restarted fixture."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import (
    MuseScoreBridge,
    _camel_case_keys,
    _snake_case_keys,
)
from mcp_score.bridge.pitch import ExpectedChord, PitchEditRequest
from mcp_score.bridge.pitch_batch import PitchBatchEdit, PitchBatchRequest
from mcp_score.bridge.receipts import GuardedPitchBatchMutation, ReceiptExecution

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_PITCH_BATCH_FIXTURE") != "1",
        reason="requires the explicitly patched owned pitch-batch fixture",
    ),
]


async def _targets(bridge: MuseScoreBridge, *, noop: bool = False) -> PitchBatchRequest:
    score = await bridge.get_score()
    assert score.title == "Guarded pitch fixture"
    passage = await bridge.read_passage(1, 1)
    chord = next(
        event
        for event in passage.measures[0].events
        if event.kind == "chord" and event.staff == 0 and event.voice == 1
    )
    melody = next(
        event
        for event in passage.measures[0].events
        if event.kind == "chord" and event.staff == 0 and event.voice == 0
    )
    edits = []
    for event, pitch, tpc in [(chord, 67, 15), (melody, 72, 14)]:
        edits.append(
            PitchEditRequest(
                measure=1,
                staff=event.staff,
                voice=event.voice,
                offset=event.offset.model_dump(),
                note_index=0,
                pitch=event.notes[0].pitch if noop else pitch,
                tpc=event.notes[0].tpc if noop else tpc,
                expected=ExpectedChord.model_validate(event.model_dump()),
            )
        )
    return PitchBatchRequest(edits=edits)


def _operation(bridge: MuseScoreBridge, request: PitchBatchRequest) -> ReceiptExecution:
    capabilities = bridge.diagnostics().receipts
    assert (
        capabilities is not None
        and "setNotePitches" in capabilities.supported_mutations
    )
    return ReceiptExecution(
        operation_id=str(uuid4()),
        expected_plugin_session=capabilities.plugin_session,
        mutation=GuardedPitchBatchMutation(kind="set_note_pitches", request=request),
    )


@pytest.mark.anyio()
async def test_native_batch_success_and_retained_history_have_one_undo_step() -> None:
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 1)
        request = await _targets(bridge)
        operation = _operation(bridge, request)
        receipt = await bridge.execute_mutation_once(operation)
        assert receipt.status == "completed" and receipt.command == "setNotePitches"
        assert receipt.original_reply is not None and "result" in receipt.original_reply
        result = PitchBatchEdit.model_validate(
            _snake_case_keys(receipt.original_reply["result"])
        )
        assert result.mutation_status == "applied" and len(result.edits) == 2
        assert [note.name for note in result.edits[0].after.notes] == ["E4", "G4"]
        assert result.edits[1].after.notes[0].name == "C5"
        actual = await bridge.read_passage(1, 1)
        expected = baseline.model_copy(deep=True)
        for edit in result.edits:
            events = expected.measures[0].events
            index = next(
                index
                for index, event in enumerate(events)
                if event.staff == edit.before.staff
                and event.voice == edit.before.voice
                and event.tick == edit.before.tick
                and event.kind == "chord"
            )
            assert edit.before == events[index]
            assert edit.before.lyrics == edit.after.lyrics
            assert edit.before.articulations == edit.after.articulations
            events[index] = edit.after
        assert actual == expected
        duplicate = await bridge.execute_mutation_once(operation)
        assert duplicate.cached and duplicate.original_reply == receipt.original_reply
        assert await bridge.read_passage(1, 1) == expected
        # One proven own applied latest command; never retry an uncertain undo.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == baseline
        historical = await bridge.execute_mutation_once(operation)
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_final_stale_target_and_all_noops_leave_prior_undo_entry() -> None:
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 1)
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 1)
        assert sentinel != baseline
        noop = await _targets(bridge, noop=True)
        result = await bridge.set_note_pitches(noop)
        assert result.mutation_status == "unchanged"
        assert await bridge.read_passage(1, 1) == sentinel
        duplicate = _camel_case_keys((await _targets(bridge)).edits[0].model_dump())
        assert isinstance(duplicate, dict)
        equivalent = {
            **duplicate,
            "offset": {"numerator": 2, "denominator": 8},
            "noteIndex": 1,
        }
        with pytest.raises(BridgeError) as aliased:
            await bridge.send_command(
                "setNotePitches", {"edits": [duplicate, equivalent]}
            )
        assert aliased.value.details["code"] == "duplicate_chord"
        assert aliased.value.details["mutation_status"] == "not_applied"
        assert await bridge.read_passage(1, 1) == sentinel
        stale = await _targets(bridge)
        stale.edits[-1].expected.notes[0].pitch += 1
        with pytest.raises(BridgeError) as error:
            await bridge.set_note_pitches(stale)
        assert error.value.details["mutation_status"] == "not_applied"
        assert error.value.details["failed_index"] == 1
        assert await bridge.read_passage(1, 1) == sentinel
        # Both batch attempts are proven not to create an undo command.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_midbatch_cancel_restores_all_targets_and_prior_undo_entry() -> (
    None
):
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 1)
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 1)
        assert sentinel != baseline
        request = await _targets(bridge)
        armed = await bridge.send_command("_testArmPitchBatchFault")
        assert armed["result"]["armed"] is True
        operation = _operation(bridge, request)
        receipt = await bridge.execute_mutation_once(operation)
        assert receipt.original_reply is not None
        failure = receipt.original_reply
        assert failure["mutation_status"] == "rolled_back"
        assert failure["rollback_verified"] is True
        assert failure["actual"] == failure["before"]
        probe = (await bridge.send_command("_testReadPitchBatchFault"))["result"]
        assert probe["armed"] is False
        observed = probe["observed"]
        assert observed["originalPitch"] == 60
        assert observed["pitch"] == observed["requestedPitch"] == 67
        assert observed["tpc"] == observed["writtenTpc"] == 15
        assert await bridge.read_passage(1, 1) == sentinel
        duplicate = await bridge.execute_mutation_once(operation)
        assert duplicate.cached and duplicate.original_reply == receipt.original_reply
        assert (await bridge.send_command("_testReadPitchBatchFault"))[
            "result"
        ] == probe
        assert await bridge.read_passage(1, 1) == sentinel
        # Verified rollback plus full current passage proves the sentinel is latest.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == baseline
    finally:
        # No cleanup undo after uncertainty, intervening state or a prior attempt.
        await bridge.disconnect()
