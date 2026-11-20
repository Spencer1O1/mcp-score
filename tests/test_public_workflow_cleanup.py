"""Run the actual acceptance control flow with uncertain/delayed fake SDK undo.

This offline graph never opens stdio, sockets or a notation application. Fixture
validation is covered by native acceptance; this isolates its failure cleanup.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from mcp_score.bridge.musescore import _camel_case_keys
from mcp_score.bridge.passage import Passage, PassageEvent, PassageMeasure, PassageNote
from mcp_score.bridge.receipts import MutationReceipt
from mcp_score.bridge.results import Duration, SelectionProperties
from tests.integration import test_public_mcp_workflow as workflow


class _DelayedUndoProbe:
    def __init__(self, outcome: str, phase: str) -> None:
        quarter = Duration(numerator=1, denominator=4)
        self.event = PassageEvent(
            kind="chord",
            type=93,
            staff=0,
            voice=0,
            tick=960,
            offset=Duration(numerator=1, denominator=2),
            duration=quarter,
            actual_duration=quarter,
            lyrics=[],
            articulations=[],
            notes=[
                PassageNote(
                    pitch=67, tpc=15, written_tpc=15, displayed_tpc=15, name="G4"
                )
            ],
        )
        self.baseline = Passage(
            start_measure=1,
            end_measure=2,
            staves=[0],
            voices=[0],
            range_complete=True,
            unsupported=[],
            measures=[
                PassageMeasure(
                    measure=1,
                    tick=0,
                    actual_duration=Duration(numerator=1, denominator=1),
                    time_signature=None,
                    events=[self.event],
                )
            ],
        )
        self.changed = self.baseline.model_copy(deep=True)
        self.changed.measures[0].events[0].notes[0] = self.event.notes[0].model_copy(
            update={
                "pitch": 69,
                "tpc": 17,
                "written_tpc": 17,
                "displayed_tpc": 17,
                "name": "A4",
            }
        )
        self.original = _camel_case_keys(
            {
                "result": {
                    "measure": 1,
                    "note_index_before": 0,
                    "note_index_after": 0,
                    "before": self.event.model_dump(),
                    "after": self.changed.measures[0].events[0].model_dump(),
                    "verified": True,
                    "mutation_status": "applied",
                    "snapshot_scope": "target_chord",
                    "limitations": [],
                }
            }
        )
        self.outcome = outcome
        self.phase = phase
        self.operation_id: str | None = None
        self.history = ["prior unrelated edit"]
        self.undo_calls = 0
        self.disconnects = 0
        self.delayed_undo = False

    def receipt(self, arguments: dict[str, Any], *, cached: bool) -> MutationReceipt:
        stale = arguments["operation_id"] != self.operation_id
        return MutationReceipt(
            plugin_session="fake-plugin",
            operation_id=arguments["operation_id"],
            status="completed",
            command="setNotePitch",
            cached=cached,
            historical=True,
            reason=None,
            original_reply={"code": "stale_target", "mutation_status": "not_applied"}
            if stale
            else self.original,
        )

    async def tool_boundary(
        self,
        session: object,
        model: object,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        if name == "connect_to_musescore":
            return SimpleNamespace(
                bridge=SimpleNamespace(
                    receipts=SimpleNamespace(
                        supported_mutations=["setNotePitch"],
                        plugin_session="fake-plugin",
                    )
                )
            )
        if name == "get_live_score_info":
            return None
        if name == "get_event_at_position":
            return SimpleNamespace(note=self.event.notes[0], event=self.event)
        if name == "get_selection_properties":
            return SelectionProperties()
        assert arguments is not None
        if name == "execute_mutation_once":
            if self.operation_id is None:
                self.operation_id = arguments["operation_id"]
                self.history.append("own pitch edit")
                if self.phase == "cleanup":
                    # Native apply succeeded, but SDK parsing hid that reply.
                    raise ValueError("SDK schema parsing failed after apply")
                return self.receipt(arguments, cached=False)
            return self.receipt(arguments, cached=True)
        if name == "read_mutation_receipt":
            return self.receipt(arguments, cached=True)
        raise AssertionError(name)

    async def read(self, session: object) -> Passage:
        return self.changed if "own pitch edit" in self.history else self.baseline

    async def initialize(self) -> None:
        pass

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name, output_schema={"type": "object"})
                for name in workflow._REQUIRED_TOOLS
            ]
        )

    async def call_tool(
        self, name: str, arguments: object = None, **kwargs: Any
    ) -> SimpleNamespace:
        if name == "undo_last_action":
            self.undo_calls += 1
            if self.undo_calls == 1:
                # The first undo remains queued while error cleanup runs. A
                # second undo could pop our edit now and an unrelated edit later.
                self.delayed_undo = True
                if self.outcome == "timeout":
                    raise TimeoutError("undo reply unavailable")
                if self.outcome == "cancelled":
                    raise asyncio.CancelledError("undo cancelled after send")
                return SimpleNamespace(is_error=True, content="outcome_unknown")
            self.history.pop()
        elif name == "disconnect_from_musescore":
            self.disconnects += 1
        else:
            raise AssertionError(name)
        return SimpleNamespace(is_error=False, content=[])

    def complete_delayed_undo(self) -> None:
        assert self.delayed_undo
        self.history.pop()
        self.delayed_undo = False


@pytest.mark.anyio()
@pytest.mark.parametrize("outcome", ["error", "timeout", "cancelled"])
@pytest.mark.parametrize("phase", ["normal", "cleanup"])
async def test_uncertain_undo_is_sent_once_even_if_historical_pitch_is_applied(
    outcome: str,
    phase: str,
) -> None:
    probe = _DelayedUndoProbe(outcome, phase)
    error = {
        "error": AssertionError,
        "timeout": TimeoutError,
        "cancelled": asyncio.CancelledError,
    }[outcome]
    with (
        patch.object(workflow, "_call", probe.tool_boundary),
        patch.object(workflow, "_read_preserving_state", probe.read),
        patch.object(workflow, "_assert_fixture"),
        pytest.raises(error),
    ):
        await workflow._scenario(probe)  # type: ignore[arg-type]

    assert probe.undo_calls == 1
    assert probe.disconnects == 1
    assert probe.history == ["prior unrelated edit", "own pitch edit"]
    probe.complete_delayed_undo()
    assert probe.history == ["prior unrelated edit"]
