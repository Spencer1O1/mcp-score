"""Run the actual receipt ledger with the actual guarded pitch handler."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_pitch_plugin import _GRAPH

_NODE = shutil.which("node")
_PLUGIN = Path(__file__).parent.parent / "src/mcp_score/musescore/plugin"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for QML JS tests")


def _execute(expression: str) -> Any:
    declarations = []
    for name in ["passage", "pitch", "receipts"]:
        source = "\n".join(
            line
            for line in (_PLUGIN / f"{name}.js").read_text().splitlines()
            if not line.startswith(".")
        )
        declarations.append(f"const {name}Source=" + json.dumps(source) + ";")
    setup = r"""
const Receipts=vm.runInNewContext(receiptsSource+'\nthis;');
plugin.receiptState=Receipts.newState();
let calls=0;
plugin.commands={setNotePitch(p,args){calls++;return Pitch.setNotePitch(p,args);}};
const request={receiptVersion:1,operationId:'caller-operation',
 expectedPluginSession:plugin.receiptState.sessionId,
 mutation:{kind:'set_note_pitch',request:params}};
function execute(){return Receipts.executeMutationOnce(plugin,request);}
function lookup(){return Receipts.readMutationReceipt(plugin,request);}
"""
    result = subprocess.run(
        [
            _NODE or "node",
            "-e",
            "\n".join(declarations) + _GRAPH + setup + "{\n" + expression + "\n}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_canonical_payload_executes_once_and_returns_history() -> None:
    result = _execute(r"""
const first=execute();
request.mutation.request=Object.fromEntries(Object.entries(params).reverse());
request.mutation.request.expected=Object.fromEntries(Object.entries(params.expected).reverse());
request.mutation.request.expected.notes=params.expected.notes.map(
 note=>Object.fromEntries(Object.entries(note).reverse()));
const again=execute();
const saved=plugin.receiptState.entries[request.operationId].replyJson;
again.result.originalReply.result.after.notes[0].name='corrupted copy';
const read=lookup();
console.log(JSON.stringify({first,again,read,calls,history:score.history,
 serializedUnchanged:saved===plugin.receiptState.entries[request.operationId].replyJson}));
""")
    assert result["calls"] == 1
    assert result["history"] == ["prior user change", "setNotePitch"]
    assert result["first"]["result"]["cached"] is False
    assert result["read"]["result"]["cached"] is True
    assert (
        result["read"]["result"]["originalReply"]
        == result["first"]["result"]["originalReply"]
    )
    assert result["serializedUnchanged"]


def test_changed_payload_conflicts_and_original_reply_survives_undo_like_changes() -> (
    None
):
    result = _execute(r"""
const first=execute();
chord.notes[0].data.pitch=60;chord.notes[0].data.tpc1=14;chord.notes[0].data.tpc2=14;
const afterUndo=execute();
request.mutation.request.pitch=69;request.mutation.request.tpc=17;
const conflict=execute();
console.log(JSON.stringify({first,afterUndo,conflict,calls,pitch:chord.notes[0].pitch}));
""")
    assert result["calls"] == 1 and result["pitch"] == 60
    assert (
        result["afterUndo"]["result"]["originalReply"]
        == result["first"]["result"]["originalReply"]
    )
    assert result["conflict"]["code"] == "operation_id_conflict"


def test_reservation_precedes_handler_reentrancy_and_errors_are_retained() -> None:
    result = _execute(r"""
let pending;
plugin.commands.setNotePitch=()=>{calls++;pending=execute();
 return {error:'native verification failed',mutation_status:'applied_unverified'};};
const first=execute();const second=execute();
console.log(JSON.stringify({first,second,pending,calls}));
""")
    assert result["pending"]["result"]["status"] == "pending"
    assert result["calls"] == 1
    assert result["second"]["result"]["originalReply"] == {
        "error": "native verification failed",
        "mutation_status": "applied_unverified",
    }
    assert result["first"]["result"]["status"] == "completed"


@pytest.mark.parametrize(
    "body",
    [
        "throw Error('after native side effect');",
        "return {get result(){throw Error('native property failure');}};",
        "const result={};result.self=result;return result;",
        "return {result:NaN};",
        "return {result:()=>true};",
        "return {result:'x'.repeat(300000)};",
    ],
)
def test_unknown_results_keep_reserved_id_without_a_second_handler_call(
    body: str,
) -> None:
    result = _execute(
        "plugin.commands.setNotePitch=()=>{calls++;" + body + "};"
        "const first=execute();const second=execute();"
        "console.log(JSON.stringify({first,second,calls,size:plugin.receiptState.size}));"
    )
    assert result["calls"] == 1 and result["size"] == 1
    assert result["first"]["result"]["status"] == "unknown"
    assert result["second"]["result"]["status"] == "unknown"
    assert result["second"]["result"]["originalReply"] is None


def test_full_ledger_rejects_new_ids_but_keeps_existing_reads_and_conflicts() -> None:
    result = _execute(r"""
