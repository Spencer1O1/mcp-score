"""One public stdio workflow, exclusively in the disposable Actions passage phase.

No bridge methods, injected contexts, selection setup, or fault hooks are used.
Native selection and logical cursor are observable; dirty/viewport state is not.
"""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from fractions import Fraction
from typing import Any
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

from mcp_score.bridge.current_selection import CurrentSelection
from mcp_score.bridge.event_address import LocatedEvent
from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.passage import Passage
from mcp_score.bridge.pitch import PitchEdit
from mcp_score.bridge.receipts import MutationReceipt
from mcp_score.bridge.results import Duration, ScoreInfo, SelectionProperties
from mcp_score.tools.connection import MuseScoreConnected

_PHASE_ENABLED = (
    os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("MCP_SCORE_INTEGRATION") == "1"
    and os.environ.get("MCP_SCORE_PASSAGE_FIXTURE") == "1"
    and os.environ.get("MCP_SCORE_PUBLIC_WORKFLOW_FIXTURE") == "1"
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _PHASE_ENABLED,
        reason="requires the explicitly enabled disposable Actions passage phase",
    ),
]
_CALL_TIMEOUT = 15
_WORKFLOW_TIMEOUT = 120
_REQUIRED_TOOLS = {
    "connect_to_musescore",
    "disconnect_from_musescore",
    "get_live_score_info",
    "get_measure_content",
    "read_passage",
    "get_event_at_position",
    "read_current_selection",
    "get_selection_properties",
    "execute_mutation_once",
    "read_mutation_receipt",
    "undo_last_action",
}


async def _call[T: BaseModel](
    session: ClientSession,
    model: type[T],
    name: str,
    arguments: dict[str, Any] | None = None,
) -> T:
    reply = await session.call_tool(name, arguments, read_timeout_seconds=_CALL_TIMEOUT)
    assert not reply.is_error, (name, reply.content)
    assert reply.structured_content is not None, name
    return model.model_validate(reply.structured_content)


def _fingerprint(passage: Passage) -> list[tuple[object, ...]]:
    """All known rhythmic/spelling/lyric contents of both fixture measures."""
    return sorted(
        (
            measure.measure,
            event.staff,
            event.voice,
            event.tick,
            event.offset.numerator,
            event.offset.denominator,
            event.kind,
            event.duration.numerator if event.duration else None,
            event.duration.denominator if event.duration else None,
            tuple(
                (n.pitch, n.tpc, n.written_tpc, n.displayed_tpc, n.name)
                for n in event.notes
            ),
            tuple(
                (lyric.text, lyric.verse, lyric.syllabic_name, lyric.placement_name)
                for lyric in event.lyrics or []
            ),
        )
        for measure in passage.measures
        for event in measure.events
        if event.kind != "other"
    )


def _assert_fixture(info: ScoreInfo, passage: Passage) -> None:
    # Refuse to mutate an arbitrary score even if the phase was misconfigured.
    assert _PHASE_ENABLED
    assert info.title == "Live passage fixture" and info.measure_count == 2
    assert info.part_count == 3 and [
        (p.start_staff, p.end_staff) for p in info.parts
    ] == [(0, 1), (2, 2), (3, 3)]
    assert passage.source == "live" and passage.range_complete
    assert passage.staves == [0, 1, 2, 3] and passage.voices == [0, 1, 2, 3]
    assert [(m.measure, m.tick) for m in passage.measures] == [(1, 0), (2, 1920)]
    for measure in passage.measures:
        assert measure.actual_duration == Duration(numerator=1, denominator=1)
        assert measure.time_signature is not None
        assert (
            measure.time_signature.numerator,
            measure.time_signature.denominator,
        ) == (4, 4)
        for event in measure.events:
            if event.kind != "other":
                assert event.actual_duration == event.duration
                assert event.lyrics is not None
    expected: list[tuple[object, ...]] = []
    for index, (pitch, tpc, name, texts, syllables) in enumerate(
        [
            (71, 19, "B4", ["Hel", "One"], ["BEGIN", "SINGLE"]),
            (69, 17, "A4", ["lo"], ["MIDDLE"]),
            (67, 15, "G4", ["world"], ["END"]),
            (65, 13, "F4", ["finé &amp; more"], ["SINGLE"]),
        ]
    ):
        offset = Duration(numerator=index, denominator=4)
        # Match the reader's reduced fraction without float conversions.
        reduced = Fraction(offset.numerator, offset.denominator)
        expected.append(
            (
                1,
                0,
                0,
                index * 480,
                reduced.numerator,
                reduced.denominator,
                "chord",
                1,
                4,
                ((pitch, tpc, tpc, tpc, name),),
                tuple(
                    (text, verse, syllable, "BELOW")
                    for verse, (text, syllable) in enumerate(
                        zip(texts, syllables, strict=True)
                    )
                ),
            )
        )
    expected.extend(
        [
            (1, 0, 1, 0, 0, 1, "rest", 1, 4, (), ()),
            (
                1,
                0,
                1,
                480,
                1,
                4,
                "chord",
                3,
                4,
                ((60, 14, 14, 14, "C4"), (64, 18, 18, 18, "E4")),
                (("low", 0, "SINGLE", "ABOVE"),),
            ),
            (2, 0, 0, 1920, 0, 1, "rest", 1, 1, (), ()),
            (
                2,
                1,
                0,
                1920,
                0,
                1,
                "chord",
                1,
                1,
                ((43, 15, 15, 15, "G2"),),
                (("bass", 0, "SINGLE", "BELOW"),),
            ),
        ]
    )
    for staff in (1, 2, 3):
        expected.append((1, staff, 0, 0, 0, 1, "rest", 1, 1, (), ()))
    for staff in (2, 3):
        expected.append((2, staff, 0, 1920, 0, 1, "rest", 1, 1, (), ()))
    assert _fingerprint(passage) == sorted(expected)
    first = next(
        e
        for e in passage.measures[0].events
        if e.kind == "chord" and e.staff == 0 and e.voice == 0
    )
    if first.articulations is not None:
        assert len(first.articulations) == 1
        assert (
            first.articulations[0].name
            and "staccato" in first.articulations[0].name.lower()
        )


