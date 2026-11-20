"""Actual batch JS with independent native-shaped chords and an undo journal."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.pitch_batch import PitchBatchEdit
from tests.test_pitch_plugin import _GRAPH

_NODE = shutil.which("node")
_PLUGIN = Path(__file__).parent.parent / "src/mcp_score/musescore/plugin"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for QML JS tests")
_SETUP = r"""
const Batch=vm.runInNewContext(batchSource+'\nthis;',{Pitch,Passage,Score});
first.is=other=>other===first;chord.is=other=>other===chord;
first.lyrics=[{text:'first word',verse:0,syllabic:0,placement:1}];
chord.lyrics=[{text:'chord word',verse:1,syllabic:0,placement:1}];
first.articulations=[];chord.articulations=[];
let applications=0;
const apply=Pitch.applyPitchPlan;
Pitch.applyPitchPlan=plan=>{applications++;apply(plan);};
score.startCmd=()=>{
 starts++;saved=[first,chord].map(value=>({chord:value,text:value.text,
 notes:value.notes.map(n=>[n,{...n.data}]),
 lyrics:value.lyrics.map(v=>({...v})),
 articulations:value.articulations.map(v=>({...v}))}));
};
score.endCmd=cancel=>{
 if(cancel){cancels++;
  if(controls.failCancel)throw Error('cancel outcome unknown');
  for(const value of saved){
   value.chord.notes=value.notes.map(([n,data])=>{
    Object.assign(n.data,data);return n;});
   value.chord.text=value.text;value.chord.lyrics=value.lyrics;
   value.chord.articulations=value.articulations;
  }
  if(controls.corruptRollback)first.notes[0].data.pitch++;
 }else{
  score.history.push('setNotePitches');
  if(score.postCommit)score.postCommit();
  if(score.throwAfterCommit)throw Error('commit outcome uncertain');
 }
};
plugin.cmd=()=>{throw Error('GLOBAL UNDO FORBIDDEN');};
params.expected=Passage.describeEvent(plugin,chord,0,0,480,0);
const second={measure:1,staff:0,voice:0,offset:{numerator:0,denominator:1},
 noteIndex:0,pitch:72,tpc:14,expected:Passage.describeEvent(plugin,first,0,0,0,0)};
