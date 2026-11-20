"""Read live musical contents independently of the application's cursor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import StrictInt, ValidationError

from mcp_score.bridge.current_selection import CurrentSelection
from mcp_score.bridge.event_address import EventAddress, LocatedEvent, locate_event
from mcp_score.bridge.passage import Passage
from mcp_score.bridge.results import SelectionProperties
from mcp_score.bridge.staff_context import StaffContext, StaffContextRequest
from mcp_score.context import ScoreContext
from mcp_score.tools import (
    ToolError,
    require_bridge,
    require_measure,
    require_measure_range,
    score_tool,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = ["register"]


@score_tool
async def read_staff_context(
    context: ScoreContext,
    start_measure: StrictInt,
    end_measure: StrictInt,
    staff: StrictInt | None = None,
) -> StaffContext:
    """Read effective native staff context at passage start and observed changes.

    Separate from event reads: no voice filtering, cursor creation or tempo query.
    Direct native getters return instrument, raw clef/key codes, local meter,
    time stretch and full chromatic/diatonic transposition on MuseScore 4.6+.
    4.4 returns instrument-only observations with unavailable field reasons.
    Position precision is native_fraction on 4.6+, native_integer_ticks on 4.4;
    absolute_time and offset use whole notes, tick is supplementary integer data.
    Meter spelling is preserved separately from actual pickup/irregular length.

    Native segments are sampled within half-open measures, including changes
    without notes in an otherwise empty voice. Unchanged samples are coalesced.
    sampling_complete confirms traversal of bounded samples, not full context
    fidelity. Missing getters return null plus reasons. Tempo, custom key notation,
    written pitch/octave and displayed measure labels remain unsupported.
    No guessing key modes/names or toggling concert pitch. No score revision or
    identity guarantees. Maximum 64 measures, 4096 staff-by-point samples and
    bounded traversal; excess requests fail without partial timelines. Not Dorico.

    Args:
        start_measure: First ordinal measure (1-indexed, inclusive).
        end_measure: Last ordinal measure (1-indexed, inclusive).
        staff: Zero-based staff index; omit to query every staff independently.
    """
    try:
        request = StaffContextRequest(
            start_measure=start_measure,
            end_measure=end_measure,
            staff=staff,
        )
    except ValidationError as error:
        raise ToolError(f"Invalid context range: {error}") from error
    if request.end_measure < request.start_measure:
        raise ToolError("end_measure must be >= start_measure")
    return await require_bridge(context).read_staff_context(
        request.start_measure, request.end_measure, request.staff
    )


@score_tool
async def read_passage(
    context: ScoreContext,
    start_measure: int,
    end_measure: int,
    staff: int | None = None,
    voice: int | None = None,
) -> Passage:
    """Read every rhythmic event in an inclusive range of live MuseScore measures.

    Returns chords, rests, concert pitch names, exact offsets and durations
    across all requested staves and voices, including unsaved changes. Does
    not move the selection or the bridge's logical cursor. Other segment
    elements and annotations include their type and text where available.
    Chord/rest lyrics include native text, zero-based verse, syllabic and
    placement codes/names; null means unavailable, [] means no attached lyrics.
    Chord articulations include native symbol IDs and localized names where
    supported (MuseScore 4.6+); older wrappers report null, not an empty list.
    The result lists notation details not yet serialized in `unsupported`;
    `range_complete` describes traversal, not full notation fidelity.
    Oversize reads fail without returning a truncated passage; request a
    smaller range. Not available with Dorico or an older MuseScore plugin.

    Args:
        start_measure: First ordinal measure (1-indexed, repeats not expanded).
        end_measure: Last ordinal measure (inclusive, 1-indexed).
        staff: Staff index (0-indexed). Omit to read all staves.
        voice: Voice index (0-3). Omit to read all voices.
    """
    bridge = require_bridge(context)
    require_measure_range(start_measure, end_measure)
    _require_track(staff, voice)
    return await bridge.read_passage(start_measure, end_measure, staff, voice)


@score_tool
async def get_measure_content(
    context: ScoreContext,
    measure: int,
    staff: int | None = None,
    voice: int | None = None,
) -> Passage:
    """Read the complete rhythmic contents of a live MuseScore measure.

    Uses the same event schema and notation limitations as read_passage.
    Includes unsaved changes without selecting the measure or moving the
    bridge cursor. Not available with Dorico or an older MuseScore plugin.

    Args:
        measure: Ordinal measure number (1-indexed, repeats not expanded).
        staff: Staff index (0-indexed). Omit to read all staves.
        voice: Voice index (0-3). Omit to read all voices.
    """
    bridge = require_bridge(context)
    require_measure(measure)
    _require_track(staff, voice)
    return await bridge.read_passage(measure, measure, staff, voice)


def _require_track(staff: int | None, voice: int | None) -> None:
    if staff is not None and staff < 0:
        raise ToolError("staff must be >= 0.")
    if voice is not None and not 0 <= voice <= 3:
        raise ToolError("voice must be between 0 and 3.")


@score_tool
async def get_event_at_position(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset_numerator: StrictInt,
    offset_denominator: StrictInt,
    note_index: StrictInt | None = None,
) -> LocatedEvent:
    """Read the chord or rest starting at an exact live MuseScore position.

    All position coordinates are explicit. Offsets are exact fractions of a
    whole note: 1/2 selects an onset halfway through a whole note, not beat 2.
    Equivalent fractions select the same onset. A position inside a sustained
    event does not match. Non-rhythmic annotations are excluded, and ambiguous
    onsets are rejected. Uses one live passage read without moving selection
    or the bridge cursor, including unsaved changes.

    Omitting note_index returns the entire event; supplying it also returns
    that tone from the event's current notes list. Indices are ephemeral
    ordering within this response. The result preserves the passage reader's
    unsupported notation list. It provides no stable event ID, score identity,
    revision token, or guarantee that the address remains valid for an edit.

    Args:
        measure: Ordinal measure (integer >= 1; repeats not expanded).
        staff: Staff index (integer >= 0).
        voice: Voice index (integer from 0 through 3).
        offset_numerator: Whole-note onset fraction numerator (integer >= 0).
        offset_denominator: Whole-note onset fraction denominator (integer > 0).
        note_index: Index into the returned chord's notes (integer >= 0).
            Omit to select the whole chord or rest.
    """
    bridge = require_bridge(context)
    try:
        address = EventAddress(
            measure=measure,
            staff=staff,
            voice=voice,
            offset_numerator=offset_numerator,
            offset_denominator=offset_denominator,
            note_index=note_index,
        )
    except ValidationError as error:
        raise ToolError(f"Invalid event address: {error}") from error
    passage = await bridge.read_passage(measure, measure, staff, voice)
    return locate_event(passage, address)


@score_tool
async def read_current_selection(context: ScoreContext) -> CurrentSelection:
    """Read the actual MuseScore UI selection, including unsaved selected contents.

    Returns an empty, distinct-element, or range selection from the native list.
    Range bounds are half-open; a null end tick means score end. The native
    list respects current selection filters; filter settings themselves are
    unavailable. It is never expanded to all events inside the bounding range.
    Selected notes are distinct from whole chords: event_context contains the
    owner chord, but only note/note_index identify the selected tone. Indices
    belong only to this snapshot. Ordinary segment-bound notes/chords/rests
    reuse passage contents, including available lyrics/articulations. Other
    types are opaque descriptors and may lack content or exact positions;
    grace-note positions and persistent identities are unsupported.
    Does not navigate, select, export, or create undo commands. The bridge's
    logical cursor is independent of this actual UI selection. Not Dorico.
    """
    return await require_bridge(context).read_current_selection()


@score_tool
async def get_selection_properties(context: ScoreContext) -> SelectionProperties:
    """Get properties of the current selection in the connected application.

    MuseScore reports its bridge logical cursor (not the actual UI selection);
    use read_current_selection for selected native elements and contents.
    Dorico reports the names, types and values of every property of the
    selected items, which is the closest its API gets to reading the score.
    """
    bridge = require_bridge(context)
    properties = await bridge.get_properties()
    return properties.model_copy(update={"warning": bridge.content_reading_limitation})


def register(server: MCPServer) -> None:
    for tool in (
        read_staff_context,
        read_passage,
        get_measure_content,
        get_event_at_position,
        get_selection_properties,
        read_current_selection,
    ):
        server.tool()(tool)
