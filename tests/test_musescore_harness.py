"""Tests for scripts/musescore_harness.py: platform layout and config seeding."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
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
            tmp_path / ".config/MuseScore/MuseScore4.ini"
        )
        assert layout.data_dir == tmp_path / ".local/share/MuseScore/MuseScore4"
        assert layout.plugins_dir == tmp_path / "Documents/MuseScore4/Plugins"
        assert layout.log_dir == layout.data_dir / "logs"

    def test_macos_uses_native_preferences_and_library_data(
        self, tmp_path: Path
    ) -> None:
        # Act
        layout = harness.layout_for("Darwin", tmp_path)

        # Assert
        assert layout.preferences == harness.MacOSPreferences(
            "org.musescore.MuseScore4"
        )
        assert (
            layout.data_dir
            == tmp_path / "Library/Application Support/MuseScore/MuseScore4"
        )
        assert layout.plugins_dir == tmp_path / "Documents/MuseScore4/Plugins"

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
            tmp_path / "Roaming/MuseScore/MuseScore4.ini"
        )
        assert layout.data_dir == tmp_path / "Local/MuseScore/MuseScore4"


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
                "name": "MuseScore-Studio-4.7.5.260831071-x86_64.msi",
                "browser_download_url": "https://example.test/x86_64.msi",
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
            ("Windows", "https://example.test/x86_64.msi"),
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
        settings = (tmp_path / ".config/MuseScore/MuseScore4.ini").read_text()
        assert settings == (
            "[application]\n"
            "hasCompletedFirstLaunchSetup=true\n"
            "welcomeDialogShowOnStartup=false\n"
            "welcomeDialogLastShownVersion=99.0.0\n"
            "checkForUpdate=false\n"
        )
        assert not stale_session.exists()

    def test_macos_writes_preferences_through_defaults(self, tmp_path: Path) -> None:
        # Arrange
        layout = harness.layout_for("Darwin", tmp_path)
        run = MagicMock()

        with patch.object(harness, "run", run):
            # Act
            harness.seed_configuration(layout)

        # Assert: Qt maps the settings path a/b to the key a.b, typed values
        commands = [call.args[0] for call in run.call_args_list]
        assert commands == [
            [
                "defaults",
                "write",
                "org.musescore.MuseScore4",
                "application.hasCompletedFirstLaunchSetup",
                "-bool",
                "true",
            ],
            [
                "defaults",
                "write",
                "org.musescore.MuseScore4",
                "application.welcomeDialogShowOnStartup",
                "-bool",
                "false",
            ],
            [
                "defaults",
                "write",
                "org.musescore.MuseScore4",
                "application.welcomeDialogLastShownVersion",
                "-string",
                "99.0.0",
            ],
            [
                "defaults",
                "write",
                "org.musescore.MuseScore4",
                "application.checkForUpdate",
                "-bool",
                "false",
            ],
        ]


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
            patch.object(harness.time, "sleep"),
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
            pytest.raises(harness.HarnessError, match="exited during startup"),
        ):
            # Act / Assert
            harness.launch(self._settings(tmp_path), tmp_path / "s.xml")

        assert popen.call_count == harness.LAUNCH_ATTEMPTS


class TestMuseScoreProcessPattern:
    def test_linux_matches_the_appimage_binary_path(self) -> None:
        # The AppImage's binary renames its process, so the pattern must be
        # something that survives in the command line.
        assert harness.musescore_process_pattern("Linux") == "bin/mscore4portable"

    def test_windows_matches_the_image_name(self) -> None:
        assert harness.musescore_process_pattern("Windows") == "MuseScore4.exe"


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
        ):
            harness.trigger_plugin(settings)

        assert commands[0] == [
            "xdotool",
            "search",
            "--onlyvisible",
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
        ):
            harness.trigger_plugin(settings)
        assert ["xdotool", "windowfocus", "--sync", "42"] in commands

    def test_linux_missing_visible_window_reports_startup_failure(self) -> None:
        settings = MagicMock(system="Linux")
        with (
            patch.object(harness, "run", return_value=MagicMock(stdout="")),
            patch.object(harness, "print_log_tails") as diagnostics,
            pytest.raises(harness.HarnessError, match="no MuseScore window"),
        ):
            harness.trigger_plugin(settings)
        diagnostics.assert_called_once_with(settings.layout)


class TestBridgeReadiness:
    def test_start_requires_responsive_bridge_after_menu_activation(
        self, tmp_path: Path
    ) -> None:
        executable = tmp_path / "MuseScore"
        executable.touch()
        settings = MagicMock(system="Windows", executable=executable)
        wait = AsyncMock(return_value=False)
        trigger = MagicMock()
        with (
            patch.object(harness, "seed_configuration"),
            patch.object(harness, "launch"),
            patch.object(harness, "trigger_plugin", trigger),
            patch.object(harness, "wait_for_bridge", wait),
            patch.object(harness, "print_log_tails") as diagnostics,
            pytest.raises(harness.HarnessError, match="bridge plugin did not answer"),
        ):
            harness.start(settings, tmp_path / "fixture.musicxml")
        trigger.assert_called_once_with(settings)
        wait.assert_awaited_once_with("ws://localhost:8765", 60.0)
        diagnostics.assert_called_once_with(settings.layout)
