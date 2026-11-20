"""Live multi-staff reads with fixtures/live-passage.musicxml open.

Set MCP_SCORE_PASSAGE_FIXTURE=1 after opening that fixture in the isolated
MuseScore integration instance. The ordinary bridge suite uses fixture.musicxml.
"""

from __future__ import annotations

import os

import pytest

from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import Duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_PASSAGE_FIXTURE") != "1",
        reason="requires live-passage.musicxml and MCP_SCORE_PASSAGE_FIXTURE=1",
    ),
]


@pytest.mark.anyio()
async def test_read_all_staves_voices_chords_and_measure_rests() -> None:
    bridge = MuseScoreBridge()
    try:
        score = await bridge.get_score()
        assert score.title == "Live passage fixture"
        passage = await bridge.read_passage(1, 1)
    finally:
        await bridge.disconnect()

    assert passage.staves == [0, 1, 2, 3]
    assert passage.voices == [0, 1, 2, 3]
    events = passage.measures[0].events
    melody = [
        event
        for event in events
        if event.staff == 0 and event.voice == 0 and event.kind == "chord"
    ]
    assert [event.notes[0].name for event in melody] == ["B4", "A4", "G4", "F4"]
    assert all(
        event.duration == Duration(numerator=1, denominator=4) for event in melody
    )
    chord = next(
        event
        for event in events
        if event.staff == 0 and event.voice == 1 and event.kind == "chord"
    )
    assert [note.name for note in chord.notes] == ["C4", "E4"]
    assert chord.offset == Duration(numerator=1, denominator=4)
    assert chord.duration == Duration(numerator=3, denominator=4)
    assert chord.actual_duration == Duration(numerator=3, denominator=4)
    for staff in [1, 2, 3]:
        rest = next(
            event for event in events if event.staff == staff and event.kind == "rest"
        )
        assert rest.offset == Duration(numerator=0, denominator=1)
        assert rest.duration == Duration(numerator=1, denominator=1)
        assert rest.actual_duration == Duration(numerator=1, denominator=1)


@pytest.mark.anyio()
async def test_read_voice_filter_keeps_measure_start_and_bridge_position() -> None:
    bridge = MuseScoreBridge()
    try:
        await bridge.go_to_staff(3)
        before = await bridge.get_cursor_info()
        passage = await bridge.read_passage(1, 1, staff=0, voice=1)
        after = await bridge.get_cursor_info()
    finally:
        await bridge.disconnect()

    assert passage.staves == [0] and passage.voices == [1]
    chord = next(event for event in passage.measures[0].events if event.kind == "chord")
    assert chord.offset == Duration(numerator=1, denominator=4)
    assert chord.tick == 480
    assert before == after
