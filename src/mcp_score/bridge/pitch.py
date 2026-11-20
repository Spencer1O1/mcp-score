"""Strict guarded-edit inputs and actual native pitch-edit results."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from mcp_score.bridge.passage import PassageEvent  # noqa: TC001

# Pydantic resolves the nested result annotation at runtime.
from mcp_score.bridge.results import Duration, Result

__all__ = [
    "ExpectedChord",
    "ExpectedPitchNote",
    "PitchEdit",
    "PitchEditRequest",
    "WholeNoteFraction",
]

_MAX_EXACT_INTEGER = 9007199254740991


class WholeNoteFraction(Duration):
    """An exact nonnegative request fraction; numeric coercion is forbidden."""

    numerator: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)
    denominator: StrictInt = Field(gt=0, le=_MAX_EXACT_INTEGER)


class ExpectedPitchNote(Result):
    pitch: StrictInt = Field(ge=0, le=127)
    tpc: StrictInt = Field(ge=-1, le=33)
    written_tpc: StrictInt = Field(ge=-1, le=33)
    displayed_tpc: StrictInt = Field(ge=-1, le=33)
    name: StrictStr


class ExpectedChord(Result):
    """A complete, strictly typed target snapshot from a fresh live read."""

    kind: Literal["chord"]
    type: StrictInt
    staff: StrictInt = Field(ge=0)
    voice: StrictInt = Field(ge=0, le=3)
    tick: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)
    offset: WholeNoteFraction
    duration: WholeNoteFraction
    actual_duration: WholeNoteFraction
    notes: list[ExpectedPitchNote] = Field(min_length=1)
    text: StrictStr | None = None


class PitchEditRequest(Result):
    """Coordinates and preconditions that must survive validation unchanged."""

    measure: StrictInt = Field(ge=1, le=_MAX_EXACT_INTEGER)
    staff: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)
    voice: StrictInt = Field(ge=0, le=3)
    offset: WholeNoteFraction
    note_index: StrictInt = Field(ge=0, le=_MAX_EXACT_INTEGER)
    pitch: StrictInt = Field(ge=0, le=127)
    tpc: StrictInt = Field(ge=-1, le=33)
    expected: ExpectedChord


class PitchEdit(Result):
    """Actual before/after notation, read inside the synchronous edit handler."""

    measure: int
    note_index_before: int
    """Ephemeral chord-tone index in the required expected snapshot."""
    note_index_after: int
    """Index after editing; a later operation must take a fresh snapshot."""
    before: PassageEvent
    after: PassageEvent
    verified: Literal[True]
    mutation_status: Literal["applied", "unchanged"]
    snapshot_scope: Literal["target_chord"]
    limitations: list[str]
    """Target matching does not establish application-wide identity or revision."""
