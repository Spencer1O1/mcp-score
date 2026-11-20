"""Native duration rewriting in the disposable guarded-duration fixture.

Older application versions actively prove rejection; their jobs do not skip.
"""

from __future__ import annotations

import os
from fractions import Fraction

import pytest

from mcp_score.bridge import BridgeError
from mcp_score.bridge.duration_edit import ExpectedDurationEvent
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import Duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_GUARDED_DURATION_FIXTURE") != "1",
        reason="requires disposable guarded-duration.musicxml fixture",
    ),
]


@pytest.mark.anyio()
@pytest.mark.parametrize(("staff", "voice"), [(0, 0), (0, 1), (4, 0), (5, 0)])
async def test_native_shorten_rest_fill_peers_transposition_and_single_undo(
    staff: int,
    voice: int,
) -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        before = await bridge.read_passage(1, 1)
        cursor = await bridge.get_cursor_info()
        events = before.measures[0].events
        snapshot = [
            ExpectedDurationEvent.model_validate(event.model_dump())
            for event in events
            if event.staff == staff and event.voice == voice
        ]
        event = next(event for event in snapshot if event.kind == "chord")
        assert event.duration is not None
        old = Fraction(event.duration.numerator, event.duration.denominator)
        desired = old / 2
        duration = Duration(
            numerator=desired.numerator, denominator=desired.denominator
        )
        # CI sets the installed application release, independently of plugin version.
        # The handler itself checks the actual native application version/metadata.
        release = tuple(
            int(piece) for piece in os.environ["MUSESCORE_VERSION"].split(".")
        )
        if release < (4, 7):
            with pytest.raises(BridgeError) as rejected:
                await bridge.set_event_duration(
                    1, staff, voice, event.offset, duration, snapshot
                )
            assert rejected.value.details["code"] == "unsupported_native_api"
            assert rejected.value.details["mutation_status"] == "not_applied"
        else:
            result = await bridge.set_event_duration(
                1, staff, voice, event.offset, duration, snapshot
            )
            applied = result.mutation_status == "applied"
            actual = await bridge.read_passage(1, 1)
            assert result.verified and applied
            assert result.selection_status == "not_preserved"
            assert result.before == events and result.after == actual.measures[0].events
            target = next(
                e
                for e in result.after
                if e.staff == staff
                and e.voice == voice
                and e.kind == "chord"
                and e.tick == event.tick
            )
            assert [note.model_dump() for note in target.notes] == [
                note.model_dump() for note in event.notes
            ]
            assert target.duration == duration and target.actual_duration == duration
            assert any(
                e.kind == "rest"
                and e.staff == staff
                and e.voice == voice
                and e.tick == event.tick + desired * 1920
                for e in result.after
            )
            assert [
                e for e in result.after if e.staff != staff or e.voice != voice
            ] == [e for e in events if e.staff != staff or e.voice != voice]
            untouched = [
                original
                for original in events
                if not (
                    original.kind == "chord"
                    and original.staff == staff
                    and original.voice == voice
                    and original.tick == event.tick
                )
            ]
            assert all(original in result.after for original in untouched)
            fresh = [
                ExpectedDurationEvent.model_validate(e.model_dump())
                for e in result.after
                if e.staff == staff and e.voice == voice
            ]
            noop = await bridge.set_event_duration(
                1, staff, voice, event.offset, duration, fresh
            )
            assert noop.mutation_status == "unchanged"
            assert noop.selection_status == "unchanged"
            # The single undo below must undo the edit, with no no-op in between.
            assert "setEventDuration" in bridge.diagnostics().commands
            assert "setEventDuration" not in bridge.diagnostics().sequence_commands
        current_cursor = await bridge.get_cursor_info()
        # CursorInfo includes live contents under the cursor. Editing that very
        # chord must change element.duration while every position field stays put.
        assert current_cursor.model_dump(exclude={"element"}) == cursor.model_dump(
            exclude={"element"}
        )
        if applied and (staff, voice, event.tick) == (
            cursor.staff,
            cursor.voice,
            cursor.tick,
        ):
            assert current_cursor.element is not None
            assert current_cursor.element.duration == duration
        else:
            assert current_cursor.element == cursor.element
    finally:
        if applied:
            await bridge.undo()
        restored = await bridge.read_passage(1, 1)
        restored_cursor = await bridge.get_cursor_info()
        await bridge.disconnect()
    assert restored == before
    assert restored_cursor == cursor


