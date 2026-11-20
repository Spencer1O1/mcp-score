"""Manipulation tools: change the score in the connected application.

Legacy edits navigate to a measure first. Guarded pitch edits instead resolve
an explicit position and validate its expected contents atomically in the plugin.
Unsupported operations return the application's own explanation:
Dorico's Remote Control API triggers commands but cannot type into
popovers or move the selection, so most of these tools work with MuseScore
only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import StrictInt, ValidationError

from mcp_score.bridge.duration_edit import (
    DurationEdit,
    DurationEditRequest,
    ExpectedDurationEvent,
)
from mcp_score.bridge.pitch import (
    ExpectedChord,
    PitchEdit,
    PitchEditRequest,
    WholeNoteFraction,
)
from mcp_score.bridge.results import (
    BarlineSet,
    ChordSymbolAdded,
    CursorPosition,
    Duration,
    DynamicAdded,
    KeySignatureSet,
    MeasuresAppended,
    NoteAdded,
    RehearsalMarkAdded,
    TempoSet,
    TimeSignatureSet,
    Transposed,
)
from mcp_score.context import ScoreContext
from mcp_score.tools import (
    ToolError,
    navigate,
    require_bridge,
    require_measure,
    require_measure_range,
    score_tool,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = ["register"]

MIN_MIDI_PITCH = 0
MAX_MIDI_PITCH = 127


@score_tool
async def add_live_note(
    context: ScoreContext,
    measure: int,
    pitch: int,
    numerator: int = 1,
    denominator: int = 4,
    staff: int = 0,
) -> NoteAdded:
    """Add a note at the start of a measure in the live score.

    Consecutive calls on the same measure append notes one after another,
    since the application advances its cursor after each note. Not
    available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        pitch: MIDI pitch (60 = middle C).
        numerator: Duration numerator (default 1, with denominator 4 = quarter note).
        denominator: Duration denominator (default 4).
        staff: Staff index (0-indexed, default: 0).
    """
    bridge = require_bridge(context)
    require_measure(measure)
    if not MIN_MIDI_PITCH <= pitch <= MAX_MIDI_PITCH:
        raise ToolError(f"pitch must be between {MIN_MIDI_PITCH} and {MAX_MIDI_PITCH}.")
    if numerator < 1 or denominator < 1:
        raise ToolError("numerator and denominator must be >= 1.")
    await navigate(bridge, measure, staff)
    return await bridge.add_note(
        pitch, Duration(numerator=numerator, denominator=denominator)
    )


@score_tool
async def set_live_event_duration(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset: WholeNoteFraction,
    duration: WholeNoteFraction,
    expected: list[ExpectedDurationEvent],
) -> DurationEdit:
    """Shorten an ordinary live chord and fill its released time with rests.

    Pass ALL events from a fresh get_measure_content read filtered to the explicit staff
    and voice as expected. This complete voice-measure snapshot is checked inside
    the synchronous native handler before any mutation. The only policy is
    shorten_fill_rest: no extension or bar crossing. Only undotted whole through
    32nd durations are accepted. MuseScore 4.7+ (major version 4) is required for
    native duration rewriting and spanner inspection; older versions reject.

    Target attachments (including lyrics/articulations), beams, ties, tuplets,
    grace notes, drum/tab staves, microtonality and intersecting or unknown score
    spanners reject. Distant spanner time/track metadata is verified unchanged.
    Successful edits return actual before/after events across ALL measure tracks,
    checking neighboring events and spelling, and create one undo step. No-op
    requests create none. Selection/layout may change; logical cursor is untouched.
    This snapshot does not identify a score instance or global revision and cannot
    detect switching to identical contents. Not available with Dorico.

    Args:
        measure: Ordinal measure (1-indexed).
        staff: Explicit staff index (0-indexed).
        voice: Explicit voice index (0-3).
        offset: Exact chord onset within the measure, in whole notes.
        duration: New shorter or equal undotted binary whole-note duration.
        expected: Complete fresh target voice measure events, including annotations.
    """
    bridge = require_bridge(context)
    try:
        request = DurationEditRequest.model_validate(
            {
                "measure": measure,
                "staff": staff,
                "voice": voice,
                "offset": offset.model_dump(),
                "duration": duration.model_dump(),
                "policy": "shorten_fill_rest",
                "expected": [event.model_dump() for event in expected],
            }
        )
    except ValidationError as error:
        raise ToolError(str(error)) from error
    return await bridge.set_event_duration(
        request.measure,
        request.staff,
        request.voice,
        request.offset,
        request.duration,
        request.expected,
    )


@score_tool
async def set_live_note_pitch(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset: WholeNoteFraction,
    note_index: StrictInt,
    pitch: StrictInt,
    tpc: StrictInt,
    expected: ExpectedChord,
) -> PitchEdit:
    """Match a live chord snapshot and change one tone's concert pitch and spelling.

    Pass the complete chord event from a fresh live read as `expected`, including
    every tone's written_tpc. The plugin checks the onset, track, rhythm, pitches,
    and spellings in the same synchronous handler as the edit. A mismatch rejects
    the edit without opening an undo step. Successful edits return actual native
    before/after events and create one undo step; no-op requests create none.
    Chord-tone indices belong only to that snapshot, not to later reads.
    Lyrics and articulations in the snapshot are accepted as informational
    metadata; the pitch guard does not compare them or protect their changes.

    This guards only the target chord, not a score identity or global revision;
    switching to another score with identical target contents is not detected.
    Tied, grace, tuplet, drum, tablature, duplicate-tone and microtonal targets are
    rejected. Written octave is not returned. Not available with Dorico.

    Args:
        measure: Ordinal measure (1-indexed, repeats not expanded).
        staff: Explicit staff index (0-indexed).
        voice: Explicit voice index (0-3).
        offset: Exact onset within the measure, as a whole-note fraction.
        note_index: Tone index in expected.notes (0-indexed, snapshot-local).
        pitch: Replacement sounding MIDI pitch (0-127; 60 is middle C).
        tpc: Replacement concert tonal pitch class (-1 to 33); must match pitch.
        expected: Complete live chord event, with written_tpc for every tone.
    """
    bridge = require_bridge(context)
    try:
        request = PitchEditRequest.model_validate(
            {
                "measure": measure,
                "staff": staff,
                "voice": voice,
                "offset": offset.model_dump(),
                "note_index": note_index,
                "pitch": pitch,
                "tpc": tpc,
                "expected": expected.model_dump(),
            }
        )
    except ValidationError as error:
        raise ToolError(f"Invalid guarded pitch request: {error}") from error
    return await bridge.set_note_pitch(
        request.measure,
        request.staff,
        request.voice,
        request.offset,
        request.note_index,
        request.pitch,
        request.tpc,
        request.expected,
    )


@score_tool
async def add_live_rehearsal_mark(
    context: ScoreContext, measure: int, text: str
) -> RehearsalMarkAdded:
    """Add a rehearsal mark to a measure in the live score.

    Dorico numbers rehearsal marks itself and ignores the text (the result
    says so in a warning).

    Args:
        measure: Measure number (1-indexed).
        text: Rehearsal mark text (e.g. "A", "B", "Intro").
    """
    bridge = require_bridge(context)
    require_measure(measure)
    await navigate(bridge, measure)
    return await bridge.add_rehearsal_mark(text)


@score_tool
async def add_live_chord_symbol(
    context: ScoreContext, measure: int, symbol: str
) -> ChordSymbolAdded:
    """Add a chord symbol to a measure in the live score.

    Not available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        symbol: Chord symbol (e.g. "Cmaj7", "Dm7", "G7").
    """
    bridge = require_bridge(context)
    require_measure(measure)
    await navigate(bridge, measure)
    return await bridge.add_chord_symbol(symbol)


@score_tool
async def add_live_dynamic(
    context: ScoreContext, measure: int, dynamic: str, staff: int = 0
) -> DynamicAdded:
    """Add a dynamic marking to a measure in the live score.

    Not available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        dynamic: Dynamic such as "pp", "p", "mp", "mf", "f", "ff", "sfz".
        staff: Staff index (0-indexed, default: 0).
    """
    bridge = require_bridge(context)
    require_measure(measure)
    await navigate(bridge, measure, staff)
    return await bridge.add_dynamic(dynamic)


@score_tool
async def set_live_barline(
    context: ScoreContext, measure: int, barline_type: str
) -> BarlineSet:
    """Set the bar line at the end of a measure in the live score.

    Args:
        measure: Measure number (1-indexed).
        barline_type: One of "normal", "double", "final", "dashed", "dotted",
            "tick", "short", "startRepeat", "endRepeat" or "endStartRepeat".
            "startRepeat" marks the start of this measure; "endStartRepeat"
            ends a repeat here and starts one in the next measure. Dorico
            supports "double", "final", "startRepeat" and "endRepeat".
    """
    bridge = require_bridge(context)
    require_measure(measure)
    await navigate(bridge, measure)
    return await bridge.set_barline(barline_type)


@score_tool
async def set_live_key_signature(
    context: ScoreContext, measure: int, fifths: int
) -> KeySignatureSet:
    """Set the key signature from a measure onward in the live score.

    Not available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        fifths: Sharps (positive) or flats (negative): 0 = C major,
            2 = D major, -3 = Eb major.
    """
    bridge = require_bridge(context)
    require_measure(measure)
    await navigate(bridge, measure)
    return await bridge.set_key_signature(fifths)


@score_tool
async def set_live_time_signature(
    context: ScoreContext, measure: int, numerator: int, denominator: int
) -> TimeSignatureSet:
    """Set the time signature from a measure onward in the live score.

    Not available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        numerator: Beats per measure (e.g. 3 in 3/4).
        denominator: Beat unit (e.g. 4 in 3/4).
    """
    bridge = require_bridge(context)
    require_measure(measure)
    if numerator < 1 or denominator < 1:
        raise ToolError("numerator and denominator must be >= 1.")
    await navigate(bridge, measure)
    return await bridge.set_time_signature(numerator, denominator)


@score_tool
async def set_live_tempo(
    context: ScoreContext, measure: int, bpm: int, text: str | None = None
) -> TempoSet:
    """Set the tempo at a measure in the live score.

    Not available with Dorico.

    Args:
        measure: Measure number (1-indexed).
        bpm: Beats per minute.
        text: Optional display text (e.g. "Swing", "Allegro").
    """
    bridge = require_bridge(context)
    require_measure(measure)
    if bpm < 1:
        raise ToolError("bpm must be >= 1.")
    await navigate(bridge, measure)
    return await bridge.set_tempo(bpm, text)


@score_tool
async def append_live_measures(
    context: ScoreContext, count: int = 1
) -> MeasuresAppended:
    """Append empty measures to the end of the live score.

    Not available with Dorico.

    Args:
        count: How many measures to append (default: 1).
    """
    bridge = require_bridge(context)
    if count < 1:
        raise ToolError("count must be >= 1.")
    return await bridge.append_measures(count)


@score_tool
async def transpose_passage(
    context: ScoreContext,
    start_measure: int,
    end_measure: int,
    staff: int,
    semitones: int,
) -> Transposed:
    """Transpose the notes of a passage by a number of semitones in the live score.

    Notes are moved with conventional spelling (a minor second up turns C
    into Db). Key signatures and chord symbols in the passage are left
    unchanged. Not available with Dorico, which cannot select a range.

    Args:
        start_measure: First measure (1-indexed).
        end_measure: Last measure (inclusive, 1-indexed).
        staff: Staff index (0-indexed).
        semitones: Semitones to transpose (positive = up, negative = down).
    """
    bridge = require_bridge(context)
    require_measure_range(start_measure, end_measure)
    await navigate(bridge, start_measure, staff)
    await bridge.select_range(start_measure, end_measure, staff, staff)
    return await bridge.transpose(semitones)


@score_tool
async def undo_last_action(context: ScoreContext) -> CursorPosition:
    """Undo the last change in the connected application.

    Reports where the cursor is afterwards, since undoing can remove the
    measure it was on.
    """
    return await require_bridge(context).undo()


def register(server: MCPServer) -> None:
    for tool in (
        add_live_note,
        set_live_note_pitch,
        set_live_event_duration,
        add_live_rehearsal_mark,
        add_live_chord_symbol,
        add_live_dynamic,
        set_live_barline,
        set_live_key_signature,
        set_live_time_signature,
        set_live_tempo,
        append_live_measures,
        transpose_passage,
        undo_last_action,
    ):
        server.tool()(tool)
