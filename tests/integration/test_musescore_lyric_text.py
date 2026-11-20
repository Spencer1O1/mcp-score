"""Existing ordinary lyric edits in already-owned pitch and both display fixtures."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from mcp_score.bridge.chord_tone import ChordToneSnapshot
from mcp_score.bridge.lyric_text import LyricTextEdit, LyricTextRequest
from mcp_score.bridge.musescore import MuseScoreBridge, _snake_case_keys
from mcp_score.bridge.receipts import GuardedLyricTextMutation, ReceiptExecution

_MODE = os.environ.get("MCP_SCORE_LYRIC_FIXTURE")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _MODE not in {"pitch", "normal", "concert"},
        reason="requires owned lyric fixture phase",
    ),
]


def _supported() -> bool:
    return tuple(map(int, os.environ["MUSESCORE_VERSION"].split("."))) >= (4, 7)


def _measure() -> int:
    return 1 if _MODE == "pitch" else 3


def _target(passage, staff: int):
    return next(
        e
        for e in passage.measures[0].events
        if e.kind == "chord" and e.staff == staff and e.lyrics
    )


def _request(
    passage, staff: int, text: str = 'Sing & <b>"café"</b> \'🙂'
) -> LyricTextRequest:
    event = _target(passage, staff)
    return LyricTextRequest(
        measure=_measure(),
        staff=staff,
        voice=event.voice,
        offset=event.offset.model_dump(),
        lyric_index=0,
        plain_text=text,
        expected=ChordToneSnapshot.model_validate(event.model_dump()),
    )


def _ticket(bridge, request) -> ReceiptExecution:
    capability = bridge.diagnostics().receipts
    assert capability is not None and "setLyricText" in capability.supported_mutations
    return ReceiptExecution(
        operation_id=str(uuid4()),
        expected_plugin_session=capability.plugin_session,
        mutation=GuardedLyricTextMutation(kind="set_lyric_text", request=request),
    )


async def _read(bridge):
    info = await bridge.get_score()
    expected_title = {
        "pitch": "Guarded pitch fixture",
        "normal": "Guarded note rest fixture",
        "concert": "Guarded note rest concert fixture",
    }[_MODE]
    assert info.title == expected_title
    return await bridge.read_passage(_measure(), _measure())


@pytest.mark.anyio()
@pytest.mark.parametrize("staff", [0] if _MODE == "pitch" else [0, 1, 2])
async def test_native_text_escaping_peers_noop_stale_receipt_and_single_undo(
    staff: int,
) -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _read(bridge)
        event = _target(before, staff)
        cursor = await bridge.get_cursor_info()
        ticket = _ticket(bridge, _request(before, staff))
        receipt = await bridge.execute_mutation_once(ticket)
        assert receipt.status == "completed" and receipt.original_reply is not None
        if not _supported():
            assert receipt.original_reply["code"] == "unsupported_native_api"
            assert receipt.original_reply["mutation_status"] == "not_applied"
            assert await _read(bridge) == before
            return
        assert "result" in receipt.original_reply
        result = LyricTextEdit.model_validate(
            _snake_case_keys(receipt.original_reply["result"])
        )
        assert result.verified and result.mutation_status == "applied"
        assert result.before == event and result.after.notes == event.notes
        assert result.after.duration == event.duration
        assert result.after.articulations == event.articulations
        if _MODE == "pitch":
            assert len(event.notes) == 2 and event.articulations
        xml = "Sing &amp; &lt;b&gt;&quot;café&quot;&lt;/b&gt; '🙂"
        assert result.selected_after.xml_text == xml
        assert result.selected_after.plain_text == ticket.mutation.request.plain_text
        expected = before.model_copy(deep=True)
        target = _target(expected, staff)
        target.lyrics[0].text = xml
        assert result.after == target
        assert await _read(bridge) == expected
        duplicate = await bridge.execute_mutation_once(ticket)
        assert duplicate.cached and duplicate.original_reply == receipt.original_reply
        # A true no-op and a stale complete chord must create no history entry.
        selection_before_noop = await bridge.read_current_selection()
        noop = await bridge.execute_mutation_once(
            _ticket(bridge, _request(expected, staff))
        )
        assert noop.original_reply["result"]["mutationStatus"] == "unchanged"
        assert await bridge.read_current_selection() == selection_before_noop
        stale = await bridge.execute_mutation_once(
            _ticket(bridge, _request(before, staff, "stale"))
        )
        assert stale.original_reply["code"] == "stale_target"
        assert stale.original_reply["mutation_status"] == "not_applied"
        now = await bridge.get_cursor_info()
        assert now.model_dump(exclude={"element"}) == cursor.model_dump(
            exclude={"element"}
        )
        assert await _read(bridge) == expected
        await bridge.undo()  # no retry/cleanup after this unreceipted attempt
        assert await _read(bridge) == before
        historical = await bridge.execute_mutation_once(ticket)
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert await _read(bridge) == before
        assert "setLyricText" not in bridge.diagnostics().sequence_commands
    finally:
        await bridge.disconnect()


@pytest.mark.anyio()
async def test_native_post_text_fault_restores_original_and_prior_history() -> None:
    bridge = MuseScoreBridge()
    try:
        before = await _read(bridge)
        if not _supported():
            receipt = await bridge.execute_mutation_once(
                _ticket(bridge, _request(before, 0))
            )
            assert receipt.original_reply["code"] == "unsupported_native_api"
            assert await _read(bridge) == before
            return
        # The sentinel is an observable own edit in the same read range.
        await bridge.go_to_measure(_measure())
        await bridge.go_to_staff(0)
        await bridge.set_tempo(97)
        sentinel = await _read(bridge)
        assert sentinel != before
        ticket = _ticket(bridge, _request(sentinel, 0))
        assert (await bridge.send_command("_testArmLyricText"))["result"][
            "armed"
        ] is True
        receipt = await bridge.execute_mutation_once(ticket)
        assert receipt.status == "completed" and receipt.original_reply is not None
        failure = receipt.original_reply
        assert (
            failure["mutation_status"] == "rolled_back"
            and failure["rollback_verified"] is True
        )
        assert failure["before"] == failure["actual"]
        probe = (await bridge.send_command("_testReadLyricTextFault"))["result"]
        assert probe["armed"] is False
        assert probe["observed"]["plainText"] == ticket.mutation.request.plain_text
        assert (
            probe["observed"]["xmlText"]
            == "Sing &amp; &lt;b&gt;&quot;café&quot;&lt;/b&gt; '🙂"
        )
        assert await _read(bridge) == sentinel
        historical = await bridge.execute_mutation_once(ticket)
        assert historical.cached and historical.original_reply == receipt.original_reply
        assert (await bridge.send_command("_testReadLyricTextFault"))["result"] == probe
        assert await _read(bridge) == sentinel
        await bridge.undo()  # known latest own sentinel; never cleanup/retry undo
        assert await _read(bridge) == before
    finally:
        await bridge.disconnect()
