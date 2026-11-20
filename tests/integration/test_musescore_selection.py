"""Actual native selection reads; writes only set up the disposable CI fixture."""

from __future__ import annotations

import os
from typing import Any

import pytest

from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import Duration

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_SELECTION_FIXTURE") != "1",
        reason="requires the isolated passage fixture with its fixed selection driver",
    ),
]


async def _setup(bridge: MuseScoreBridge, mode: str, **params: Any) -> dict[str, Any]:
    reply = await bridge.send_command("_testSelectionSetup", {"mode": mode, **params})
    assert "error" not in reply, reply
    return reply["result"]


@pytest.mark.anyio()
async def test_native_empty_selection_is_distinct_from_bridge_cursor() -> None:
    bridge = MuseScoreBridge()
    try:
        await bridge.go_to_staff(3)
        before = await _setup(bridge, "clear")
        selected = await bridge.read_current_selection()
        after = await _setup(bridge, "observe")
        assert "_testSelectionSetup" in bridge.diagnostics().commands
    finally:
        await bridge.disconnect()
    assert (
        selected.kind == "none" and selected.elements == [] and selected.range is None
    )
    assert before == after
    assert after["cursor"] == [1, 3, 0]
    assert after["dirtyStateAvailable"] is False and after["dirty"] is None


@pytest.mark.anyio()
async def test_native_single_tone_and_disjoint_notes_preserve_selection() -> None:
    bridge = MuseScoreBridge()
    try:
        tone = {"measure": 1, "track": 1, "tick": 480, "noteIndex": 1}
        before = await _setup(bridge, "notes", notes=[tone])
        selected = await bridge.read_current_selection()
        after = await _setup(bridge, "observe")
        disjoint_before = await _setup(
            bridge,
            "notes",
            notes=[
                {"measure": 1, "track": 0, "tick": 0, "noteIndex": 0},
                tone,
            ],
        )
        disjoint = await bridge.read_current_selection()
        disjoint_after = await _setup(bridge, "observe")
    finally:
        await bridge.disconnect()
    assert selected.kind == "list" and len(selected.elements) == 1
    item = selected.elements[0]
    assert item.kind == "note" and item.note_index == 1 and item.measure == 1
    assert item.note is not None and item.note.name == "E4"
    assert item.event_context is not None
    assert [note.name for note in item.event_context.notes] == ["C4", "E4"]
    assert item.event_context.staff == 0 and item.event_context.voice == 1
    assert item.event_context.offset == Duration(numerator=1, denominator=4)
    assert item.event_context.lyrics is not None
    assert item.event_context.lyrics[0].text == "low"
    assert before == after
    assert disjoint.kind == "list" and len(disjoint.elements) == 2
    assert [item.note.name for item in disjoint.elements if item.note] == ["B4", "E4"]
    assert disjoint_before == disjoint_after


@pytest.mark.anyio()
async def test_native_range_bounds_contents_and_attachment_metadata_preserved() -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _setup(
            bridge,
            "range",
            startTick=0,
            endTick=1920,
            startStaff=0,
            endStaffExclusive=2,
        )
        selected = await bridge.read_current_selection()
        after = await _setup(bridge, "observe")
        until_end_before = await _setup(
            bridge,
            "range",
            startTick=1920,
            endTick=10000,
            startStaff=0,
            endStaffExclusive=4,
        )
        until_end = await bridge.read_current_selection()
        until_end_after = await _setup(bridge, "observe")
    finally:
        await bridge.disconnect()
    assert selected.kind == "range" and selected.range is not None
    assert selected.range.start_tick == 0 and selected.range.end_tick == 1920
    assert selected.range.start_staff == 0 and selected.range.end_staff_exclusive == 2
    assert len(selected.elements) == len(before["elements"])
    melody = [
        item
        for item in selected.elements
        if item.kind == "note"
        and item.event_context is not None
        and item.event_context.staff == 0
        and item.event_context.voice == 0
    ]
    assert [item.note.name for item in melody if item.note] == ["B4", "A4", "G4", "F4"]
    assert melody[0].event_context is not None
    assert melody[0].event_context.lyrics is not None
    assert melody[0].event_context.lyrics[0].text == "Hel"
    version = os.environ.get("MUSESCORE_VERSION")
    if version and tuple(map(int, version.split(".")[:2])) >= (4, 6):
        assert melody[0].event_context.articulations
    assert selected.filter_state == "not_exposed"
    assert before == after and until_end_before == until_end_after
    assert until_end.range is not None and until_end.range.end_tick is None
    assert any(item.measure == 2 for item in until_end.elements)


@pytest.mark.anyio()
async def test_native_selection_read_does_not_add_an_undo_step() -> None:
    bridge = MuseScoreBridge()
    edited = False
    try:
        baseline = await bridge.read_passage(1, 1)
        await _setup(
            bridge,
            "range",
            startTick=0,
            endTick=1920,
            startStaff=0,
            endStaffExclusive=1,
        )
        await bridge.transpose(1)
        edited = True
        before = await _setup(bridge, "observe")
        selected = await bridge.read_current_selection()
        after = await _setup(bridge, "observe")
        changed = await bridge.read_passage(1, 1)
        await bridge.undo()
        edited = False
        restored = await bridge.read_passage(1, 1)
    finally:
        if edited:
            await bridge.undo()
        await bridge.disconnect()
    assert selected.elements and before == after
    assert changed != baseline and restored == baseline
