"""Actual JS selection reads against native-shaped wrapper identities and lists."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.current_selection import CurrentSelection
from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.results import Duration

_PLUGIN = Path(__file__).parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node is needed for plugin JS tests"
)
_GRAPH = r"""
const vm=require('node:vm');
const Constants={ticksPerWholeNote:1920,voicesPerStaff:4};
const Score={requireScore:p=>p.curScore?null:{error:'No score'}};
const Passage=vm.runInNewContext(passageSource+'\nthis;',{Constants,Score});
const CurrentSelection=vm.runInNewContext(selectionSource+'\nthis;',
 {Constants,Score,Passage});
function native(id,data){return Object.assign({id,
 is(other){return !!other&&other.id===this.id;}},data);}
function chord(id,pitches,track){
 const result=native(id,{type:93,track,parent:null,
 duration:{numerator:1,denominator:4},actualDuration:{numerator:1,denominator:4},
 lyrics:[{text:'Hel',verse:0,syllabic:1,placement:1}],
 articulations:[{symbol:123,subtypeName:()=> 'Staccato',placement:0}],notes:[]});
 result.notes=pitches.map(([pitch,tpc],index)=>native(id+'n'+index,
 {type:21,track,parent:result,pitch,tpc1:tpc,tpc2:tpc,tpc,_name:()=> 'Note'}));
 return result;
}
const melody=chord('melody',[[71,19]],0);
const chord2=chord('chord',[[60,14],[64,18]],1);
const bass=chord('bass',[[43,15]],12);
const rest=native('rest',{type:25,track:4,parent:null,lyrics:[],
 duration:{numerator:1,denominator:1},actualDuration:{numerator:1,denominator:1}});
const first=native('m1',{type:100,nextMeasure:null,firstSegment:null});
const second=native('m2',{type:100,nextMeasure:null,firstSegment:null});
first.nextMeasure=second;
const seg1=native('s1',{type:99,tick:0,parent:first,
 elementAt:t=>({0:melody,4:rest}[t]||null)});
const seg2=native('s2',{type:99,tick:480,parent:first,elementAt:t=>t===1?chord2:null});
const seg3=native('s3',{type:99,tick:1920,parent:second,elementAt:t=>t===12?bass:null});
first.firstSegment=seg1;second.firstSegment=seg3;
melody.parent=seg1;rest.parent=seg1;chord2.parent=seg2;bass.parent=seg3;
const lyric=native('lyric',{type:101,track:1,parent:chord2,text:'Hel'});
const opaque=native('opaque',{type:999,track:-1,parent:null,
 text:'Title',_name:()=> 'Text'});
const selection={isRange:false,elements:[],startSegment:seg1,endSegment:seg3,
 startStaff:0,endStaff:4,
 select(){throw Error('selection write');},selectRange(){throw Error('range write');},
 clear(){throw Error('clear write');}};
const plugin={Element:{NOTE:21,CHORD:93,REST:25,SEGMENT:99},
 Lyrics:{SINGLE:0,BEGIN:1,END:2,MIDDLE:3},Placement:{ABOVE:0,BELOW:1},
 curScore:{selection,nstaves:4,firstMeasure:first,
 startCmd(){throw Error('undo write');},endCmd(){throw Error('undo write');},
 newCursor(){throw Error('cursor created');}},
 cursorMeasure:17,cursorStaff:3,cursorVoice:2,cmd(){throw Error('UI command');}};
