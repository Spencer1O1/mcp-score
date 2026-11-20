"""Strict single existing-lyric edits; appearance values remain native/private."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StrictInt, StrictStr

from mcp_score.bridge.chord_tone import ChordToneSnapshot  # noqa: TC001
from mcp_score.bridge.passage import PassageEvent  # noqa: TC001
from mcp_score.bridge.pitch import WholeNoteFraction  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = ["LyricTextEdit", "LyricTextRequest", "NativeLyricText", "PlainLyricText"]


def _plain_text(value: str) -> str:
    # No normalization or trimming: the exact supplied scalars must round-trip.
    for character in value:
        point = ord(character)
        if (
            point < 0x20
            or 0x7F <= point <= 0x9F
            or 0xD800 <= point <= 0xDFFF
            or 0xFDD0 <= point <= 0xFDEF
            or point & 0xFFFF in {0xFFFE, 0xFFFF}
            or point in {0x2028, 0x2029}
        ):
            raise ValueError(
                "Lyric requires single-line Unicode scalar text "
                "without controls or noncharacters"
            )
    return value


PlainLyricText = Annotated[
    StrictStr, Field(min_length=1, max_length=512), AfterValidator(_plain_text)
]


class LyricTextRequest(Result):
    measure: StrictInt = Field(ge=1, le=9007199254740991)
    staff: StrictInt = Field(ge=0, le=9007199254740991)
    voice: StrictInt = Field(ge=0, le=3)
    offset: WholeNoteFraction
    lyric_index: StrictInt = Field(ge=0, le=9007199254740991)
    plain_text: PlainLyricText
    expected: ChordToneSnapshot


class NativeLyricText(Result):
    """Actual native string/timing metadata; no float appearance fields in receipts."""

    xml_text: str
    plain_text: str
    verse: int
    syllabic: int
    placement: int
    lyric_ticks: WholeNoteFraction


class LyricTextEdit(Result):
    measure: int
    lyric_index: int
    before: PassageEvent
    after: PassageEvent
    selected_before: NativeLyricText
    selected_after: NativeLyricText
    verified: Literal[True]
    mutation_status: Literal["applied", "unchanged"]
    snapshot_scope: Literal["target_chord"]
    selection_status: Literal["unchanged", "not_preserved"]
    verified_native_fields: list[str]
    limitations: list[str]
