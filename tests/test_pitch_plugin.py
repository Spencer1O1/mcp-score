"""Run the actual atomic edit handler against a native-property test graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.pitch import PitchEdit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node is needed for plugin JS tests"
)

_GRAPH = r"""
const vm = require('node:vm');
const Constants = {ticksPerWholeNote:1920,voicesPerStaff:4,
    minTpc:-1,maxTpc:33,minMidiPitch:0,maxMidiPitch:127};
const Score = {requireScore: p => p.curScore ? null : {error:"No score"}};
const Passage = vm.runInNewContext(passageSource + '\nthis;', {Constants,Score});
const Pitch = vm.runInNewContext(pitchSource + '\nthis;', {Constants,Score,Passage});
const controls = {reorder:false,ignoreTpc2:false,throwTpc2:false,peerChange:false};
function note(pitch,tpc,writtenTpc=tpc) {
    const data = {pitch,tpc1:tpc,tpc2:writtenTpc};
    const result = {data,noteType:0,tieBack:null,tieForward:null,tuning:0,
        is(other) {return other === result;}};
    Object.defineProperty(result,'pitch',{get:()=>data.pitch,set:value=>{
        data.pitch=value;
        if (controls.reorder) chord.notes.sort((a,b)=>a.pitch-b.pitch);
    }});
    Object.defineProperty(result,'tpc1',{get:()=>data.tpc1,set:value=>data.tpc1=value});
    Object.defineProperty(result,'tpc2',{get:()=>data.tpc2,set:value=>{
        if (controls.throwTpc2) throw new Error('native setter failed');
        if (!controls.ignoreTpc2) data.tpc2=value;
        if (controls.peerChange) chord.notes.find(n=>n!==result).data.pitch+=1;
    }});
    Object.defineProperty(result,'tpc',{get:()=>displayConcert?data.tpc1:data.tpc2});
    return result;
}
let displayConcert=true;
const part={hasPitchedStaff:true,hasDrumStaff:false,hasTabStaff:false};
const chord={type:93,noteType:0,tuplet:null,graceNotes:[],staff:{part},
    notes:[note(60,14),note(64,18)],duration:{numerator:3,denominator:4},
    actualDuration:{numerator:3,denominator:4}};
const first={type:93,noteType:0,tuplet:null,graceNotes:[],staff:{part},
    notes:[note(71,19)],duration:{numerator:1,denominator:4},
    actualDuration:{numerator:1,denominator:4}};
const secondSegment={tick:480,elementAt:track=>track===0?chord:null,
    annotations:[],nextInMeasure:null};
const firstSegment={tick:0,elementAt:track=>track===0?first:null,
    annotations:[],nextInMeasure:secondSegment};
const measure={firstSegment,nextMeasure:null,
    timesigActual:{numerator:1,denominator:1},timesigNominal:{numerator:4,denominator:4}};
let saved=[];
let starts=0;
let cancels=0;
const score={nstaves:1,firstMeasure:measure,history:['prior user change'],
    postCommit:null,throwAfterCommit:false,
    startCmd(){starts++;saved=chord.notes.map(n=>[n,{...n.data}]);},
    endCmd(cancel){
        if (cancel) {
            cancels++; for (const [n,data] of saved) Object.assign(n.data,data);
        }
        else {
            this.history.push('setNotePitch');
            if(this.postCommit)this.postCommit();
            if(this.throwAfterCommit)throw new Error('commit outcome uncertain');
        }
    }};
const plugin={curScore:score,Element:{CHORD:93,REST:25},NoteType:{NORMAL:0},
    cursorMeasure:7,cursorStaff:0,cursorVoice:2};
function snapshot(){return Passage.describeEvent(plugin,chord,0,0,480,0);}
const params={measure:1,staff:0,voice:0,offset:{numerator:1,denominator:4},
    noteIndex:0,pitch:67,tpc:15,expected:snapshot()};
