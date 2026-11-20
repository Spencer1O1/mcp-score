"""The actual context acceptance coroutine must not undo a no-op setup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.integration import test_musescore_context as context_tests


@pytest.mark.anyio()
async def test_noop_transpose_does_not_authorize_cleanup_undo() -> None:
    baseline = object()
    bridge = SimpleNamespace(
        read_passage=AsyncMock(return_value=baseline),
        select_range=AsyncMock(),
        transpose=AsyncMock(),
        undo=AsyncMock(),
        disconnect=AsyncMock(),
    )
    acceptance = (
        context_tests.test_context_reads_preserve_selection_cursor_contents_and_undo
    )
    with (
        patch.object(context_tests, "MuseScoreBridge", return_value=bridge),
        pytest.raises(AssertionError),
    ):
        await acceptance()
    assert bridge.transpose.await_count == 1
    assert bridge.read_passage.await_count == 2
    bridge.undo.assert_not_awaited()
    bridge.disconnect.assert_awaited_once()
