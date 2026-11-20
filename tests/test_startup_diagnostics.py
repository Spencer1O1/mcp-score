"""Bounded startup evidence, ownership refusals and actual helper failure paths."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_musescore_harness import harness, make_settings

_PROBE_PATH = Path(__file__).parents[1] / "scripts/macos_startup_probe.py"
_SPEC = importlib.util.spec_from_file_location("macos_startup_probe", _PROBE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.fixture
def owned(tmp_path: Path):
    settings = make_settings(tmp_path)
    harness.claim_profile(settings.layout)
    harness.prepare_directories(settings.layout)
    harness.save_owned(settings.layout, [])
    return settings


def test_latest_report_is_bounded_atomic_and_does_not_follow_links(owned) -> None:
    layout = owned.layout
    destination = layout.root / harness.STARTUP_DIAGNOSTICS_FILE
    harness.write_startup_diagnostics(layout, {"version": 1, "phase": "first"})
    original = destination.read_bytes()
    with pytest.raises(harness.HarnessError, match="size bound"):
        harness.write_startup_diagnostics(layout, {"large": "x" * 65536})
    assert destination.read_bytes() == original
    replacement = "rename" if os.name != "nt" else None
    if replacement:
        with (
            patch.object(harness.os, replacement, side_effect=OSError("disk failed")),
            pytest.raises(OSError, match="disk failed"),
        ):
            harness.write_startup_diagnostics(layout, {"version": 2})
        assert destination.read_bytes() == original
        assert not list(layout.root.glob(".startup-diagnostics-*.tmp"))
    harness.write_startup_diagnostics(layout, {"version": 1, "phase": "ready"})
    assert json.loads(destination.read_bytes())["phase"] == "ready"


@pytest.mark.parametrize(
    "linked", ["root", "parent", "marker", "state", "report", "stdout", "plugins"]
)
def test_linked_controls_refuse_before_read_or_write(
    owned, tmp_path: Path, linked: str
) -> None:
    layout = owned.layout
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    paths = {
        "root": layout.root,
        "marker": layout.root / harness.PROFILE_MARKER,
        "state": layout.state_file,
        "report": layout.root / harness.STARTUP_DIAGNOSTICS_FILE,
        "stdout": layout.stdout_log,
        "plugins": layout.plugins_dir,
    }
    if linked == "parent":
        parent = tmp_path / "alias"
        parent.symlink_to(layout.root.parent, target_is_directory=True)
        layout = harness.layout_for("Linux", parent / layout.root.name)
    else:
        path = paths[linked]
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        path.symlink_to(
            outside if linked in {"root", "plugins"} else sentinel,
            target_is_directory=linked in {"root", "plugins"},
        )
    with pytest.raises(harness.HarnessError, match="[Ll]inked"):
        harness.write_startup_diagnostics(layout, {"phase": "failed"})
    assert sentinel.read_text() == "untouched"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_absent_marker_never_adopts_root(owned) -> None:
    marker = owned.layout.root / harness.PROFILE_MARKER
    marker.unlink()
    with pytest.raises(harness.HarnessError, match="marked owned"):
        harness.write_startup_diagnostics(owned.layout, {})
    assert not marker.exists()


def test_process_snapshot_discards_reused_identity_without_unowned_details(
    owned,
) -> None:
    identity = harness.OwnedProcess(42, 123.0, "/owned/mscore", "musescore")
    harness.save_owned(owned.layout, [identity])
    process = SimpleNamespace(status=lambda: "sleeping")
    with patch.object(harness, "matching_process", side_effect=[process, None]):
        result = harness.startup_process_status(owned.layout)
    assert result == [{"pid": 42, "ownership": "identity_changed"}]
    with patch.object(harness, "matching_process", return_value=None):
        result = harness.startup_process_status(owned.layout)
    assert result == [{"pid": 42, "ownership": "exited_or_identity_changed"}]


def test_launch_records_first_exit_and_tail_before_retry_overwrites_stdout(
    owned,
) -> None:
    trace = harness.StartupDiagnostics(
        owned, Path("tests/integration/fixtures/fixture.musicxml")
    )
    first = SimpleNamespace(pid=41, poll=lambda: 7)
    second = SimpleNamespace(pid=42, poll=lambda: None)
    process_identity = harness.OwnedProcess(41, 123.0, "/owned/mscore", "musescore")

    def start(settings, score):
        if not owned.layout.stdout_log.exists():
            owned.layout.stdout_log.write_text("first crash evidence")
            return first
        owned.layout.stdout_log.write_text("second attempt")
        return second

    with (
        patch.object(harness, "_start", side_effect=start),
        patch.object(harness, "track_process"),
        patch.object(harness, "refresh_owned"),
        patch.object(harness, "STARTUP_SECONDS", 0),
        patch.object(
            harness,
            "startup_process_status",
            return_value=[{**asdict(process_identity), "ownership": "verified"}],
        ),
    ):
        assert harness.launch(owned, Path("fixture.xml"), trace) is second
    report = json.loads(
        (owned.layout.root / harness.STARTUP_DIAGNOSTICS_FILE).read_bytes()
    )
    first_attempt, second_attempt = report["attempts"]
    assert first_attempt["exit_code"] == 7
    assert first_attempt["stdout_tail"] == "first crash evidence"
    assert first_attempt["initial_processes"][0]["created"] == 123.0
    assert first_attempt["elapsed_seconds"] >= 0
    assert second_attempt["state"] == "running" and second_attempt["exit_code"] is None
    assert not Path(report["fixture"]).is_absolute()


@pytest.mark.parametrize("write_fails", [False, True])
def test_original_start_error_survives_diagnostics_and_cleanup_failure(
    owned, tmp_path: Path, write_fails: bool, caplog
) -> None:
    score = tmp_path / "fixture.xml"
    score.write_text("fixture")
    primary = harness.HarnessError("original launch error")
    writes = (
        patch.object(
            harness, "write_startup_diagnostics", side_effect=OSError("disk full")
        )
        if write_fails
        else patch.object(
            harness,
            "startup_window_status",
            return_value={"status": "permission_denied", "windows": []},
        )
    )

    def cleanup(layout):
        if not write_fails:
            report = json.loads(
                (layout.root / harness.STARTUP_DIAGNOSTICS_FILE).read_bytes()
            )
            assert (
                report["phase"] == "failed" and report["failure_phase"] == "launching"
            )
            assert report["failure"]["message"] == str(primary)
            assert report["ui"]["status"] == "permission_denied"
        raise harness.HarnessError("secondary cleanup refusal")

    with (
        patch.object(harness, "preflight"),
        patch.object(harness, "start_xvfb"),
        patch.object(harness, "seed_configuration"),
        patch.object(harness, "launch", side_effect=primary),
        patch.object(harness, "stop_owned", side_effect=cleanup),
        writes,
        pytest.raises(harness.HarnessError) as caught,
    ):
        harness.start(owned, score)
    assert caught.value is primary
    assert "secondary cleanup refusal" in caplog.text
    if write_fails:
        assert "diagnostics unavailable" in caplog.text


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("print('not JSON')", "malformed_output"),
        ("print('x'*40000)", "output_limit"),
        ("import time;time.sleep(10)", "timeout"),
        ('print(\'{"status":"permission_denied","windows":[]}\')', "permission_denied"),
        (
            'print(\'{"status":"captured","windows":[{"pid":999}]}\')',
            "malformed_output",
        ),
        ("raise SystemExit(4)", "helper_failed"),
    ],
)
def test_actual_fixed_child_output_limits_timeout_and_malformed_reply(
    tmp_path: Path, body: str, status: str
) -> None:
    script = tmp_path / "fixed-probe.py"
    script.write_text(body)
    with (
        patch.object(harness, "STARTUP_UI_HELPER", script),
        patch.object(harness, "STARTUP_UI_SECONDS", 0.25),
    ):
        result = harness._run_startup_ui_helper(
            {"root": "/owned", "identity": {"pid": 42}}
        )
    assert result["status"] == status and result["windows"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pipe capacity control")
def test_large_accepted_input_to_nonreading_helper_obeys_deadline_and_reaps_child(
    owned,
    tmp_path: Path,
) -> None:
    script = tmp_path / "nonreading-probe.py"
    script.write_text("import time; time.sleep(2)")
    request = {"root": "x" * 7000, "identity": {"pid": 42}}
    assert 4096 < len(json.dumps(request).encode()) < 8192
    popen = subprocess.Popen
    children = []

    def spawn(*arguments, **keywords):
        # Deliberately reduce capacity to force backpressure. This is not an
        # assertion about the default capacity on any native runner platform.
        child = popen(*arguments, **keywords, pipesize=4096)
        children.append(child)
        return child

    with (
        patch.object(harness, "STARTUP_UI_HELPER", script),
        patch.object(harness, "STARTUP_UI_SECONDS", 0.1),
        patch.object(harness.subprocess, "Popen", side_effect=spawn),
    ):
        started = time.monotonic()
        result = harness._run_startup_ui_helper(request)
        elapsed = time.monotonic() - started
    assert result == {"status": "timeout", "windows": []}
    assert elapsed < 1
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdin.closed and children[0].stdout.closed

    # A diagnostic transport timeout remains diagnostic data, never the reason
    # replacing the original failure that this report was meant to explain.
    original = harness.HarnessError("original bridge timeout")
    trace = harness.StartupDiagnostics(owned, Path("fixture.xml"))
    with patch.object(harness, "startup_window_status", return_value=result):
        trace.failure(original)
    assert trace.report["failure"]["message"] == str(original)
    assert trace.report["ui"]["status"] == "timeout"


def test_no_macos_helper_on_other_platforms_or_default_local_mac(owned) -> None:
    with patch.object(harness, "_run_startup_ui_helper") as helper:
        assert harness.startup_window_status(owned)["status"] == "not_applicable"
        with pytest.raises(harness.HarnessError, match="Local/default"):
            harness.startup_window_status(replace(owned, system="Darwin"))
    helper.assert_not_called()


def test_parent_discards_capture_if_recorded_pid_changes_after_helper(owned) -> None:
    settings = replace(owned, system="Darwin")
    identity = harness.OwnedProcess(
        42, 123.0, str(settings.executable.resolve()), "musescore"
    )
    harness.save_owned(settings.layout, [identity])
    with (
        patch.object(harness, "require_macos_runner"),
        patch.object(harness, "matching_process", side_effect=[object(), None]),
        patch.object(
            harness,
            "_run_startup_ui_helper",
            return_value={"status": "captured", "windows": [{"title": "must discard"}]},
        ),
    ):
        assert harness.startup_window_status(settings) == {
            "status": "identity_changed",
            "windows": [],
        }


class FakeAccessibility:
    def __init__(self, *, trusted: bool = True, wrong_owner: int | None = None) -> None:
        self.permission = trusted
        self.wrong_owner = wrong_owner
        self.reads = []
        self.released = []

    def trusted(self):
        return self.permission

    def application(self, pid):
        assert pid == 42
        return 1

    def owner(self, element):
        return 999 if element == self.wrong_owner else 42

    def timeout(self, element):
        pass

    def children(self, element, name, limit):
        if name == "AXWindows":
            assert limit == probe.MAX_WINDOWS
            return [2, 3], True
        assert name == "AXSheets" and limit == probe.MAX_SHEETS
        return ([4] if element == 2 else []), False

    def attribute(self, element, name):
        assert self.owner(element) == 42
        self.reads.append((element, name))
        return name == "AXModal" if name == "AXModal" else "fixture"

    def release(self, element):
        self.released.append(element)


def test_pid_scoped_windows_and_direct_sheets_have_bounded_metadata_only() -> None:
    api = FakeAccessibility()
    result = probe.inspect_windows(api, 42)
    assert result["status"] == "captured" and result["truncated"]
    assert len(result["windows"]) == 2 and len(result["windows"][0]["sheets"]) == 1
    assert result["windows"][0]["sheets"][0]["pid"] == 42
    assert sorted(api.released) == [1, 2, 3, 4]
    assert {name for _, name in api.reads} == {
        "AXTitle",
        "AXRole",
        "AXSubrole",
        "AXModal",
    }


@pytest.mark.parametrize("element", [1, 2, 4])
def test_wrong_owner_is_rejected_before_reading_its_attributes(element: int) -> None:
    api = FakeAccessibility(wrong_owner=element)
    with pytest.raises(probe.OwnershipChangedError):
        probe.inspect_windows(api, 42)
    assert not any(target == element for target, _ in api.reads)
    assert api.released


def test_permission_query_never_requests_grants_or_inspects_windows() -> None:
    api = FakeAccessibility(trusted=False)
    assert probe.inspect_windows(api, 42) == {
        "status": "permission_denied",
        "windows": [],
    }
    assert api.reads == api.released == []


def test_helper_independently_requires_hosted_boundary_before_loading_native_api() -> (
    None
):
    with (
        patch.object(probe, "Accessibility") as api,
        pytest.raises(harness.HarnessError, match="Local/default"),
    ):
        probe.probe_owned({"root": "/unowned", "identity": {}})
    api.assert_not_called()


def test_collector_copies_report_but_never_follows_linked_report(
    owned, tmp_path: Path
) -> None:
    harness.write_startup_diagnostics(owned.layout, {"version": 1, "phase": "ready"})
    report = owned.layout.root / harness.STARTUP_DIAGNOSTICS_FILE
    destination = tmp_path / "logs"
    harness.collect_logs(owned, destination)
    assert (destination / report.name).read_bytes() == report.read_bytes()
    report.unlink()
    secret = tmp_path / "outside.json"
    secret.write_text("outside sentinel")
    report.symlink_to(secret)
    harness.collect_logs(owned, tmp_path / "linked-logs")
    assert not (tmp_path / "linked-logs" / report.name).exists()
    assert secret.read_text() == "outside sentinel"


def test_linked_operation_lock_refuses_diagnostics_without_touching_target(
    owned, tmp_path: Path
) -> None:
    target = tmp_path / "outside-lock"
    target.write_text("untouched")
    lock = owned.layout.root.parent / f".{owned.layout.root.name}.harness.lock"
    lock.symlink_to(target)
    with pytest.raises(harness.HarnessError, match="operation lock"):
        harness.write_startup_diagnostics(owned.layout, {})
    assert target.read_text() == "untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory descriptor protection")
def test_root_alias_change_during_write_keeps_outside_files_untouched(
    owned, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / harness.STARTUP_DIAGNOSTICS_FILE
    sentinel.write_text("untouched")
    original = harness.validate_startup_diagnostic_paths
    calls = 0
    moved = tmp_path / "original-owned-root"

    def validate(layout):
        nonlocal calls
        calls += 1
        if calls == 2:
            layout.root.rename(moved)
            layout.root.symlink_to(outside, target_is_directory=True)
        original(layout)

    with (
        patch.object(
            harness, "validate_startup_diagnostic_paths", side_effect=validate
        ),
        pytest.raises(harness.HarnessError, match="[Ll]inked"),
    ):
        harness.write_startup_diagnostics(owned.layout, {})
    assert sentinel.read_text() == "untouched"
    assert not list(moved.glob(".startup-diagnostics-*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO refusal")
def test_special_control_file_refused_before_blocking_read(owned) -> None:
    owned.layout.state_file.unlink()
    os.mkfifo(owned.layout.state_file)
    with pytest.raises(harness.HarnessError, match="Non-regular"):
        harness.write_startup_diagnostics(owned.layout, {})


@pytest.mark.parametrize(
    "case",
    ["unrecorded", "reused", "wrong_executable", "foreign_handoff", "changed_after"],
)
def test_helper_independently_checks_persisted_and_current_identity(
    owned, case: str
) -> None:
    settings = replace(owned, system="Darwin")
    identity = harness.OwnedProcess(
        42, 123.0, str(settings.executable.resolve()), "musescore"
    )
    if case == "wrong_executable":
        identity = replace(identity, executable="/outside/mscore")
    if case == "foreign_handoff":
        identity = replace(identity, handoff_executable="/outside/other")
    harness.save_owned(settings.layout, [] if case == "unrecorded" else [identity])
    process = None if case == "reused" else object()
    checks = [process, None] if case == "changed_after" else [process]
    with (
        patch.object(harness.Settings, "from_environment", return_value=settings),
        patch.object(harness, "require_macos_runner"),
        patch.object(harness, "matching_process", side_effect=checks),
        patch.object(probe, "Accessibility", return_value=object()) as factory,
        patch.object(
            probe,
            "inspect_windows",
            return_value={
                "status": "captured",
                "windows": [{"pid": 42, "title": "discard"}],
            },
        ),
    ):
        request = {
            "root": str(settings.layout.root.absolute()),
            "identity": asdict(identity),
        }
        if case == "changed_after":
            assert probe.probe_owned(request) == {
                "status": "identity_changed",
                "windows": [],
            }
        else:
            with pytest.raises(probe.ProbeError, match="identity"):
                probe.probe_owned(request)
            factory.assert_not_called()
