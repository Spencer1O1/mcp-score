"""Owned native replacement, spelling, suffix coverage, and cancellation proof."""

from __future__ import annotations

import os
from fractions import Fraction

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.duration_edit import ExpectedDurationEvent
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.note_rest import FillRestRequest, ReplaceNoteRequest

_MODE = os.environ.get("MCP_SCORE_NOTE_REST_FIXTURE")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _MODE not in {"normal", "concert"},
        reason="requires owned note/rest fixture phase",
    ),
]


def _supported() -> bool:
    return tuple(map(int, os.environ["MUSESCORE_VERSION"].split("."))) >= (4, 7)


def _request(
    passage, measure: int, staff: int, voice: int, fill: bool, *, full: bool = False
):
    events = passage.measures[measure - 1].events
    snapshot = [
        ExpectedDurationEvent.model_validate(e.model_dump())
        for e in events
        if e.staff == staff and e.voice == voice
    ]
    target = next(e for e in snapshot if e.kind == ("rest" if fill else "chord"))
    common = dict(
        measure=measure,
        staff=staff,
        voice=voice,
        offset=target.offset.model_dump(),
        expected=snapshot,
    )
    if fill:
        return FillRestRequest(
            **common,
            pitch=61,
            tpc=21,
            duration={"numerator": 1, "denominator": 1 if full else 4},
        )
    return ReplaceNoteRequest(**common)


async def _baseline(bridge: MuseScoreBridge):
    info = await bridge.get_score()
    assert info.title == "Guarded note rest " + (
        "concert fixture" if _MODE == "concert" else "fixture"
    )
    before = await bridge.read_passage(1, 4)
    assert before.staves == [0, 1, 2] and len(before.measures) == 4
    for staff in range(3):
        events = [
            e
            for e in before.measures[0].events
            if e.staff == staff and e.voice == 0 and e.kind != "other"
        ]
        assert len(events) == 1 and events[0].kind == "rest"
        assert (
            events[0].actual_duration.numerator == events[0].actual_duration.denominator
        )
        second = [
            e
            for e in before.measures[1].events
            if e.staff == staff and e.voice == 0 and e.kind != "other"
        ]
        assert [e.kind for e in second] == ["rest", "chord", "chord"]
        assert [e.offset.numerator / e.offset.denominator for e in second] == [
            0,
            0.5,
            0.75,
        ]
    return before


async def _apply(bridge: MuseScoreBridge, request):
    if isinstance(request, FillRestRequest):
        return await bridge.fill_rest_with_note(request)
    return await bridge.replace_note_with_rest(request)


