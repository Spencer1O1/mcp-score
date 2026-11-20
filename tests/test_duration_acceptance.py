"""Exercise the native acceptance coroutine with real result/precondition models."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mcp_score.bridge.duration_edit import DurationEdit
from mcp_score.bridge.musescore import _snake_case_keys
from tests.integration import test_musescore_duration as native_tests
from tests.test_duration_extension_plugin import _extension


@pytest.mark.anyio()
@pytest.mark.parametrize(
    ("changed_field", "undo_fails"),
    [
        (field, False)
        for field in [
            None,
            "pitch",
            "tpc",
            "written_tpc",
            "displayed_tpc",
            "name",
            "order",
        ]
    ]
    + [(None, True)],
)
async def test_extension_acceptance_compares_note_values_then_noop_and_single_undo(
    monkeypatch: pytest.MonkeyPatch, changed_field: str | None, undo_fails: bool
) -> None:
    observed = _extension(
        "chord.notes.push({...note,pitch:64,tpc1:18,tpc2:20,tpc:20});",
        refresh=True,
    )
    result = DurationEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    target = next(event for event in result.after if event.kind == "chord")
    if changed_field == "order":
        target.notes.reverse()
    elif changed_field is not None:
        value = getattr(target.notes[0], changed_field)
        setattr(
            target.notes[0],
            changed_field,
            "wrong spelling" if changed_field == "name" else value + 1,
        )
    before = SimpleNamespace(measures=[SimpleNamespace(events=result.before)])
    after = SimpleNamespace(measures=[SimpleNamespace(events=result.after)])
    noop = result.model_copy(
        update={"mutation_status": "unchanged", "selection_status": "unchanged"}
    )
    bridge = SimpleNamespace(
        read_passage=AsyncMock(side_effect=[before, after, before]),
        get_cursor_info=AsyncMock(return_value="same cursor"),
        set_event_duration=AsyncMock(side_effect=[result, noop]),
        diagnostics=lambda: SimpleNamespace(
            commands=["extendEventDuration"], sequence_commands=[]
        ),
        undo=AsyncMock(
            side_effect=RuntimeError("unknown undo outcome") if undo_fails else None
        ),
        disconnect=AsyncMock(),
    )
    monkeypatch.setenv("MUSESCORE_VERSION", "4.7.5")
    acceptance = (
        native_tests.test_native_extension_consumes_only_complete_rests_then_single_undo
    )
    with patch.object(native_tests, "MuseScoreBridge", return_value=bridge):
        if undo_fails:
            with pytest.raises(RuntimeError, match="unknown undo outcome"):
                await acceptance(0, 0)
        elif changed_field is None:
            await acceptance(0, 0)
        else:
            with pytest.raises(AssertionError):
                await acceptance(0, 0)
    assert bridge.set_event_duration.await_count == (2 if changed_field is None else 1)
    bridge.undo.assert_awaited_once()
    bridge.disconnect.assert_awaited_once()