function run(){
    const reply=Pitch.setNotePitch(plugin,params);
    return {reply,actual:snapshot(),starts,cancels,history:score.history,
        first:first.notes.map(n=>n.pitch),cursor:[plugin.cursorMeasure,plugin.cursorVoice]};
}
"""


def _run(expression: str = "") -> Any:
    sources = {}
    for name in ["passage", "pitch"]:
        sources[name] = "\n".join(
            line
            for line in (_PLUGIN_DIR / f"{name}.js").read_text().splitlines()
            if not line.startswith(".")
        )
    script = (
        "const passageSource="
        + json.dumps(sources["passage"])
        + ";\n"
        + "const pitchSource="
        + json.dumps(sources["pitch"])
        + ";\n"
        + _GRAPH
        + expression
        + "\nconsole.log(JSON.stringify(run()));"
    )
    completed = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def test_edit_changes_only_selected_tone_and_reads_actual_reordered_chord() -> None:
    response = _run("controls.reorder=true;")
    result = PitchEdit.model_validate(_snake_case_keys(response["reply"]["result"]))
    assert [note.name for note in result.before.notes] == ["C4", "E4"]
    assert [note.name for note in result.after.notes] == ["E4", "G4"]
    assert result.note_index_before == 0 and result.note_index_after == 1
    assert result.before.offset == result.after.offset
    assert result.before.duration == result.after.duration
    assert result.verified and result.mutation_status == "applied"
    assert result.snapshot_scope == "target_chord" and result.limitations
    assert response["actual"] == response["reply"]["result"]["after"]
    assert response["history"] == ["prior user change", "setNotePitch"]
    assert response["first"] == [71] and response["cursor"] == [7, 2]


def test_noop_does_not_open_command_or_undo_prior_action() -> None:
    response = _run("params.pitch=60;params.tpc=14;")
    assert response["reply"]["result"]["mutationStatus"] == "unchanged"
    assert response["starts"] == 0 and response["cancels"] == 0
    assert response["history"] == ["prior user change"]


@pytest.mark.parametrize(
    "change",
    [
        "chord.notes[0].data.pitch=62;",
        "chord.notes[1].data.tpc2=19;",
        "chord.duration={numerator:1,denominator:2};",
        "params.expected.tick=0;",
        "params.expected.voice=1;",
        "params.expected.offset={numerator:0,denominator:1};",
    ],
)
def test_stale_snapshot_rejects_before_opening_command(change: str) -> None:
    response = _run(change)
    assert response["reply"]["code"] == "stale_target"
    assert response["reply"]["mutation_status"] == "not_applied"
    assert response["starts"] == 0
    assert response["history"] == ["prior user change"]


@pytest.mark.parametrize(
    "change",
    [
        "chord.notes[0].tieForward={};",
        "chord.tuplet={};",
        "chord.graceNotes=[{}];",
        "chord.noteType=1;",
        "part.hasDrumStaff=true;",
        "part.hasTabStaff=true;",
        "delete part.hasPitchedStaff;",
        "chord.notes[0].tuning=0.5;",
        "delete chord.notes[0].tieBack;",
    ],
)
def test_unsupported_native_target_rejects_without_touching_undo(change: str) -> None:
    response = _run(change)
    assert response["reply"]["code"] == "unsupported_target"
    assert response["reply"]["mutation_status"] == "not_applied"
    assert response["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "params.pitch=61;params.tpc=14;",
        "params.offset={numerator:1,denominator:7};",
        "params.noteIndex=3;",
        "params.pitch=128;",
        "params.tpc=34;",
    ],
)
def test_invalid_positions_and_spellings_never_start_mutation(change: str) -> None:
    response = _run(change)
    assert "error" in response["reply"]
    assert response["reply"]["mutation_status"] == "not_applied"
    assert response["starts"] == 0


def test_old_snapshot_without_written_tpc_requires_fresh_read() -> None:
    response = _run("delete params.expected.notes[0].writtenTpc;")
    assert response["reply"]["code"] == "invalid_snapshot"
    assert "read again" in response["reply"]["error"]
    assert response["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "chord.notes[1].data.pitch=60;chord.notes[1].data.tpc1=14;chord.notes[1].data.tpc2=14;params.expected=snapshot();",
        "params.pitch=64;params.tpc=18;",
    ],
)
def test_existing_or_resulting_unisons_are_rejected_as_unsupported(change: str) -> None:
    response = _run(change)
    assert response["reply"]["code"] == "unsupported_target"
    assert response["starts"] == 0


@pytest.mark.parametrize("control", ["ignoreTpc2", "throwTpc2", "peerChange"])
def test_failed_native_setters_rollback_only_open_command_and_verify_restoration(
    control: str,
) -> None:
    response = _run(f"controls.{control}=true;")
    assert response["reply"]["code"] == "verification_failed"
    assert response["reply"]["mutation_status"] == "rolled_back"
    assert [note["name"] for note in response["actual"]["notes"]] == ["C4", "E4"]
    assert response["cancels"] == 1
    assert response["history"] == ["prior user change"]


def test_post_commit_mismatch_reports_applied_unverified_without_global_undo() -> None:
    response = _run("score.postCommit=()=>chord.notes[1].data.pitch+=1;")
    assert response["reply"]["code"] == "verification_failed"
    assert response["reply"]["mutation_status"] == "applied_unverified"
    assert response["history"] == ["prior user change", "setNotePitch"]
    assert response["cancels"] == 0


def test_transposing_spelling_interval_is_preserved_in_written_display() -> None:
    response = _run(
        "chord.notes[0].data.tpc2=16;displayConcert=false;params.expected=snapshot();"
        "params.pitch=61;params.tpc=21;"
    )
    result = PitchEdit.model_validate(_snake_case_keys(response["reply"]["result"]))
    assert result.after.notes[0].pitch == 61
    assert result.after.notes[0].tpc == 21
    assert result.after.notes[0].written_tpc == 23
    assert result.after.notes[0].displayed_tpc == 23


def test_written_spelling_out_of_native_range_rejects_before_mutation() -> None:
    response = _run(
        "chord.notes[0].data.tpc2=33;params.expected=snapshot();params.pitch=61;params.tpc=21;"
    )
    assert response["reply"]["code"] == "unsupported_spelling"
    assert response["starts"] == 0


def test_commit_exception_never_cancels_an_unknown_undo_scope() -> None:
    response = _run("score.throwAfterCommit=true;")
    assert response["reply"]["code"] == "verification_failed"
    assert response["reply"]["mutation_status"] == "unknown"
    assert response["history"] == ["prior user change", "setNotePitch"]
    assert response["cancels"] == 0
