"""Build and smoke-test the wheel in a fresh environment, outside source imports.

Requires uv and access to build/runtime dependencies. This delivery check is
separate from offline pytest and never connects to a notation application.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from importlib.metadata import metadata
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = Path("musescore/plugin")
_SKILL = Path(".claude/skills/score-generate")
_REQUIRED_TOOLS = {
    "get_musescore_bridge_info",
    "read_passage",
    "get_event_at_position",
    "read_current_selection",
    "add_live_chord_tone",
    "remove_live_chord_tone",
    "fill_live_rest",
    "set_live_lyric_text",
    "replace_live_note_with_rest",
    "set_live_note_pitch",
    "set_live_event_duration",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest() -> dict[str, str]:
    files = {
        path.relative_to(_ROOT / "src/mcp_score").as_posix(): digest(path)
        for path in (_ROOT / "src/mcp_score").rglob("*")
        if path.is_file() and path.suffix in {".py", ".qml", ".js"}
    }
    for path in (_ROOT / _SKILL).rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            files[(_SKILL / path.relative_to(_ROOT / _SKILL)).as_posix()] = digest(path)
    return files


async def probe_installed(manifest_file: Path) -> dict[str, object]:
    # These imports must resolve only inside the newly installed wheel's venv.
    import mcp_score
    from mcp_score.cli import install_plugin
    from mcp_score.guide import load_guide
    from mcp_score.resources import PLUGIN_DIRECTORY, SKILL_DIRECTORY, package_path
    from mcp_score.server import create_server

    assert mcp_score.__file__ is not None
    package = Path(mcp_score.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    manifest = json.loads(manifest_file.read_text())
    expected = manifest["files"]
    for name, checksum in expected.items():
        installed_file = package / name
        assert installed_file.is_file(), f"Missing wheel resource: {name}"
        assert digest(installed_file) == checksum, f"Changed wheel resource: {name}"
    project = manifest["project"]
    installed_metadata = metadata(project["name"])
    assert installed_metadata["Version"] == project["version"]
    installed_urls = dict(
        value.split(", ", 1) for value in installed_metadata.get_all("Project-URL", [])
    )
    assert installed_urls == project["urls"], installed_urls

    plugin = package_path(str(PLUGIN_DIRECTORY)).resolve()
    skill = package_path(str(SKILL_DIRECTORY)).resolve()
    assert plugin == package / _PLUGIN, plugin
    assert skill == package / _SKILL, skill
    assert load_guide(), "Bundled generation guide is empty"
    imports = re.compile(r'^\s*\.?import\s+"([^"]+)"', re.MULTILINE)
    resources = [path for path in plugin.rglob("*") if path.suffix in {".qml", ".js"}]
    for resource in resources:
        for reference in imports.findall(resource.read_text()):
            target = (resource.parent / reference).resolve()
            assert target.is_relative_to(plugin) and target.is_file(), (
                resource,
                reference,
            )
        assert "_testSelectionSetup" not in resource.read_text(), resource
    assert not (plugin / "selection-fixture.js").exists()
    assert not (plugin / "chord-tone-fixture.js").exists()

    probe_root = manifest_file.parent / "installed-probe"
    probe_root.mkdir()  # Never adopt or replace a pre-existing directory.
    copied_plugin = install_plugin(probe_root / "plugins")
    copied_files = {
        path.relative_to(copied_plugin).as_posix(): digest(path)
        for path in copied_plugin.rglob("*")
        if path.is_file()
    }
    packaged_files = {
        path.relative_to(plugin).as_posix(): digest(path)
        for path in plugin.rglob("*")
        if path.is_file()
    }
    assert copied_files == packaged_files, "Plugin installation changed bundled files"

    tools = await create_server().list_tools()
    assert {tool.name for tool in tools} >= _REQUIRED_TOOLS
    for tool in tools:
        if tool.name != "score_generation_guide":
            assert tool.output_schema, f"Missing output schema: {tool.name}"
            json.dumps(tool.output_schema)
    for name in (
        "current_selection",
        "duration_edit",
        "event_address",
        "pitch",
        "passage",
    ):
        module = importlib.import_module(f"mcp_score.bridge.{name}")
        assert module.__file__ and Path(module.__file__).resolve().is_relative_to(
            package
        )
    for name, module in tuple(sys.modules.items()):
        if name.startswith("mcp_score") and module and module.__file__:
            assert Path(module.__file__).resolve().is_relative_to(package), name
    return {
        "version": installed_metadata["Version"],
        "project_urls": installed_urls,
        "package_path": str(package),
        "source_imports": False,
        "verified_resource_files": len(expected),
        "plugin_imports_resolved": True,
        "plugin_installed_into_temporary_directory": True,
        "registered_tools": sorted(tool.name for tool in tools),
    }


def build_and_probe(uv: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-score-wheel-") as directory:
        scratch = Path(directory).resolve()
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(scratch / "dist"), str(_ROOT)],
            check=True,
        )
        wheels = list((scratch / "dist").glob("*.whl"))
        assert len(wheels) == 1, wheels
        environment = scratch / "environment"
        subprocess.run(
            [uv, "venv", "--python", sys.executable, str(environment)], check=True
        )
        binary = environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            [uv, "pip", "install", "--python", str(binary), str(wheels[0])], check=True
        )
        manifest = scratch / "manifest.json"
        project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
        manifest.write_text(
            json.dumps(
                {
                    "files": source_manifest(),
                    "project": {
                        key: project[key] for key in ("name", "version", "urls")
                    },
                }
            )
        )
        subprocess.run(
            [
                str(binary),
                "-I",
                str(Path(__file__).resolve()),
                "--probe",
                str(manifest),
            ],
            cwd=scratch,
            check=True,
        )
        entrypoint = environment / (
            "Scripts/mcp-score.exe" if sys.platform == "win32" else "bin/mcp-score"
        )
        clean_environment = dict(os.environ)
        clean_environment.pop("PYTHONPATH", None)
        clean_environment.pop("PYTHONHOME", None)
        help_result = subprocess.run(
            [str(entrypoint), "--help"],
            cwd=scratch,
            text=True,
            capture_output=True,
            check=True,
            env=clean_environment,
        )
        assert "install-plugin" in help_result.stdout
        sys.stdout.write(
            json.dumps(
                {"wheel": wheels[0].name, "sha256": digest(wheels[0]), "cli_help": True}
            )
            + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable (default: on PATH)")
    parser.add_argument("--probe", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.probe:
        sys.stdout.write(
            json.dumps(asyncio.run(probe_installed(arguments.probe)), indent=2) + "\n"
        )
        return
    uv = shutil.which(arguments.uv)
    if uv is None:
        parser.error("uv is required for the wheel smoke check")
    build_and_probe(str(Path(uv).resolve()))


if __name__ == "__main__":
    main()
