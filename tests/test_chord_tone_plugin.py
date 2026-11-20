"""Execute actual native chord-topology handlers with adoption/deletion faults."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from mcp_score.bridge.chord_tone import ChordToneEdit
from mcp_score.bridge.musescore import _snake_case_keys
from tests.test_pitch_plugin import _GRAPH, _NODE, _PLUGIN_DIR

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node required for plugin tests")

_TOPOLOGY = r"""
const ChordToneFixture=vm.runInNewContext(fixtureSource+'\nthis;',{});
const ChordTone=vm.runInNewContext(chordToneSource+'\nthis;',
    {Pitch,Passage,Score,ChordToneFixture});
score.title='Guarded pitch fixture';
const Receipts=vm.runInNewContext(receiptSource+'\nthis;',{});
let creations=0,adds=0,removes=0;
Object.assign(controls,{ignoreAdd:false,throwAdd:false,ignoreRemove:false,throwRemove:false,
    damage:false,cancelDamage:false,cancelThrow:false});
function ordinary(value){
    Object.assign(value,{elements:[],spannerForward:[],spannerBack:[]});return value;}
chord.notes.forEach(ordinary);
chord.lyrics=[{text:'keep',verse:0,syllabic:0,placement:1}];
chord.articulations=[{symbol:12,subtypeName:()=> 'staccato',placement:0,
    articulationAnchor:0,direction:0}];
const deleted=new Set();
function guarded(value){
    return new Proxy(value,{get(target,key,receiver){
        if(deleted.has(receiver))throw Error('DELETED WRAPPER ACCESS '+String(key));
        return Reflect.get(target,key,receiver);
    }});
}
chord.notes=chord.notes.map(guarded);
plugin.Element.NOTE=22;
plugin.newElement=function(type){creations++;
    if(type!==22)throw Error('wrong factory type');
    return ordinary(note(48,14));};
chord.add=function(value){adds++;if(controls.ignoreAdd)return;
    value.data.tpc1=14;value.data.tpc2=14; // Native adoption overwrites spelling.
    chord.notes.push(value);chord.notes.sort((a,b)=>a.pitch-b.pitch);
    if(controls.damage)chord.lyrics[0].text='lost';
    if(controls.throwAdd)throw Error('fault after real adoption');};
chord.remove=function(value){removes++;if(controls.ignoreRemove)return;
    chord.notes=chord.notes.filter(n=>n!==value);deleted.add(value);
    if(controls.damage)chord.articulations=[];
    if(controls.throwRemove)throw Error('fault after real deletion');};
score.startCmd=function(){starts++;saved={notes:chord.notes.map(n=>({...n.data})),
    lyrics:chord.lyrics.map(x=>({...x})),articulations:chord.articulations.slice()};};
score.endCmd=function(cancel){if(cancel){cancels++;
    if(controls.cancelThrow)throw Error('cancel failed');
    chord.notes=saved.notes.map(data=>guarded(ordinary(note(data.pitch,data.tpc1,data.tpc2))));
    chord.lyrics=saved.lyrics;chord.articulations=saved.articulations;
    if(controls.cancelDamage)chord.notes[0].data.pitch++;
} else {this.history.push('chord-tone');
    if(this.postCommit)this.postCommit();
    if(this.throwAfterCommit)throw Error('commit uncertain');}};
plugin.cmd=()=>{throw Error('GLOBAL UNDO FORBIDDEN');};
params.expected=snapshot();
params.pitch=67;params.tpc=15;
let operation='add';
function toneRun(){const before=snapshot();const reply=operation==='add'
    ?ChordTone.addChordTone(plugin,params):ChordTone.removeChordTone(plugin,params);
    let actual=null;try{actual=snapshot();}catch(unavailable){}
    return {reply,before,actual,starts,cancels,creations,adds,removes,
        history:score.history,cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]};}