Receipts.capacity=2;
const first=execute();request.operationId='second';execute();
request.operationId='third';const full=execute();
request.operationId='caller-operation';const retained=lookup();
const duplicate=execute();
request.mutation.request.pitch=69;const conflict=execute();
console.log(JSON.stringify({first,full,retained,duplicate,conflict,calls,size:plugin.receiptState.size}));
""")
    assert result["calls"] == 2 and result["size"] == 2
    assert result["full"]["code"] == "receipt_ledger_full"
    assert (
        result["retained"]["result"]["originalReply"]
        == result["first"]["result"]["originalReply"]
    )
    assert result["duplicate"]["result"]["cached"]
    assert result["conflict"]["code"] == "operation_id_conflict"


def test_restart_rejects_old_session_even_when_new_ledger_is_empty() -> None:
    result = _execute(r"""
const first=execute();const old=request.expectedPluginSession;
plugin.receiptState=Receipts.newState();
const retry=execute();const read=lookup();
console.log(JSON.stringify({retry,read,calls,fresh:old!==plugin.receiptState.sessionId}));
""")
    assert result["fresh"] and result["calls"] == 1
    assert result["retry"]["code"] == result["read"]["code"] == "stale_plugin_session"


@pytest.mark.parametrize(
    "change",
    [
        "request.receiptVersion=2;",
        "delete request.expectedPluginSession;",
        "request.operationId='__proto__';",
        "request.operationId='trailing\\n';",
        "request.operationId='';",
        "request.mutation.kind='undo';",
        "request.mutation.extra=true;",
        "request.mutation.request.offset.numerator=9007199254740992;",
        "request.mutation.request.pitch=Infinity;",
        "request.mutation.request.extra='x'.repeat(70000);",
        "request.mutation.request.extra=Array(17000).fill(1);",
        "let nested={};request.mutation.request.extra=nested;"
        "for(let i=0;i<40;i++){nested.child={};nested=nested.child;}",
        "request.mutation.request.extra=()=>true;",
    ],
)
def test_invalid_wrapper_and_payload_reject_before_reservation_or_native_call(
    change: str,
) -> None:
    result = _execute(
        change + "console.log(JSON.stringify({reply:execute(),calls,"
        "size:plugin.receiptState.size}));"
    )
    assert "error" in result["reply"]
    assert result["calls"] == result["size"] == 0


@pytest.mark.parametrize("operation_id", ["constructor", "toString", "hasOwnProperty"])
def test_own_key_lookup_does_not_confuse_operation_ids_with_prototypes(
    operation_id: str,
) -> None:
    result = _execute(
        "request.operationId=" + json.dumps(operation_id) + ";"
        "const first=execute();const second=execute();"
        "console.log(JSON.stringify({first,second,calls}));"
    )
    assert result["calls"] == 1
    assert (
        result["second"]["result"]["originalReply"]
        == result["first"]["result"]["originalReply"]
    )


def test_serialization_exception_retains_unknown_without_executing_again() -> None:
    result = _execute(r"""
const stringify=Receipts.JSON.stringify;
Receipts.JSON.stringify=value=>{
 if(value && value.failSerialization)throw Error('wire serialization failed');
 return stringify(value);
};
plugin.commands.setNotePitch=()=>{calls++;return {failSerialization:true};};
const first=execute();const second=execute();
console.log(JSON.stringify({first,second,calls}));
""")
    assert result["calls"] == 1
    assert result["second"]["result"]["status"] == "unknown"
    assert "wire serialization failed" in result["second"]["result"]["reason"]
    assert result["second"]["result"]["originalReply"] is None
