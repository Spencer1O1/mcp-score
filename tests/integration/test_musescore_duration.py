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
        assert await bridge.get_cursor_info() == cursor
    finally:
        if applied:
            await bridge.undo()
        restored = await bridge.read_passage(1, 1)
        await bridge.disconnect()
    assert restored == before


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
