"""Run the actual lyric mutation, native metadata guards, and receipt graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcp_score.bridge.lyric_text import LyricTextEdit
from mcp_score.bridge.musescore import _snake_case_keys

_PLUGIN = Path(__file__).parent.parent / "src/mcp_score/musescore/plugin"
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node required for actual JS")


def _run(
    change: str = "", *, module_source: str | None = None, output: str = "runLyric()"
) -> Any:
    names = [
        "Constants",
        "Score",
        "Passage",
        "Pitch",
        "ChordTone",
        "Duration",
        "Context",
        "Rhythm",
        "Receipts",
        "LyricText",
    ]
    filenames = {"ChordTone": "chord-tone", "LyricText": "lyric-text"}
    sources = {
        name: "\n".join(
            line
            for line in (_PLUGIN / (filenames.get(name, name.lower()) + ".js"))
            .read_text()
            .splitlines()
            if not line.startswith(".")
        )
        for name in names
    }
    if module_source is not None:
        names.insert(-1, "LyricTextFixture")
        sources["LyricText"] = "\n".join(
            line for line in module_source.splitlines() if not line.startswith(".")
        )
        sources["LyricTextFixture"] = "\n".join(
            line
            for line in (
                Path(__file__).parent / "integration/plugin/lyric-text-fixture.js"
            )
            .read_text()
            .splitlines()
            if not line.startswith(".")
        )
    graph = (
        Path(__file__).with_name("note_rest_graph.js").read_text()
        + Path(__file__).with_name("lyric_text_graph.js").read_text()
    )
    script = (
        "const names="
        + json.dumps(names)
        + ";const sources="
        + json.dumps(sources)
        + ";"
        + graph
        + change
        + "\nconsole.log(JSON.stringify("
        + output
        + "));"
    )
    result = subprocess.run(
        [_NODE or "node", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_literal_unicode_xml_roundtrip_preserves_chord_and_private_style() -> None:
    observed = _run()
    result = LyricTextEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.selected_after.plain_text == 'Sing & <b>"café"</b> \'🙂'
    assert (
        result.selected_after.xml_text
        == "Sing &amp; &lt;b&gt;&quot;café&quot;&lt;/b&gt; '🙂"
    )
    assert result.before.notes == result.after.notes
    assert result.before.duration == result.after.duration
    assert "fontSize" in result.verified_native_fields
    assert observed["history"] == ["prior", "edit"] and observed["cursor"] == [2, 1, 3]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\x00",
        "a\nb",
        "\r",
        "\t",
        "\x7f",
        "\u009f",
        "\ud800",
        "\udfff",
        "\ufdd0",
        "\ufffe",
        "\U0010ffff",
        "\u2028",
        "x" * 513,
    ],
)
def test_invalid_plain_text_rejects_before_command(text: str) -> None:
    observed = _run("params.plainText=" + json.dumps(text) + ";")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == 0


@pytest.mark.parametrize(
    "change",
    [
        "plugin.mscoreMinorVersion=4;",
        "plugin.mscoreMinorVersion=6;",
        "lyric.syllabic=1;",
        "lyric.lyricTicks=frac(1,4);",
        "lyric.lyricTicks={};",
        "lyric.isMelisma=true;",
        "delete lyric.isMelisma;",
        "lyric.separator={};",
        "delete lyric.separator;",
        "text='<b>keep chord</b>';",
        "text='&apos;';",
        "lyric.fontSize=NaN;",
        "delete lyric.fontStyle;",
        "lyric.frameType=1;",
        "lyric.align=0;",
        "lyric.align=[];",
        "lyric.align=[0,0.5];",
        "delete lyric.color;",
        "score.excerpts=[{}];",
        "score.excerpts={};",
        "score.spanners={};",
        "score.spanners={length:-1};",
        "current.lyrics.push({...lyric});",
        "lyric.verse=-1;",
    ],
)
def test_unknown_rich_or_linked_lyrics_reject_before_opening_undo(change: str) -> None:
    observed = _run(change + "params.expected=snapshot()[0];")
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == 0 and observed["history"] == ["prior"]


@pytest.mark.parametrize(
    "change",
    [
        "params.expected.lyrics[0].text='stale';",
        "params.expected.notes[0].writtenTpc++;",
        "params.expected.articulations=[{}];",
        "params.expected.lyrics=[];",
    ],
)
def test_full_chord_snapshot_rejects_stale_pitch_and_attachment_state(
    change: str,
) -> None:
    observed = _run(change)
    assert observed["reply"]["code"] == "stale_target" and observed["starts"] == 0


def test_noop_creates_no_undo_entry() -> None:
    observed = _run("params.plainText='keep chord';")
    assert observed["reply"]["result"]["mutationStatus"] == "unchanged"
    assert observed["starts"] == 0 and observed["history"] == ["prior"]


@pytest.mark.parametrize("control", ["ignore", "fail", "style", "peer"])
def test_failed_setter_or_appearance_peer_damage_cancels_only_owned_scope(
    control: str,
) -> None:
    observed = _run(f"lyricControls.{control}=true;")
    assert observed["reply"]["mutation_status"] == "rolled_back"
    assert observed["cancels"] == 1 and observed["history"] == ["prior"]


def test_known_committed_unreadable_outcome_is_not_downgraded_or_undone() -> None:
    observed = _run(
        "controls.postCommit=()=>{Passage.readPassage=()=>{"
        "throw Error('unavailable');};};"
    )
    assert observed["reply"]["mutation_status"] == "applied_unverified"
    assert observed["cancels"] == 0 and observed["history"] == ["prior", "edit"]


def test_commit_exception_never_cancels_a_different_command() -> None:
    observed = _run(
        "const end=score.endCmd;score.endCmd=cancel=>{end(cancel);"
        "if(!cancel)throw Error('uncertain');};"
    )
    assert (
        observed["reply"]["mutation_status"] == "unknown" and observed["cancels"] == 0
    )


def test_receipt_retains_integer_json_reply_and_duplicate_after_undo_is_history() -> (
    None
):
    observed = _run(
        "const ticket={receiptVersion:1,operationId:'lyric-1',"
        "expectedPluginSession:'test-session',"
        "mutation:{kind:'set_lyric_text',request:params}};"
        "const first=Receipts.executeMutationOnce(plugin,ticket);"
        "text='keep chord';const duplicate=Receipts.executeMutationOnce(plugin,ticket);"
        "const changed=JSON.parse(JSON.stringify(ticket));"
        "changed.mutation.request.plainText='different';"
        "const conflict=Receipts.executeMutationOnce(plugin,changed);",
        output="({first,duplicate,conflict,starts,text})",
    )
    assert observed["first"]["result"]["status"] == "completed"
    assert observed["first"]["result"]["command"] == "setLyricText"
    assert observed["duplicate"]["result"]["cached"] is True
    assert (
        observed["duplicate"]["result"]["originalReply"]
        == observed["first"]["result"]["originalReply"]
    )
    assert observed["conflict"]["code"] == "operation_id_conflict"
    assert observed["starts"] == 1 and observed["text"] == "keep chord"


def test_malformed_native_utf16_is_rejected_before_native_plain_text_parser() -> None:
    observed = _run(
        "text='\\ud800';let reads=0;Object.defineProperty(lyric,'plainText',"
        "{get(){reads++;throw Error('unsafe parser must not run');}});",
        output="({run:runLyric(),reads})",
    )
    assert observed["reads"] == 0
    assert observed["run"]["reply"]["mutation_status"] == "not_applied"
    assert observed["run"]["starts"] == 0


@pytest.mark.parametrize("text", ["🙂" * 512, "e\u0301", "&lt;", "'", '"'])
def test_valid_scalar_boundaries_and_entities_remain_literal(text: str) -> None:
    observed = _run("params.plainText=" + json.dumps(text) + ";")
    result = LyricTextEdit.model_validate(_snake_case_keys(observed["reply"]["result"]))
    assert result.selected_after.plain_text == text


@pytest.mark.parametrize(
    "value",
    [
        "undefined",
        "'unknown'",
        "'101'",
        "null",
        "false",
        "0",
        "-1",
        "0.5",
        "NaN",
        "Infinity",
        "9007199254740992",
    ],
)
def test_unknown_enum_and_matching_native_type_cannot_establish_lyric_identity(
    value: str,
) -> None:
    observed = _run(
        "plugin.Element.LYRICS=" + value + ";lyric.type=" + value + ";"
        "params.expected=snapshot()[0];"
    )
    assert observed["reply"]["code"] == "unsupported_native_api"
    assert observed["reply"]["mutation_status"] == "not_applied"
    assert observed["starts"] == observed["cancels"] == observed["lyricWrites"] == 0
    assert observed["history"] == ["prior"] and observed["text"] == "keep chord"


@pytest.mark.parametrize(
    "value", ["undefined", "'unknown'", "'101'", "null", "false", "0", "-1", "101.5"]
)
def test_unknown_selected_or_surviving_lyric_type_rejects_before_write(
    value: str,
) -> None:
    for selected in (True, False):
        change = (
            "lyric.type=" + value + ";"
            if selected
            else ("current.lyrics.push({...lyric,verse:1,type:" + value + "});")
        )
        observed = _run(change + "params.expected=snapshot()[0];")
        assert observed["reply"]["mutation_status"] == "not_applied"
        assert observed["starts"] == observed["cancels"] == observed["lyricWrites"] == 0
        assert observed["history"] == ["prior"] and observed["text"] == "keep chord"
