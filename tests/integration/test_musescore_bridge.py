"""Integration tests: the WebSocket bridge against a live MuseScore plugin.

Requires ``MCP_SCORE_INTEGRATION=1`` and a running MuseScore Studio with
``tests/integration/fixtures/fixture.musicxml`` open and the bridge plugin
started (``uv run scripts/musescore_harness.py start <fixture>``).

Every mutating test undoes its change and checks that the score is back
to what it found, so the tests do not depend on their order. The
read-only checks of the untouched fixture still run first. A command the
plugin refuses raises ``BridgeError``, and a reply that does not fit its
result model fails validation, so a step that must succeed needs no
assertion on its reply.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.pitch import ExpectedChord
from mcp_score.bridge.results import BarlineSet, Duration, Note

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mcp_score.bridge.passage import Passage

pytestmark = pytest.mark.integration

# What the committed fixture score contains: four quarter notes C4 D4 E4 F4.
_FIXTURE_TITLE = "Fixture"
_FIXTURE_PART_COUNT = 1
_FIXTURE_MEASURE_COUNT = 1
_FIXTURE_FIRST_PITCH = 60
_FIXTURE_FIRST_TPC = 14  # C natural

_QUARTER_NOTE = Duration(numerator=1, denominator=4)
_MIDDLE_C = 60
_TEMPO_BPM = 96
_CHORD_SYMBOL = "Cmaj7"
_MINOR_THIRD_DOWN = -3
_A_NATURAL_TPC = 17
_DOUBLE_BARLINE_TYPE = 2
_FIRST_STAFF = 0


@asynccontextmanager
async def _connected_bridge() -> AsyncGenerator[MuseScoreBridge]:
    """A bridge that auto-connects on first use and always disconnects."""
    bridge = MuseScoreBridge()
    try:
        yield bridge
    finally:
        await bridge.disconnect()


async def _measure_count(bridge: MuseScoreBridge) -> int:
    score = await bridge.get_score()
    return score.measure_count


async def _first_note(bridge: MuseScoreBridge) -> Note:
    """Pitch and spelling of the first note of the chord at the cursor."""
    cursor = await bridge.get_cursor_info()
    assert cursor.element is not None
    assert cursor.element.notes is not None
    return cursor.element.notes[0]


class TestMuseScoreBridgeReadsFixture:
    """Read-only checks; run first because later tests modify the score."""

    @pytest.mark.anyio()
    async def test_ping_returns_true(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            # Act
            alive = await bridge.ping()

        # Assert
        assert alive is True

    @pytest.mark.anyio()
    async def test_get_score_returns_fixture_metadata(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            # Act
            score = await bridge.get_score()

        # Assert
        assert score.title == _FIXTURE_TITLE
        assert score.part_count == _FIXTURE_PART_COUNT
        assert len(score.parts) == _FIXTURE_PART_COUNT
        assert score.measure_count == _FIXTURE_MEASURE_COUNT

    @pytest.mark.anyio()
    async def test_go_to_measure_out_of_range_raises(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            count = await _measure_count(bridge)

            # Act / Assert
            with pytest.raises(BridgeError, match="out of range"):
                await bridge.go_to_measure(count + 1)

    @pytest.mark.anyio()
    async def test_passage_returns_every_quarter_note_without_moving_cursor(
        self,
    ) -> None:
        async with _connected_bridge() as bridge:
            before = await bridge.get_cursor_info()

            passage = await bridge.read_passage(1, 1)
            diagnostics = bridge.diagnostics()
            after = await bridge.get_cursor_info()

        chords = [
            event for event in passage.measures[0].events if event.kind == "chord"
        ]
        assert [event.notes[0].name for event in chords] == ["C4", "D4", "E4", "F4"]
        assert [event.tick for event in chords] == [0, 480, 960, 1440]
        assert [event.offset for event in chords] == [
            Duration(numerator=0, denominator=1),
            Duration(numerator=1, denominator=4),
            Duration(numerator=1, denominator=2),
            Duration(numerator=3, denominator=4),
        ]
        assert all(event.duration == _QUARTER_NOTE for event in chords)
        assert all(event.actual_duration == _QUARTER_NOTE for event in chords)
        assert "readPassage" in diagnostics.commands
        assert "readPassage" not in diagnostics.sequence_commands
        assert passage.range_complete is True
        assert passage.unsupported
        assert passage.measures[0].actual_duration == Duration(
            numerator=1, denominator=1
        )
        assert passage.measures[0].time_signature is not None
        assert passage.measures[0].time_signature.numerator == 4
        assert passage.measures[0].time_signature.denominator == 4
        assert after == before

    @pytest.mark.anyio()
    async def test_passage_observes_unsaved_edit_and_does_not_insert_undo_step(
        self,
    ) -> None:
        async with _connected_bridge() as bridge:
            before = await bridge.read_passage(1, 1, staff=0, voice=0)
            await bridge.select_range(1, 1, 0, 0)
            await bridge.transpose(1)
            try:
                changed = await bridge.read_passage(1, 1, staff=0, voice=0)
            finally:
                await bridge.undo()
            restored = await bridge.read_passage(1, 1, staff=0, voice=0)

        def pitches(passage: Passage) -> list[int]:
            return [
                note.pitch
                for measure in passage.measures
                for event in measure.events
                for note in event.notes
            ]

        assert pitches(changed) == [pitch + 1 for pitch in pitches(before)]
        assert restored == before


class TestMuseScoreBridgeCapabilities:
    @pytest.mark.anyio()
    async def test_negotiated_commands_match_live_plugin_dispatch(self) -> None:
        async with _connected_bridge() as bridge:
            assert await bridge.connect()
            diagnostics = bridge.diagnostics()
            assert diagnostics.plugin_version
            assert diagnostics.protocol_version == 1
            assert "getScore" in diagnostics.commands
            assert "readCurrentSelection" in diagnostics.commands
            assert "_testSelectionSetup" not in diagnostics.commands
            assert "appendMeasures" in diagnostics.sequence_commands
            assert "getScore" not in diagnostics.sequence_commands
            assert await bridge.ping()

    @pytest.mark.anyio()
    async def test_plugin_rejects_incompatible_client_protocol(self) -> None:
        async with _connected_bridge() as bridge:
            with pytest.raises(BridgeError, match="Unsupported bridge protocol"):
                await bridge.send_command("getCapabilities", {"protocolVersion": 2})
            assert await bridge.ping()


class TestMuseScoreBridgeModifiesScore:
    @pytest.mark.anyio()
    async def test_append_measures_then_undo_restores_count(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            count_before = await _measure_count(bridge)

            # Act
            appended = await bridge.append_measures(2)
            count_after = await _measure_count(bridge)
            position = await bridge.undo()
            count_restored = await _measure_count(bridge)

        # Assert
        assert appended.total_measures == count_before + 2
        assert count_after == count_before + 2
        assert count_restored == count_before
        assert position.measure <= count_before

    @pytest.mark.anyio()
    async def test_add_note_in_appended_measure_moves_cursor_there(self) -> None:
        # Arrange: add an empty measure to write into.
        async with _connected_bridge() as bridge:
            count_before = await _measure_count(bridge)
            await bridge.append_measures(1)
            new_measure = count_before + 1

            # Act
            moved = await bridge.go_to_measure(new_measure)
            await bridge.add_note(_MIDDLE_C, _QUARTER_NOTE)
            cursor = await bridge.get_cursor_info()
            await bridge.undo()  # the note
            await bridge.undo()  # the measure
            count_restored = await _measure_count(bridge)

        # Assert
        assert moved.measure == new_measure
        assert cursor.measure == new_measure
        assert count_restored == count_before

    @pytest.mark.anyio()
    async def test_set_tempo_returns_bpm(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)

            # Act
            tempo = await bridge.set_tempo(_TEMPO_BPM)
            await bridge.undo()

        # Assert
        assert tempo.bpm == _TEMPO_BPM
        assert tempo.measure == 1

    @pytest.mark.anyio()
    async def test_add_chord_symbol_returns_text(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)

            # Act
            chord = await bridge.add_chord_symbol(_CHORD_SYMBOL)
            await bridge.undo()
            still_alive = await bridge.ping()

        # Assert
        assert chord.text == _CHORD_SYMBOL
        assert still_alive is True

    @pytest.mark.anyio()
    async def test_set_barline_changes_end_barline_and_undoes(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)

            # Act
            changed = await bridge.set_barline("double")
            await bridge.undo()
            still_alive = await bridge.ping()

        # Assert
        assert changed == BarlineSet(barline_type="double", measure=1)
        assert still_alive is True

    @pytest.mark.anyio()
    async def test_set_barline_rejects_unknown_type(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)

            # Act / Assert
            with pytest.raises(BridgeError, match="Unknown barline type"):
                await bridge.set_barline("wavy")

    @pytest.mark.anyio()
    async def test_transpose_shifts_pitch_and_spelling_then_undo_restores(
        self,
    ) -> None:
        # Arrange: select the fixture measure on its only staff.
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)
            await bridge.select_range(1, 1, _FIRST_STAFF, _FIRST_STAFF)

            # Act
            transposed = await bridge.transpose(_MINOR_THIRD_DOWN)
            note_after = await _first_note(bridge)
            await bridge.undo()
            note_restored = await _first_note(bridge)

        # Assert: C4 down a minor third is A3, spelled A (not Bbb).
        assert transposed.semitones == _MINOR_THIRD_DOWN
        assert note_after.pitch == _FIXTURE_FIRST_PITCH + _MINOR_THIRD_DOWN
        assert note_after.tpc == _A_NATURAL_TPC
        assert note_restored.pitch == _FIXTURE_FIRST_PITCH
        assert note_restored.tpc == _FIXTURE_FIRST_TPC


class TestMuseScoreBridgeSequences:
    @pytest.mark.anyio()
    async def test_malformed_late_step_rejects_before_score_or_cursor_changes(
        self,
    ) -> None:
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)
            before = await bridge.get_cursor_info()
            count_before = await _measure_count(bridge)

            # Send malformed JSON-level input directly: process_sequence's typed
            # convenience method accepts only dictionaries, while the wire can
            # contain null. No preceding append/navigation may execute.
            with pytest.raises(BridgeError) as exc_info:
                await bridge.send_command(
                    "processSequence",
                    {
                        "sequence": [
                            {"action": "appendMeasures", "params": {"count": 1}},
                            {
                                "action": "goToMeasure",
                                "params": {"measure": count_before + 1},
                            },
                            None,
                        ]
                    },
                )
            after = await bridge.get_cursor_info()
            count_after = await _measure_count(bridge)

        assert exc_info.value.details["failedIndex"] == 2
        assert exc_info.value.details["mutationStatus"] == "not_applied"
        assert exc_info.value.details["rollbackStatus"] == "not_needed"
        assert count_after == count_before
        assert (after.measure, after.staff, after.voice) == (
            before.measure,
            before.staff,
            before.voice,
        )

    @pytest.mark.anyio()
    async def test_failed_sequence_rolls_back_score_and_cursor(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            await bridge.go_to_measure(1)
            count_before = await _measure_count(bridge)

            # Act: the last step fails, so the appended measure must vanish.
            with pytest.raises(BridgeError) as exc_info:
                await bridge.process_sequence(
                    [
                        {"action": "appendMeasures", "params": {"count": 1}},
                        {
                            "action": "goToMeasure",
                            "params": {"measure": count_before + 1},
                        },
                        {"action": "goToMeasure", "params": {"measure": 999}},
                    ]
                )
            count_after = await _measure_count(bridge)
            cursor = await bridge.get_cursor_info()

        # Assert
        assert exc_info.value.details["failedIndex"] == 2
        assert exc_info.value.details["failedAction"] == "goToMeasure"
        assert exc_info.value.details["mutationStatus"] == "unknown"
        assert exc_info.value.details["rollbackStatus"] == "requested"
        assert count_after == count_before
        assert cursor.measure == 1

    @pytest.mark.anyio()
    async def test_sequence_commits_as_one_undo_step(self) -> None:
        # Arrange
        async with _connected_bridge() as bridge:
            count_before = await _measure_count(bridge)

            # Act
            response = await bridge.process_sequence(
                [
                    {"action": "appendMeasures", "params": {"count": 1}},
                    {"action": "goToMeasure", "params": {"measure": count_before + 1}},
                    {"action": "addNote", "params": {"pitch": _MIDDLE_C}},
                    {"action": "addChordSymbol", "params": {"text": _CHORD_SYMBOL}},
                ]
            )
            count_after = await _measure_count(bridge)
            await bridge.undo()
            count_restored = await _measure_count(bridge)

        # Assert
        assert response["result"]["count"] == 4
        assert count_after == count_before + 1
        assert count_restored == count_before


class TestMuseScoreGuardedPitch:
    @pytest.mark.anyio()
    async def test_guarded_pitch_reads_back_and_one_undo_restores_score(self) -> None:
        async with _connected_bridge() as bridge:
            before = await bridge.read_passage(1, 1, 0, 0)
            target = [
                event for event in before.measures[0].events if event.kind == "chord"
            ][2]
            applied = False
            try:
                changed = await bridge.set_note_pitch(
                    1,
                    0,
                    0,
                    target.offset,
                    0,
                    67,
                    15,
                    ExpectedChord.model_validate(target.model_dump()),
                )
                applied = changed.mutation_status == "applied"
                after = await bridge.read_passage(1, 1, 0, 0)
                assert changed.before == target
                assert [
                    note.name
                    for event in after.measures[0].events
                    for note in event.notes
                ] == ["C4", "D4", "G4", "F4"]
                assert (
                    changed.after
                    == [
                        event
                        for event in after.measures[0].events
                        if event.kind == "chord"
                    ][2]
                )
            finally:
                if applied:
                    await bridge.undo()
            restored = await bridge.read_passage(1, 1, 0, 0)
        assert restored == before

    @pytest.mark.anyio()
    async def test_stale_guard_and_noop_do_not_add_undo_steps(self) -> None:
        async with _connected_bridge() as bridge:
            before = await bridge.read_passage(1, 1, 0, 0)
            target = next(
                event for event in before.measures[0].events if event.kind == "chord"
            )
            expected = ExpectedChord.model_validate(target.model_dump())
            applied = False
            try:
                changed = await bridge.set_note_pitch(
                    1, 0, 0, target.offset, 0, 61, 21, expected
                )
                applied = changed.mutation_status == "applied"
                with pytest.raises(BridgeError) as error:
                    await bridge.set_note_pitch(
                        1, 0, 0, target.offset, 0, 62, 16, expected
                    )
                assert error.value.details["code"] == "stale_target"
                noop = await bridge.set_note_pitch(
                    1,
                    0,
                    0,
                    target.offset,
                    0,
                    61,
                    21,
                    ExpectedChord.model_validate(changed.after.model_dump()),
                )
                assert noop.mutation_status == "unchanged"
            finally:
                if applied:
                    await bridge.undo()
            restored = await bridge.read_passage(1, 1, 0, 0)
        assert restored == before
