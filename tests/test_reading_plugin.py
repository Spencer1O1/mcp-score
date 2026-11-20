"""Score summaries read native nominal meter, not a nonexistent cursor field."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.results import ScoreInfo, TimeSignature

_PLUGIN = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node required for plugin tests")


def _summary(change: str = "") -> dict[str, Any]:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        for name in ["Constants", "Score", "Reading"]
    }
    script = (
        "const sources="
        + json.dumps(sources)
        + r""";
const vm = require('node:vm');
const modules = {MS:{Cursor:{SCORE_START:0}}};
for (const name of ['Constants','Score','Reading']) {
    modules[name] = vm.runInNewContext(sources[name]+'\nthis;', {...modules});
}
const first = {timesigNominal:{numerator:4,denominator:4},
    timesigActual:{numerator:1,denominator:1},nextMeasure:null};
const selection = {elements:[{pitch:71}],isRange:false};
const history = ['prior edit'];
const score = {title:'Unsaved score',parts:[{partName:'Piano',startTrack:0,endTrack:8}],
    firstMeasure:first,selection,dirty:true,history,
    newCursor(){
        // Deliberately no timeSignature: it is not a supported native property.
        return {keySignature:-1,measure:null,
            rewind(){this.measure=score.firstMeasure;},
            nextMeasure(){this.measure=this.measure.nextMeasure;}};
    },
    startCmd(){throw Error('Read must never start an undo command');},
    endCmd(){throw Error('Read must never end an undo command');}
};
const plugin = {curScore:score,cursorMeasure:7,cursorStaff:1,cursorVoice:3,
    cmd(){throw Error('Read must not invoke a UI action');}};
"""
        + change
        + r"""
const before = JSON.stringify(plugin);
const reply = modules.Reading.getScore(plugin,{});
console.log(JSON.stringify({reply,before,after:JSON.stringify(plugin),
    sameSelection:score.selection===selection,sameHistory:score.history===history}));
"""
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("nominal", "actual", "expected"),
    [
        ((4, 4), (1, 1), TimeSignature(numerator=4, denominator=4)),
        ((2, 2), (1, 1), TimeSignature(numerator=2, denominator=2)),
        ((4, 4), (1, 4), TimeSignature(numerator=4, denominator=4)),
    ],
)
def test_score_summary_preserves_opening_nominal_meter_and_state(
    nominal: tuple[int, int], actual: tuple[int, int], expected: TimeSignature
) -> None:
    observed = _summary(
        "first.timesigNominal="
        + json.dumps(dict(zip(["numerator", "denominator"], nominal, strict=True)))
        + ";first.timesigActual="
        + json.dumps(dict(zip(["numerator", "denominator"], actual, strict=True)))
        + ";"
    )
    summary = ScoreInfo.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert summary.time_signature == expected
    assert summary.title == "Unsaved score" and summary.measure_count == 1
    assert summary.key_signature == -1
    assert summary.parts[0].start_staff == 0 and summary.parts[0].end_staff == 1
    assert observed["before"] == observed["after"]
    assert observed["sameSelection"] and observed["sameHistory"]


@pytest.mark.parametrize(
    "change",
    [
        "score.firstMeasure=null;",
        "delete first.timesigNominal;",
        "first.timesigNominal=null;",
        "first.timesigNominal={};",
        "first.timesigNominal={numerator:0,denominator:4};",
        "first.timesigNominal={numerator:-1,denominator:4};",
        "first.timesigNominal={numerator:4,denominator:0};",
        "first.timesigNominal={numerator:4,denominator:-4};",
        "first.timesigNominal={numerator:3.5,denominator:4};",
        "first.timesigNominal={numerator:4,denominator:2.5};",
        "first.timesigNominal={numerator:'4',denominator:4};",
        "first.timesigNominal={numerator:4,denominator:true};",
        "first.timesigNominal={numerator:NaN,denominator:4};",
        "first.timesigNominal={numerator:4,denominator:Infinity};",
    ],
)
def test_missing_or_invalid_nominal_meter_returns_null_without_mutation(
    change: str,
) -> None:
    observed = _summary(change)
    assert observed["reply"]["result"]["timeSignature"] is None
    assert observed["before"] == observed["after"]
    assert observed["sameSelection"] and observed["sameHistory"]
