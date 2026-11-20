"""The application's current native selection, distinct from a logical cursor."""

from __future__ import annotations

from typing import Literal

# Pydantic evaluates nested output annotations at runtime.
from mcp_score.bridge.passage import PassageEvent, PassageNote  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = ["CurrentSelection", "SelectionItem", "SelectionRange"]


class SelectionRange(Result):
    """Native half-open range bounds; these do not describe selection filters."""

    start_tick: int
    end_tick: int | None
    """Exclusive end tick; null means the native range extends to score end."""
    start_staff: int
    end_staff_exclusive: int
    """Exclusive staff boundary, matching native staffEnd/selectRange semantics."""


class SelectionItem(Result):
    """One native selected element; a note does not select all its chord peers."""

    kind: Literal["note", "chord", "rest", "other"]
    type: int
    """Native element type code."""
    name: str | None
    """Native localized element name, where exposed."""
    text: str | None
    track: int | None
    """Native track; null when no staff/voice assignment is exposed."""
    measure: int | None
    """Ordinal measure of the resolved segment, or null when unresolved."""
    event_context: PassageEvent | None
    """Owner chord for a note; selected chord/rest/other event otherwise."""
    note_index: int | None
    """Selected tone's index in event_context.notes, scoped to this read only."""
    note: PassageNote | None
    """Only the selected tone; null for whole chords/rests/opaque elements."""


class CurrentSelection(Result):
    """A single synchronous read of the actual native GUI selection."""

    source: Literal["live"] = "live"
    kind: Literal["none", "list", "range"]
    range: SelectionRange | None
    elements: list[SelectionItem]
    elements_complete: Literal[True]
    """Every native selected-list entry was returned; not full notation fidelity."""
    observed_voices: list[int]
    """Voices encountered in selected tracks, not the configured voice filter."""
    filter_state: Literal["not_exposed"] = "not_exposed"
    unsupported: list[str]
