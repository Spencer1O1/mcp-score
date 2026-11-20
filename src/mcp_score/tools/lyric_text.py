"""Guard one existing ordinary lyric without adding/removing or reformatting it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import StrictInt, ValidationError

from mcp_score.bridge.chord_tone import ChordToneSnapshot
from mcp_score.bridge.lyric_text import LyricTextEdit, LyricTextRequest, PlainLyricText
from mcp_score.bridge.pitch import WholeNoteFraction
from mcp_score.context import ScoreContext
from mcp_score.tools import ToolError, require_bridge, score_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = ["register"]


@score_tool
async def set_live_lyric_text(
    context: ScoreContext,
    measure: StrictInt,
    staff: StrictInt,
    voice: StrictInt,
    offset: WholeNoteFraction,
    lyric_index: StrictInt,
    plain_text: PlainLyricText,
    expected: ChordToneSnapshot,
) -> LyricTextEdit:
    """Change ONE existing ordinary SINGLE lyric's plain text, preserving notation.

    Pass a complete fresh chord snapshot from get_measure_content, including all
    ordered lyrics/articulations, and its ephemeral zero-based lyric_index.
    The snapshot is checked atomically before mutation. Native MuseScore 4.7+
    in major version 4 and a known empty master excerpt list are required.
    Only canonical plain XML lyric text with SINGLE syllabic, zero lyric ticks,
    no melisma and no lyric separator is eligible. No add/delete, verse, timing,
    syllabic, placement, rich formatting or symbol changes. Note/chord slurs are
    allowed only with unchanged exposed native spanner metadata.

    plain_text is literal Unicode, never XML: &, <, > and double quotes are
    escaped internally; apostrophes stay literal. Nonempty single-line valid
    scalars only, at most 512 characters, without controls/noncharacters or
    unpaired surrogates. No trimming or Unicode normalization. Actual native XML
    and decoded plain text must round-trip. Font/style/alignment/user offsets,
    visibility/autoplace and lyric timing are checked privately; response includes
    explicit verified field names without float appearance values. All exposed
    measure events across tracks and score-spanner metadata are checked unchanged
    outside the declared text edit. Equal text makes no undo entry; otherwise one.

    Selection/layout may change; logical bridge position is untouched. Snapshot
    scope is exposed target-chord fields, not global revision/identity, unexposed
    links or every engraving property. Identical contents in another score cannot
    be detected. Direct calls have no duplicate protection; use execute_mutation_once
    with kind set_lyric_text for retained caller ID/session history. Never replay
    an uncertain mutation blindly. No Dorico or sequence support.
    """
    try:
        request = LyricTextRequest(
            measure=measure,
            staff=staff,
            voice=voice,
            offset=offset,
            lyric_index=lyric_index,
            plain_text=plain_text,
            expected=expected,
        )
    except ValidationError as error:
        raise ToolError(str(error)) from error
    return await require_bridge(context).set_lyric_text(request)


def register(server: MCPServer) -> None:
    server.tool()(set_live_lyric_text)
