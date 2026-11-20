"""Exercise the actual traversal module against a small in-memory score graph.

The real MuseScore integration tests validate the QML/API boundary. These
Node-backed tests cover traversal edge cases without launching an application.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.musescore import _snake_case_keys
from mcp_score.bridge.passage import Passage
from mcp_score.bridge.results import Duration, TimeSignature

_PLUGIN_PATH = (
    Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin/passage.js"
)
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node is needed for plugin JS tests"
)

# Two measures: a descending melody, a simultaneous chord in voice 2,
# a full-measure rest on another staff, and an empty position before voice 2.
_SCORE_GRAPH = r"""
const Constants = { ticksPerWholeNote: 1920, voicesPerStaff: 4 };
const Score = { requireScore: plugin => plugin.curScore ? null : {error: "No score"} };
const chordType = 93;
const restType = 25;
function chord(pitches, duration = {numerator:1, denominator:4}) {
    return {type:chordType, duration, actualDuration:duration,
        notes:pitches.map(([pitch,tpc]) => ({pitch,tpc,tpc1:tpc}))};
}
function segment(tick, elements, annotations = []) {
    return {tick, elementAt: track => elements[track] || null,
        annotations, nextInMeasure:null};
}
const segments = [
    segment(0, {0:chord([[71,19]]), 4:{type:restType,
        duration:{numerator:4,denominator:4},
        actualDuration:{numerator:4,denominator:4}}}),
    segment(480, {0:chord([[69,17]]), 1:chord([[60,14],[64,18]])}),
    segment(960, {0:chord([[67,15]])}),
    segment(1440, {0:chord([[65,13]])})
];
segments.forEach((value,index) => value.nextInMeasure = segments[index+1] || null);
const secondSegment = segment(1920, {0:chord([[59,7]])},
    [{type:126,track:0,text:"mf"}]);
const second = {firstSegment:secondSegment, timesigActual:{numerator:3,denominator:4},
    timesigNominal:{numerator:3,denominator:4},
    nextMeasure:null};
const first = {firstSegment:segments[0], timesigActual:{numerator:1,denominator:1},
    timesigNominal:{numerator:4,denominator:4},
    nextMeasure:second};
const plugin = {Element:{CHORD:chordType,REST:restType},
    curScore:{nstaves:2,firstMeasure:first},
    cursorMeasure:17,cursorStaff:1,cursorVoice:3};
"""


def _execute(expression: str) -> Any:
    source = "\n".join(
        line
        for line in _PLUGIN_PATH.read_text().splitlines()
        if not line.startswith(".")
    )
    script = _SCORE_GRAPH + source + "\n" + expression
    result = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_read_collects_all_notes_chords_rests_and_offsets_without_moving_cursor() -> (
    None
):
    reply = _execute(
        "const result=readPassage(plugin,{startMeasure:1,endMeasure:2});"
        "console.log(JSON.stringify({result,position:"
        "[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice]}));"
    )
    passage = Passage.model_validate(_snake_case_keys(reply["result"]["result"]))
    first = passage.measures[0]
    melody = [event for event in first.events if event.staff == 0 and event.voice == 0]
    assert [event.notes[0].name for event in melody] == ["B4", "A4", "G4", "F4"]
    assert [event.offset for event in melody] == [
        Duration(numerator=0, denominator=1),
        Duration(numerator=1, denominator=4),
        Duration(numerator=1, denominator=2),
        Duration(numerator=3, denominator=4),
    ]
    assert all(
        event.duration == Duration(numerator=1, denominator=4) for event in melody
    )
    chord = next(event for event in first.events if event.voice == 1)
    assert [note.name for note in chord.notes] == ["C4", "E4"]
    assert chord.offset == Duration(numerator=1, denominator=4)
    rest = next(event for event in first.events if event.staff == 1)
    assert rest.kind == "rest" and rest.actual_duration == Duration(
        numerator=1, denominator=1
    )
    assert first.actual_duration == Duration(numerator=1, denominator=1)
    assert first.time_signature == TimeSignature(numerator=4, denominator=4)
    assert passage.measures[1].events[0].notes[0].name == "Cb4"
    assert passage.measures[1].events[0].offset == Duration(numerator=0, denominator=1)
    assert passage.measures[1].events[1].text == "mf"
    assert passage.range_complete and passage.unsupported
    assert reply["position"] == [17, 1, 3]


def test_read_filters_staff_voice_and_measure_without_moving_start_offset() -> None:
    reply = _execute(
        "console.log(JSON.stringify(readPassage(plugin,"
        "{startMeasure:1,endMeasure:1,staff:0,voice:1})));"
    )
    passage = Passage.model_validate(_snake_case_keys(reply["result"]))
    assert passage.staves == [0] and passage.voices == [1]
    assert len(passage.measures[0].events) == 1
    assert passage.measures[0].events[0].tick == 480
    assert passage.measures[0].events[0].offset == Duration(numerator=1, denominator=4)


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"startMeasure": 1, "endMeasure": 3}, "Measure out of range"),
        ({"startMeasure": 0, "endMeasure": 1}, "Invalid inclusive"),
        ({"startMeasure": 1.5, "endMeasure": 2}, "Invalid inclusive"),
        ({"startMeasure": 1, "endMeasure": 65}, "measure limit"),
        ({"startMeasure": 1, "endMeasure": 1, "staff": 2}, "Staff out of range"),
        ({"startMeasure": 1, "endMeasure": 1, "voice": 4}, "Voice out of range"),
    ],
)
def test_read_rejects_invalid_or_oversize_requests_without_partial_result(
    params: dict[str, object], error: str
) -> None:
    reply = _execute(
        "console.log(JSON.stringify(readPassage(plugin," + json.dumps(params) + ")));"
    )
    assert error in reply["error"]
    assert "result" not in reply


def test_read_rejects_event_overflow_without_partial_result() -> None:
    reply = _execute(
        "maxEventsPerRead=1; console.log(JSON.stringify(readPassage(plugin,"
        "{startMeasure:1,endMeasure:1})));"
    )
    assert "event limit" in reply["error"]
    assert "result" not in reply


def test_pitch_names_preserve_enharmonic_octaves_and_doubled_accidentals() -> None:
    names = _execute(
        "console.log(JSON.stringify([concertName(60,26),concertName(59,7),"
        "concertName(62,28),concertName(62,4)]));"
    )
    assert names == ["B#3", "Cb4", "C##4", "Ebb4"]