function read(){return CurrentSelection.readCurrentSelection(plugin,{});}
"""


def _execute(expression: str) -> Any:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / path).read_text().splitlines()
            if not line.startswith(".")
        )
        for name, path in [
            ("passage", "passage.js"),
            ("selection", "current-selection.js"),
        ]
    }
    script = (
        "const passageSource=" + json.dumps(sources["passage"]) + ";\n"
        "const selectionSource="
        + json.dumps(sources["selection"])
        + ";\n"
        + _GRAPH
        + expression
        + "\nconsole.log(JSON.stringify(read()));"
    )
    completed = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def _read(expression: str) -> CurrentSelection:
    return CurrentSelection.model_validate(
        _snake_case_keys(_execute(expression)["result"])
    )


def test_single_selected_tone_has_full_chord_context_but_only_one_selected_note() -> (
    None
):
    result = _read(
        "selection.elements=[Object.assign({},chord2.notes[1])];"
        "const seen=new Set();function freeze(value){"
        "if(!value||typeof value!=='object'||seen.has(value))return;seen.add(value);"
        "Object.values(value).forEach(freeze);Object.freeze(value);}freeze(plugin);"
    )
    assert result.kind == "list" and result.range is None
    assert len(result.elements) == 1
    item = result.elements[0]
    assert item.kind == "note" and item.note_index == 1
    assert item.note is not None and item.note.name == "E4"
    assert item.measure == 1 and item.track == 1
    assert item.event_context is not None
    assert [note.name for note in item.event_context.notes] == ["C4", "E4"]
    assert item.event_context.offset == Duration(numerator=1, denominator=4)
    assert item.event_context.lyrics is not None
    assert item.event_context.lyrics[0].text == "Hel"
    assert item.event_context.articulations is not None
    assert item.event_context.articulations[0].symbol == 123
    assert result.observed_voices == [1]


def test_range_returns_only_filtered_native_entries_without_expanding_bounds() -> None:
    result = _read("selection.isRange=true;selection.elements=[chord2.notes[1]];")
    assert result.kind == "range" and result.range is not None
    assert result.range.start_tick == 0 and result.range.end_tick == 1920
    assert result.range.start_staff == 0 and result.range.end_staff_exclusive == 4
    assert len(result.elements) == 1 and result.elements[0].note_index == 1
    assert result.filter_state == "not_exposed" and result.observed_voices == [1]


def test_empty_range_is_not_misreported_as_no_selection_and_null_end_is_score_end() -> (
    None
):
    result = _read("selection.isRange=true;selection.endSegment=null;")
    assert result.kind == "range" and result.elements == []
    assert result.range is not None and result.range.end_tick is None


def test_empty_native_list_does_not_read_or_report_the_bridge_cursor() -> None:
    result = _read(
        "Object.defineProperty(plugin.curScore,'firstMeasure',"
        "{get:()=>{throw Error('unnecessary traversal');}});"
    )
    assert result.kind == "none" and result.elements == [] and result.range is None
    assert result.observed_voices == [] and result.elements_complete


def test_whole_chord_rest_and_opaque_items_preserve_native_list_membership() -> None:
    result = _read("selection.elements=[chord2,rest,opaque,lyric,bass.notes[0]];")
    assert [item.kind for item in result.elements] == [
        "chord",
        "rest",
        "other",
        "other",
        "note",
    ]
    chord, rest, opaque, lyric, bass = result.elements
    assert chord.note is None and chord.note_index is None
    assert chord.event_context is not None and len(chord.event_context.notes) == 2
    assert rest.event_context is not None and rest.event_context.kind == "rest"
    assert (
        opaque.measure is None and opaque.event_context is None and opaque.track is None
    )
    assert opaque.text == "Title"
    assert lyric.measure == 1 and lyric.event_context is not None
    assert lyric.event_context.kind == "other" and lyric.text == "Hel"
    assert bass.measure == 2 and bass.event_context is not None
    assert bass.event_context.offset == Duration(numerator=0, denominator=1)


def test_grace_note_never_aliases_main_chord_onset() -> None:
    result = _read(
        "const grace=chord('grace',[[60,14]],1);grace.parent=chord2;"
        "selection.elements=[grace.notes[0]];"
    )
    assert result.elements[0].kind == "note"
    assert result.elements[0].event_context is None and result.elements[0].note is None
    assert result.elements[0].measure is None
    assert any("grace-note" in item for item in result.unsupported)


@pytest.mark.parametrize(
    "expression",
    [
        "plugin.curScore=null;",
        "plugin.curScore.selection=null;",
        "selection.elements={};",
        "selection.elements=Array(1001).fill(melody.notes[0]);",
        "selection.elements=[bass.notes[0]];CurrentSelection.maxMeasuresScanned=1;",
        "selection.isRange=true;selection.startSegment=null;",
    ],
)
def test_missing_native_state_and_limits_fail_without_partial_results(
    expression: str,
) -> None:
    reply = _execute(expression)
    assert "error" in reply and "result" not in reply
