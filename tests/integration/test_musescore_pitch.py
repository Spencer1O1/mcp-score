"""Atomic native pitch edits with guarded-pitch.musicxml in an isolated profile."""

from __future__ import annotations

import os

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.pitch import ExpectedChord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_GUARDED_PITCH_FIXTURE") != "1",
        reason="requires guarded-pitch.musicxml and MCP_SCORE_GUARDED_PITCH_FIXTURE=1",
    ),
]


@pytest.mark.anyio()
async def test_live_chord_tone_crosses_peer_and_undo_restores_entire_passage() -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        before = await bridge.read_passage(1, 1)
        event = next(
            event
            for event in before.measures[0].events
            if event.kind == "chord" and event.staff == 0 and event.voice == 1
        )
        expected = ExpectedChord.model_validate(event.model_dump())
        result = await bridge.set_note_pitch(1, 0, 1, event.offset, 0, 67, 15, expected)
        applied = result.mutation_status == "applied"
        actual = await bridge.read_passage(1, 1)
        assert result.verified
        assert result.after.notes[result.note_index_after].name == "G4"
        assert sorted(note.name for note in result.after.notes) == ["E4", "G4"]
        assert result.after.duration == result.before.duration
        assert "setNotePitch" in bridge.diagnostics().commands
        assert "setNotePitch" not in bridge.diagnostics().sequence_commands
        unchanged = [
            event
            for event in before.measures[0].events
            if not (event.staff == 0 and event.voice == 1 and event.kind == "chord")
        ]
        actual_unchanged = [
            event
            for event in actual.measures[0].events
            if not (event.staff == 0 and event.voice == 1 and event.kind == "chord")
        ]
        assert actual_unchanged == unchanged
    finally:
        if applied:
            await bridge.undo()
        restored = await bridge.read_passage(1, 1)
        await bridge.disconnect()
    assert restored == before


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("staff", "original_pitch", "original_written", "pitch", "tpc", "written"),
    [
        (
            4,
            58,
            14,
            60,
            14,
            16,
        ),  # Bb clarinet: written C4 sounds Bb3; concert C4 writes D4.
        (
            5,
            48,
            14,
            50,
            16,
            16,
        ),  # Double bass: written C4 sounds C3; concert D3 writes D4.
    ],
)
async def test_live_transposing_pitch_preserves_native_written_interval(
    staff: int,
    original_pitch: int,
    original_written: int,
    pitch: int,
    tpc: int,
    written: int,
) -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        before = await bridge.read_passage(1, 1, staff, 0)
        event = next(
            event for event in before.measures[0].events if event.kind == "chord"
        )
        assert event.notes[0].pitch == original_pitch
        assert event.notes[0].written_tpc == original_written
        result = await bridge.set_note_pitch(
            1,
            staff,
            0,
            event.offset,
            0,
            pitch,
            tpc,
            ExpectedChord.model_validate(event.model_dump()),
        )
        applied = result.mutation_status == "applied"
        actual = await bridge.read_passage(1, 1, staff, 0)
        changed = next(
            event for event in actual.measures[0].events if event.kind == "chord"
        )
        assert result.after == changed
        assert changed.notes[0].pitch == pitch and changed.notes[0].tpc == tpc
        assert changed.notes[0].written_tpc == written
        assert changed.duration == event.duration
    finally:
        if applied:
            await bridge.undo()
        restored = await bridge.read_passage(1, 1, staff, 0)
        await bridge.disconnect()
    assert restored == before


@pytest.mark.anyio()
@pytest.mark.parametrize("measure", [2, 3, 4, 5])
async def test_unsupported_live_targets_reject_without_mutation(
    measure: int,
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(measure, measure, 0, 0)
        event = next(
            event for event in before.measures[0].events if event.kind == "chord"
        )
        with pytest.raises(BridgeError) as error:
            await bridge.set_note_pitch(
                measure,
                0,
                0,
                event.offset,
                0,
                67,
                15,
                ExpectedChord.model_validate(event.model_dump()),
            )
        after = await bridge.read_passage(measure, measure, 0, 0)
    finally:
        await bridge.disconnect()
    assert error.value.details["code"] == "unsupported_target"
    assert error.value.details["mutation_status"] == "not_applied"
    assert after == before
