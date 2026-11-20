"""Actual context module, with observable direct native getters and no cursor."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.staff_context import StaffContext

_PLUGIN = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node required for native JS graph"
)
_GRAPH = r"""
const vm=require('node:vm');
const modules={};
for(const name of ['Constants','Score','Passage','Context']) {
    modules[name]=vm.runInNewContext(sources[name]+'\nthis;', {...modules});
}
const {Context}=modules;
const f=(numerator,denominator)=>({numerator,denominator,
    ticks:Math.round(numerator*1920/denominator)});
const calls=[];
const score={nstaves:2,newCursor(){throw new Error('No cursor allowed');}};
const oldInstrument={instrumentId:'clarinet',musicXmlId:'wind.reed.clarinet',
    longName:'Clarinet',shortName:'Cl.'};
const nextInstrument={...oldInstrument,instrumentId:'bass-clarinet',
    longName:'Bass clarinet'};
const part={instrumentAtTick(time){calls.push(['instrument',time]);
    const tick=typeof time==='number'?time:time.numerator*1920/time.denominator;
    return tick>=720?nextInstrument:oldInstrument;}};
score.parts=[{...part,startTrack:0,endTrack:8}];
score.staves=[0,1].map(index=>({part,
    clefType(time){calls.push(['clef',time]);
        return time.numerator/time.denominator>=3/8?20:0;},
    key(time){return time.numerator/time.denominator>=3/8?2:0;},
    timeSig(){return {timesig:f(4,4)};},timeStretch(){return f(1,1);},
    transpose(){return {chromatic:index?-12:-2,diatonic:index?-7:-1};}}));
function measure(start,length,ticks){
    const segments=ticks.map(tick=>({tick,fraction:f(tick,1920)}));
    segments.forEach((segment,index)=>segment.nextInMeasure=segments[index+1]||null);
    return {tick:f(start,1920),firstSegment:segments[0],timesigActual:f(length,1920),
        timesigNominal:f(4,4),nextMeasure:null};
}
const first=measure(0,480,[0,480]);
const second=measure(480,1920,[480,720,720,960,2400]);
const third=measure(2400,1440,[2400,2880,3840]);
first.nextMeasure=second;second.nextMeasure=third;score.firstMeasure=first;
const plugin={curScore:score,mscoreMajorVersion:4,mscoreMinorVersion:7,
    cursorMeasure:8,cursorStaff:1,cursorVoice:3,
    cmd(){throw new Error('No action allowed');}};
const params={startMeasure:2,endMeasure:3};
function run(){return {reply:Context.readStaffContext(plugin,params),calls,
    cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]};}
