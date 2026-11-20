"""Structured live passage reads, independent of the application's cursor."""

from __future__ import annotations

from typing import Literal

from mcp_score.bridge.results import Duration, Result, TimeSignature

__all__ = ["Passage", "PassageEvent", "PassageMeasure", "PassageNote"]


class PassageNote(Result):
    """A chord tone, with concert pitch spelling independent of display mode."""

    pitch: int
    """Sounding MIDI pitch; 60 is middle C."""
    tpc: int
    """Concert tonal pitch class (MuseScore tpc1)."""
    name: str
    """Concert spelling and octave, for example C#4 or Db4."""
    displayed_tpc: int
    """TPC in the current concert/transposed display mode; not a MIDI pitch."""


class PassageEvent(Result):
    """An element at an exact position in the live score."""

    kind: Literal["chord", "rest", "other"]
    type: int
    """MuseScore's element type code."""
    staff: int
    """Staff index, starting at zero."""
    voice: int
    """Voice index, starting at zero."""
    tick: int
    offset: Duration
    """Exact offset from the measure start, as a fraction of a whole note."""
    duration: Duration | None
    """Notated length, including dots; null for non-rhythmic elements."""
    actual_duration: Duration | None
    """Elapsed score time, including tuplet scaling; null when unavailable."""
    notes: list[PassageNote]
    text: str | None = None
    """Text of an annotation, where exposed by MuseScore."""


class PassageMeasure(Result):
    """One measure across the requested staves and voices."""

    measure: int
    """Ordinal measure number, starting at one, without expanding repeats."""
    tick: int
    """Absolute measure start in MuseScore ticks."""
    time_signature: TimeSignature | None
    """Actual measure time signature; may differ from nominal time in pickups."""
    events: list[PassageEvent]


class Passage(Result):
    """Live events across an inclusive measure range, without moving selection."""

    source: Literal["live"] = "live"
    start_measure: int
    end_measure: int
    staves: list[int]
    voices: list[int]
    measures: list[PassageMeasure]
    range_complete: bool
    """Whether every requested measure and rhythmic event was traversed."""
    unsupported: list[str]
    """Notation details not serialized, even when the rhythmic range is complete."""
