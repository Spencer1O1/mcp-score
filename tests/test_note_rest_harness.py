"""Fixed structural faults belong only to the harness's marked installed copy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from mcp_score.musescore.paths import PLUGIN_DIRECTORY_NAME, PLUGIN_QML_NAME
from tests.test_musescore_harness import _HARNESS_PATH, harness
from tests.test_note_rest_plugin import _run


@pytest.mark.parametrize("fill", [True, False])
def test_owned_copy_fault_observes_fresh_replacement_then_source_seed_restores(
    tmp_path: Path,
    fill: bool,
) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.claim_profile(layout)
    harness.seed_configuration(layout)
    source = _HARNESS_PATH.parent.parent / "src/mcp_score/musescore/plugin"
    original = {
        path.name: path.read_bytes() for path in source.iterdir() if path.is_file()
    }
    directory = layout.plugins_dir / PLUGIN_DIRECTORY_NAME
    harness._install_chord_tone_fixture(layout)
    harness._install_note_rest_fixture(layout)
    assert {
        path.name: path.read_bytes() for path in source.iterdir() if path.is_file()
    } == original
    assert not (source / "note-rest-fixture.js").exists()
    setup = (
        ""
        if fill
        else "current=chord(1920);rebuild();params.expected=snapshot();fill=false;"
    )
    setup += (
        "score.title='Guarded note rest fixture';NoteRestFixture.arm"
        + ("Fill" if fill else "Replace")
        + "(plugin,{});"
    )
    # Return fault primitives alongside the actual handler result.
    setup += (
        "const operation=run;run=()=>({edit:operation(),fault:plugin.noteRestFault});"
    )
    observed = _run(setup, module_source=(directory / "rhythm.js").read_text())
    assert observed["edit"]["reply"]["mutation_status"] == "rolled_back"
    assert observed["edit"]["reply"]["rollback_verified"] is True
    assert observed["edit"]["reply"]["before"] == observed["edit"]["reply"]["actual"]
    assert observed["edit"]["starts"] == observed["edit"]["cancels"] == 1
    fault = observed["fault"]
    assert fault["armed"] is False
    assert fault["observed"][0]["type"] == (93 if fill else 25)
    assert fault["observed"][0]["pitches"] == ([61] if fill else [])
    harness.seed_configuration(layout)
    assert (directory / "rhythm.js").read_bytes() == original["rhythm.js"]
    assert (directory / PLUGIN_QML_NAME).read_bytes() == original[PLUGIN_QML_NAME]
    assert not (directory / "note-rest-fixture.js").exists()


@pytest.mark.parametrize(
    "file,needle",
    [
        (PLUGIN_QML_NAME, 'import "rhythm.js" as Rhythm'),
        (PLUGIN_QML_NAME, '"replaceNoteWithRest":  Rhythm.replaceNoteWithRest,'),
        (PLUGIN_QML_NAME, "property var receiptState: null"),
        ("rhythm.js", '.import "score.js" as Score'),
        ("rhythm.js", "cursor.addNote(params.pitch,false);"),
        ("rhythm.js", "cursor.addRest();"),
    ],
)
@pytest.mark.parametrize("duplicate", [True, False])
def test_all_sentinels_validated_before_any_installed_source_write(
    tmp_path: Path,
    file: str,
    needle: str,
    duplicate: bool,
) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.claim_profile(layout)
    harness.seed_configuration(layout)
    directory = layout.plugins_dir / PLUGIN_DIRECTORY_NAME
    path = directory / file
    path.write_text(path.read_text().replace(needle, needle * 2 if duplicate else ""))
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(harness.HarnessError, match="sentinel"):
        harness._install_note_rest_fixture(layout)
    assert {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()
    } == before


@pytest.mark.parametrize(
    "destination", [PLUGIN_QML_NAME, "rhythm.js", "note-rest-fixture.js"]
)
def test_profile_ownership_and_unlinked_destinations_required(
    tmp_path: Path, destination: str
) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.seed_configuration(layout)
    with pytest.raises(harness.HarnessError, match="marked owned"):
        harness._install_note_rest_fixture(layout)
    (layout.root / harness.PROFILE_MARKER).write_text(harness.PROFILE_MARKER_CONTENT)
    outside = tmp_path / "outside.js"
    outside.write_text("untouched")
    path = layout.plugins_dir / PLUGIN_DIRECTORY_NAME / destination
    if path.exists():
        path.unlink()
    path.symlink_to(outside)
    with pytest.raises(harness.HarnessError, match="linked"):
        harness._install_note_rest_fixture(layout)
    assert outside.read_text() == "untouched"
