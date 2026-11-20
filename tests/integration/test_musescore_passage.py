"""Live multi-staff reads with fixtures/live-passage.musicxml open.

Set MCP_SCORE_PASSAGE_FIXTURE=1 after opening that fixture in the isolated
MuseScore integration instance. The ordinary bridge suite uses fixture.musicxml.
"""

from __future__ import annotations

import os

import pytest

from mcp_score.bridge import BridgeRegistry
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import Duration
from mcp_score.tools import ToolError
from mcp_score.tools.analysis import get_event_at_position
from tests.fakes import score_context

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


@pytest.mark.anyio()
async def test_lookup_exact_melody_chord_tone_and_rest_without_navigation() -> None:
    registry = BridgeRegistry()
    bridge = registry.musescore
    assert await registry.activate(bridge)
    context = score_context(registry)
    try:
        await bridge.go_to_staff(3)
        before = await bridge.get_cursor_info()
        melody = await get_event_at_position(context, 1, 0, 0, 2, 4, note_index=0)
        equivalent = await get_event_at_position(context, 1, 0, 0, 1, 2, note_index=0)
        chord = await get_event_at_position(context, 1, 0, 1, 1, 4, note_index=1)
        rest = await get_event_at_position(context, 1, 2, 0, 0, 1)
        after = await bridge.get_cursor_info()
    finally:
        await registry.deactivate(bridge)

    assert melody.note is not None and melody.note.name == "G4"
    assert melody == equivalent
    assert melody.address.offset_numerator == 1
    assert melody.address.offset_denominator == 2
    assert melody.scope == "single_read" and melody.unsupported
    assert chord.note is not None and chord.note.name == "E4"
    assert [note.name for note in chord.event.notes] == ["C4", "E4"]
    assert rest.event.kind == "rest" and rest.note is None
    assert before == after


@pytest.mark.anyio()
async def test_lookup_rejects_inside_sustained_chord_and_invalid_tone() -> None:
    registry = BridgeRegistry()
    bridge = registry.musescore
    assert await registry.activate(bridge)
    context = score_context(registry)
    try:
        before = await bridge.get_cursor_info()
        with pytest.raises(ToolError, match="exact onset"):
            await get_event_at_position(context, 1, 0, 1, 1, 2)
        with pytest.raises(ToolError, match="out of range"):
            await get_event_at_position(context, 1, 0, 1, 1, 4, note_index=2)
        with pytest.raises(ToolError, match="rest and has no chord tones"):
            await get_event_at_position(context, 1, 2, 0, 0, 1, note_index=0)
        after = await bridge.get_cursor_info()
    finally:
        await registry.deactivate(bridge)
    assert before == after


@pytest.mark.anyio()
async def test_native_lyrics_preserve_verses_syllables_placement_and_staff_voice() -> (
    None
):
    bridge = MuseScoreBridge()
    try:
        await bridge.go_to_staff(3)
        before = await bridge.get_cursor_info()
        passage = await bridge.read_passage(1, 2)
        after = await bridge.get_cursor_info()
    finally:
        await bridge.disconnect()

    melody = [
        event
        for event in passage.measures[0].events
        if event.staff == 0 and event.voice == 0 and event.kind == "chord"
    ]
    assert all(event.lyrics is not None for event in melody)
    first = melody[0].lyrics
    assert first is not None
    assert [(lyric.text, lyric.verse) for lyric in first] == [("Hel", 0), ("One", 1)]
    assert [lyric.syllabic_name for lyric in first] == ["BEGIN", "SINGLE"]
    assert all(lyric.syllabic is not None for lyric in first)
    assert all(lyric.placement_name == "BELOW" for lyric in first)
    assert all(lyric.placement is not None for lyric in first)
    for event, text, syllabic in zip(
        melody[1:],
        ["lo", "world", "finé &amp; more"],
        ["MIDDLE", "END", "SINGLE"],
        strict=True,
    ):
        assert event.lyrics is not None
        assert event.lyrics[0].text == text
        assert event.lyrics[0].syllabic_name == syllabic
    other_voice = next(
        event
        for event in passage.measures[0].events
        if event.staff == 0 and event.voice == 1 and event.kind == "chord"
    )
    assert other_voice.lyrics is not None
    assert other_voice.lyrics[0].text == "low"
    assert other_voice.lyrics[0].placement_name == "ABOVE"
    bass = next(
        event
        for event in passage.measures[1].events
        if event.staff == 1 and event.kind == "chord"
    )
    assert bass.lyrics is not None and bass.lyrics[0].text == "bass"
    no_lyrics = next(event for event in passage.measures[0].events if event.staff == 2)
    assert no_lyrics.lyrics == []
    assert any("articulations" in detail for detail in passage.unsupported)
    assert passage.range_complete
    assert before == after


@pytest.mark.anyio()
async def test_lyric_reads_preserve_selection_and_do_not_add_undo_commands() -> None:
    bridge = MuseScoreBridge()
    try:
        baseline = await bridge.read_passage(1, 2)
        await bridge.select_range(1, 1, 0, 0)
        await bridge.read_passage(1, 2)
        await bridge.transpose(1)
        try:
            changed = await bridge.read_passage(1, 2)
        finally:
            await bridge.undo()
        restored = await bridge.read_passage(1, 2)
    finally:
        await bridge.disconnect()
    assert changed.measures[0].events != baseline.measures[0].events
    assert restored == baseline
    assert changed.measures[1] == baseline.measures[1]
    assert all(
        changed_event == original_event
        for changed_event, original_event in zip(
            changed.measures[0].events, baseline.measures[0].events, strict=True
        )
        if original_event.staff != 0
    )


@pytest.mark.anyio()
async def test_native_articulation_list_tracks_wrapper_availability() -> None:
    bridge = MuseScoreBridge()
    try:
        passage = await bridge.read_passage(1, 1, staff=0, voice=0)
    finally:
        await bridge.disconnect()
    chords = [event for event in passage.measures[0].events if event.kind == "chord"]
    version = os.environ.get("MUSESCORE_VERSION")
    if version is not None and tuple(map(int, version.split(".")[:2])) < (4, 6):
        assert all(event.articulations is None for event in chords)
    else:
        # CI declares its native version. Manual runs on older wrappers can
        # still read lyrics, while reporting the articulation list unavailable.
        if version is None and chords[0].articulations is None:
            assert all(event.articulations is None for event in chords)
            return
        first = chords[0].articulations
        assert first is not None and len(first) == 1
        assert first[0].symbol is not None
        assert first[0].name is not None and "staccato" in first[0].name.lower()
        assert all(event.articulations == [] for event in chords[1:])
