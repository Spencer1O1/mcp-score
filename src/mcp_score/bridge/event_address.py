"""Exact-onset lookup within a single live passage read."""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from mcp_score.bridge.base import BridgeError

# Pydantic evaluates nested output-model annotations at runtime.
from mcp_score.bridge.passage import PassageEvent, PassageNote  # noqa: TC001
from mcp_score.bridge.results import Result

if TYPE_CHECKING:
    from mcp_score.bridge.passage import Passage

__all__ = ["EventAddress", "LocatedEvent", "locate_event"]


class EventAddress(Result):
    """A rhythmic event onset and optional chord-tone index in one read."""

    measure: int = Field(strict=True, ge=1)
    """Ordinal measure, starting at one, without expanding repeats."""
    staff: int = Field(strict=True, ge=0)
    """Staff index, starting at zero."""
    voice: int = Field(strict=True, ge=0, le=3)
    """Voice index, starting at zero."""
    offset_numerator: int = Field(strict=True, ge=0)
    """Numerator of the exact onset offset as a fraction of a whole note."""
    offset_denominator: int = Field(strict=True, gt=0)
    """Denominator of the whole-note onset offset."""
    note_index: int | None = Field(default=None, strict=True, ge=0)
    """Index into this read's chord tones; omitted to select the whole event."""


class LocatedEvent(Result):
    """A current rhythmic event, without durable identity or edit preconditions."""

    source: Literal["live"] = "live"
    scope: Literal["single_read"] = "single_read"
    """The address and note index describe only the returned live snapshot."""
    address: EventAddress
    """Requested address with its whole-note offset reduced to lowest terms."""
    event: PassageEvent
    """The entire chord or rest, including all chord tones."""
    note: PassageNote | None
    """The selected tone, or null when the whole event was requested."""
    unsupported: list[str]
    """Notation details omitted by the underlying live passage reader."""


def locate_event(passage: Passage, address: EventAddress) -> LocatedEvent:
    """Resolve an exact rhythmic onset in a complete single-measure read.

    Offsets inside a sustained event do not select that event. Non-rhythmic
    annotations are excluded. Ambiguous onsets fail rather than selecting the
    first match. This operation does not establish score identity or revision.

    Raises:
        BridgeError: When the read is incomplete, the onset is absent or
            ambiguous, or the requested chord tone does not exist.
    """
    if (
        not passage.range_complete
        or passage.start_measure != address.measure
        or passage.end_measure != address.measure
        or len(passage.measures) != 1
        or passage.measures[0].measure != address.measure
        or address.staff not in passage.staves
        or address.voice not in passage.voices
    ):
        raise BridgeError(
            "Event lookup requires a complete read of the requested measure and track."
        )

    offset = Fraction(address.offset_numerator, address.offset_denominator)
    matches: list[PassageEvent] = []
    for event in passage.measures[0].events:
        if event.staff != address.staff or event.voice != address.voice:
            continue
        if event.kind not in {"chord", "rest"}:
            continue
        if event.offset.numerator < 0 or event.offset.denominator <= 0:
            raise BridgeError(
                "The live reader returned an invalid rhythmic event offset."
            )
        event_offset = Fraction(event.offset.numerator, event.offset.denominator)
        if event_offset == offset:
            matches.append(event)

    position = (
        f"measure {address.measure}, staff {address.staff}, voice {address.voice}, "
        f"whole-note offset {offset.numerator}/{offset.denominator}"
    )
    if not matches:
        raise BridgeError(
            f"No rhythmic event starts at {position}. "
            "Lookup requires an exact onset, not a position inside a sustained event."
        )
    if len(matches) > 1:
        raise BridgeError(
            f"Ambiguous rhythmic onset at {position}; no event was selected."
        )

    event = matches[0]
    note: PassageNote | None = None
    if address.note_index is not None:
        if event.kind != "chord":
            raise BridgeError(
                f"The event at {position} is a rest and has no chord tones."
            )
        if address.note_index >= len(event.notes):
            raise BridgeError(
                f"note_index {address.note_index} is out of range at {position}; "
                f"the chord has {len(event.notes)} tones."
            )
        note = event.notes[address.note_index]

    normalized = address.model_copy(
        update={
            "offset_numerator": offset.numerator,
            "offset_denominator": offset.denominator,
        }
    )
    return LocatedEvent(
        address=normalized,
        event=event,
        note=note,
        unsupported=list(passage.unsupported),
    )
