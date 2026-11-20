"""Tests for scripts/musescore_harness.py: platform layout and config seeding."""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_score.musescore.paths import PLUGIN_DIRECTORY_NAME, PLUGIN_QML_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_HARNESS_PATH = Path(__file__).parent.parent / "scripts" / "musescore_harness.py"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("musescore_harness", _HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their (postponed) annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness: Any = _load_harness()


class TestLayoutFor:
    def test_linux_uses_xdg_directories(self, tmp_path: Path) -> None:
        # Act
        layout = harness.layout_for("Linux", tmp_path)

        # Assert
        assert layout.preferences == harness.IniPreferences(
            tmp_path / "config/MuseScore/MuseScore4.ini"
        )
        assert layout.data_dir == tmp_path / "data/MuseScore/MuseScore4"
        assert layout.plugins_dir == tmp_path / "Plugins"
        assert layout.log_dir == layout.data_dir / "logs"

    def test_macos_uses_native_preferences_and_library_data(
        self, tmp_path: Path
    ) -> None:
        # Act
        layout = harness.layout_for("Darwin", tmp_path)

        # Assert
        assert layout.preferences == harness.PlistPreferences(
            tmp_path / "Library/Preferences/org.musescore.MuseScore4.plist"
        )
        assert (
            layout.data_dir
            == tmp_path / "Library/Application Support/MuseScore/MuseScore4"
        )
        assert layout.plugins_dir == tmp_path / "Plugins"

    def test_windows_honours_appdata_variables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

        # Act
        layout = harness.layout_for("Windows", tmp_path)

        # Assert
        assert layout.preferences == harness.IniPreferences(
            tmp_path / "portable/Data/settings/MuseScore/MuseScore4.ini"
        )
        assert layout.data_dir == tmp_path / "portable/Data/settings"


class TestReleaseAssetUrl:
    _RELEASE = {
        "assets": [
            {
                "name": "MuseScore-Studio-4.7.5.260831071-aarch64.AppImage",
                "browser_download_url": "https://example.test/aarch64.AppImage",
            },
            {
                "name": "MuseScore-Studio-4.7.5.260831071-x86_64.AppImage",
                "browser_download_url": "https://example.test/x86_64.AppImage",
            },
            {
                "name": "MuseScore-Studio-4.7.5.260831071-x86_64.paf.exe",
                "browser_download_url": "https://example.test/x86_64.paf.exe",
            },
            {
                "name": "MuseScore-Studio-4.7.5.260831071.dmg",
                "browser_download_url": "https://example.test/universal.dmg",
            },
        ]
    }

    @pytest.mark.parametrize(
        ("system", "expected"),
        [
            ("Linux", "https://example.test/x86_64.AppImage"),
            ("Windows", "https://example.test/x86_64.paf.exe"),
            ("Darwin", "https://example.test/universal.dmg"),
        ],
    )
    def test_picks_the_platform_asset_of_the_release(
        self, system: str, expected: str
    ) -> None:
        # Arrange
        fetch = MagicMock(return_value=self._RELEASE)

        with patch.object(harness, "fetch_json", fetch):
            # Act
            url = harness.release_asset_url(system, "4.7.5")

        # Assert
        assert url == expected
        fetch.assert_called_once_with(f"{harness.RELEASES_API_URL}/v4.7.5")

    def test_without_matching_asset_raises(self) -> None:
        # Arrange
        with (
            patch.object(harness, "fetch_json", return_value={"assets": []}),
            pytest.raises(harness.HarnessError, match=r"no asset ending in \.dmg"),
        ):
            # Act / Assert
            harness.release_asset_url("Darwin", "4.7.5")


class TestSettings:
    def test_version_defaults_to_the_latest_listed(self, tmp_path: Path) -> None:
        # Arrange
        versions = tmp_path / "versions.json"
        versions.write_text('{"oldest": "4.4.4", "latest": "4.9.1"}')

        with (
            patch.dict(
                "os.environ", {"MUSESCORE_CACHE_DIR": str(tmp_path)}, clear=True
            ),
            patch.object(harness, "VERSIONS_FILE", versions),
        ):
            # Act
            settings = harness.Settings.from_environment()

        # Assert
        assert settings.version == "4.9.1"
        assert settings.download_url is None

    def test_explicit_download_url_skips_the_release_lookup(self) -> None:
        # Arrange
        environment = {
            "MUSESCORE_VERSION": "4.7.5",
            "MUSESCORE_DOWNLOAD_URL": "https://example.test/musescore.dmg",
        }
        lookup = MagicMock()

        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(harness, "release_asset_url", lookup),
        ):
            # Act
            url = harness.Settings.from_environment().resolve_download_url()

        # Assert
        assert url == "https://example.test/musescore.dmg"
        lookup.assert_not_called()

    def test_versions_file_lists_the_workflow_matrix(self) -> None:
        # The workflow reads oldest, middle and latest from the same file.
        versions = json.loads(harness.VERSIONS_FILE.read_text())
        assert set(versions) >= {"oldest", "middle", "latest"}
        assert versions["oldest"] < versions["middle"] < versions["latest"]