"""


def _run(change: str = "") -> Any:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        for name in ["Constants", "Score", "Passage", "Context"]
    }
    result = subprocess.run(
        [
            _NODE or "node",
            "-e",
            "const sources="
            + json.dumps(sources)
            + ";"
            + _GRAPH
            + change
            + "\nconsole.log(JSON.stringify(run()));",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_inherited_midmeasure_and_irregular_context_is_independent_of_voice() -> None:
    observed = _run()
    result = StaffContext.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.position_precision == "native_fraction" and result.sampling_complete
    assert result.sampled_point_count == 5 and result.staff_point_queries == 10
    clarinet, bass = result.timelines
    assert clarinet.start.measure == 2 and clarinet.start.offset.numerator == 0
    assert clarinet.start.instrument is not None
    assert clarinet.start.instrument.instrument_id == "clarinet"
    change = clarinet.changes[0]
    assert (
        change.tick == 720
        and change.offset.numerator == 1
        and change.offset.denominator == 8
    )
    assert change.native_key == 2 and change.native_clef_type == 20
    assert (
        change.instrument is not None
        and change.instrument.instrument_id == "bass-clarinet"
    )
    assert clarinet.changes[-1].measure == 3
    assert clarinet.changes[-1].measure_actual_duration.numerator == 3
    assert clarinet.changes[-1].measure_actual_duration.denominator == 4
    assert bass.start.native_transposition is not None
    assert bass.start.native_transposition.chromatic == -12
    assert bass.start.native_transposition.diatonic == -7
    assert observed["cursor"] == [8, 1, 3]
    assert all(entry[1]["numerator"] >= 480 for entry in observed["calls"])


def test_half_open_end_and_duplicate_segment_positions_are_sampled_once() -> None:
    result = _run("params.startMeasure=1;params.endMeasure=1;")["reply"]["result"]
    assert result["sampledPointCount"] == 1
    assert result["timelines"][0]["changes"] == []
    assert result["timelines"][0]["start"]["measureActualDuration"] == {
        "numerator": 1,
        "denominator": 4,
    }
    assert result["timelines"][0]["start"]["measureNominalMeter"] == {
        "numerator": 4,
        "denominator": 4,
    }


def test_old_native_api_returns_explicit_partial_integer_tick_context() -> None:
    observed = _run("plugin.mscoreMinorVersion=4;")
    result = StaffContext.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.position_precision == "native_integer_ticks"
    for timeline in result.timelines:
        assert timeline.start.instrument is not None
        assert (
            timeline.start.native_key is None
            and timeline.start.native_transposition is None
        )
        assert {entry.field for entry in timeline.start.unavailable_fields} == {
            "native_clef_type",
            "native_key",
            "local_meter",
            "time_stretch",
            "native_transposition",
        }
    assert all(isinstance(entry[1], int) for entry in observed["calls"])


@pytest.mark.parametrize(
    "change",
    [
        "delete score.staves[0].key;",
        "score.staves[0].key=()=>{throw new Error('unavailable');};",
        "score.staves[0].transpose=()=>({chromatic:NaN,diatonic:-1});",
    ],
)
def test_missing_or_invalid_getter_is_null_with_field_reason(change: str) -> None:
    result = _run(change)["reply"]["result"]["timelines"][0]["start"]
    assert result["unavailableFields"]
    assert result["nativeKey"] is None or result["nativeTransposition"] is None


@pytest.mark.parametrize(
    "change",
    [
        "Context.maxQueries=2;",
        "score.nstaves=1000000000;",
        "Context.maxSegmentsScanned=1;",
        "params.staff=2;",
        "params.startMeasure=true;",
        "params.endMeasure=99;",
        "second.tick=null;",
    ],
)
def test_invalid_or_over_budget_read_fails_wholly_before_native_queries(
    change: str,
) -> None:
    observed = _run(change)
    assert "error" in observed["reply"] and "result" not in observed["reply"]
    assert observed["calls"] == []


def test_fractional_native_position_survives_integer_tick_rounding() -> None:
    observed = _run("second.firstSegment.nextInMeasure.fraction=f(1921,5760);")
    result = observed["reply"]["result"]
    assert result["positionPrecision"] == "native_fraction"
    assert any(
        entry[1]["numerator"] == 1921 and entry[1]["denominator"] == 5760
        for entry in observed["calls"]
    )


def test_native_nonunit_local_meter_and_stretch_are_not_inferred_from_measure() -> None:
    result = _run(
        "score.staves[1].timeSig=()=>({timesig:f(3,4)});"
        "score.staves[1].timeStretch=()=>f(3,4);"
    )["reply"]["result"]
    point = result["timelines"][1]["start"]
    assert point["localMeter"] == {"numerator": 3, "denominator": 4}
    assert point["timeStretch"] == {"numerator": 3, "denominator": 4}
    assert point["measureNominalMeter"] == {"numerator": 4, "denominator": 4}


def test_start_uses_native_measure_tick_even_if_first_segment_is_later() -> None:
    observed = _run("second.firstSegment=second.firstSegment.nextInMeasure;")
    point = observed["reply"]["result"]["timelines"][0]["start"]
    assert point["tick"] == 480 and point["absoluteTime"] == {
        "numerator": 1,
        "denominator": 4,
    }
    assert point["offset"] == {"numerator": 0, "denominator": 1}
