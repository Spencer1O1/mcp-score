"""Bounded native staff-context observations, separate from passage events."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt

from mcp_score.bridge.results import Duration, Result, TimeSignature

__all__ = [
    "ContextPoint",
    "InstrumentContext",
    "NativeTransposition",
    "StaffContext",
    "StaffContextRequest",
    "StaffTimeline",
    "UnavailableField",
]


class UnavailableField(Result):
    field: str
    """Public snake_case field path, including nested instrument fields."""
    reason: str


class InstrumentContext(Result):
    instrument_id: str | None
    musicxml_id: str | None
    long_name: str | None
    short_name: str | None


class NativeTransposition(Result):
    chromatic: int
    """Native chromatic steps, including full octave multiples."""
    diatonic: int
    """Native diatonic steps; not inferred from a semitone remainder."""


class ContextPoint(Result):
    measure: int
    tick: int
    """Supplementary native integer tick; use absolute_time for exact positioning."""
    absolute_time: Duration
    offset: Duration
    measure_nominal_meter: TimeSignature
    measure_actual_duration: Duration
    instrument: InstrumentContext | None
    native_clef_type: int | None
    """Raw Staff.clefType code, not a name or written-pitch conversion."""
    native_key: int | None
    """Raw Staff.key value; custom key notation and mode are not represented."""
    local_meter: TimeSignature | None
    time_stretch: Duration | None
    native_transposition: NativeTransposition | None
    unavailable_fields: list[UnavailableField]


class StaffTimeline(Result):
    staff: int
    start: ContextPoint
    changes: list[ContextPoint]
    """Observed value changes at native segment positions, in time order."""


class StaffContext(Result):
    source: Literal["live"]
    start_measure: int
    end_measure: int
    staves: list[int]
    native_major_version: int
    native_minor_version: int
    position_precision: Literal["native_fraction", "native_integer_ticks"]
    sampling: Literal["passage_start_and_native_segment_changes"]
    sampling_complete: Literal[True]
    """All bounded samples traversed; does not establish full context fidelity."""
    tempo_status: Literal["not_queried"]
    sampled_point_count: int
    staff_point_queries: int
    timelines: list[StaffTimeline]
    limitations: list[str]


class StaffContextRequest(Result):
    start_measure: StrictInt = Field(ge=1, le=9007199254740991)
    end_measure: StrictInt = Field(ge=1, le=9007199254740991)
    staff: StrictInt | None = Field(default=None, ge=0, le=9007199254740991)