@pytest.mark.anyio()
@pytest.mark.parametrize(
    "measure,staff,voice,fill,full",
    [
        (1, 0, 0, True, False),
        (1, 1, 0, True, False),
        (1, 2, 0, True, False),
        (1, 0, 0, True, True),
        (2, 0, 0, True, False),
        (2, 0, 1, True, False),
        (2, 0, 0, False, False),
        (2, 1, 0, False, False),
        (2, 2, 0, False, False),
    ],
)
async def test_native_replacement_verifies_peers_spelling_and_single_undo(
    measure: int,
    staff: int,
    voice: int,
    fill: bool,
    full: bool,
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _baseline(bridge)
        cursor = await bridge.get_cursor_info()
        request = _request(before, measure, staff, voice, fill, full=full)
        if not _supported():
            with pytest.raises(BridgeError) as rejected:
                await _apply(bridge, request)
            assert rejected.value.details["code"] == "unsupported_native_api"
            assert rejected.value.details["mutation_status"] == "not_applied"
            assert await bridge.read_passage(1, 4) == before
            return
        result = await _apply(bridge, request)
        assert result.verified and result.mutation_status == "applied"
        assert result.selection_status == "not_preserved"
        actual = await bridge.read_passage(1, 4)
        assert result.before == before.measures[measure - 1].events
        assert result.after == actual.measures[measure - 1].events
        expected = before.model_copy(deep=True)
        expected.measures[measure - 1].events = result.after
        assert actual == expected
        assert [e for e in result.after if e.staff != staff or e.voice != voice] == [
            e for e in result.before if e.staff != staff or e.voice != voice
        ]
        target = next(
            e
            for e in result.after
            if e.staff == staff
            and e.voice == voice
            and e.offset.model_dump() == request.offset.model_dump()
            and e.kind != "other"
        )
        assert target.kind == ("chord" if fill else "rest")
        if fill:
            note = target.notes[0]
            assert (note.pitch, note.tpc, note.written_tpc) == (
                61,
                21,
                23 if staff == 1 else 21,
            )
            assert note.displayed_tpc == (
                note.tpc if _MODE == "concert" else note.written_tpc
            )
            assert target.duration == target.actual_duration
            assert target.duration.model_dump() == request.duration.model_dump()
            original = next(
                e
                for e in result.before
                if e.staff == staff
                and e.voice == voice
                and e.offset.model_dump() == request.offset.model_dump()
                and e.kind == "rest"
            )
            end = (
                Fraction(original.tick)
                + Fraction(
                    original.actual_duration.numerator,
                    original.actual_duration.denominator,
                )
                * 1920
            )
            covered = (
                target.tick
                + Fraction(request.duration.numerator, request.duration.denominator)
                * 1920
            )
            suffix = [
                e
                for e in result.after
                if e.staff == staff and e.voice == voice and covered <= e.tick < end
            ]
            for rest in suffix:
                assert rest.kind == "rest" and not rest.notes and rest.tick == covered
                covered += (
                    Fraction(
                        rest.actual_duration.numerator, rest.actual_duration.denominator
                    )
                    * 1920
                )
            assert covered == end
        else:
            original = next(
                e
                for e in result.before
                if e.staff == staff
                and e.voice == voice
                and e.offset.model_dump() == request.offset.model_dump()
                and e.kind == "chord"
            )
            assert (
                target.actual_duration == original.actual_duration and not target.notes
            )
        current_cursor = await bridge.get_cursor_info()
        assert current_cursor.model_dump(exclude={"element"}) == cursor.model_dump(
            exclude={"element"}
        )
        command = "fillRestWithNote" if fill else "replaceNoteWithRest"
        assert command in bridge.diagnostics().commands
        assert command not in bridge.diagnostics().sequence_commands
        # Exact current passage proves this own successful operation is latest.
        # No cleanup or retry undo follows an uncertain result/failed assertion.
        assert await bridge.read_passage(1, 4) == expected
        await bridge.undo()
        assert await bridge.read_passage(1, 4) == before
        assert await bridge.get_cursor_info() == cursor
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_unsafe_and_stale_requests_reject_without_changing_passage() -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _baseline(bridge)
        stale = _request(before, 2, 0, 0, True)
        stale_note = next(
            event for event in stale.expected if event.kind == "chord" and event.notes
        )
        stale_note.notes[0].written_tpc += 1
        too_long = _request(before, 2, 0, 0, True, full=True)
        attached = _request(before, 3, 0, 0, False)
        dotted = _request(before, 4, 0, 0, True)
        for request in [stale, too_long, attached, dotted]:
            with pytest.raises(BridgeError) as rejected:
                await _apply(bridge, request)
            assert rejected.value.details["mutation_status"] == "not_applied"
            assert rejected.value.details["code"] in {
                "unsupported_native_api",
                "stale_target",
                "unsupported_target",
            }
            assert await bridge.read_passage(1, 4) == before
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
@pytest.mark.parametrize("fill", [True, False])
async def test_native_post_rewrite_failure_restores_measure_and_preserves_prior_history(
    fill: bool,
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _baseline(bridge)
        if not _supported():
            with pytest.raises(BridgeError) as rejected:
                await _apply(bridge, _request(before, 2, 0, 0, fill))
            assert rejected.value.details["code"] == "unsupported_native_api"
            assert await bridge.read_passage(1, 4) == before
            return
        # Observable own sentinel in measure1, outside the measure2 target.
        await bridge.go_to_measure(1)
        await bridge.go_to_staff(0)
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 4)
        assert sentinel != before
        request = _request(sentinel, 2, 0, 0, fill)
        armed = await bridge.send_command(
            "_testArmRestFill" if fill else "_testArmNoteRest"
        )
        assert armed["result"]["armed"] is True
        with pytest.raises(BridgeError) as failed:
            await _apply(bridge, request)
        assert failed.value.details["mutation_status"] == "rolled_back"
        assert failed.value.details["before"] == failed.value.details["actual"]
        probe = (await bridge.send_command("_testReadNoteRestFault"))["result"]
        assert probe["armed"] is False and probe["kind"] == (
            "fill" if fill else "replace"
        )
        changed = next(
            e for e in probe["observed"] if e["tick"] == (1920 if fill else 2880)
        )
        assert changed["pitches"] == ([61] if fill else [])
        assert Fraction(
            changed["duration"]["numerator"], changed["duration"]["denominator"]
        ) == Fraction(1, 4)
        assert await bridge.read_passage(1, 4) == sentinel
        # Rollback, native changed-state evidence and exact latest sentinel
        # authorize one undo of our prior sentinel. Never retry/cleanup undo.
        await bridge.undo()
        assert await bridge.read_passage(1, 4) == before
    finally:
        await bridge.disconnect()
