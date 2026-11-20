"""Guarded time-preserving rest/note replacement in the live application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import StrictInt

from mcp_score.bridge.duration_edit import ExpectedDurationEvent
from mcp_score.bridge.note_rest import FillRestRequest, NoteRestEdit, ReplaceNoteRequest
from mcp_score.bridge.pitch import WholeNoteFraction
from mcp_score.context import ScoreContext
from mcp_score.tools import require_bridge, score_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = ["register"]


@score_tool
async def fill_live_rest(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset: WholeNoteFraction,
    duration: WholeNoteFraction,
    pitch: StrictInt,
    tpc: StrictInt,
    expected: list[ExpectedDurationEvent],
) -> NoteRestEdit:
    """Replace a plain rest onset with one pitched note, leaving trailing rests.

    MuseScore 4.7+ in major version 4 only. Pass ALL fresh get_measure_content
    events filtered to the explicit staff and voice as expected. This snapshot
    is matched atomically before any mutation. A canonical undotted whole through
    32nd note must fit entirely in the original rest and bar. Ordinary full-measure
    rests require a regular 4/4 bar; pickups and irregular full-measure rests reject.
    No occupied time is overwritten or shifted. Native suffix rests may be dotted
    or split; their exact contiguous coverage is verified, with other measure
    tracks, adjacent measure events, exposed context and spanner metadata.

    Pitch is concert MIDI; tpc is concert spelling (C=14, F#=20). Native staff
    transposition supplies written TPC without changing the score display mode.
    Dotted targets, beams, attachments, tuplets, ties, grace notes, microtonality,
    drum/tab, nonunit time stretch and intersecting/unknown spanners reject.
    Actual before/after all-track measure events are returned after one undo step.
    UI selection/layout may change; the logical bridge cursor stays put. Snapshot
    coverage is exposed notation, not full engraving or score identity/revision;
    identical contents in another score cannot be distinguished. Not available
    with Dorico. No sequence or mutation-receipt support; never automatically
    retry this mutation after an uncertain response.
    """
    request = FillRestRequest(
        measure=measure,
        staff=staff,
        voice=voice,
        offset=offset,
        duration=duration,
        pitch=pitch,
        tpc=tpc,
        expected=expected,
    )
    return await require_bridge(context).fill_rest_with_note(request)


@score_tool
async def replace_live_note_with_rest(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset: WholeNoteFraction,
    expected: list[ExpectedDurationEvent],
) -> NoteRestEdit:
    """Replace an ordinary single-tone chord with equal-duration plain rest coverage.

    Native rhythmic decomposition may split rests; their exact interval union
    must equal the original chord span. Supply ALL fresh target staff/voice
    measure events as expected. Atomic native
    validation and actual all-track read-back prevent partial or stale replacement.
    Only undotted whole through 32nd, ordinary unbeamed single-tone chords without
    lyrics, articulations, other attachments, ties, tuplets, grace notes,
    microtonality or intersecting/unknown spanners are supported. Requires
    MuseScore 4.7+ in major version 4 and pitched non-drum/non-tab staff with unit
    time stretch. Other events, adjacent measures, exposed context and spanner
    metadata are verified unchanged. One undo step, no time shifting. UI selection
    and layout may change; logical bridge cursor is untouched. No app-wide score
    identity/revision or full engraving guarantee. No Dorico, sequence, mutation
    receipts or automatic replay; inspect an uncertain response before proceeding.
    """
    request = ReplaceNoteRequest(
        measure=measure,
        staff=staff,
        voice=voice,
        offset=offset,
        expected=expected,
    )
    return await require_bridge(context).replace_note_with_rest(request)


def register(server: MCPServer) -> None:
    server.tool()(fill_live_rest)
    server.tool()(replace_live_note_with_rest)
