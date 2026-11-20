"""Direct native context reads in the isolated staff-context MusicXML fixture."""

from __future__ import annotations

import os

import pytest

from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import Duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_CONTEXT_FIXTURE") != "1",
        reason="requires owned disposable staff-context.musicxml fixture",
    ),
]


@pytest.mark.anyio()
async def test_native_inherited_midmeasure_pickup_irregular_and_transposition() -> None:
    bridge = MuseScoreBridge()
    try:
        score = await bridge.get_score()
        assert score.title == "Staff context fixture" and score.measure_count == 3
        result = await bridge.read_staff_context(1, 3)
        assert result.sampling_complete and result.staves == [0, 1, 2]
        installed = tuple(
            int(piece) for piece in os.environ["MUSESCORE_VERSION"].split(".")
        )
        assert (result.native_major_version, result.native_minor_version) == installed[
            :2
        ]
        assert result.tempo_status == "not_queried"
        assert result.staff_point_queries == result.sampled_point_count * 3
        assert any("tempo" in detail for detail in result.limitations)
        flute = result.timelines[0]
        assert flute.start.measure_nominal_meter.numerator == 4
        assert flute.start.measure_nominal_meter.denominator == 4
        assert flute.start.measure_actual_duration == Duration(
            numerator=1, denominator=4
        )
        assert flute.start.instrument is not None
        assert "flute" in (flute.start.instrument.long_name or "").lower()
        change = next(
            point
            for point in flute.changes
            if point.instrument is not None
            and point.instrument.instrument_id != flute.start.instrument.instrument_id
        )
        assert (
            change.instrument is not None and change.instrument.instrument_id == "oboe"
        )
        assert change.measure == 2 and change.tick == 960
        assert change.offset == Duration(numerator=1, denominator=4)
        assert change.absolute_time == Duration(numerator=1, denominator=2)
        inherited = await bridge.read_staff_context(3, 3, 0)
        point = inherited.timelines[0].start
        assert point.instrument == change.instrument
        assert point.offset == Duration(numerator=0, denominator=1)
        assert point.absolute_time == Duration(numerator=5, denominator=4)
        assert point.measure_actual_duration == Duration(numerator=3, denominator=4)
        assert (
            point.measure_nominal_meter.numerator,
            point.measure_nominal_meter.denominator,
        ) == (4, 4)
        empty_voice = await bridge.read_passage(2, 2, 0, 3)
        assert not any(
            event.kind != "other" for event in empty_voice.measures[0].events
        )
        if result.native_minor_version < 6:
            assert result.position_precision == "native_integer_ticks"
            for timeline in result.timelines:
                assert timeline.start.native_key is None
                assert timeline.start.native_transposition is None
                assert len(timeline.start.unavailable_fields) >= 5
        else:
            assert result.position_precision == "native_fraction"
            assert change.native_clef_type != flute.start.native_clef_type
            assert point.native_clef_type == change.native_clef_type
            assert change.native_key == point.native_key == 2
            assert point.local_meter is not None
            assert (point.local_meter.numerator, point.local_meter.denominator) == (
                4,
                4,
            )
            assert point.time_stretch == Duration(numerator=1, denominator=1)
            clarinet = result.timelines[1].start.native_transposition
            bass = result.timelines[2].start.native_transposition
            assert clarinet is not None and (clarinet.chromatic, clarinet.diatonic) == (
                -2,
                -1,
            )
            assert bass is not None and (bass.chromatic, bass.diatonic) == (-12, -7)
        diagnostics = bridge.diagnostics()
        assert "readStaffContext" in diagnostics.commands
        assert "readStaffContext" not in diagnostics.sequence_commands
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_context_reads_preserve_selection_cursor_contents_and_undo() -> None:
    bridge = MuseScoreBridge()
    changed = None
    needs_undo = False
    try:
        baseline = await bridge.read_passage(1, 3)
        await bridge.select_range(2, 2, 0, 0)
        await bridge.transpose(1)
        changed = await bridge.read_passage(1, 3)
        assert changed != baseline
        # A successful legacy reply alone does not prove an undoable edit.
        needs_undo = True
        selection = await bridge.read_current_selection()
        cursor = await bridge.get_cursor_info()
        await bridge.read_staff_context(1, 3)
        await bridge.read_staff_context(3, 3, 1)
        assert await bridge.read_current_selection() == selection
        assert await bridge.get_cursor_info() == cursor
        assert await bridge.read_passage(1, 3) == changed
        # A context read must not sit above the known transpose in undo history.
        needs_undo = False
        await bridge.undo()
        assert await bridge.read_passage(1, 3) == baseline
    finally:
        try:
            if (
                needs_undo
                and changed is not None
                and await bridge.read_passage(1, 3) == changed
            ):
                needs_undo = False
                await bridge.undo()
        finally:
            await bridge.disconnect()
