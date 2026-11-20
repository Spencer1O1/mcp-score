"""Native rest-coverage guards and verification in the actual duration module."""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_duration_plugin import _run

_GRAPH = r"""
const vm=require('node:vm');
const modules={};
for(const name of ['Constants','Score','Passage','Pitch','Duration']) {
    modules[name]=vm.runInNewContext(sources[name]+'\nthis;', {...modules});
}
const {Passage,Duration}=modules;
const part={hasPitchedStaff:true,hasDrumStaff:false,hasTabStaff:false};
const note={pitch:60,tpc1:14,tpc2:16,tpc:16,noteType:0,tieBack:null,tieForward:null,
    tuning:0,accidentalType:0,accidental:null,elements:[],spannerForward:[],spannerBack:[],
    isTrillCueNote:false};
const chord={type:93,noteType:0,tuplet:null,graceNotes:[],staff:{part},notes:[note],
    staffMove:0,beam:null,arpeggio:null,spanArpeggio:null,tremoloSingleChord:null,
    tremoloTwoChord:null,elements:[],lyrics:[],articulations:[],isTrillCueNote:false};
const peer={...chord,notes:[{...note,pitch:64,tpc1:18,tpc2:20,tpc:20}],
    duration:{numerator:1,denominator:4},actualDuration:{numerator:1,denominator:4}};
const other={...peer,notes:[{...note,pitch:67,tpc1:15,tpc2:17,tpc:17}]};
function rest(tick,length=240) {
    return {tick,element:{type:25,track:0,duration:{numerator:length,denominator:1920},
        actualDuration:{numerator:length,denominator:1920},tuplet:null,topTuplet:null,
        beam:null,staffMove:0,gap:false,visible:true,generated:false,isFullMeasureRest:false,
        elements:[],lyrics:[]}};
}
let nativeRests=[rest(480),rest(720)];
let written=480,starts=0,cancels=0;
const history=['prior user change'];
const controls={ignore:false,throwSetter:false,keepRest:false,peer:false,
    rollbackDamage:false,postCommit:null};
const measure={firstSegment:null,nextMeasure:null,
    timesigActual:{numerator:1,denominator:1},timesigNominal:{numerator:4,denominator:4}};
let segments=[];
function rebuild() {
    segments=[{tick:0,element:chord},...nativeRests,{tick:960,element:peer}];
    segments.sort((left,right)=>left.tick-right.tick);
    segments.forEach((segment,index)=>{
        segment.annotations=segment.annotations||[];
        segment.nextInMeasure=segments[index+1]||null;
        segment.elementAt=track=>track===0?segment.element:(track===1&&segment.tick===0?other:null);
    });
    measure.firstSegment=segments[0];
}
Object.defineProperty(chord,'duration',{get:()=>({numerator:written,denominator:1920}),set:value=>{
    if(controls.throwSetter)throw new Error('native setter failed');
    if(controls.ignore)return;
    written=value.numerator*1920/value.denominator;
    chord.actualDuration=value;
    nativeRests=controls.keepRest?nativeRests:nativeRests.filter(rest=>rest.tick>=written);
    if(controls.peer)other.notes[0].pitch++;
    rebuild();
}});
chord.actualDuration={numerator:1,denominator:4};
let saved;
const score={nstaves:1,firstMeasure:measure,spanners:[],
    startCmd(){starts++;saved={written,rests:nativeRests.slice(),pitch:other.notes[0].pitch};},
    endCmd(cancel){if(cancel){cancels++;written=saved.written;nativeRests=saved.rests;
        chord.actualDuration={numerator:written,denominator:1920};
        other.notes[0].pitch=saved.pitch;if(controls.rollbackDamage)other.notes[0].pitch++;
        rebuild();}
        else {history.push('setEventDuration');
            if(controls.postCommit)controls.postCommit();}},
};
const plugin={curScore:score,Element:{CHORD:93,REST:25},NoteType:{NORMAL:0},
    Accidental:{NONE:0},mscoreMajorVersion:4,mscoreMinorVersion:7,
    fraction:(numerator,denominator)=>({numerator,denominator}),
    cursorMeasure:9,cursorStaff:1,cursorVoice:3,
    cmd(){throw new Error('Global undo must not be called');}};
function snapshot(){return Passage.readPassage(plugin,{startMeasure:1,endMeasure:1})
    .result.measures[0].events;}
rebuild();
const params={measure:1,staff:0,voice:0,offset:{numerator:0,denominator:1},
    duration:{numerator:1,denominator:2},policy:'extend_into_rests',
    expected:snapshot().filter(event=>event.voice===0)};
function run(){
    const before=snapshot();
    const reply=Duration.extendEventDuration(plugin,params);
    let actual=null;try {actual=snapshot();}catch(unavailable){}
    return {reply,starts,cancels,history,before,actual,
        cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]};
}
"""


def _extension(change: str = "", *, refresh: bool = False) -> Any:
    if refresh:
        change += "rebuild();params.expected=snapshot().filter(event=>event.voice===0);"
    return _run(change, graph=_GRAPH)


def test_extension_consumes_exact_rest_coverage_and_preserves_every_other_track() -> (
    None
):
    observed = _extension()
    result = observed["reply"]["result"]
    assert result["mutationStatus"] == "applied" and result["verified"]
    assert observed["starts"] == 1 and observed["cancels"] == 0
    assert observed["history"] == ["prior user change", "setEventDuration"]
    assert result["after"] == observed["actual"]
    assert result["before"][0]["notes"] == result["after"][0]["notes"]
    assert [event["tick"] for event in result["after"] if event["voice"] == 0] == [
        0,
        960,
    ]
    assert result["before"][-1] == result["after"][-1]
    assert [event for event in result["before"] if event["voice"] == 1] == [
        event for event in result["after"] if event["voice"] == 1
    ]
    assert observed["cursor"] == [9, 1, 3]