const batch={edits:[params,second]};
function batchRun(){
 const reply=Batch.setNotePitches(plugin,batch);
 let actual=null;try{actual=[Passage.describeEvent(plugin,chord,0,0,480,0),
 Passage.describeEvent(plugin,first,0,0,0,0)];}catch(error){}
 return {reply,actual,applications,starts,cancels,history:score.history,
  cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]};
}
"""


def _execute(
    expression: str = "", *, output: str = "batchRun()", batch_source: str | None = None
) -> Any:
    declarations = []
    for name, path in [
        ("passage", "passage.js"),
        ("pitch", "pitch.js"),
        ("batch", "pitch-batch.js"),
        ("receipts", "receipts.js"),
    ]:
        text = (
            batch_source
            if name == "batch" and batch_source is not None
            else (_PLUGIN / path).read_text()
        )
        source = "\n".join(
            line for line in text.splitlines() if not line.startswith(".")
        )
        declarations.append(f"const {name}Source=" + json.dumps(source) + ";")
    result = subprocess.run(
        [
            _NODE or "node",
            "-e",
            "\n".join(declarations)
            + _GRAPH
            + _SETUP
            + "{\n"
            + expression
            + "\nconsole.log(JSON.stringify("
            + output
            + "));\n}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_native_targets_use_one_command_and_preserve_metadata() -> None:
    response = _execute("controls.reorder=true;")
    result = PitchBatchEdit.model_validate(
        _snake_case_keys(response["reply"]["result"])
    )
    assert (
        result.mutation_status == "applied" and result.snapshot_scope == "target_chords"
    )
    assert [note.name for note in result.edits[0].after.notes] == ["E4", "G4"]
    assert result.edits[0].note_index_after == 1
    assert result.edits[1].after.notes[0].name == "C5"
    assert all(edit.before.lyrics == edit.after.lyrics for edit in result.edits)
    assert response["starts"] == 1 and response["cancels"] == 0
    assert response["history"] == ["prior user change", "setNotePitches"]
    assert response["cursor"] == [7, 0, 2]


@pytest.mark.parametrize(
    "change",
    [
        "second.expected.notes[0].pitch++;",
        "first.notes[0].tieForward={};",
        "second.pitch=73;second.tpc=14;",
        "second.noteIndex=4;",
        "batch.edits=Array(33).fill(params);",
    ],
)
def test_final_target_semantic_failure_has_zero_writes_and_commands(
    change: str,
) -> None:
    response = _execute(change)
    assert response["reply"]["mutation_status"] == "not_applied"
    assert response["starts"] == response["applications"] == response["cancels"] == 0
    assert response["history"] == ["prior user change"]


@pytest.mark.parametrize("note_index", [0, 1])
def test_equivalent_onsets_same_chord_are_rejected_even_for_different_tones(
    note_index: int,
) -> None:
    response = _execute(
        "batch.edits[1]={...params,offset:{numerator:2,denominator:8},"
        "noteIndex:" + str(note_index) + "};"
    )
    assert response["reply"]["code"] == "duplicate_chord"
    assert response["starts"] == response["applications"] == 0


def test_all_noops_create_zero_commands_and_mixed_batch_reports_each_target() -> None:
    noop = _execute("params.pitch=60;params.tpc=14;second.pitch=71;second.tpc=19;")
    assert noop["reply"]["result"]["mutationStatus"] == "unchanged"
    assert noop["starts"] == noop["applications"] == 0
    mixed = _execute("second.pitch=71;second.tpc=19;")
    assert mixed["starts"] == 1 and mixed["applications"] == 1
    assert [edit["mutationStatus"] for edit in mixed["reply"]["result"]["edits"]] == [
        "applied",
        "unchanged",
    ]


@pytest.mark.parametrize("fault", ["setter", "lyric", "articulation", "peer"])
def test_midbatch_fault_cancels_only_open_command_and_verifies_every_exposed_target(
    fault: str,
) -> None:
    setup = {
        "setter": "if(applications===2)controls.throwTpc2=true;apply(plan);",
        "lyric": "apply(plan);if(applications===2)chord.lyrics[0].text='lost lyric';",
        "articulation": "apply(plan);if(applications===2)"
        "chord.articulations=[{symbol:999}];",
        "peer": "apply(plan);if(applications===2)"
        "chord.notes.find(n=>n.pitch===64).data.pitch++;",
    }[fault]
    response = _execute("Pitch.applyPitchPlan=plan=>{applications++;" + setup + "};")
    assert response["reply"]["mutation_status"] == "rolled_back"
    assert response["reply"]["rollback_verified"] is True
    assert response["reply"]["actual"] == response["reply"]["before"]
    assert response["starts"] == response["cancels"] == 1
    assert response["history"] == ["prior user change"]


@pytest.mark.parametrize(
    "restore_fault", ["controls.failCancel=true;", "controls.corruptRollback=true;"]
)
def test_uncertain_or_incomplete_restore_never_claims_rollback(
    restore_fault: str,
) -> None:
    response = _execute(
        restore_fault + "Pitch.applyPitchPlan=plan=>{applications++;"
        "if(applications===2)throw Error('midbatch');apply(plan);};"
    )
    assert response["reply"]["mutation_status"] == "unknown"
    assert response["reply"]["rollback_verified"] is False
    assert response["cancels"] == 1 and response["history"] == ["prior user change"]


@pytest.mark.parametrize(
    ("fault", "status"),
    [
        ("score.throwAfterCommit=true;", "unknown"),
        ("score.postCommit=()=>{first.notes[0].data.pitch++;};", "applied_unverified"),
        (
            "score.postCommit=()=>{Passage.describeEvent=()=>{"
            "throw Error('permanent read failure');};};",
            "applied_unverified",
        ),
    ],
)
def test_commit_boundary_never_cancels_closed_command_or_loses_known_commit_status(
    fault: str, status: str
) -> None:
    response = _execute(fault)
    assert response["reply"]["mutation_status"] == status
    assert response["cancels"] == 0
    assert response["history"] == ["prior user change", "setNotePitches"]


def test_receipted_batch_records_actual_command_and_conflicts_across_kinds() -> None:
    result = _execute(
        r"""
const Receipts=vm.runInNewContext(receiptsSource+'\nthis;');
plugin.receiptState=Receipts.newState();
plugin.commands={setNotePitch:Pitch.setNotePitch,setNotePitches:Batch.setNotePitches};
const operation={receiptVersion:1,operationId:'batch-op',
 expectedPluginSession:plugin.receiptState.sessionId,
 mutation:{kind:'set_note_pitches',request:batch}};
const first=Receipts.executeMutationOnce(plugin,operation);
const duplicate=Receipts.executeMutationOnce(plugin,operation);
operation.mutation={kind:'set_note_pitch',request:params};
const conflict=Receipts.executeMutationOnce(plugin,operation);
""",
        output="({first,duplicate,conflict,applications,starts,history:score.history})",
    )
    assert result["first"]["result"]["command"] == "setNotePitches"
    assert result["duplicate"]["result"]["cached"] is True
    assert (
        result["duplicate"]["result"]["originalReply"]
        == result["first"]["result"]["originalReply"]
    )
    assert result["conflict"]["code"] == "operation_id_conflict"
    assert result["applications"] == 2 and result["starts"] == 1


def test_targets_are_checked_together_again_before_opening_any_command() -> None:
    response = _execute(
        "const prepare=Pitch.planPitchEdit;let prepared=0;"
        "Pitch.planPitchEdit=(p,args)=>{const result=prepare(p,args);"
        "if(++prepared===2)chord.notes[1].data.pitch++;return result;};"
    )
    assert response["reply"]["code"] == "preflight_changed"
    assert response["reply"]["mutation_status"] == "not_applied"
    assert response["starts"] == response["applications"] == response["cancels"] == 0
    assert response["actual"][0]["notes"][1]["pitch"] == 65
