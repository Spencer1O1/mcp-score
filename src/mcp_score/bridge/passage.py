"""Structured live passage reads, independent of the application's cursor."""

from __future__ import annotations

from typing import Literal

# Pydantic evaluates result annotations at runtime.
from pydantic import StrictInt, StrictStr  # noqa: TC002

from mcp_score.bridge.results import Duration, Result, TimeSignature

__all__ = [
    "Passage",
    "PassageArticulation",
    "PassageEvent",
    "PassageLyric",
    "PassageMeasure",
    "PassageNote",
]


class PassageArticulation(Result):
    """A native chord articulation; null fields are unavailable."""

    symbol: StrictInt | None
    """Native SymId code, scoped to the application's version, not an event ID."""
    name: StrictStr | None
    """Native localized subtype name; not a stable machine-readable identifier."""
    placement: StrictInt | None
    """Native placement property, not the final geometric engraving position."""
    placement_name: Literal["ABOVE", "BELOW"] | None
    anchor: StrictInt | None
    """Native articulation-anchor enum code, where available."""
    direction: StrictInt | None
    """Native direction enum code, where available."""


class PassageLyric(Result):
    """One native lyric attached to a chord/rest; null fields are unavailable."""

    text: StrictStr | None
    """Native text, including any MuseScore rich-text markup; not normalized."""
    verse: StrictInt | None
    """Native zero-based verse index, not a displayed verse label."""
    syllabic: StrictInt | None
    """Native syllabic enum code, retained even when its name is unknown."""
    syllabic_name: Literal["SINGLE", "BEGIN", "END", "MIDDLE"] | None
    placement: StrictInt | None
    """Native placement enum code, not a geometric layout coordinate."""
    placement_name: Literal["ABOVE", "BELOW"] | None


class PassageNote(Result):
    """A chord tone, with concert pitch spelling independent of display mode."""

    pitch: int
    """Sounding MIDI pitch; 60 is middle C."""
    tpc: int
    """Concert tonal pitch class (MuseScore tpc1)."""
    name: str
    """Concert spelling and octave, for example C#4 or Db4."""
    written_tpc: int | None = None
    """Written TPC (tpc2), without octave; null on older plugins."""
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
    lyrics: list[PassageLyric] | None = None
    """Native chord/rest lyrics; [] means none, null means unavailable/inapplicable."""
    articulations: list[PassageArticulation] | None = None
    """Chord articulations; [] means none, null means unavailable/inapplicable."""


class PassageMeasure(Result):
    """One measure across the requested staves and voices."""

    measure: int
    """Ordinal measure number, starting at one, without expanding repeats."""
    tick: int
    """Absolute measure start in MuseScore ticks."""
    actual_duration: Duration | None
    """Actual measure length, as a fraction of a whole note (including pickups)."""
    time_signature: TimeSignature | None
    """Nominal meter, where exposed; never inferred from the measure length."""
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
