"""Native structural tone edits and fixed post-change cancellation in owned CI."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.chord_tone import (
    AddChordToneRequest,
    ChordToneEdit,
    ChordToneSnapshot,
    RemoveChordToneRequest,
)
from mcp_score.bridge.musescore import MuseScoreBridge, _snake_case_keys
from mcp_score.bridge.receipts import (
    GuardedAddChordToneMutation,
    GuardedRemoveChordToneMutation,
    ReceiptExecution,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_CHORD_TONE_FIXTURE") != "1",
        reason="requires explicitly patched owned chord-tone fixture",
    ),
]


def _can_remove() -> bool:
    return tuple(map(int, os.environ["MUSESCORE_VERSION"].split("."))) >= (4, 6)


def _event(passage, staff: int = 0):
    return next(
        event
        for event in passage.measures[0].events
        if event.kind == "chord"
        and event.staff == staff
        and event.voice == (1 if staff == 0 else 0)
    )


def _request(passage, operation: str, staff: int = 0):
    event = _event(passage, staff)
    common = dict(
        measure=1,
        staff=staff,
        voice=event.voice,
        offset=event.offset.model_dump(),
        expected=ChordToneSnapshot.model_validate(event.model_dump()),
    )
    if operation == "remove":
        return RemoveChordToneRequest(**common, note_index=0)
    pitch, tpc = {0: (61, 21), 4: (60, 14), 5: (50, 16)}[staff]
    return AddChordToneRequest(**common, pitch=pitch, tpc=tpc)


def _operation(bridge: MuseScoreBridge, request):
    capability = bridge.diagnostics().receipts
    assert capability is not None
    mutation = (
        GuardedAddChordToneMutation(kind="add_chord_tone", request=request)
        if isinstance(request, AddChordToneRequest)
        else GuardedRemoveChordToneMutation(kind="remove_chord_tone", request=request)
    )
    return ReceiptExecution(
        operation_id=str(uuid4()),
        expected_plugin_session=capability.plugin_session,
        mutation=mutation,
    )


def _replaced(passage, result: ChordToneEdit):
    expected = passage.model_copy(deep=True)
    index = next(
        index
        for index, event in enumerate(expected.measures[0].events)
        if event == result.before
    )
    expected.measures[0].events[index] = result.after
    return expected


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "operation,staff", [("add", 0), ("add", 4), ("add", 5), ("remove", 0)]
)
async def test_native_tone_edit_preserves_entire_passage_and_one_undo(
    operation: str, staff: int
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(1, 1)
        cursor = await bridge.get_cursor_info()
        event = _event(before, staff)
        if staff == 0:
            assert event.lyrics and event.lyrics[0].text == "keep chord"
            if _can_remove():
                assert event.articulations
        request = _request(before, operation, staff)
        if operation == "remove" and not _can_remove():
            with pytest.raises(BridgeError) as rejected:
                await bridge.remove_chord_tone(request)
            assert rejected.value.details["mutation_status"] == "not_applied"
            assert await bridge.read_passage(1, 1) == before
            return
        ticket = _operation(bridge, request)
        receipt = await bridge.execute_mutation_once(ticket)
        assert (
            receipt.command == operation + "ChordTone"
            and receipt.original_reply is not None
        )
        assert "result" in receipt.original_reply
        result = ChordToneEdit.model_validate(
            _snake_case_keys(receipt.original_reply["result"])
        )
        assert result.verified and result.mutation_status == "applied"
        assert result.before == event and result.after.duration == event.duration
        assert (
            result.after.lyrics == event.lyrics
            and result.after.articulations == event.articulations
        )
        if operation == "add":
            added = result.after.notes[result.note_index_after]
            assert added.pitch == request.pitch and added.tpc == request.tpc
            assert (
                added.written_tpc
                == request.tpc + event.notes[0].written_tpc - event.notes[0].tpc
            )
        else:
            assert [note.model_dump() for note in result.after.notes] == [
                note.model_dump() for note in event.notes[1:]
            ]
        expected = _replaced(before, result)
        assert await bridge.read_passage(1, 1) == expected
        duplicate = await bridge.execute_mutation_once(ticket)
        assert duplicate.cached and duplicate.original_reply == receipt.original_reply
        assert await bridge.read_passage(1, 1) == expected
        current_cursor = await bridge.get_cursor_info()
        assert current_cursor.model_dump(exclude={"element"}) == cursor.model_dump(
            exclude={"element"}
        )
        # Exact full current passage proves this own applied edit is latest.
        # No retry or cleanup undo if this attempt's outcome becomes uncertain.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == before
        historical = await bridge.execute_mutation_once(ticket)
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert await bridge.read_passage(1, 1) == before
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_unsafe_requests_leave_prior_own_sentinel_undo_entry() -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(1, 1)
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 1)
        assert sentinel != before
        duplicate = _request(sentinel, "add")
        duplicate.pitch = duplicate.expected.notes[0].pitch
        duplicate.tpc = duplicate.expected.notes[0].tpc
        stale = _request(sentinel, "add")
        stale.expected.lyrics[0].text = "stale text"
        last = next(
            event
            for event in sentinel.measures[0].events
            if event.kind == "chord" and event.staff == 0 and event.voice == 0
        )
        removal = RemoveChordToneRequest(
            measure=1,
            staff=0,
            voice=0,
            offset=last.offset.model_dump(),
            note_index=0,
            expected=ChordToneSnapshot.model_validate(last.model_dump()),
        )
        for request in [duplicate, stale, removal]:
            with pytest.raises(BridgeError) as rejected:
                if isinstance(request, AddChordToneRequest):
                    await bridge.add_chord_tone(request)
                else:
                    await bridge.remove_chord_tone(request)
            assert rejected.value.details["mutation_status"] == "not_applied"
            assert await bridge.read_passage(1, 1) == sentinel
        # Confirmed observable sentinel, all rejected calls, exact current passage.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == before
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_native_structural_cancel_restores_target_and_prior_history(
    operation: str,
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(1, 1)
        request = _request(before, operation)
        if operation == "remove" and not _can_remove():
            # The old wrapper cannot prove empty endpoints: refuse before arming.
            with pytest.raises(BridgeError) as rejected:
                await bridge.remove_chord_tone(request)
            assert rejected.value.details["mutation_status"] == "not_applied"
            assert await bridge.read_passage(1, 1) == before
            return
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 1)
        assert sentinel != before
        request = _request(sentinel, operation)
        armed = await bridge.send_command("_testArmChordTone" + operation.title())
        assert armed["result"]["armed"] is True
        ticket = _operation(bridge, request)
        receipt = await bridge.execute_mutation_once(ticket)
        assert (
            receipt.command == operation + "ChordTone"
            and receipt.original_reply is not None
        )
        failure = receipt.original_reply
        assert (
            failure["mutation_status"] == "rolled_back"
            and failure["rollback_verified"] is True
        )
        assert failure["before"] == failure["actual"]
        probe = (await bridge.send_command("_testReadChordToneFault"))["result"]
        assert probe["kind"] == operation and probe["armed"] is False
        original = _event(sentinel)
        assert probe["observed"]["beforeCount"] == len(original.notes)
        assert probe["observed"]["afterCount"] == len(original.notes) + (
            1 if operation == "add" else -1
        )
        expected_pitches = (
            [note.pitch for note in original.notes] + [request.pitch]
            if operation == "add"
            else [note.pitch for note in original.notes[1:]]
        )
        assert sorted(probe["observed"]["pitches"]) == sorted(expected_pitches)
        assert await bridge.read_passage(1, 1) == sentinel
        duplicate = await bridge.execute_mutation_once(ticket)
        assert duplicate.cached and duplicate.original_reply == receipt.original_reply
        assert (await bridge.send_command("_testReadChordToneFault"))["result"] == probe
        assert await bridge.read_passage(1, 1) == sentinel
        # Full rollback + exact sentinel contents authorize only this one undo.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == before
    finally:
        # Failure/unknown outcomes never authorize cleanup undo.
        await bridge.disconnect()
