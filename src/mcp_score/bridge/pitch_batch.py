"""Bounded independent pitch targets, preflighted together in one native handler."""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

# Pydantic evaluates nested input/output annotations at runtime.
from mcp_score.bridge.pitch import PitchEdit, PitchEditRequest  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = ["PitchBatchTargets", "PitchBatchRequest", "PitchBatchEdit"]

PitchBatchTargets = Annotated[
    list[PitchEditRequest], Field(min_length=1, max_length=32)
]


class PitchBatchRequest(Result):
    """Independent targets; the native handler also rejects aliased chord objects."""

    edits: PitchBatchTargets

    @model_validator(mode="after")
    def _distinct_chord_onsets(self) -> Self:
        seen: set[tuple[int, int, int, Fraction]] = set()
        for edit in self.edits:
            key = (
                edit.measure,
                edit.staff,
                edit.voice,
                Fraction(edit.offset.numerator, edit.offset.denominator),
            )
            if key in seen:
                raise ValueError("Batch targets must belong to distinct chords")
            seen.add(key)
        return self


class PitchBatchEdit(Result):
    """Actual native snapshots for every target, in request order."""

    edits: list[PitchEdit] = Field(min_length=1, max_length=32)
    verified: Literal[True]
    mutation_status: Literal["applied", "unchanged"]
    snapshot_scope: Literal["target_chords"]
    limitations: list[str]
