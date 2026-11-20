"""Run actual sequence/action/undo modules with an observable native API double.

Native integration tests cover MuseScore's real undo implementation. This graph
exposes command boundaries and injected exceptions without launching an app.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    _NODE is None, reason="Node is needed for plugin JS tests"
)

_GRAPH = r"""
const vm = require('node:vm');
const MS = {Cursor:{SCORE_START:0}};
const modules = {};
function load(name, source) {
    const context = vm.createContext({MS, ...modules});
    vm.runInContext(source, context);
    modules[name] = context;
}
const history = [{measures:7}]; // An unrelated previous user edit.
const calls = [];
const score = {
    measures:1, nstaves:2,
    startCmd(name) { calls.push(['start',name]); this.saved={measures:this.measures}; },
    endCmd(rollback) {
        calls.push(['end',rollback]);
        if (rollback) this.measures=this.saved.measures;
        else history.push(this.saved);
    },
    appendMeasures(count) { this.measures+=count; },
    newCursor() {
        let index=0;
        return {
            rewind() { index=0; }, nextMeasure() { index++; },
            get measure() { return index < score.measures ? {} : null; }
        };
    }
};
const plugin = {curScore:score,cursorMeasure:1,cursorStaff:1,cursorVoice:2,
    cmd() { throw new Error('Global undo is forbidden for failed sequences'); }};
function observe(reply) {
    return {reply, measures:score.measures,
        cursor:[plugin.cursorMeasure,plugin.cursorStaff,plugin.cursorVoice],
        calls, history};
}
"""


def _execute(expression: str) -> Any:
    loads = []
    for name in ("Constants", "Score", "Reading", "Editing", "Selection", "Sequence"):
        source = "\n".join(
            line
            for line in (_PLUGIN / f"{name.lower()}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        loads.append(f"load({json.dumps(name)},{json.dumps(source)});")
    result = subprocess.run(
        [
            _NODE or "node",
            "-e",
            _GRAPH + "\n".join(loads) + "const Sequence=modules.Sequence;" + expression,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "invalid_step",
    [
        None,
        [],
        "addNote",
        1,
        {},
        {"action": None},
        {"action": 1},
        {"action": ""},
        {"action": "unknown"},
        {"action": "toString"},
        {"action": "constructor"},
        {"action": "__proto__"},
        {"action": "ping", "params": None},
        {"action": "ping", "params": []},
        {"action": "ping", "params": "bad"},
        {"action": "ping", "params": False},
    ],
)
def test_invalid_late_step_rejects_before_mutation_navigation_or_undo(
    invalid_step: object,
) -> None:
    sequence = [
        {"action": "appendMeasures", "params": {"count": 1}},
        {"action": "goToMeasure", "params": {"measure": 2}},
        invalid_step,
    ]
    observed = _execute(
        "console.log(JSON.stringify(observe(Sequence.processSequence(plugin,"
        + json.dumps({"sequence": sequence})
        + "))));"
    )
    assert observed["reply"]["failedIndex"] == 2
    assert observed["reply"]["mutationStatus"] == "not_applied"
    assert observed["reply"]["rollbackStatus"] == "not_needed"
    assert observed["measures"] == 1
    assert observed["cursor"] == [1, 1, 2]
    assert observed["calls"] == []
    assert observed["history"] == [{"measures": 7}]


@pytest.mark.parametrize("thrown", ["new Error('native mutation failed')", "null"])
def test_throw_inside_actual_action_rolls_back_and_restores_entire_logical_cursor(
    thrown: str,
) -> None:
    observed = _execute(
        "score.appendMeasures=function(count) {this.measures+=count;"
        "if (this.measures===3) {plugin.cursorVoice=3;throw " + thrown + ";}};"
        "const reply=Sequence.processSequence(plugin,{sequence:["
        "{action:'appendMeasures',params:{count:1}},"
        "{action:'goToMeasure',params:{measure:2}},"
        "{action:'goToStaff',params:{staff:0}},"
        "{action:'appendMeasures',params:{count:1}}]});"
        "console.log(JSON.stringify(observe(reply)));"
    )
    assert observed["reply"]["failedIndex"] == 3
    assert observed["reply"]["failedAction"] == "appendMeasures"
    assert observed["reply"]["mutationStatus"] == "unknown"
    assert observed["reply"]["rollbackStatus"] == "requested"
    assert observed["measures"] == 1
    assert observed["cursor"] == [1, 1, 2]
    assert observed["calls"] == [["start", "processSequence"], ["end", True]]
    assert observed["history"] == [{"measures": 7}]


def test_runtime_dependent_validation_failure_restores_score_and_cursor() -> None:
    observed = _execute(
        "console.log(JSON.stringify(observe(Sequence.processSequence(plugin,{sequence:["
        "{action:'appendMeasures',params:{count:1}},"
        "{action:'goToMeasure',params:{measure:2}},"
        "{action:'appendMeasures',params:{count:0}}]}))));"
    )
    assert observed["reply"]["failedIndex"] == 2
    assert observed["reply"]["rollbackStatus"] == "requested"
    assert observed["measures"] == 1
    assert observed["cursor"] == [1, 1, 2]
    assert observed["history"] == [{"measures": 7}]


def test_dependent_success_is_one_undo_step_and_retains_successful_cursor() -> None:
    observed = _execute(
        "const reply=Sequence.processSequence(plugin,{sequence:["
        "{action:'appendMeasures',params:{count:1}},"
        "{action:'goToMeasure',params:{measure:2}},"
        "{action:'goToStaff',params:{staff:0}}, {action:'ping'}]});"
        "const beforeUndo=observe(reply);"
        "score.measures=history.pop().measures;"
        "console.log(JSON.stringify({beforeUndo,restored:score.measures,history}));"
    )
    success = observed["beforeUndo"]
    assert success["reply"]["result"]["count"] == 4
    assert success["measures"] == 2
    assert success["cursor"] == [2, 0, 2]
    assert success["calls"] == [["start", "processSequence"], ["end", False]]
    assert observed["restored"] == 1
    assert observed["history"] == [{"measures": 7}]


@pytest.mark.parametrize("failing_boundary", ["start", "rollback", "commit"])
def test_transaction_boundary_throw_restores_cursor_without_claiming_score_rollback(
    failing_boundary: str,
) -> None:
    setup = (
        "score.startCmd=function() {plugin.cursorStaff=0;throw new Error('start');};"
        if failing_boundary == "start"
        else "score.endCmd=function() {throw new Error('end');};"
    )
    steps = [{"action": "appendMeasures", "params": {"count": 1}}]
    if failing_boundary == "rollback":
        steps.append({"action": "goToMeasure", "params": {"measure": 99}})
    observed = _execute(
        setup
        + "console.log(JSON.stringify(observe(Sequence.processSequence(plugin,"
        + json.dumps({"sequence": steps})
        + "))));"
    )
    assert observed["reply"]["mutationStatus"] == "unknown"
    assert observed["reply"]["rollbackStatus"] == "unknown"
    assert observed["cursor"] == [1, 1, 2]
    assert observed["measures"] == (1 if failing_boundary == "start" else 2)
    assert observed["history"] == [{"measures": 7}]
