"""Exact rhythmic event lookup and its MCP input boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.event_address import EventAddress, locate_event
from mcp_score.bridge.passage import Passage, PassageEvent, PassageMeasure, PassageNote
from mcp_score.bridge.results import Duration
from mcp_score.server import create_server
from mcp_score.tools import ToolError
from mcp_score.tools.analysis import get_event_at_position
from tests.fakes import BridgeCall

if TYPE_CHECKING:
    from mcp_score.bridge import BridgeRegistry
    from mcp_score.context import ScoreContext
    from tests.fakes import FakeBridge


@pytest.fixture
def chord() -> PassageEvent:
    return PassageEvent(
        kind="chord",
        type=124,
        staff=0,
        voice=0,
        tick=960,
        offset=Duration(numerator=1, denominator=2),
        duration=Duration(numerator=1, denominator=2),
        actual_duration=Duration(numerator=1, denominator=2),
        notes=[
            PassageNote(pitch=60, tpc=14, name="C4", displayed_tpc=14),
            PassageNote(pitch=64, tpc=18, name="E4", displayed_tpc=18),
        ],
    )


def passage_with(*events: PassageEvent) -> Passage:
    return Passage(
        start_measure=1,
        end_measure=1,
        staves=[0],
        voices=[0],
        measures=[
            PassageMeasure(
                measure=1,
                tick=0,
                actual_duration=Duration(numerator=1, denominator=1),
                time_signature=None,
                events=list(events),
            )
        ],
        range_complete=True,
        unsupported=["grace-note contents", "tie endpoints"],
    )


def address(**overrides: Any) -> EventAddress:
    return EventAddress.model_validate(
        {
            "measure": 1,
            "staff": 0,
            "voice": 0,
            "offset_numerator": 2,
            "offset_denominator": 4,
            **overrides,
        }
    )


def test_equivalent_fraction_selects_whole_chord_and_preserves_read_limits(
    chord: PassageEvent,
) -> None:
    passage = passage_with(chord)
    before = passage.model_dump()
    result = locate_event(passage, address())
    assert result.address.offset_numerator == 1
    assert result.address.offset_denominator == 2
    assert result.event == chord
    assert [note.name for note in result.event.notes] == ["C4", "E4"]
    assert result.note is None
    assert result.scope == "single_read"
    assert result.unsupported == passage.unsupported
    assert passage.model_dump() == before


def test_explicit_chord_tone_index_selects_current_note(chord: PassageEvent) -> None:
    result = locate_event(passage_with(chord), address(note_index=1))
    assert result.note == chord.notes[1]
    assert result.address.note_index == 1
    assert len(result.event.notes) == 2


def test_simultaneous_annotations_and_other_tracks_do_not_make_onset_ambiguous(
    chord: PassageEvent,
) -> None:
    annotation = chord.model_copy(update={"kind": "other", "notes": [], "text": "mf"})
    other_staff = chord.model_copy(update={"staff": 1})
    other_voice = chord.model_copy(update={"voice": 1})
    result = locate_event(
        passage_with(annotation, other_staff, other_voice, chord), address()
    )
    assert result.event == chord


def test_position_inside_sustained_note_is_not_an_onset(chord: PassageEvent) -> None:
    with pytest.raises(BridgeError, match="exact onset"):
        locate_event(
            passage_with(chord), address(offset_numerator=5, offset_denominator=8)
        )


def test_duplicate_rhythmic_onsets_fail_without_selecting_first(
    chord: PassageEvent,
) -> None:
    with pytest.raises(BridgeError, match="Ambiguous rhythmic onset"):
        locate_event(passage_with(chord, chord.model_copy()), address())


def test_empty_track_reports_absent_onset() -> None:
    with pytest.raises(BridgeError, match="No rhythmic event starts"):
        locate_event(passage_with(), address())


def test_rest_is_a_whole_event_but_has_no_chord_tones(chord: PassageEvent) -> None:
    rest = chord.model_copy(update={"kind": "rest", "notes": []})
    assert locate_event(passage_with(rest), address()).event.kind == "rest"
    with pytest.raises(BridgeError, match="rest and has no chord tones"):
        locate_event(passage_with(rest), address(note_index=0))


def test_chord_tone_out_of_range_reports_available_tones(chord: PassageEvent) -> None:
    with pytest.raises(BridgeError, match="the chord has 2 tones"):
        locate_event(passage_with(chord), address(note_index=2))


@pytest.mark.parametrize(
    "changes",
    [
        {"range_complete": False},
        {"measures": []},
        {"start_measure": 2},
        {"end_measure": 2},
        {"staves": [1]},
        {"voices": [1]},
    ],
)
def test_incomplete_or_wrong_passage_is_not_used_for_lookup(
    chord: PassageEvent, changes: dict[str, object]
) -> None:
    with pytest.raises(BridgeError, match="complete read"):
        locate_event(passage_with(chord).model_copy(update=changes), address())


@pytest.mark.parametrize(
    "offset",
    [Duration(numerator=-1, denominator=2), Duration(numerator=1, denominator=0)],
)
def test_malformed_reader_offset_is_reported_as_protocol_error(
    chord: PassageEvent, offset: Duration
) -> None:
    with pytest.raises(BridgeError, match="invalid rhythmic event offset"):
        locate_event(
            passage_with(chord.model_copy(update={"offset": offset})), address()
        )


@pytest.mark.anyio()
async def test_tool_uses_one_scoped_read_without_navigation(
    connected_bridge: FakeBridge, context: ScoreContext, chord: PassageEvent
) -> None:
    connected_bridge.reply("read_passage", passage_with(chord))
    result = await get_event_at_position(context, 1, 0, 0, 2, 4, note_index=1)
    assert result.note is not None and result.note.name == "E4"
    assert connected_bridge.calls == [BridgeCall("read_passage", (1, 1, 0, 0))]


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measure", 0),
        ("staff", -1),
        ("voice", 4),
        ("offset_numerator", -1),
        ("offset_numerator", 1.5),
        ("offset_numerator", 1.0),
        ("offset_numerator", "1"),
        ("offset_numerator", True),
        ("offset_denominator", 0),
        ("offset_denominator", -1),
        ("offset_denominator", 4.0),
        ("offset_denominator", "4"),
        ("note_index", -1),
        ("note_index", 0.0),
    ],
)
async def test_mcp_rejects_invalid_coordinates_before_reading(
    registry: BridgeRegistry,
    connected_bridge: FakeBridge,
    context: ScoreContext,
    field: str,
    value: object,
) -> None:
    server = create_server(registry)
    arguments: dict[str, object] = {
        "measure": 1,
        "staff": 0,
        "voice": 0,
        "offset_numerator": 1,
        "offset_denominator": 4,
    }
    arguments[field] = value
    with pytest.raises(ToolError):
        await server.call_tool("get_event_at_position", arguments, context=context)
    assert connected_bridge.calls == []


@pytest.mark.anyio()
async def test_tool_reports_reader_error_without_cursor_fallback(
    connected_bridge: FakeBridge, context: ScoreContext
) -> None:
    connected_bridge.fail("read_passage", "Measure out of range")
    with pytest.raises(ToolError, match="Measure out of range"):
        await get_event_at_position(context, 99, 0, 0, 0, 1)
    assert connected_bridge.calls == [BridgeCall("read_passage", (99, 99, 0, 0))]
