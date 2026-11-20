"""Exercise the actual duration handler with native rewriting and failure probes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.duration_edit import DurationEdit
from mcp_score.bridge.musescore import _snake_case_keys

_PLUGIN = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node required for plugin graph")

_GRAPH = r"""
const vm=require('node:vm');
const modules={};
for(const name of ['Constants','Score','Passage','Pitch','Duration']) {
    modules[name]=vm.runInNewContext(sources[name]+'\nthis;', {...modules});
}
const {Constants,Passage,Duration}=modules;
const part={hasPitchedStaff:true,hasDrumStaff:false,hasTabStaff:false};
const tone={pitch:60,tpc1:14,tpc2:16,tpc:16,noteType:0,tieBack:null,tieForward:null,
    tuning:0,accidentalType:0,accidental:null,elements:[],spannerForward:[],spannerBack:[],
    isTrillCueNote:false};
const chord={type:93,noteType:0,tuplet:null,graceNotes:[],staff:{part},notes:[tone],
    actualDuration:{numerator:1,denominator:2},staffMove:0,beam:null,arpeggio:null,
    spanArpeggio:null,tremoloSingleChord:null,tremoloTwoChord:null,elements:[],lyrics:[],
    articulations:[],isTrillCueNote:false};
const following={...chord,notes:[{...tone,pitch:64,tpc1:18,tpc2:20,tpc:20}],
    duration:{numerator:1,denominator:2},lyrics:[{text:'stay',verse:0,syllabic:0,placement:1}]};
let length={numerator:1,denominator:2};
let starts=0,cancels=0;
const history=['prior'];
const controls={ignore:false,peer:false,throwSetter:false,postCommit:null};
let segments=[];
function rebuild(rest=false) {
    segments=[{tick:0,element:chord},{tick:960,element:following}];
    if(rest) segments.splice(1,0,{tick:480,element:{type:25,
        duration:{numerator:1,denominator:4},actualDuration:{numerator:1,denominator:4},lyrics:[]}});
    segments.forEach((segment,index)=>{
        segment.annotations=[];segment.nextInMeasure=segments[index+1]||null;
        segment.elementAt=track=>track===0?segment.element:null;
    });
    measure.firstSegment=segments[0];
}
const measure={firstSegment:null,nextMeasure:null,
    timesigActual:{numerator:1,denominator:1},
    timesigNominal:{numerator:4,denominator:4}};
Object.defineProperty(chord,'duration',{get:()=>length,set:value=>{
    if(controls.throwSetter)throw new Error('setter failed');
    if(controls.ignore)return;
    length=value;chord.actualDuration=value;rebuild(true);
    if(controls.peer) following.notes[0].pitch++;
}});
rebuild();
let saved;
const score={nstaves:1,firstMeasure:measure,spanners:[],
    startCmd(){starts++;saved={length:{...length},pitch:following.notes[0].pitch};},
    endCmd(cancel){if(cancel){cancels++;length=saved.length;chord.actualDuration=length;
        following.notes[0].pitch=saved.pitch;rebuild();}
        else {history.push('setEventDuration');
            if(controls.postCommit)controls.postCommit();}},
};
const plugin={curScore:score,Element:{CHORD:93,REST:25},NoteType:{NORMAL:0},
    Accidental:{NONE:0},mscoreMajorVersion:4,mscoreMinorVersion:7,
    fraction:(numerator,denominator)=>({numerator,denominator}),
    cursorMeasure:9,cursorStaff:1,cursorVoice:3};
function snapshot(){return Passage.readPassage(plugin,{startMeasure:1,endMeasure:1})
    .result.measures[0].events;}
const params={measure:1,staff:0,voice:0,offset:{numerator:0,denominator:1},
    duration:{numerator:1,denominator:4},policy:'shorten_fill_rest',expected:snapshot()};
function run(){const reply=Duration.setEventDuration(plugin,params);
    return {reply,starts,cancels,history,
        cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]};}
