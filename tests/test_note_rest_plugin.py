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


def _run(change: str = "") -> Any:
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        for name in _MODULES
    }
    script = (
        "const names="
        + json.dumps(_MODULES)
        + ";const sources="
        + json.dumps(sources)
        + ";"
        + _GRAPH
        + change
        + "\nconsole.log(JSON.stringify(run()));"
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
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
