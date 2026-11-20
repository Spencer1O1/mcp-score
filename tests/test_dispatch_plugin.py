"""Exercise the QML dispatcher's actual source without starting MuseScore."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.dispatch_cases import INVALID_ENVELOPES

_QML = (
    Path(__file__).resolve().parent.parent
    / "src/mcp_score/musescore/plugin/mcp-score-bridge.qml"
)
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node is needed for plugin JS tests"
)


def _dispatch(message: str) -> Any:
    source = _QML.read_text()
    start = source.index("    function handleMessage(message) {")
    end = source.index("\n    // ── WebSocket server", start)
    script = (
        "const root={}; const logPrefix='test'; const console={log(){}};"
        "const calls=[];"
        "const commands={ping(plugin,params){calls.push({command:'ping',params});"
        "return {result:'pong'};},inspect(plugin,params){"
        "calls.push({command:'inspect',params});"
        "return {result:{samePlugin:plugin===root,params}};}};"
        + source[start:end]
        + "const reply=handleMessage("
        + json.dumps(message)
        + "); const callsBeforeFollowup=calls.length;"
        + 'const followup=handleMessage(\'{"command":"ping"}\');'
        + "process.stdout.write(JSON.stringify({reply,callsBeforeFollowup,"
        + "followup,calls}));"
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("message", "error"), INVALID_ENVELOPES)
def test_malformed_envelope_returns_error_without_dispatch_and_allows_followup(
    message: str, error: str
) -> None:
    observed = _dispatch(message)
    assert error in observed["reply"]["error"]
    assert "result" not in observed["reply"]
    assert observed["callsBeforeFollowup"] == 0
    assert observed["followup"] == {"result": "pong"}
    assert observed["calls"] == [{"command": "ping", "params": {}}]


@pytest.mark.parametrize(
    "message",
    [
        '{"command":"inspect"}',
        '{"command":"inspect","params":{}}',
        '{"command":"inspect","params":{"nested":[1,null,false],"text":"ok"}}',
    ],
)
def test_valid_envelope_dispatches_with_original_params_and_plugin(
    message: str,
) -> None:
    observed = _dispatch(message)
    expected_params = json.loads(message).get("params", {})
    assert observed["reply"] == {
        "result": {"samePlugin": True, "params": expected_params}
    }
    assert observed["callsBeforeFollowup"] == 1
    assert observed["calls"][0] == {"command": "inspect", "params": expected_params}
    assert observed["followup"] == {"result": "pong"}