async def _read_preserving_state(session: ClientSession) -> Passage:
    cursor = await _call(session, SelectionProperties, "get_selection_properties")
    selection = await _call(session, CurrentSelection, "read_current_selection")
    passage = await _call(
        session, Passage, "read_passage", {"start_measure": 1, "end_measure": 2}
    )
    measure = await _call(session, Passage, "get_measure_content", {"measure": 1})
    assert measure.measures == passage.measures[:1]
    assert await _call(session, CurrentSelection, "read_current_selection") == selection
    assert (
        await _call(session, SelectionProperties, "get_selection_properties") == cursor
    )
    return passage


def _applied(receipt: MutationReceipt) -> bool:
    original = receipt.original_reply or {}
    result = original.get("result")
    return (
        receipt.status == "completed"
        and isinstance(result, dict)
        and result.get("mutationStatus") == "applied"
        and result.get("verified") is True
    )


async def _scenario(session: ClientSession) -> None:
    await session.initialize()
    tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} >= _REQUIRED_TOOLS, (
        "Legacy/incomplete MCP server catalog"
    )
    assert all(
        tool.output_schema for tool in tools.tools if tool.name in _REQUIRED_TOOLS
    )
    connected = await _call(session, MuseScoreConnected, "connect_to_musescore")
    capabilities = connected.bridge.receipts
    assert (
        capabilities is not None and "setNotePitch" in capabilities.supported_mutations
    )
    info = await _call(session, ScoreInfo, "get_live_score_info")
    baseline = await _read_preserving_state(session)
    _assert_fixture(info, baseline)
    located = await _call(
        session,
        LocatedEvent,
        "get_event_at_position",
        {
            "measure": 1,
            "staff": 0,
            "voice": 0,
            "offset_numerator": 1,
            "offset_denominator": 2,
            "note_index": 0,
        },
    )
    assert located.note is not None and located.note.name == "G4"
    expected_after = baseline.model_copy(deep=True)
    target = next(
        e
        for e in expected_after.measures[0].events
        if e.kind == "chord" and e.staff == 0 and e.voice == 0 and e.tick == 960
    )
    target.notes[0] = target.notes[0].model_copy(
        update={
            "pitch": 69,
            "tpc": 17,
            "written_tpc": 17,
            "displayed_tpc": 17,
            "name": "A4",
        }
    )
    # Retain session, ID and exact payload before the first send. Do not mutate it.
    request = {
        "operation_id": str(uuid4()),
        "expected_plugin_session": capabilities.plugin_session,
        "mutation": {
            "kind": "set_note_pitch",
            "request": {
                "measure": 1,
                "staff": 0,
                "voice": 0,
                "offset": {"numerator": 1, "denominator": 2},
                "note_index": 0,
                "pitch": 69,
                "tpc": 17,
                "expected": located.event.model_dump(),
            },
        },
    }
    identity = {
        key: request[key] for key in ("operation_id", "expected_plugin_session")
    }
    original_cursor = await _call(
        session, SelectionProperties, "get_selection_properties"
    )
    submitted = False
    undo_attempted = False
    stale_identity: dict[str, Any] | None = None
    try:
        submitted = True
        receipt = await _call(
            session, MutationReceipt, "execute_mutation_once", request
        )
        assert receipt.operation_id == identity["operation_id"]
        assert receipt.plugin_session == identity["expected_plugin_session"]
        assert _applied(receipt) and not receipt.cached and receipt.historical
        assert receipt.original_reply is not None
        edit = PitchEdit.model_validate(
            _snake_case_keys(receipt.original_reply["result"])
        )
        assert edit.verified and edit.before == located.event and edit.after == target
        edited_cursor = await _call(
            session, SelectionProperties, "get_selection_properties"
        )
        # The cursor's embedded element can legitimately be the edited note.
        assert edited_cursor.model_dump(exclude={"cursor": {"element"}}) == (
            original_cursor.model_dump(exclude={"cursor": {"element"}})
        )
        assert await _read_preserving_state(session) == expected_after
        repeat = await _call(session, MutationReceipt, "execute_mutation_once", request)
        assert repeat.cached and repeat.original_reply == receipt.original_reply
        assert await _read_preserving_state(session) == expected_after
        stale = deepcopy(request)
        stale["operation_id"] = str(uuid4())
        stale_identity = {key: stale[key] for key in identity}
        rejected = await _call(session, MutationReceipt, "execute_mutation_once", stale)
        assert rejected.status == "completed" and rejected.original_reply is not None
        assert rejected.original_reply.get("code") == "stale_target"
        assert rejected.original_reply.get("mutation_status") == "not_applied"
        assert await _read_preserving_state(session) == expected_after
        # Undo is unreceipted. Once sent, even an error/timeout cannot authorize
        # another undo from the historical pitch receipt or matching contents.
        undo_attempted = True
        undo = await session.call_tool(
            "undo_last_action", {}, read_timeout_seconds=_CALL_TIMEOUT
        )
        assert not undo.is_error, undo.content
        assert await _read_preserving_state(session) == baseline
        historical = await _call(
            session, MutationReceipt, "execute_mutation_once", request
        )
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert await _read_preserving_state(session) == baseline
    finally:
        try:
            if submitted and not undo_attempted:
                # SDK/schema failures can hide an applied reply. Resolve the
                # PUBLIC receipt, without replaying; unknown grants no undo authority.
                retained = await _call(
                    session, MutationReceipt, "read_mutation_receipt", identity
                )
                assert retained.operation_id == identity["operation_id"]
                assert retained.plugin_session == identity["expected_plugin_session"]
                if _applied(retained):
                    if stale_identity is not None:
                        stale_history = await _call(
                            session,
                            MutationReceipt,
                            "read_mutation_receipt",
                            stale_identity,
                        )
                        assert (
                            stale_history.operation_id == stale_identity["operation_id"]
                        )
                        assert (
                            stale_history.plugin_session
                            == stale_identity["expected_plugin_session"]
                        )
                        original = stale_history.original_reply or {}
                        if (
                            stale_history.status != "completed"
                            or original.get("mutation_status") != "not_applied"
                        ):
                            raise RuntimeError(
                                "Cleanup refused: later mutation uncertain; no undo"
                            )
                    current = await _read_preserving_state(session)
                    if current != expected_after:
                        raise RuntimeError(
                            "Cleanup refused: unexpected or intervening edit; no undo"
                        )
                    undo_attempted = True
                    cleanup = await session.call_tool(
                        "undo_last_action", {}, read_timeout_seconds=_CALL_TIMEOUT
                    )
                    assert not cleanup.is_error, cleanup.content
                    assert await _read_preserving_state(session) == baseline
                else:
                    original = retained.original_reply or {}
                    result = original.get("result")
                    unchanged = (
                        isinstance(result, dict)
                        and result.get("mutationStatus") == "unchanged"
                    )
                    if (
                        original.get("mutation_status") != "not_applied"
                        and not unchanged
                    ):
                        raise RuntimeError(
                            "Cleanup refused: original mutation uncertain; no undo"
                        )
        finally:
            disconnected = await session.call_tool(
                "disconnect_from_musescore", {}, read_timeout_seconds=_CALL_TIMEOUT
            )
            assert not disconnected.is_error, disconnected.content


@pytest.mark.anyio()
async def test_public_stdio_read_guarded_edit_retry_stale_and_single_undo() -> None:
    # This starts only the Python MCP server; the owned MuseScore fixture already runs.
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from mcp_score.cli import main; raise SystemExit(main(['serve']))",
        ],
    )
    async with (
        asyncio.timeout(_WORKFLOW_TIMEOUT),
        stdio_client(parameters) as streams,
        ClientSession(*streams) as session,
    ):
        await _scenario(session)
