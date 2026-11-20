"""The disposable driver resolves chord onsets across native segment types."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_NODE = shutil.which("node")
_DRIVER = Path(__file__).parent / "integration/plugin/selection-fixture.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for QML JS tests")
_GRAPH = r"""
const selected=[];
const melody={type:93,notes:[{id:'B4',type:21,track:0,pitch:71}]};
const otherVoice={type:93,notes:[{id:'C4',type:21,track:1,pitch:60},
 {id:'E4',type:21,track:1,pitch:64}]};
const signatures={tick:0,elementAt:t=>({type:10}),nextInMeasure:null};
const keySignature={tick:0,elementAt:t=>({type:11}),nextInMeasure:null};
const atZero={tick:0,elementAt:t=>t===0?melody:null,nextInMeasure:null};
const atQuarter={tick:480,elementAt:t=>t===1?otherVoice:null,nextInMeasure:null};
signatures.nextInMeasure=keySignature;keySignature.nextInMeasure=atZero;
atZero.nextInMeasure=atQuarter;
let clears=0;
const selection={isRange:false,elements:selected,
 clear(){clears++;selected.length=0;return true;},
 select(note,add){if(!add)selected.length=0;selected.push(note);return true;}};
const plugin={Element:{NOTE:21,CHORD:93},cursorMeasure:1,cursorStaff:0,cursorVoice:0,
 curScore:{selection,nstaves:4,firstMeasure:{firstSegment:signatures,nextMeasure:null}}};
const params={mode:'notes',notes:[
 {measure:1,track:0,tick:0,noteIndex:0},
 {measure:1,track:1,tick:480,noteIndex:1}]};
"""


def _run(change: str = "") -> Any:
    source = "\n".join(
        line for line in _DRIVER.read_text().splitlines() if not line.startswith(".")
    )
    script = (
        source
        + _GRAPH
        + change
        + "\nconsole.log(JSON.stringify({reply:setup(plugin,params),clears,"
        "selected:selected.map(note=>note.id)}));"
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_setup_skips_same_tick_signatures_and_selects_disjoint_tones() -> None:
    result = _run()
    assert "error" not in result["reply"]
    assert result["selected"] == ["B4", "E4"]
    assert result["clears"] == 1
    assert [item["pitch"] for item in result["reply"]["result"]["elements"]] == [71, 64]


@pytest.mark.parametrize(
    "change",
    [
        "params.notes[0].tick=1;",  # Inside the quarter note is not an exact onset.
        "params.notes[0].track=true;",  # No Boolean-to-integer coordinate coercion.
        "params.notes[0].tick=9007199254740992;",
        "params.notes[0].noteIndex=1;",  # Single-note melody has no second tone.
        "const duplicate={tick:0,elementAt:t=>t===0?melody:null,"
        "nextInMeasure:atQuarter};"
        "atZero.nextInMeasure=duplicate;",
    ],
)
def test_missing_ambiguous_or_invalid_target_fails_before_changing_selection(
    change: str,
) -> None:
    result = _run(change)
    assert "error" in result["reply"]
    assert result["selected"] == [] and result["clears"] == 0