@pytest.mark.anyio()
async def test_target_attachments_reject_and_stale_neighbor_cannot_be_overwritten() -> (
    None
):
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(1, 1, 0, 0)
        snapshot = [
            ExpectedDurationEvent.model_validate(event.model_dump())
            for event in before.measures[0].events
        ]
        chord = next(e for e in snapshot if e.kind == "chord" and e.tick == 480)
        with pytest.raises(BridgeError) as rejected:
            await bridge.set_event_duration(
                1, 0, 0, chord.offset, Duration(numerator=1, denominator=8), snapshot
            )
        assert rejected.value.details["mutation_status"] == "not_applied"
        assert rejected.value.details["code"] in {
            "unsupported_target",
            "unsupported_native_api",
        }
        next(e for e in reversed(snapshot) if e.kind == "chord").notes[
            0
        ].written_tpc += 1
        target = next(e for e in snapshot if e.kind == "chord")
        with pytest.raises(BridgeError) as stale:
            await bridge.set_event_duration(
                1, 0, 0, target.offset, Duration(numerator=1, denominator=8), snapshot
            )
        assert stale.value.details["code"] in {"stale_target", "unsupported_native_api"}
        assert await bridge.read_passage(1, 1, 0, 0) == before
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_live_intersecting_slur_rejects_without_mutation() -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(3, 3, 4, 0)
        snapshot = [
            ExpectedDurationEvent.model_validate(event.model_dump())
            for event in before.measures[0].events
        ]
        event = next(e for e in snapshot if e.kind == "chord")
        with pytest.raises(BridgeError) as rejected:
            await bridge.set_event_duration(
                3, 4, 0, event.offset, Duration(numerator=1, denominator=4), snapshot
            )
        assert rejected.value.details["code"] in {
            "unsupported_spanner",
            "unsupported_target",
            "unsupported_native_api",
        }
        assert rejected.value.details["mutation_status"] == "not_applied"
        assert await bridge.read_passage(3, 3, 4, 0) == before
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
@pytest.mark.parametrize(("staff", "voice"), [(0, 0), (0, 1), (4, 0), (5, 0)])
async def test_native_extension_consumes_only_complete_rests_then_single_undo(
    staff: int, voice: int
) -> None:
    bridge = MuseScoreBridge()
    applied = False
    try:
        before = await bridge.read_passage(4, 4)
        cursor_before = await bridge.get_cursor_info()
        events = before.measures[0].events
        snapshot = [
            ExpectedDurationEvent.model_validate(event.model_dump())
            for event in events
            if event.staff == staff and event.voice == voice
        ]
        target = next(event for event in snapshot if event.kind == "chord")
        desired = Duration(numerator=1, denominator=1 if voice == 1 else 2)
        assert target.actual_duration is not None
        onset = target.tick
        old_end = (
            onset
            + Fraction(
                target.actual_duration.numerator, target.actual_duration.denominator
            )
            * 1920
        )
        new_end = onset + Fraction(desired.numerator, desired.denominator) * 1920
        consumed = [event for event in snapshot if old_end <= event.tick < new_end]
        assert consumed and all(event.kind == "rest" for event in consumed)
        if voice == 1:
            assert len(consumed) == 2
        release = tuple(map(int, os.environ["MUSESCORE_VERSION"].split(".")))
        if release < (4, 7):
            with pytest.raises(BridgeError) as rejected:
                await bridge.set_event_duration(
                    4,
                    staff,
                    voice,
                    target.offset,
                    desired,
                    snapshot,
                    "extend_into_rests",
                )
            assert rejected.value.details["code"] == "unsupported_native_api"
            assert rejected.value.details["mutation_status"] == "not_applied"
        else:
            result = await bridge.set_event_duration(
                4, staff, voice, target.offset, desired, snapshot, "extend_into_rests"
            )
            applied = result.mutation_status == "applied"
            assert applied and result.verified
            actual = await bridge.read_passage(4, 4)
            assert result.before == events and result.after == actual.measures[0].events
            changed = next(
                event
                for event in result.after
                if event.staff == staff
                and event.voice == voice
                and event.tick == onset
                and event.kind == "chord"
            )
            assert changed.duration == changed.actual_duration == desired
            assert changed.notes == target.notes
            assert not [
                event
                for event in result.after
                if event.staff == staff
                and event.voice == voice
                and old_end <= event.tick < new_end
            ]
            preserved = [
                event
                for event in events
                if not (
                    event.staff == staff
                    and event.voice == voice
                    and onset <= event.tick < new_end
                    and event.kind != "other"
                )
            ]
            assert all(event in result.after for event in preserved)
            fresh = [
                ExpectedDurationEvent.model_validate(event.model_dump())
                for event in result.after
                if event.staff == staff and event.voice == voice
            ]
            noop = await bridge.set_event_duration(
                4, staff, voice, target.offset, desired, fresh, "extend_into_rests"
            )
            assert noop.mutation_status == "unchanged"
            assert noop.selection_status == "unchanged"
            assert "extendEventDuration" in bridge.diagnostics().commands
            assert "extendEventDuration" not in bridge.diagnostics().sequence_commands
        cursor_after = await bridge.get_cursor_info()
        assert cursor_after == cursor_before
    finally:
        if applied:
            await bridge.undo()
        restored = await bridge.read_passage(4, 4)
        await bridge.disconnect()
    assert restored == before


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("measure", "staff", "voice", "duration", "reason"),
    [
        (5, 0, 0, Duration(numerator=1, denominator=2), "partial rest"),
        (5, 0, 1, Duration(numerator=1, denominator=2), "following note"),
        (5, 2, 0, Duration(numerator=1, denominator=2), "fermata rest"),
        (5, 3, 0, Duration(numerator=1, denominator=2), "hidden rest"),
        (6, 0, 0, Duration(numerator=1, denominator=2), "bar crossing"),
        (7, 0, 0, Duration(numerator=1, denominator=1), "tuplet rests"),
    ],
)
async def test_native_unsafe_extension_refuses_without_partial_mutation(
    measure: int, staff: int, voice: int, duration: Duration, reason: str
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(measure, measure)
        events = before.measures[0].events
        snapshot = [
            ExpectedDurationEvent.model_validate(event.model_dump())
            for event in events
            if event.staff == staff and event.voice == voice
        ]
        target = next(event for event in snapshot if event.kind == "chord")
        assert target.actual_duration is not None
        old_end = (
            target.tick
            + Fraction(
                target.actual_duration.numerator, target.actual_duration.denominator
            )
            * 1920
        )
        new_end = (
            target.tick + Fraction(duration.numerator, duration.denominator) * 1920
        )
        if reason == "partial rest":
            # Prove that import retained this notation, rather than silently
            # decomposing it into an already-supported exact boundary.
            assert any(
                event.kind == "rest"
                and event.tick == old_end
                and event.actual_duration is not None
                and event.tick
                + Fraction(
                    event.actual_duration.numerator, event.actual_duration.denominator
                )
                * 1920
                > new_end
                for event in snapshot
            )
        if reason == "following note":
            assert any(
                event.kind == "chord" and old_end <= event.tick < new_end
                for event in snapshot
            )
        if reason == "tuplet rests":
            assert any(
                event.kind == "rest" and event.duration != event.actual_duration
                for event in snapshot
            )
        with pytest.raises(BridgeError) as rejected:
            await bridge.set_event_duration(
                measure,
                staff,
                voice,
                target.offset,
                duration,
                snapshot,
                "extend_into_rests",
            )
        assert rejected.value.details["mutation_status"] == "not_applied", reason
        assert rejected.value.details["code"] in {
            "unsupported_native_api",
            "unsupported_rest_coverage",
            "unsupported_target",
        }, reason
        assert await bridge.read_passage(measure, measure) == before, reason
    finally:
        await bridge.disconnect()
