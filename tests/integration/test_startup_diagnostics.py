"""Read-only owned-window proof in the already-running disposable macOS fixture."""

from __future__ import annotations

import os

import pytest

from mcp_score.bridge.musescore import MuseScoreBridge
from tests.test_musescore_harness import harness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MCP_SCORE_STARTUP_DIAGNOSTICS_FIXTURE") != "1",
        reason="requires the existing verified hosted macOS fixture startup",
    ),
]


@pytest.mark.anyio()
async def test_owned_startup_probe_preserves_score_selection_cursor_and_own_undo() -> (
    None
):
    settings = harness.Settings.from_environment()
    harness.require_macos_runner(settings)
    bridge = MuseScoreBridge()
    try:
        before = await bridge.read_passage(1, 1)
        await bridge.set_tempo(97)
        sentinel = await bridge.read_passage(1, 1)
        assert sentinel != before
        selection = await bridge.read_current_selection()
        cursor = await bridge.get_cursor_info()
        ownership = settings.layout.state_file.read_bytes()
        report = harness.capture_startup_diagnostics(settings)
        assert report["phase"] == "ready" and report["elapsed_seconds"] >= 0
        assert not report["fixture"].startswith("/")
        assert report["attempts"][-1]["state"] == "running"
        processes = report["probe"]["processes"]
        owned = [item for item in processes if item["ownership"] == "verified"]
        assert any(
            item["executable"] == str(settings.executable.resolve()) for item in owned
        )
        ui = report["probe"]["ui"]
        # Permission is optional; no grant/reset/prompt is attempted by the helper.
        # Unexpected errors, malformed output or timeouts cannot pass as success.
        assert ui["status"] in {"captured", "permission_denied"}, ui
        if ui["status"] == "captured":
            assert ui["windows"]
            for window in ui["windows"]:
                assert any(item["pid"] == window["pid"] for item in owned)
                assert isinstance(window["role"], str)
                for sheet in window.get("sheets", []):
                    assert sheet["pid"] == window["pid"]
        else:
            assert ui["windows"] == []
        assert settings.layout.state_file.read_bytes() == ownership
        assert await bridge.read_passage(1, 1) == sentinel
        assert await bridge.read_current_selection() == selection
        assert await bridge.get_cursor_info() == cursor
        assert not any(
            "StartupDiagnostic" in command for command in bridge.diagnostics().commands
        )
        # The confirmed own sentinel is still current. One attempt only; failure
        # or unknown outcomes never authorize a cleanup/retry undo.
        await bridge.undo()
        assert await bridge.read_passage(1, 1) == before
    finally:
        await bridge.disconnect()
