"""Private fixed post-TEXT fault: actual graph, ownership and source restoration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mcp_score.musescore.paths import PLUGIN_DIRECTORY_NAME, PLUGIN_QML_NAME
from tests.test_lyric_text_plugin import _run
from tests.test_musescore_harness import _HARNESS_PATH, harness

if TYPE_CHECKING:
    from pathlib import Path


def test_fixed_owned_copy_fault_observes_text_and_rollback_then_seed_restores(
    tmp_path: Path,
) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.claim_profile(layout)
    harness.seed_configuration(layout)
    source = _HARNESS_PATH.parent.parent / "src/mcp_score/musescore/plugin"
    original = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    directory = layout.plugins_dir / PLUGIN_DIRECTORY_NAME
    harness._install_pitch_batch_fixture(layout)
    harness._install_chord_tone_fixture(layout)
    harness._install_note_rest_fixture(layout)
    harness._install_lyric_fixture(layout)
    observed = _run(
        "score.title='Guarded pitch fixture';LyricTextFixture.arm(plugin,{});",
        module_source=(directory / "lyric-text.js").read_text(),
        output="({run:runLyric(),fault:plugin.lyricTextFault})",
    )
    failure = observed["run"]["reply"]
    assert (
        failure["mutation_status"] == "rolled_back"
        and failure["rollback_verified"] is True
    )
    assert failure["before"] == failure["actual"]
    assert observed["run"]["history"] == ["prior"] and observed["run"]["cancels"] == 1
    assert observed["fault"]["armed"] is False
    assert observed["fault"]["observed"]["plainText"] == 'Sing & <b>"café"</b> \'🙂'
    assert (
        observed["fault"]["observed"]["xmlText"]
        == "Sing &amp; &lt;b&gt;&quot;café&quot;&lt;/b&gt; '🙂"
    )
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == original
    assert not (source / "lyric-text-fixture.js").exists()
    harness.seed_configuration(layout)
    assert (directory / "lyric-text.js").read_bytes() == original["lyric-text.js"]
    assert (directory / PLUGIN_QML_NAME).read_bytes() == original[PLUGIN_QML_NAME]
    assert not (directory / "lyric-text-fixture.js").exists()


@pytest.mark.parametrize(
    "file,needle",
    [
        (PLUGIN_QML_NAME, 'import "lyric-text.js" as LyricText'),
        (PLUGIN_QML_NAME, '"setLyricText":         LyricText.setLyricText,'),
        (PLUGIN_QML_NAME, "property var receiptState: null"),
        ("lyric-text.js", '.import "score.js" as Score'),
        ("lyric-text.js", "before.lyric.text=encode(params.plainText);"),
    ],
)
@pytest.mark.parametrize("duplicate", [True, False])
def test_all_paths_and_sentinels_checked_before_fault_writes(
    tmp_path: Path, file: str, needle: str, duplicate: bool
) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.claim_profile(layout)
    harness.seed_configuration(layout)
    directory = layout.plugins_dir / PLUGIN_DIRECTORY_NAME
    path = directory / file
    path.write_text(path.read_text().replace(needle, needle * 2 if duplicate else ""))
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(harness.HarnessError, match="sentinel"):
        harness._install_lyric_fixture(layout)
    assert {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()
    } == before


@pytest.mark.parametrize(
    "destination", [PLUGIN_QML_NAME, "lyric-text.js", "lyric-text-fixture.js"]
)
def test_unowned_or_linked_copy_refused(tmp_path: Path, destination: str) -> None:
    layout = harness.layout_for("Linux", tmp_path / "owned")
    harness.seed_configuration(layout)
    with pytest.raises(harness.HarnessError, match="marked owned"):
        harness._install_lyric_fixture(layout)
    (layout.root / harness.PROFILE_MARKER).write_text(harness.PROFILE_MARKER_CONTENT)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    target = layout.plugins_dir / PLUGIN_DIRECTORY_NAME / destination
    if target.exists():
        target.unlink()
    target.symlink_to(outside)
    with pytest.raises(harness.HarnessError, match="linked"):
        harness._install_lyric_fixture(layout)
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("enabled", [False, True])
def test_launch_installs_lyric_fault_once_before_first_of_two_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.delenv("MUSESCORE_LYRIC_TEST_SETUP", raising=False)
    if enabled:
        monkeypatch.setenv("MUSESCORE_LYRIC_TEST_SETUP", "1")
    layout = harness.layout_for("Linux", tmp_path / "owned")
    settings = harness.Settings("Linux", "4.7.5", tmp_path, layout)
    dead, alive = MagicMock(), MagicMock()
    dead.poll.return_value = -6
    alive.poll.return_value = None
    attempts = iter([dead, alive])
    events: list[str] = []

    def install(actual_layout: object) -> None:
        assert actual_layout is layout
        events.append("install")

    def start(*arguments: object) -> MagicMock:
        events.append("start")
        return next(attempts)

    with (
        patch.object(harness, "_install_lyric_fixture", side_effect=install),
        patch.object(harness, "_start", side_effect=start),
        patch.object(harness, "track_process"),
        patch.object(harness, "refresh_owned"),
        patch.object(harness, "STARTUP_SECONDS", 0),
    ):
        process = harness.launch(settings, tmp_path / "score.musicxml")

    assert process is alive
    assert events == (["install", "start", "start"] if enabled else ["start", "start"])