class TestSeedConfiguration:
    def test_installs_plugin_and_enables_it_under_both_uri_schemes(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        layout = harness.layout_for("Linux", tmp_path)

        # Act
        harness.seed_configuration(layout)

        # Assert: plugin installed, enabled for 4.4 (muse://) and 4.5+ (musescore://)
        plugin = layout.plugins_dir / PLUGIN_DIRECTORY_NAME
        assert (plugin / PLUGIN_QML_NAME).is_file()
        assert (plugin / "score.js").is_file()
        enabled = json.loads((layout.data_dir / "extensions/config.json").read_text())
        assert [entry["uri"] for entry in enabled] == list(harness.PLUGIN_URIS)
        assert all(
            entry["actions"] == [{"code": "main", "exec_point": "manually"}]
            for entry in enabled
        )

    def test_preserves_existing_shortcuts_and_does_not_create_bindings(
        self, tmp_path: Path
    ) -> None:
        layout = harness.layout_for("Linux", tmp_path)
        layout.data_dir.mkdir(parents=True)
        shortcuts = layout.data_dir / "shortcuts.xml"
        existing = "<Shortcuts><SC><key>save</key><seq>Ctrl+S</seq></SC></Shortcuts>"
        shortcuts.write_text(existing)

        harness.seed_configuration(layout)
        assert shortcuts.read_text() == existing

        shortcuts.unlink()
        harness.seed_configuration(layout)
        assert not shortcuts.exists()

    def test_suppresses_startup_dialogs_and_stale_session(self, tmp_path: Path) -> None:
        # Arrange
        layout = harness.layout_for("Linux", tmp_path)
        stale_session = layout.data_dir / "session"
        stale_session.mkdir(parents=True)
        (stale_session / "score.mscz").write_bytes(b"")

        # Act
        harness.seed_configuration(layout)

        # Assert
        settings = (tmp_path / "config/MuseScore/MuseScore4.ini").read_text()
        assert "hasCompletedFirstLaunchSetup=true" in settings
        assert "welcomeDialogShowOnStartup=false" in settings
        assert "welcomeDialogLastShownVersion=99.0.0" in settings
        assert "checkForUpdate=false" in settings
        assert str(layout.plugins_dir) in settings
        assert not stale_session.exists()

    def test_macos_writes_only_the_private_plist(self, tmp_path: Path) -> None:
        layout = harness.layout_for("Darwin", tmp_path)
        with patch.object(harness, "run") as run:
            harness.seed_configuration(layout)
        run.assert_not_called()
        with layout.preferences.file.open("rb") as source:
            preferences = plistlib.load(source)
        assert preferences["application.hasCompletedFirstLaunchSetup"] is True
        assert preferences["application.checkForUpdate"] is False
        assert preferences["application.paths.myPlugins"] == str(layout.plugins_dir)


class TestLaunch:
    @staticmethod
    def _settings(tmp_path: Path) -> Any:
        layout = harness.layout_for("Darwin", tmp_path)
        layout.stdout_log.parent.mkdir(parents=True, exist_ok=True)
        return harness.Settings("Darwin", "4.7.5", tmp_path, layout)

    def test_relaunches_once_when_musescore_dies_during_startup(
        self, tmp_path: Path
    ) -> None:
        # Arrange: the first process exits during startup, the second stays up
        dead = MagicMock()
        dead.poll.return_value = -6
        alive = MagicMock()
        alive.poll.return_value = None
        popen = MagicMock(side_effect=[dead, alive])

        with (
            patch.object(harness, "musescore_running", return_value=False),
            patch.object(harness.subprocess, "Popen", popen),
            patch.object(harness, "track_process"),
            patch.object(harness, "refresh_owned"),
            patch.object(harness, "STARTUP_SECONDS", 0),
            patch.object(harness, "child_environment", return_value={}),
        ):
            # Act
            process = harness.launch(self._settings(tmp_path), tmp_path / "s.xml")

        # Assert
        assert process is alive
        assert popen.call_count == 2

    def test_gives_up_after_the_second_startup_crash(self, tmp_path: Path) -> None:
        # Arrange
        dead = MagicMock()
        dead.poll.return_value = -6
        popen = MagicMock(return_value=dead)

        with (
            patch.object(harness, "musescore_running", return_value=False),
            patch.object(harness.subprocess, "Popen", popen),
            patch.object(harness.time, "sleep"),
            patch.object(harness, "print_log_tails"),
            patch.object(harness, "track_process"),
            patch.object(harness, "refresh_owned"),
            patch.object(harness, "STARTUP_SECONDS", 0),
            patch.object(harness, "child_environment", return_value={}),
            pytest.raises(harness.HarnessError, match="exited during startup"),
        ):
            # Act / Assert
            harness.launch(self._settings(tmp_path), tmp_path / "s.xml")

        assert popen.call_count == harness.LAUNCH_ATTEMPTS


class TestPluginMenuStartup:
    def test_linux_uses_visible_window_and_menu_navigation(self) -> None:
        settings = MagicMock(system="Linux")
        commands: list[list[str]] = []

        def record(command: list[str], **kwargs: object) -> MagicMock:
            commands.append(command)
            return MagicMock(stdout="42\n", returncode=0)

        with (
            patch.object(harness, "run", side_effect=record),
            patch.object(harness.time, "sleep"),
            patch.object(harness, "owned_musescore_pid", return_value=77),
        ):
            harness.trigger_plugin(settings)

        assert commands[0] == [
            "xdotool",
            "search",
            "--onlyvisible",
            "--pid",
            "77",
            "--class",
            "mscore|MuseScore",
        ]
        assert [command for command in commands if command[1] == "key"] == [
            ["xdotool", "key", "--clearmodifiers", "Escape"],
            ["xdotool", "key", "--clearmodifiers", "alt+p"],
            ["xdotool", "key", "--clearmodifiers", "End", "Right"],
            ["xdotool", "key", "--clearmodifiers", "Return"],
        ]
        assert not any("mousemove" in command for command in commands)

    def test_linux_falls_back_to_windowfocus_without_window_manager(self) -> None:
        settings = MagicMock(system="Linux")
        commands: list[list[str]] = []

        def record(command: list[str], **kwargs: object) -> MagicMock:
            commands.append(command)
            return MagicMock(stdout="42\n", returncode=int("windowactivate" in command))

        with (
            patch.object(harness, "run", side_effect=record),
            patch.object(harness.time, "sleep"),
            patch.object(harness, "owned_musescore_pid", return_value=77),
        ):
            harness.trigger_plugin(settings)
        assert ["xdotool", "windowfocus", "--sync", "42"] in commands

    def test_linux_missing_visible_window_reports_startup_failure(self) -> None:
        settings = MagicMock(system="Linux")
        with (
            patch.object(harness, "run", return_value=MagicMock(stdout="")),
            patch.object(harness, "print_log_tails") as diagnostics,
            patch.object(harness, "owned_musescore_pid", return_value=77),
            pytest.raises(harness.HarnessError, match="no MuseScore window"),
        ):
            harness.trigger_plugin(settings)
        diagnostics.assert_called_once_with(settings.layout)


def make_settings(tmp_path: Path, system: str = "Linux") -> Any:
    layout = harness.layout_for(system, tmp_path / "profile")
    cache = tmp_path / "cache"
    executable = cache / "squashfs-root/AppRun"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    settings = harness.Settings(system, "4.7.5", cache, layout)
    if system != "Linux":
        settings.executable.parent.mkdir(parents=True, exist_ok=True)
        settings.executable.touch()
    return settings


def sleeping_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


class TestOwnership:
    def test_existing_application_refuses_before_any_profile_write(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        score = tmp_path / "fixture.musicxml"
        score.touch()
        with (
            patch.object(harness, "musescore_running", return_value=True),
            patch.object(harness, "seed_configuration") as seed,
            pytest.raises(harness.HarnessError, match="already running"),
        ):
            harness.start(settings, score)
        seed.assert_not_called()
        assert not settings.layout.root.exists()

    def test_nonempty_unowned_directory_is_never_adopted(self, tmp_path: Path) -> None:
        layout = harness.layout_for("Linux", tmp_path)
        existing = tmp_path / "important.txt"
        existing.write_text("preserve")
        with pytest.raises(harness.HarnessError, match="nonempty unowned"):
            harness.claim_profile(layout)
        assert existing.read_text() == "preserve"
        assert not (tmp_path / harness.PROFILE_MARKER).exists()

    def test_private_path_symlink_cannot_redirect_writes(self, tmp_path: Path) -> None:
        root = tmp_path / "profile"
        root.mkdir()
        (root / harness.PROFILE_MARKER).write_text(harness.PROFILE_MARKER_CONTENT)
        normal = tmp_path / "normal"
        normal.mkdir()
        (root / "config").symlink_to(normal, target_is_directory=True)
        with pytest.raises(harness.HarnessError, match="escapes"):
            harness.claim_profile(harness.layout_for("Linux", root))
        assert list(normal.iterdir()) == []

    def test_stop_only_terminates_owned_pid_and_preserves_unrelated_process(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        harness.claim_profile(settings.layout)
        owned, unrelated = sleeping_process(), sleeping_process()
        try:
            harness.track_process(settings.layout, owned, "musescore")
            with patch.object(
                harness,
                "run",
                side_effect=AssertionError("No name-based process commands"),
            ):
                harness.stop(settings)
            assert owned.poll() is not None
            assert unrelated.poll() is None
            assert not settings.layout.state_file.exists()
        finally:
            for process in [owned, unrelated]:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    def test_reused_pid_identity_is_not_signaled(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        harness.claim_profile(settings.layout)
        unrelated = sleeping_process()
        try:
            identity = harness.identify(
                harness.psutil.Process(unrelated.pid), "musescore"
            )
            stale = harness.OwnedProcess(
                identity.pid, identity.created - 100, identity.executable, identity.kind
            )
            harness.save_owned(settings.layout, [stale])
            harness.stop(settings)
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_missing_state_does_not_search_for_or_kill_applications(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        harness.claim_profile(settings.layout)
        with patch.object(
            harness,
            "musescore_running",
            side_effect=AssertionError("Not an ownership check"),
        ):
            harness.stop(settings)

    def test_owned_xvfb_cleanup_uses_identity_not_display_name(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        harness.claim_profile(settings.layout)
        owned = sleeping_process()
        try:
            harness.track_process(settings.layout, owned, "xvfb")
            harness.stop(settings)
            assert owned.poll() is not None
        finally:
            if owned.poll() is None:
                owned.terminate()
            owned.wait(timeout=5)

    def test_fixture_restart_replaces_only_owned_process(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        score = tmp_path / "fixture.musicxml"
        score.touch()
        launched: list[subprocess.Popen[bytes]] = []

        def launch(*arguments: object) -> subprocess.Popen[bytes]:
            process = sleeping_process()
            launched.append(process)
            return process

        with (
            patch.object(harness, "preflight"),
            patch.object(harness, "start_xvfb"),
            patch.object(harness, "_start", side_effect=launch),
            patch.object(harness, "STARTUP_SECONDS", 0),
            patch.object(harness, "trigger_plugin"),
            patch.object(harness, "verify_private_paths"),
            patch.object(harness, "wait_for_bridge", AsyncMock(return_value=True)),
        ):
            try:
                harness.start(settings, score)
                first = harness.read_owned(settings.layout)
                harness.stop(settings)
                harness.start(settings, score)
                second = harness.read_owned(settings.layout)
                assert first[0].pid != second[0].pid
                assert launched[0].poll() is not None and launched[1].poll() is None
                harness.stop(settings)
            finally:
                for process in launched:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)

    def test_failed_plugin_start_cleans_only_new_owned_processes(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        score = tmp_path / "fixture.musicxml"
        score.touch()
        owned, unrelated = sleeping_process(), sleeping_process()
        with (
            patch.object(harness, "preflight"),
            patch.object(harness, "start_xvfb"),
            patch.object(harness, "_start", return_value=owned),
            patch.object(harness, "STARTUP_SECONDS", 0),
            patch.object(harness, "trigger_plugin"),
            patch.object(harness, "wait_for_bridge", AsyncMock(return_value=False)),
            patch.object(harness, "print_log_tails"),
        ):
            try:
                with pytest.raises(
                    harness.HarnessError, match="bridge plugin did not answer"
                ):
                    harness.start(settings, score)
                assert owned.poll() is not None
                assert unrelated.poll() is None
            finally:
                for process in [owned, unrelated]:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)


class TestPrivateEnvironment:
    @pytest.mark.parametrize("system", ["Linux", "Windows"])
    def test_child_paths_are_private_without_repurposing_home(
        self, tmp_path: Path, system: str
    ) -> None:
        settings = make_settings(tmp_path, system)
        before = dict(os.environ)
        environment = harness.child_environment(settings)
        assert os.environ == before
        assert environment.get("HOME") == before.get("HOME")
        assert environment.get("CODEX_HOME") == before.get("CODEX_HOME")
        assert environment["TMPDIR"] == str(settings.layout.root / "tmp")
        assert environment["QSG_RHI_PIPELINE_CACHE_LOAD"] == str(
            settings.layout.root / "cache/qt-pipeline.cache"
        )
        assert (
            environment["QSG_RHI_PIPELINE_CACHE_SAVE"]
            == environment["QSG_RHI_PIPELINE_CACHE_LOAD"]
        )
        if system == "Linux":
            assert environment["XDG_DATA_HOME"] == str(settings.layout.root / "data")
        if system == "Windows":
            assert settings.executable.is_relative_to(settings.layout.root)
            assert "Program Files" not in str(settings.executable)

    def test_normal_profile_snapshot_detects_changed_bytes(
        self, tmp_path: Path
    ) -> None:
        normal = tmp_path / "normal"
        normal.mkdir()
        settings_file = normal / "MuseScore4.ini"
        settings_file.write_text("unchanged")
        baseline = tmp_path / "baseline.json"
        with patch.object(harness, "normal_profile_paths", return_value=[normal]):
            harness.snapshot_normal_profile("Linux", baseline)
            harness.verify_normal_profile("Linux", baseline)
            settings_file.write_text("changed")
            with pytest.raises(
                harness.HarnessError, match="normal MuseScore profile changed"
            ):
                harness.verify_normal_profile("Linux", baseline)


def test_occupied_bridge_refuses_before_creating_private_profile(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    score = tmp_path / "fixture.musicxml"
    score.touch()
    with socket.socket() as occupied:
        if os.name != "nt":
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with (
            patch.object(harness, "BRIDGE_PORT", occupied.getsockname()[1]),
            patch.object(harness, "musescore_running", return_value=False),
            patch.object(harness, "seed_configuration") as seed,
            pytest.raises(harness.HarnessError, match="Bridge port is unavailable"),
        ):
            harness.start(settings, score)
    seed.assert_not_called()
    assert not settings.layout.root.exists()


def test_windows_path_preferences_use_forward_slashes_in_private_ini(
    tmp_path: Path,
) -> None:
    root = PureWindowsPath("C:/Users/test/private profile")
    preferences = harness.IniPreferences(tmp_path / "private.ini")
    layout = MagicMock(
        root=root,
        plugins_dir=root / "Plugins",
        data_dir=tmp_path / "data",
        preferences=preferences,
    )
    with patch.object(harness, "install_plugin"):
        harness.seed_configuration(layout)
    text = preferences.file.read_text()
    assert "myPlugins=C:/Users/test/private profile/Plugins" in text
    assert "myScores=C:/Users/test/private profile/myScores" in text
    assert "\\" not in text


def test_application_resolution_uses_only_verified_owned_executable(
    tmp_path: Path,
) -> None:
    layout = harness.layout_for("Darwin", tmp_path)
    stale = harness.OwnedProcess(41, 1.0, "/private/old/mscore", "musescore")
    owned = harness.OwnedProcess(
        42, 2.0, "/private/application/MacOS/mscore", "musescore"
    )
    process = MagicMock(pid=42)
    process.name.return_value = "MuseScore Studio Display Name"
    with (
        patch.object(harness, "refresh_owned"),
        patch.object(harness, "read_owned", return_value=[stale, owned]),
        patch.object(harness, "matching_process", side_effect=[None, process]),
        patch.object(
            harness.psutil,
            "process_iter",
            side_effect=AssertionError("Do not scan unrelated processes"),
        ),
    ):
        assert harness.owned_musescore_pid(layout) == 42


def test_stale_application_executable_never_authorizes_gui_activation(
    tmp_path: Path,
) -> None:
    layout = harness.layout_for("Darwin", tmp_path)
    stale = harness.OwnedProcess(41, 1.0, "/private/old/mscore", "musescore")
    with (
        patch.object(harness, "refresh_owned"),
        patch.object(harness, "read_owned", return_value=[stale]),
        patch.object(harness, "matching_process", return_value=None),
        pytest.raises(harness.HarnessError, match="uniquely identify"),
    ):
        harness.owned_musescore_pid(layout)


@pytest.mark.parametrize("escape", ["root", "state", "lock"])
def test_stop_refuses_symlinked_ownership_paths_before_signaling(
    tmp_path: Path, escape: str
) -> None:
    settings = make_settings(tmp_path)
    harness.claim_profile(settings.layout)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "preserve.txt"
    sentinel.write_text("unchanged")
    if escape == "root":
        original = settings.layout.root
        relocated = external / "profile"
        original.rename(relocated)
        original.symlink_to(relocated, target_is_directory=True)
    elif escape == "state":
        record = external / "record.json"
        record.write_text('{"version":1,"processes":[]}')
        settings.layout.state_file.symlink_to(record)
    else:
        lock = (
            settings.layout.root.parent / f".{settings.layout.root.name}.harness.lock"
        )
        lock.symlink_to(sentinel)
    with (
        patch.object(harness, "matching_process") as matched,
        pytest.raises(harness.HarnessError, match="symlink"),
    ):
        harness.stop(settings)
    matched.assert_not_called()
    assert sentinel.read_text() == "unchanged"


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec preserves the same process PID")
@pytest.mark.parametrize("allow_handoff", [True, False])
def test_real_exec_handoff_requires_original_creation_time_and_exact_expected_binary(
    tmp_path: Path, allow_handoff: bool
) -> None:
    import shutil
    import time

    executable = shutil.which("sleep")
    if executable is None:
        pytest.skip("requires the POSIX sleep utility")
    target = Path(executable).resolve()
    settings = make_settings(tmp_path)
    harness.claim_profile(settings.layout)
    release = tmp_path / "release"
    ready = tmp_path / "ready"
    script = (
        "from pathlib import Path; import os,time; "
        f"ready=Path({str(ready)!r}); release=Path({str(release)!r}); "
        "ready.touch(); "
        "\nwhile not release.exists(): time.sleep(.01)\n"
        f"os.execv({str(target)!r}, ['sleep', '60'])"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        before = harness.identify(harness.psutil.Process(process.pid), "musescore")
        permitted = (
            str(target) if allow_handoff else str(Path(sys.executable).resolve())
        )
        identity = harness.OwnedProcess(
            before.pid, before.created, before.executable, before.kind, permitted
        )
        harness.save_owned(settings.layout, [identity])
        release.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = harness.psutil.Process(process.pid)
            if Path(current.exe()).resolve() == target:
                break
            time.sleep(0.01)
        assert Path(current.exe()).resolve() == target
        assert current.create_time() == before.created
        if allow_handoff:
            assert harness.matching_process(identity) is not None
            harness.refresh_owned(settings.layout)
            after = harness.read_owned(settings.layout)[0]
            assert after.executable == str(target)
            assert after.handoff_executable is None
            assert after.pid == before.pid and after.created == before.created
            harness.stop(settings)
            assert process.poll() is not None
        else:
            assert harness.matching_process(identity) is None
            harness.stop(settings)
            assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_expected_exec_target_does_not_accept_a_reused_pid(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    harness.claim_profile(settings.layout)
    unrelated = sleeping_process()
    try:
        identity = harness.identify(harness.psutil.Process(unrelated.pid), "musescore")
        stale = harness.OwnedProcess(
            identity.pid,
            identity.created - 100,
            "/old/launcher",
            identity.kind,
            identity.executable,
        )
        harness.save_owned(settings.layout, [stale])
        harness.stop(settings)
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX TIME_WAIT reuse semantics")
def test_stopped_owned_listener_time_wait_does_not_block_fixture_restart(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, "Darwin")
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = listener.accept()
    accepted.close()  # active closer owns TIME_WAIT on the listener's port
    client.close()
    listener.close()
    with (
        patch.object(harness, "BRIDGE_PORT", port),
        patch.object(harness, "musescore_running", return_value=False),
    ):
        harness.preflight(settings)


@contextmanager
def hosted_mac(tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    """Mock hosted runner policy only; never access the real account profile."""
    home = tmp_path / "runner"
    temporary = home / "work/_temp"
    temporary.mkdir(parents=True)
    settings = replace(
        make_settings(tmp_path / "artifacts", "Darwin"), macos_runner_mode=True
    )
    environment = {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "macOS",
        "GITHUB_RUN_ID": "123",
        "RUNNER_TEMP": str(temporary),
    }
    with (
        patch.dict(os.environ, environment),
        patch.object(harness.platform, "system", return_value="Darwin"),
        patch.object(harness, "local_account", return_value=("runner", home)),
        patch.object(Path, "home", return_value=home),
    ):
        yield settings, harness.require_macos_runner(settings)


def test_default_macos_refuses_before_any_profile_or_native_write(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, "Darwin")
    with (
        patch.object(harness, "claim_profile") as claim,
        patch.object(harness, "run") as run,
        pytest.raises(harness.HarnessError, match="Local/default macOS"),
    ):
        harness.start(settings, tmp_path / "score.musicxml")
    claim.assert_not_called()
    run.assert_not_called()
    assert not settings.layout.root.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("GITHUB_ACTIONS", "false"),
        ("RUNNER_OS", "Linux"),
        ("GITHUB_RUN_ID", "invalid"),
        ("RUNNER_TEMP", "/tmp"),
    ],
)
def test_macos_rejects_unverified_runner_metadata(
    tmp_path: Path, key: str, value: str
) -> None:
    with (
        hosted_mac(tmp_path) as (settings, _),
        patch.dict(os.environ, {key: value}),
        pytest.raises(harness.HarnessError),
    ):
        harness.require_macos_runner(settings)


def test_macos_requires_explicit_mode_even_on_hosted_runner(tmp_path: Path) -> None:
    with (
        hosted_mac(tmp_path) as (settings, _),
        pytest.raises(harness.HarnessError, match="Local/default macOS"),
    ):
        harness.require_macos_runner(replace(settings, macos_runner_mode=False))


def test_runner_account_and_owned_artifacts_are_separate(tmp_path: Path) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        harness.claim_profile(settings.layout)
        assert not account.home.is_relative_to(settings.layout.root)
        assert not settings.layout.root.is_relative_to(account.home)
        environment = harness.child_environment(settings)
        assert environment["HOME"] == os.environ["HOME"]
        assert "CFFIXED_USER_HOME" not in environment
        assert "__CFPREFERENCES_AVOID_DAEMON" not in environment
        account.data_dir.mkdir(parents=True)
        sentinel = account.data_dir / "native-account-data"
        sentinel.write_text("left for runner disposal")
        harness.clean(settings)
        assert not settings.layout.root.exists()
        assert sentinel.read_text() == "left for runner disposal"
        with pytest.raises(harness.HarnessError, match="home"):
            harness.claim_profile(harness.layout_for("Darwin", account.home))


def test_runner_existing_session_and_fixture_restart_are_atomic_owned_moves(
    tmp_path: Path,
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        harness.claim_profile(settings.layout)
        source = account.data_dir / "session"
        for fixture in ("first-start", "fixture-restart"):
            source.mkdir(parents=True)
            (source / "project").write_text(fixture)
            harness.move_runner_session(settings, account)
            assert not source.exists()
        moves = json.loads(
            (settings.layout.root / "runner-session-moves.json").read_text()
        )
        assert len(moves) == 2
        assert [
            Path(move["owned_backup"]).joinpath("project").read_text() for move in moves
        ] == ["first-start", "fixture-restart"]
        harness.clean(settings)
        assert account.data_dir.exists()
        assert all(not Path(move["owned_backup"]).exists() for move in moves)


def test_runner_session_symlink_is_refused_without_touching_target(
    tmp_path: Path,
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        harness.claim_profile(settings.layout)
        account.data_dir.mkdir(parents=True)
        target = tmp_path / "unrelated"
        target.mkdir()
        source = account.data_dir / "session"
        source.symlink_to(target, target_is_directory=True)
        with pytest.raises(harness.HarnessError, match="symlinked"):
            harness.move_runner_session(settings, account)
        assert source.is_symlink() and target.is_dir()


def test_failed_runner_session_move_preserves_source(tmp_path: Path) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        harness.claim_profile(settings.layout)
        source = account.data_dir / "session"
        source.mkdir(parents=True)
        sentinel = source / "project"
        sentinel.write_text("keep")
        with (
            patch.object(Path, "rename", side_effect=OSError("cross-device")),
            pytest.raises(harness.HarnessError, match="atomically"),
        ):
            harness.move_runner_session(settings, account)
        assert sentinel.read_text() == "keep"
        assert not (settings.layout.root / "runner-session-moves.json").exists()


def test_runner_boundary_reports_native_mutation_without_preservation_claim(
    tmp_path: Path,
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        baseline = tmp_path / "before.json"
        baseline.write_text("{}")
        report = tmp_path / "logs/boundary.json"
        with (
            patch.object(harness, "musescore_running", return_value=False),
            patch.object(
                harness,
                "normal_profile_snapshot",
                return_value={str(account.preferences_file): "changed"},
            ),
        ):
            harness.verify_runner_account_boundary(settings, baseline, report)
        result = json.loads(report.read_text())
        assert result["isolation_boundary"] == "disposable-github-hosted-macos-account"
        assert result["native_account_mutation_expected"] is True
        assert result["per_process_private_profile_claim"] is False
        assert result["owned_artifact_root_removed"] is True


def test_runner_boundary_rejects_mutation_outside_account(tmp_path: Path) -> None:
    with hosted_mac(tmp_path) as (settings, _):
        baseline = tmp_path / "before.json"
        baseline.write_text("{}")
        with (
            patch.object(harness, "musescore_running", return_value=False),
            patch.object(
                harness,
                "normal_profile_snapshot",
                return_value={str(tmp_path / "outside"): "changed"},
            ),
            pytest.raises(harness.HarnessError, match="escaped"),
        ):
            harness.verify_runner_account_boundary(
                settings, baseline, tmp_path / "report.json"
            )


def test_hosted_mac_seeds_native_account_only_after_explicit_guard(
    tmp_path: Path,
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        harness.claim_profile(settings.layout)
        preferences = harness.startup_preferences(settings.layout)
        with (
            patch.object(harness, "startup_preferences", return_value=preferences),
            patch.object(
                harness,
                "run",
                return_value=MagicMock(stdout=preferences["mcp-score/profileNonce"]),
            ) as run,
        ):
            harness.seed_macos_runner(settings, account)
        commands = [call.args[0] for call in run.call_args_list]
        assert commands[0][:3] == [
            "defaults",
            "write",
            harness.MACOS_PREFERENCES_DOMAIN,
        ]
        assert commands[-1] == [
            "defaults",
            "read",
            harness.MACOS_PREFERENCES_DOMAIN,
            "mcp-score.profileNonce",
        ]
        assert (account.data_dir / "extensions/config.json").is_file()
        assert not settings.layout.preferences.file.exists()


def test_runner_data_root_symlink_is_refused(tmp_path: Path) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        account.data_dir.parent.mkdir(parents=True)
        target = account.home / "elsewhere"
        target.mkdir()
        account.data_dir.symlink_to(target, target_is_directory=True)
        with pytest.raises(harness.HarnessError, match="symlinked"):
            harness.require_macos_runner(settings)
        assert target.is_dir()


@pytest.mark.parametrize(
    "location", ["data", "library", "preferences", "session", "symlink", "alias-parent"]
)
def test_native_mac_namespace_cannot_be_claimed_or_deleted(
    tmp_path: Path, location: str
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        roots = {
            "data": account.data_dir,
            "library": account.home / "Library",
            "preferences": account.preferences_file,
            "session": account.data_dir / "session/child",
            "symlink": account.home / "artifact-alias",
            "alias-parent": account.home
            / "library-alias/Application Support/MuseScore/MuseScore4",
        }
        root = roots[location]
        if location == "alias-parent":
            account.data_dir.mkdir(parents=True)
            (account.home / "library-alias").symlink_to(
                account.home / "Library", target_is_directory=True
            )
        elif location == "symlink":
            account.data_dir.mkdir(parents=True)
            root.symlink_to(account.data_dir, target_is_directory=True)
        else:
            root.mkdir(parents=True)
        # Even a pre-existing valid marker cannot authorize adopting native data.
        (root / harness.PROFILE_MARKER).write_text(harness.PROFILE_MARKER_CONTENT)
        sentinel = root / "native-sentinel"
        sentinel.write_text("untouched")
        dangerous = replace(settings, layout=harness.layout_for("Darwin", root))
        with pytest.raises(harness.HarnessError, match="overlaps"):
            harness.require_macos_runner(dangerous)
        for operation in (
            lambda: harness.claim_profile(dangerous.layout),
            lambda: harness.install(dangerous),
            lambda: harness.start(dangerous, tmp_path / "score.musicxml"),
            lambda: harness.stop(dangerous),
            lambda: harness.clean(dangerous),
        ):
            with (
                patch.object(harness, "stop_owned") as stop_owned,
                pytest.raises(harness.HarnessError, match="overlaps"),
            ):
                operation()
            stop_owned.assert_not_called()
            assert sentinel.read_text() == "untouched"
            assert (root / harness.PROFILE_MARKER).is_file()
        with pytest.raises(harness.HarnessError, match="overlaps"):
            harness.clean(replace(dangerous, macos_runner_mode=False))
        assert sentinel.read_text() == "untouched"


def test_mac_default_cache_artifact_root_is_disjoint_from_native_paths(
    tmp_path: Path,
) -> None:
    with hosted_mac(tmp_path) as (settings, account):
        root = account.home / "work/mcp-score/.musescore/profile"
        legitimate = replace(settings, layout=harness.layout_for("Darwin", root))
        assert harness.require_macos_runner(legitimate) == account
        harness.claim_profile(legitimate.layout)
        harness.clean(legitimate)
        assert not root.exists()
        assert account.home.exists()