@pytest.mark.parametrize(
    "change",
    [
        "nativeRests=[];",
        "nativeRests=[rest(481,240),rest(721,240)];",
        "nativeRests=[rest(480,480),rest(720,240)];",
        "nativeRests=[rest(480,960)];",  # Endpoint inside a rest.
        "nativeRests=[rest(240,960)];",  # Rest already overlaps original chord.
        "nativeRests[0].element={...peer};",
        "nativeRests[0].element.type=999;",
        "nativeRests[0].element.tuplet={};",
        "nativeRests[0].element.topTuplet={};",
        "nativeRests[0].element.elements=[{}];",
        "nativeRests[0].element.lyrics=[{}];",
        "nativeRests[0].element.gap=true;",
        "nativeRests[0].element.visible=false;",
        "nativeRests[0].element.generated=true;",
        "nativeRests[0].element.isFullMeasureRest=true;",
        "nativeRests[0].element.staffMove=1;",
        "nativeRests[0].element.beam={};",
        "nativeRests[0].element.track=1;",
        "delete nativeRests[0].element.elements;",
        "delete nativeRests[0].element.gap;",
        "delete nativeRests[0].element.topTuplet;",
        "nativeRests[0].element.actualDuration={numerator:1,denominator:12};",
        "nativeRests[0].annotations=[{track:0,type:999}];",
        "nativeRests[0].annotations=[{track:-1,type:999}];",
        "nativeRests[0].annotations=[{type:999}];",
    ],
)
def test_unsafe_native_rest_coverage_rejects_before_command(change: str) -> None:
    observed = _extension(change, refresh=True)
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["reply"]["code"] == "unsupported_rest_coverage"
    assert observed["starts"] == observed["cancels"] == 0
    assert observed["history"] == ["prior user change"]
    assert observed["before"] == observed["actual"]


def test_chord_onset_annotations_are_preserved_outside_consumed_rests() -> None:
    observed = _extension(
        "rebuild();segments[0].annotations=[{type:999,track:0,text:'at onset'}];"
        "const originalRebuild=rebuild;rebuild=function(){originalRebuild();"
        "segments[0].annotations=[{type:999,track:0,text:'at onset'}];};",
        refresh=True,
    )
    assert observed["reply"]["result"]["mutationStatus"] == "applied"


def test_extension_noop_keeps_undo_history_empty() -> None:
    observed = _extension("params.duration={numerator:1,denominator:4};")
    assert observed["reply"]["result"]["mutationStatus"] == "unchanged"
    assert observed["starts"] == observed["cancels"] == 0
    assert observed["history"] == ["prior user change"]


@pytest.mark.parametrize("control", ["ignore", "throwSetter", "keepRest", "peer"])
def test_failed_extension_rolls_back_only_current_scope_and_verifies_contents(
    control: str,
) -> None:
    observed = _extension(f"controls.{control}=true;")
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["cancels"] == 1
    assert observed["before"] == observed["actual"]
    assert observed["history"] == ["prior user change"]


def test_failed_rollback_readback_remains_unknown() -> None:
    observed = _extension("controls.peer=true;controls.rollbackDamage=true;")
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 1 and observed["before"] != observed["actual"]


def test_rollback_cannot_claim_restoration_when_native_rest_flags_changed() -> None:
    observed = _extension(
        "controls.ignore=true;const end=score.endCmd;"
        "score.endCmd=function(cancel){end.call(this,cancel);"
        "if(cancel)nativeRests[0].element.gap=true;};"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 1
    # Passage events alone would incorrectly appear fully restored.
    assert observed["before"] == observed["actual"]


def test_postcommit_extension_verification_failure_never_global_undo() -> None:
    observed = _extension("controls.postCommit=()=>{other.notes[0].pitch++;};")
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0
    assert observed["history"] == ["prior user change", "setEventDuration"]


def test_commit_exception_preserves_unknown_outcome_without_cancelling() -> None:
    observed = _extension(
        "const end=score.endCmd;score.endCmd=function(cancel){end.call(this,cancel);"
        "if(!cancel)throw new Error('commit uncertain');};"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 0 and observed["before"] != observed["actual"]
    assert observed["history"] == ["prior user change", "setEventDuration"]


@pytest.mark.parametrize("minor", [4, 6])
def test_older_native_versions_actively_reject_extension(minor: int) -> None:
    observed = _extension(f"plugin.mscoreMinorVersion={minor};")
    assert observed["reply"]["code"] == "unsupported_native_api"
    assert observed["starts"] == 0


def test_extension_refuses_intersecting_spanner_before_rest_rewrite() -> None:
    observed = _extension(
        "score.spanners=[{type:10,track:1,spannerTrack2:1,anchor:0,"
        "spannerTick:{numerator:1,denominator:4},"
        "spannerTicks:{numerator:1,denominator:4}}];"
    )
    assert observed["reply"]["code"] == "unsupported_spanner"
    assert observed["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "Duration.extendEventDuration=Duration.setEventDuration;",
        "params.policy='shorten_fill_rest';",
    ],
)
def test_contradictory_wire_policy_rejects_before_mutation(change: str) -> None:
    observed = _extension(change)
    assert observed["reply"]["code"] == "invalid_target"
    assert observed["starts"] == 0
