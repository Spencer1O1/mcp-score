"""Strict preconditions and actual measure read-back for native duration edits."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from mcp_score.bridge.passage import (  # noqa: TC001
    PassageArticulation,
    PassageEvent,
    PassageLyric,
)
from mcp_score.bridge.pitch import (  # noqa: TC001
    ExpectedPitchNote,
    WholeNoteFraction,
)
from mcp_score.bridge.results import Result

__all__ = [
    "DurationEdit",
    "DurationEditRequest",
    "DurationPolicy",
    "ExpectedDurationEvent",
]

DurationPolicy = Literal["shorten_fill_rest", "extend_into_rests"]


class ExpectedDurationEvent(Result):
    """Every event in the fresh target voice's complete measure read."""

    kind: Literal["chord", "rest", "other"]
    type: StrictInt
    staff: StrictInt = Field(ge=0)
    voice: StrictInt = Field(ge=0, le=3)
    tick: StrictInt = Field(ge=0, le=9007199254740991)
    offset: WholeNoteFraction
    duration: WholeNoteFraction | None
    actual_duration: WholeNoteFraction | None
    notes: list[ExpectedPitchNote]
    text: StrictStr | None = None
    lyrics: list[PassageLyric] | None = None
    articulations: list[PassageArticulation] | None = None


class DurationEditRequest(Result):
    measure: StrictInt = Field(ge=1, le=9007199254740991)
    staff: StrictInt = Field(ge=0, le=9007199254740991)
    voice: StrictInt = Field(ge=0, le=3)
    offset: WholeNoteFraction
    duration: WholeNoteFraction
    policy: DurationPolicy = "shorten_fill_rest"
    expected: list[ExpectedDurationEvent] = Field(min_length=1)


class DurationEdit(Result):
    """Actual before/after events across all tracks of the edited measure."""

    measure: int
    staff: int
    voice: int
    before: list[PassageEvent]
    after: list[PassageEvent]
    verified: Literal[True]
    mutation_status: Literal["applied", "unchanged"]
    snapshot_scope: Literal["target_voice_measure"]
    selection_status: Literal["unchanged", "not_preserved"]
    """Native rewriting may deselect; no selection restoration is attempted."""
    limitations: list[str]
