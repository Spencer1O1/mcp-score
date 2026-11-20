"""Tests for scripts/musescore_harness.py: platform layout and config seeding."""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_score.musescore.paths import PLUGIN_DIRECTORY_NAME, PLUGIN_QML_NAME

if TYPE_CHECKING:
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
    @pytest.mark.parametrize("system", ["Linux", "Windows", "Darwin"])
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
        if system == "Linux":
            assert environment["XDG_DATA_HOME"] == str(settings.layout.root / "data")
        if system == "Darwin":
            assert environment["CFFIXED_USER_HOME"] == str(settings.layout.root)
            assert environment["__CFPREFERENCES_AVOID_DAEMON"] == "1"
        if system == "Windows":
            assert settings.executable.is_relative_to(settings.layout.root)
            assert "Program Files" not in str(settings.executable)

    def test_macos_probe_reads_private_native_domain_only(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, "Darwin")
        harness.seed_configuration(settings.layout)
        with settings.layout.preferences.file.open("rb") as source:
            nonce = plistlib.load(source)["mcp-score.profileNonce"]
        responses = [
            MagicMock(stdout=nonce + "\n"),
            MagicMock(
                stdout=str(settings.layout.root / "Library/Application Support") + "\n"
            ),
        ]
        with patch.object(harness, "run", side_effect=responses) as run:
            harness.verify_macos_preferences(settings)
        first = run.call_args_list[0]
        assert first.args[0][0:2] == ["defaults", "read"]
        assert first.kwargs["env"]["CFFIXED_USER_HOME"] == str(settings.layout.root)

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