"""


def _run(change: str = "", *, graph: str = _GRAPH) -> Any:
    sources = {}
    for name in ["Constants", "Score", "Passage", "Pitch", "Duration"]:
        sources[name] = "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
    script = "const sources=" + json.dumps(sources) + ";" + graph + change
    script += "\nconsole.log(JSON.stringify(run()));"
    process = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(process.stdout)


def test_native_shorten_preserves_written_tpc_and_neighbor() -> None:
    observed = _run()
    result = DurationEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.verified and result.mutation_status == "applied"
    assert result.selection_status == "not_preserved"
    assert [event.kind for event in result.after] == ["chord", "rest", "chord"]
    assert result.after[0].notes == result.before[0].notes
    assert result.after[-1] == result.before[-1]
    assert observed["history"] == ["prior", "setEventDuration"]
    assert observed["cursor"] == [9, 1, 3]


@pytest.mark.parametrize("minor", [4, 6])
def test_older_native_versions_reject_before_undo(minor: int) -> None:
    observed = _run(f"plugin.mscoreMinorVersion={minor};")
    assert observed["reply"]["code"] == "unsupported_native_api"
    assert observed["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "tone.pitch++;",
        "following.notes[0].tpc2++;",
        "following.lyrics[0].text='stale';",
        "params.expected.pop();",
        "params.expected[0].tick=480;",
    ],
)
def test_stale_voice_interval_rejects_before_any_mutation(change: str) -> None:
    observed = _run(change)
    assert observed["reply"]["code"] == "stale_target"
    assert observed["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "score.spanners=[{}];",
        "delete score.spanners;",
        "chord.lyrics=[{}];",
        "chord.articulations=[{}];",
        "delete chord.elements;",
        "chord.tremoloTwoChord={};",
        "chord.beam={};",
        "chord.staffMove=1;",
        "chord.tuplet={};",
        "tone.tieForward={};",
        "tone.elements=[{}];",
        "tone.spannerForward=[{}];",
        "tone.accidentalType=999;",
        "tone.tuning=1;",
        "part.hasTabStaff=true;",
        "params.duration={numerator:1,denominator:1};",
        "params.duration={numerator:3,denominator:16};",
    ],
)
def test_unknown_or_unsupported_native_context_never_opens_undo(change: str) -> None:
    observed = _run(change + "params.expected=snapshot();")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == 0


def test_noop_does_not_open_undo() -> None:
    observed = _run("params.duration={numerator:1,denominator:2};")
    assert observed["reply"]["result"]["mutationStatus"] == "unchanged"
    assert observed["history"] == ["prior"] and observed["starts"] == 0


@pytest.mark.parametrize("control", ["ignore", "throwSetter", "peer"])
def test_failed_write_or_neighbor_damage_cancels_only_open_operation(
    control: str,
) -> None:
    observed = _run(f"controls.{control}=true;")
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["cancels"] == 1 and observed["history"] == ["prior"]


def test_postcommit_read_failure_reports_known_applied_without_global_undo() -> None:
    observed = _run(
        "controls.postCommit=()=>{Passage.readPassage=()=>{"
        "throw new Error('read failed');};};"
    )
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0 and observed["history"] == [
        "prior",
        "setEventDuration",
    ]


def test_distant_spanner_allows_edit_but_intersecting_span_rejects() -> None:
    span = (
        "const span={type:10,track:0,spannerTrack2:0,anchor:0,"
        "spannerTick:{numerator:2,denominator:1},"
        "spannerTicks:{numerator:1,denominator:2}};score.spanners=[span];"
    )
    accepted = _run(span)
    assert accepted["reply"]["result"]["mutationStatus"] == "applied"
    rejected = _run(span + "span.spannerTick={numerator:1,denominator:4};")
    assert rejected["reply"]["code"] == "unsupported_spanner"
    assert rejected["starts"] == 0


def test_postcommit_spanner_damage_is_reported_without_global_undo() -> None:
    observed = _run(
        "const span={type:10,track:0,spannerTrack2:0,anchor:0,"
        "spannerTick:{numerator:2,denominator:1},"
        "spannerTicks:{numerator:1,denominator:2}};score.spanners=[span];"
        "controls.postCommit=()=>{span.spannerTrack2=1;};"
    )
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0


def test_commit_exception_reports_unknown_without_cancelling_another_operation() -> (
    None
):
    observed = _run(
        "const nativeEnd=score.endCmd;score.endCmd=function(cancel){"
        "nativeEnd.call(this,cancel);if(!cancel)throw new Error('commit uncertain');};"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 0
    assert observed["history"] == ["prior", "setEventDuration"]


def test_rollback_cannot_claim_restored_when_measure_context_remains_changed() -> None:
    observed = _run(
        "controls.peer=true;const nativeEnd=score.endCmd;"
        "score.endCmd=function(cancel){nativeEnd.call(this,cancel);"
        "if(cancel)measure.timesigNominal={numerator:2,denominator:2};};"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 1
