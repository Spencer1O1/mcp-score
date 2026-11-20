"""Strict snapshots for native rest/note replacement without shifting time."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt

from mcp_score.bridge.duration_edit import ExpectedDurationEvent  # noqa: TC001
from mcp_score.bridge.passage import PassageEvent  # noqa: TC001
from mcp_score.bridge.pitch import WholeNoteFraction  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = ["FillRestRequest", "NoteRestEdit", "ReplaceNoteRequest"]


class ReplaceNoteRequest(Result):
    """The complete target voice measure is checked before the native write."""

    measure: StrictInt = Field(ge=1, le=9007199254740991)
    staff: StrictInt = Field(ge=0, le=9007199254740991)
    voice: StrictInt = Field(ge=0, le=3)
    offset: WholeNoteFraction
    expected: list[ExpectedDurationEvent] = Field(min_length=1)


class FillRestRequest(ReplaceNoteRequest):
    duration: WholeNoteFraction
    pitch: StrictInt = Field(ge=0, le=127)
    tpc: StrictInt = Field(ge=-1, le=33)


class NoteRestEdit(Result):
    """Actual all-track measure events, reread after structural replacement."""

    measure: int
    staff: int
    voice: int
    before: list[PassageEvent]
    after: list[PassageEvent]
    verified: Literal[True]
    mutation_status: Literal["applied"]
    snapshot_scope: Literal["target_voice_measure"]
    selection_status: Literal["not_preserved"]
    limitations: list[str]
