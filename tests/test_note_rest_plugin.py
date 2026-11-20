"""Run the real replacement/import graph, including native rewrite failures."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.note_rest import NoteRestEdit

_PLUGIN = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node required for plugin graph")
_MODULES = ["Constants", "Score", "Passage", "Pitch", "Duration", "Context", "Rhythm"]
_GRAPH = Path(__file__).with_name("note_rest_graph.js").read_text()


def _run(change: str = "", *, module_source: str | None = None) -> Any:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        for name in _MODULES
    }
    names = _MODULES.copy()
    if module_source is not None:
        sources["Rhythm"] = "\n".join(
            line for line in module_source.splitlines() if not line.startswith(".")
        )
        names.insert(-1, "NoteRestFixture")
        sources["NoteRestFixture"] = "\n".join(
            line
            for line in (
                Path(__file__).parent / "integration/plugin/note-rest-fixture.js"
            )
            .read_text()
            .splitlines()
            if not line.startswith(".")
        )
    script = (
        "const names="
        + json.dumps(names)
        + ";const sources="
        + json.dumps(sources)
        + ";"
        + _GRAPH
        + change
        + "\nconsole.log(JSON.stringify(run()));"
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("concert", [True, False])
@pytest.mark.parametrize("duration", [1920, 960, 480, 240, 120, 60])
def test_fill_full_measure_rest_verifies_native_pitch_spelling_and_exact_suffix(
    concert: bool,
    duration: int,
) -> None:
    observed = _run(
        f"concert={json.dumps(concert)};params.duration=frac({duration},1920);"
    )
    result = NoteRestEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    note = result.after[0].notes[0]
    assert (note.pitch, note.tpc, note.written_tpc) == (61, 21, 23)
    assert note.displayed_tpc == (21 if concert else 23)
    assert [e for e in result.after if e.staff == 1] == [
        e for e in result.before if e.staff == 1
    ]
    assert observed["history"] == ["prior", "edit"]
    assert observed["cursor"] == [2, 1, 3]


def test_delete_single_note_with_equal_duration_rest_without_peer_change() -> None:
    observed = _run(
        "current=chord(1920);rebuild();params.expected=snapshot();fill=false;"
    )
    result = NoteRestEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.after[0].kind == "rest" and not result.after[0].notes
    assert result.after[0].actual_duration == result.before[0].actual_duration
    assert [e for e in result.after if e.staff == 1] == [
        e for e in result.before if e.staff == 1
    ]


@pytest.mark.parametrize(
    "change",
    [
        "plugin.mscoreMinorVersion=4;",
        "plugin.mscoreMinorVersion=6;",
        "delete plugin.Segment;",
        "delete score.spanners;",
    ],
)
def test_older_or_incomplete_api_rejects_before_mutation(change: str) -> None:
    observed = _run(change)
    assert observed["reply"]["code"] == "unsupported_native_api"
    assert observed["starts"] == observed["writes"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "params.expected=[];",
        "params.expected[0].actualDuration=frac(1,2);",
        "current.lyrics=[{text:'new'}];",
        "params.expected[0].notes=[{}];",
    ],
)
def test_stale_complete_measure_guard_never_opens_undo(change: str) -> None:
    observed = _run(change)
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "current.tuplet={};",
        "current.elements=[{}];",
        "current.visible=false;",
        "current.gap=true;",
        "delete current.generated;",
        "current.staffMove=1;",
        "current.beam={};",
        "part.hasDrumStaff=true;",
        "part.hasTabStaff=true;",
        "staff.timeStretch=()=>frac(2,1);",
        "staff.transpose=()=>null;",
        "score.style.value=()=>null;",
        "interval.chromatic=999999999999999;",
        "params.pitch=60;",
        "params.tpc=33;",
        "controls.badCursor=true;",
        "measure.timesigActual=frac(3,4);",
        "measure.timesigNominal=frac(2,2);",
        "segments[0].annotations=[{track:0}];",
        "score.spanners=[{}];",
    ],
)
def test_unsupported_or_unknown_target_rejects_before_write(change: str) -> None:
    observed = _run(change + "params.expected=snapshot();")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == observed["writes"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "current.notes.push(tone(64,18,20));",
        "current.notes[0].tuning=1;",
        "current.notes[0].accidentalType=999;",
        "current.notes[0].tieForward={};",
        "current.lyrics=[{}];",
        "current.articulations=[{}];",
        "current.beam={};",
        "current.duration=frac(3,4);current.actualDuration=frac(3,4);",
    ],
)
def test_deletion_rejects_chords_attachments_microtonality_and_dots(
    change: str,
) -> None:
    observed = _run(
        "current=chord(1920);fill=false;"
        + change
        + "rebuild();params.expected=snapshot();"
    )
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == 0


@pytest.mark.parametrize("control", ["ignore", "failWrite", "damage"])
@pytest.mark.parametrize("fill", [True, False])
def test_failed_write_cancels_only_known_open_scope_and_verifies_restoration(
    control: str,
    fill: bool,
) -> None:
    setup = (
        ""
        if fill
        else "current=chord(1920);rebuild();params.expected=snapshot();fill=false;"
    )
    observed = _run(setup + f"controls.{control}=true;")
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["history"] == ["prior"] and observed["cancels"] == 1


def test_postcommit_read_failure_preserves_known_applied_status_without_undo() -> None:
    observed = _run(
        "controls.postCommit=()=>{Passage.readPassage=()=>{"
        "throw new Error('read unavailable');};};"
    )
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0 and observed["history"] == ["prior", "edit"]


def test_commit_exception_cannot_cancel_an_unrelated_command() -> None:
    observed = _run(
        "const end=score.endCmd;score.endCmd=cancel=>{end(cancel);"
        "if(!cancel)throw Error('unknown commit');};"
    )
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 0


def test_adjacent_measure_damage_triggers_known_scope_cancellation() -> None:
    observed = _run(
        "const input=score.newCursor;score.newCursor=()=>{const c=input();"
        "const add=c.addNote;c.addNote=p=>{add.call(c,p);"
        "nextPeer.notes[0].pitch++;};return c;};"
    )
    # Cancellation deliberately cannot restore this simulated external damage.
    assert observed["reply"]["mutation_status"] == "unknown"
    assert observed["cancels"] == 1


def test_multiple_native_suffix_rests_are_verified_as_exact_interval_union() -> None:
    observed = _run(
        "const input=score.newCursor;score.newCursor=()=>{const c=input();"
        "const add=c.addNote;c.addNote=p=>{add.call(c,p);"
        "suffix=[{tick:480,element:rest(480)},{tick:960,element:rest(960)}];"
        "rebuild();};return c;};"
    )
    assert observed["reply"]["result"]["verified"] is True
    assert len(observed["reply"]["result"]["after"]) == 4


@pytest.mark.parametrize(
    "damage",
    [
        "suffix[0].tick++;",
        "suffix[0].element.elements=[{}];",
        "suffix[0].element.visible=false;",
        "suffix[0].element.actualDuration=frac(1,2);",
    ],
)
def test_suffix_gap_duration_or_hidden_attachment_cannot_commit(damage: str) -> None:
    observed = _run(
        "const input=score.newCursor;score.newCursor=()=>{const c=input();"
        "const add=c.addNote;c.addNote=p=>{add.call(c,p);"
        + damage
        + "rebuild();};return c;};"
    )
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["history"] == ["prior"]


def test_distant_spanner_preserved_and_intersecting_spanner_rejected() -> None:
    span = (
        "score.spanners=[{type:1,track:0,spannerTrack2:0,anchor:0,"
        "spannerTick:frac(3,1),spannerTicks:frac(1,2)}];"
    )
    assert _run(span)["reply"]["result"]["verified"] is True
    rejected = _run(span + "score.spanners[0].spannerTick=frac(1,4);")
    assert rejected["reply"]["mutation_status"] == "not_applied"
    assert rejected["starts"] == 0


def test_octave_transposing_rest_fill_keeps_requested_concert_midi() -> None:
    observed = _run("interval.chromatic=12;interval.diatonic=7;")
    result = NoteRestEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    note = result.after[0].notes[0]
    assert (note.pitch, note.tpc, note.written_tpc) == (61, 21, 21)


@pytest.mark.parametrize(
    "stretch",
    [
        "{}",
        "{numerator:1}",
        "{denominator:1}",
        "{numerator:0,denominator:0}",
        "{numerator:-1,denominator:-1}",
        "{numerator:1.5,denominator:1.5}",
        "{numerator:'1',denominator:'1'}",
        "{numerator:true,denominator:true}",
        "{numerator:NaN,denominator:NaN}",
        "{numerator:Infinity,denominator:Infinity}",
        "{numerator:9007199254740992,denominator:9007199254740992}",
    ],
)
def test_malformed_native_unit_stretch_cannot_open_mutation(stretch: str) -> None:
    observed = _run("staff.timeStretch=()=> (" + stretch + ");")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == observed["writes"] == observed["cancels"] == 0
    assert observed["history"] == ["prior"]


@pytest.mark.parametrize(
    "spans",
    [
        "{}",
        "{length:-1}",
        "{length:'0'}",
        "{length:0.5}",
        "{length:false}",
        "{length:Infinity}",
        "{length:NaN}",
        "{length:9007199254740992}",
    ],
)
def test_unknown_native_spanner_list_never_proves_empty(spans: str) -> None:
    observed = _run("score.spanners=" + spans + ";")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == observed["writes"] == 0
    assert observed["history"] == ["prior"]


def test_malformed_postcommit_spanner_list_retains_applied_unverified_status() -> None:
    observed = _run("controls.postCommit=()=>{score.spanners={};};")
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0 and observed["history"] == ["prior", "edit"]


@pytest.mark.parametrize("earlier_length", ["frac(1,2)", "null", "{}", "frac(0,1)"])
def test_earlier_same_voice_overlap_or_unknown_duration_rejects_before_command(
    earlier_length: str,
) -> None:
    observed = _run(
        "current=rest(480);const earlier=chord(960);"
        "earlier.actualDuration=" + earlier_length + ";"
        "const first=segment(0,earlier,peer);const target=segment(480,current);"
        "first.nextInMeasure=target;segments=[first,target];measure.firstSegment=first;"
        "params.offset=frac(1,4);params.expected=snapshot();"
    )
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == observed["writes"] == 0
    assert observed["history"] == ["prior"]
