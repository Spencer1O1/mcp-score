"""Strict whole-chord snapshots for isolated native tone addition/removal."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from mcp_score.bridge.passage import (  # noqa: TC001
    PassageArticulation,
    PassageEvent,
    PassageLyric,
)
from mcp_score.bridge.pitch import ExpectedChord, WholeNoteFraction  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = [
    "ChordToneSnapshot",
    "AddChordToneRequest",
    "RemoveChordToneRequest",
    "ChordToneEdit",
]

_MAX_EXACT_INTEGER = 9007199254740991


class ChordToneSnapshot(ExpectedChord):
    """All exposed fields from a fresh chord read, including attachment metadata."""

    text: StrictStr | None = Field(...)
    lyrics: list[PassageLyric] | None = Field(...)
    articulations: list[PassageArticulation] | None = Field(...)


class _ChordToneTarget(Result):
    measure: StrictInt = Field(ge=1, le=_MAX_EXACT_INTEGER)
    staff: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)
    voice: StrictInt = Field(ge=0, le=3)
    offset: WholeNoteFraction
    expected: ChordToneSnapshot


class AddChordToneRequest(_ChordToneTarget):
    pitch: StrictInt = Field(ge=0, le=127)
    tpc: StrictInt = Field(ge=-1, le=33)


class RemoveChordToneRequest(_ChordToneTarget):
    note_index: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)


class ChordToneEdit(Result):
    """Actual target snapshots; indices are ephemeral within this operation."""

    operation: Literal["add", "remove"]
    measure: int
    note_index_before: int | None
    note_index_after: int | None
    before: PassageEvent
    after: PassageEvent
    verified: Literal[True]
    mutation_status: Literal["applied"]
    snapshot_scope: Literal["target_chord"]
    selection_status: Literal["not_preserved"]
    limitations: list[str]