plugin.receiptState=Receipts.newState();
plugin.commands={addChordTone:ChordTone.addChordTone,removeChordTone:ChordTone.removeChordTone};
"""


def _execute(
    change: str = "", output: str = "toneRun()", *, module_source: str | None = None
) -> Any:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN_DIR / filename).read_text().splitlines()
            if not line.startswith(".")
        )
        for name, filename in [
            ("passageSource", "passage.js"),
            ("pitchSource", "pitch.js"),
            ("chordToneSource", "chord-tone.js"),
            ("receiptSource", "receipts.js"),
        ]
    }
    fixture_path = (
        _PLUGIN_DIR.parents[3] / "tests/integration/plugin/chord-tone-fixture.js"
    )
    sources["fixtureSource"] = "\n".join(
        line
        for line in fixture_path.read_text().splitlines()
        if not line.startswith(".")
    )
    if module_source is not None:
        sources["chordToneSource"] = "\n".join(
            line for line in module_source.splitlines() if not line.startswith(".")
        )
    script = "\n".join(
        f"const {key}={json.dumps(value)};" for key, value in sources.items()
    )
    script += _GRAPH + _TOPOLOGY + change + f"\nconsole.log(JSON.stringify({output}));"
    result = subprocess.run(
        [_NODE or "node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_topology_changes_only_intended_tone_and_keeps_metadata_one_command(
    operation: str,
) -> None:
    observed = _execute(f"operation={json.dumps(operation)};")
    result = ChordToneEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.operation == operation and result.verified
    assert result.before.lyrics == result.after.lyrics
    assert result.before.articulations == result.after.articulations
    assert result.before.duration == result.after.duration
    assert [n.pitch for n in result.after.notes] == (
        [60, 64, 67] if operation == "add" else [64]
    )
    assert observed["starts"] == 1 and observed["cancels"] == 0
    assert observed["cursor"] == [7, 0, 2]
    assert observed["history"] == ["prior user change", "chord-tone"]


@pytest.mark.parametrize("delta", [0, 2])
@pytest.mark.parametrize("display", [True, False])
def test_addition_adoption_then_explicit_concert_written_spelling(
    delta: int, display: bool
) -> None:
    observed = _execute(
        f"displayConcert={str(display).lower()};"
        f"chord.notes.forEach(n=>n.data.tpc2=n.tpc1+{delta});params.expected=snapshot();"
        "params.pitch=61;params.tpc=21;"
    )
    added = next(
        n for n in observed["reply"]["result"]["after"]["notes"] if n["pitch"] == 61
    )
    assert added["tpc"] == 21 and added["writtenTpc"] == 21 + delta
    assert added["displayedTpc"] == (21 if display else 21 + delta)
    assert added["name"] == "C#4"


@pytest.mark.parametrize(
    "change",
    [
        "params.pitch=60;params.tpc=14;",
        "params.pitch=true;",
        "params.tpc=14;",
        "params.offset.numerator=9007199254740992;",
        "params.expected.notes[1].pitch++;",
        "delete params.expected.lyrics;",
        "params.expected.lyrics[0].text='stale';",
        "params.expected.articulations[0].symbol++;",
        "chord.notes[0].tieForward={};",
        "chord.tuplet={};",
        "chord.graceNotes=[{}];",
        "part.hasTabStaff=true;",
        "chord.notes[0].tuning=1;",
        "chord.notes[0].accidental={subtype:25};",
        "chord.notes[1].data.tpc2++;params.expected=snapshot();",
        "operation='remove';chord.notes.length=1;params.expected=snapshot();",
        "operation='remove';params.noteIndex=2;",
        "operation='remove';chord.notes[0].elements=[{}];",
        "operation='remove';chord.notes[0].spannerForward=[{}];",
        "operation='remove';chord.notes[0].spannerBack=[{}];",
        "operation='remove';delete chord.notes[0].spannerForward;",
        "operation='remove';delete chord.notes[0].elements;",
    ],
)
def test_unsafe_preconditions_never_open_a_command(change: str) -> None:
    observed = _execute(change)
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert (
        observed["starts"]
        == observed["cancels"]
        == observed["creations"]
        == observed["adds"]
        == observed["removes"]
        == 0
    )
    assert observed["before"] == observed["actual"]
    assert observed["history"] == ["prior user change"]


@pytest.mark.parametrize(
    "operation,control",
    [
        ("add", "ignoreAdd"),
        ("add", "throwAdd"),
        ("add", "throwTpc2"),
        ("add", "damage"),
        ("remove", "ignoreRemove"),
        ("remove", "throwRemove"),
        ("remove", "damage"),
    ],
)
def test_faults_restore_full_target_using_fresh_wrappers(
    operation: str, control: str
) -> None:
    observed = _execute(f"operation='{operation}';controls.{control}=true;")
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["reply"]["rollback_verified"] is True
    assert observed["before"] == observed["actual"]
    assert observed["cancels"] == 1 and observed["history"] == ["prior user change"]
    assert "DELETED WRAPPER ACCESS" not in observed["reply"]["error"]


@pytest.mark.parametrize("control", ["cancelThrow", "cancelDamage"])
def test_incomplete_cancel_remains_unknown(control: str) -> None:
    observed = _execute(
        f"operation='remove';controls.throwRemove=true;controls.{control}=true;"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "score.postCommit=()=>{chord.lyrics[0].text='lost';};",
        "score.postCommit=()=>{Passage.describeEvent=()=>{"
        "throw Error('read unavailable');};"
        "};",
    ],
)
def test_known_committed_failure_stays_applied_unverified_without_cancel(
    change: str,
) -> None:
    observed = _execute(change)
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0 and observed["history"] == [
        "prior user change",
        "chord-tone",
    ]


def test_commit_attempt_uncertainty_revokes_cancellation_authority() -> None:
    observed = _execute("score.throwAfterCommit=true;")
    assert (
        observed["reply"]["mutation_status"] == "unknown" and observed["cancels"] == 0
    )


def test_metadata_object_key_order_is_irrelevant_but_array_order_is_not() -> None:
    observed = _execute(
        "params.expected.lyrics[0]=Object.fromEntries(Object.entries(params.expected.lyrics[0]).reverse());"
    )
    assert observed["reply"]["result"]["verified"] is True


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_receipts_preserve_actual_command_and_historical_structural_result(
    operation: str,
) -> None:
    observed = _execute(
        f"operation='{operation}';",
        r"""(()=>{
        const request={receiptVersion:1,operationId:'retained',
            expectedPluginSession:plugin.receiptState.sessionId,
            mutation:{kind:operation+'_chord_tone',request:params}};
        const first=Receipts.executeMutationOnce(plugin,request);
        const second=Receipts.executeMutationOnce(plugin,request);
        request.mutation.kind=operation==='add'?'remove_chord_tone':'add_chord_tone';
        const conflict=Receipts.executeMutationOnce(plugin,request);
        return {first,second,conflict,adds,removes,starts};})()""",
    )
    assert observed["first"]["result"]["command"] == operation + "ChordTone"
    assert (
        observed["first"]["result"]["originalReply"]
        == observed["second"]["result"]["originalReply"]
    )
    assert observed["second"]["result"]["cached"] is True
    assert observed["conflict"]["code"] == "operation_id_conflict"
    assert observed["starts"] == 1 and observed["adds"] + observed["removes"] == 1
