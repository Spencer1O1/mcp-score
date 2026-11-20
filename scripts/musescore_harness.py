"""Install and drive a real MuseScore Studio for the integration tests.

Subcommands:

    install         Download MuseScore into the cache and print the executable
    start <score>   Launch MuseScore with a score and the bridge plugin listening
    stop            Stop only recorded, identity-verified owned processes
    collect-logs    Copy logs from the private profile
    clean           Stop owned processes and remove the private profile

Works on Linux (AppImage under Xvfb), Windows (official portable build), and
macOS (DMG). The version defaults to the newest one listed in
``tests/integration/musescore-versions.json``
and can be set with ``MUSESCORE_VERSION``; its release asset is looked up
through the GitHub releases API (``GITHUB_TOKEN`` raises the rate limit)
unless ``MUSESCORE_DOWNLOAD_URL`` names it directly. ``MUSESCORE_CACHE_DIR``
is where downloads and the unpacked application live.

The default test profile is private to the harness cache. Starting refuses an
existing application or occupied endpoint before writing configuration. HOME
and the ordinary MuseScore profile are never redirected or rewritten.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
import websockets
from websockets.exceptions import WebSocketException

from mcp_score.cli import install_plugin
from mcp_score.musescore.paths import PLUGIN_DIRECTORY_NAME, PLUGIN_QML_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "Layout",
    "Settings",
    "clean",
    "collect_logs",
    "install",
    "latest_version",
    "layout_for",
    "main",
    "release_asset_url",
    "seed_configuration",
    "start",
    "stop",
]

logger = logging.getLogger("musescore-harness")

# ── Defaults ──────────────────────────────────────────────────────────

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPOSITORY_ROOT / "tests" / "integration" / "musescore-versions.json"
RELEASES_API_URL = "https://api.github.com/repos/musescore/MuseScore/releases/tags"
# The release asset for each platform, by how its file name ends.
RELEASE_ASSET_SUFFIXES: dict[str, str] = {
    "Linux": "-x86_64.AppImage",
    "Windows": "-x86_64.paf.exe",
    "Darwin": ".dmg",
}

BRIDGE_PORT = 8765
BRIDGE_TIMEOUT_SECONDS = 60.0
BRIDGE_POLL_INTERVAL_SECONDS = 2.0
BRIDGE_ROUND_TRIP_SECONDS = 2.0
STARTUP_SECONDS = 45.0
LAUNCH_ATTEMPTS = 2
DIALOG_DISMISS_SECONDS = 2.0
LOG_TAIL_LINES = 40

# MuseScore identifies a plugin by its QML file's path under the plugins
# directory (ExtPluginsLoader::makeUri).
PLUGIN_PATH = f"{PLUGIN_DIRECTORY_NAME}/{PLUGIN_QML_NAME}"
# MuseScore 4.4 identifies plugins with a muse:// URI, 4.5 and later with
# musescore://; each version ignores the entry it does not know.
PLUGIN_URIS = (
    f"muse://extensions/v1/{PLUGIN_PATH}",
    f"musescore://extensions/v1/{PLUGIN_PATH}",
)
MENU_OPEN_SECONDS = 1.0

# Linux only: the virtual display. Not :99, which is the first display
# xvfb-run picks; the render tests run under xvfb-run in the same CI job.
XVFB_DISPLAY = os.environ.get("MUSESCORE_DISPLAY", ":87")
XVFB_SCREEN = "1600x1000x24"
XVFB_STARTUP_SECONDS = 10.0

UNREACHABLE_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    WebSocketException,
    TimeoutError,
)


class HarnessError(Exception):
    """A step of the harness failed; the message is meant for the console."""


# ── Platform layout ───────────────────────────────────────────────────

# Preferences that keep MuseScore from opening dialogs over the score
# window: the first-launch wizard, welcome dialog, and update notifications.
# MuseScore forces the welcome dialog back on whenever the last version it
# was shown for is older than the running one, so that version is set far
# into the future. Keys are Qt settings paths.
STARTUP_PREFERENCES: dict[str, bool | str] = {
    "application/hasCompletedFirstLaunchSetup": True,
    "application/welcomeDialogShowOnStartup": False,
    "application/welcomeDialogLastShownVersion": "99.0.0",
    "application/checkForUpdate": False,
}
MACOS_PREFERENCES_DOMAIN = "org.musescore.MuseScore4"
PROFILE_MARKER = ".mcp-score-owned-profile"
PROFILE_MARKER_CONTENT = "mcp-score integration profile v1\n"
PROCESS_STATE_VERSION = 1
MUSESCORE_PROCESS_NAMES = {
    "musescore4.exe",
    "mscore",
    "mscore4portable",
    "musescore",
    "musescore4",
}
PROCESS_POLL_SECONDS = 0.25
STOP_TIMEOUT_SECONDS = 5.0
PATH_PREFERENCES = (
    "myPlugins",
    "myScores",
    "myTemplates",
    "myStyles",
    "mySoundfonts",
    "myMusicFonts",
    "myVSTs",
)


@dataclass(frozen=True)
class IniPreferences:
    """A Qt ini file, as MuseScore uses on Linux and Windows."""

    file: Path

    def write(self, preferences: Mapping[str, bool | str]) -> None:
        sections: dict[str, list[str]] = {}
        for path, value in preferences.items():
            section, _, key = path.rpartition("/")
            sections.setdefault(section, []).append(f"{key}={_ini_value(value)}")
        text = "".join(
            f"[{section}]\n" + "".join(f"{line}\n" for line in lines)
            for section, lines in sections.items()
        )
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(text)


def _ini_value(value: bool | str) -> str:
    return str(value).lower() if isinstance(value, bool) else value


@dataclass(frozen=True)
class PlistPreferences:
    """An absolute private native-preferences plist, without global defaults writes."""

    file: Path

    def write(self, preferences: Mapping[str, bool | str]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        values = {key.replace("/", "."): value for key, value in preferences.items()}
        with self.file.open("wb") as output:
            plistlib.dump(values, output)


@dataclass(frozen=True)
class Layout:
    """Private test profile paths; none refer to the ordinary application profile."""

    system: str
    root: Path
    preferences: IniPreferences | PlistPreferences
    data_dir: Path
    plugins_dir: Path
    stdout_log: Path

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_file(self) -> Path:
        return self.root / "processes.json"


def layout_for(system: str, profile_root: Path) -> Layout:
    """Resolve only paths under an explicitly private test profile root.

    Windows uses the official WIN_PORTABLE build, whose QSettings and app data
    live relative to the extracted executable. macOS uses CoreFoundation's
    CFFIXED_USER_HOME; Linux uses per-child XDG directories. HOME is unchanged.
    """
    root = profile_root.absolute()
    if system == "Windows":
        data = root / "portable/Data/settings"
        preferences = IniPreferences(data / "MuseScore/MuseScore4.ini")
    elif system == "Darwin":
        data = root / "Library/Application Support/MuseScore/MuseScore4"
        preferences = PlistPreferences(
            root / "Library/Preferences" / f"{MACOS_PREFERENCES_DOMAIN}.plist"
        )
    else:
        data = root / "data/MuseScore/MuseScore4"
        preferences = IniPreferences(root / "config/MuseScore/MuseScore4.ini")
    return Layout(
        system, root, preferences, data, root / "Plugins", root / "musescore.log"
    )


def latest_version(versions_file: Path) -> str:
    """The newest MuseScore version listed in *versions_file*."""
    versions: dict[str, str] = json.loads(versions_file.read_text())
    return versions["latest"]


def fetch_json(url: str) -> Any:
    """GET *url* and decode the JSON body, sending the GitHub token if set."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request) as response:  # noqa: S310
        return json.load(response)


def release_asset_url(system: str, version: str) -> str:
    """The download URL of the *version* release asset for *system*.

    Asset names carry a build number (``MuseScore-Studio-4.7.5.260831071.dmg``)
    that only the release itself knows, so the release is looked up through
    the GitHub API and the asset chosen by its platform suffix.
    """
    suffix = RELEASE_ASSET_SUFFIXES[system]
    release = fetch_json(f"{RELEASES_API_URL}/v{version}")
    assets: list[dict[str, Any]] = release.get("assets", [])
    for asset in assets:
        if str(asset["name"]).endswith(suffix):
            return str(asset["browser_download_url"])
    raise HarnessError(f"release v{version} has no asset ending in {suffix}")


@dataclass(frozen=True)
class Settings:
    """Everything the harness needs, resolved from the environment."""

    system: str
    version: str
    cache_dir: Path
    layout: Layout
    download_url: str | None = None
    """An explicit asset URL; otherwise the release is looked up on demand."""

    @classmethod
    def from_environment(cls) -> Settings:
        system = platform.system()
        version = os.environ.get("MUSESCORE_VERSION") or latest_version(VERSIONS_FILE)
        cache_dir = Path(
            os.environ.get(
                "MUSESCORE_CACHE_DIR",
                Path.home() / ".cache" / "mcp-score" / f"musescore-{version}",
            )
        )
        profile = Path(os.environ.get("MUSESCORE_PROFILE_DIR", cache_dir / "profile"))
        layout = layout_for(system, profile)
        url = os.environ.get("MUSESCORE_DOWNLOAD_URL") or None
        return cls(system, version, cache_dir, layout, url)

    def resolve_download_url(self) -> str:
        return self.download_url or release_asset_url(self.system, self.version)

    @property
    def executable(self) -> Path:
        """Where the MuseScore executable is once ``install`` has run."""
        if self.system == "Windows":
            return self.layout.root / "portable/App/MuseScore/bin/MuseScore4.exe"
        if self.system == "Darwin":
            return self.cache_dir / "MuseScore 4.app" / "Contents" / "MacOS" / "mscore"
        return self.cache_dir / "squashfs-root" / "AppRun"


# ── install ───────────────────────────────────────────────────────────


def download(url: str, destination: Path) -> None:
    logger.info("downloading %s", url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out)


def install(settings: Settings) -> Path:
    """Make MuseScore available and return its executable."""
    executable = settings.executable
    if executable.exists():
        logger.info("MuseScore Studio %s already installed", settings.version)
        return executable

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    if settings.system == "Windows":
        claim_profile(settings.layout)
        installer = settings.cache_dir / "MuseScore.paf.exe"
        if not installer.exists():
            download(settings.resolve_download_url(), installer)
        # Extract the official WIN_PORTABLE binary. Do not execute an installer
        # or launcher that changes system installation/registry configuration.
        run(["7z", "x", "-y", f"-o{settings.layout.root / 'portable'}", str(installer)])
    elif settings.system == "Darwin":
        image = settings.cache_dir / "MuseScore.dmg"
        if not image.exists():
            download(settings.resolve_download_url(), image)
        mount_point = settings.cache_dir / "mnt"
        logger.info("mounting %s", image.name)
        run(
            [
                "hdiutil",
                "attach",
                "-nobrowse",
                "-quiet",
                "-mountpoint",
                str(mount_point),
                str(image),
            ]
        )
        try:
            shutil.copytree(
                mount_point / "MuseScore 4.app", settings.cache_dir / "MuseScore 4.app"
            )
        finally:
            run(["hdiutil", "detach", "-quiet", str(mount_point)])
        # Files copied out of a downloaded image carry the quarantine flag.
        run(
            [
                "xattr",
                "-dr",
                "com.apple.quarantine",
                str(settings.cache_dir / "MuseScore 4.app"),
            ],
            check=False,
        )
    else:
        appimage = settings.cache_dir / "MuseScore.AppImage"
        download(settings.resolve_download_url(), appimage)
        appimage.chmod(0o755)
        # Extracting avoids needing FUSE, which CI runners and containers lack.
        logger.info("extracting AppImage")
        run([str(appimage), "--appimage-extract"], cwd=settings.cache_dir)
        appimage.unlink()

    if not executable.exists():
        raise HarnessError(f"installation did not produce {executable}")
    return executable


# ── start ─────────────────────────────────────────────────────────────


def seed_configuration(layout: Layout) -> None:
    """Install and enable the bridge as the only manually activated plugin.

    Menu activation selects the only enabled category and plugin, without
    depending on persisted shortcuts or desktop coordinates.
    """
    install_plugin(layout.plugins_dir)

    preferences = dict(STARTUP_PREFERENCES)
    preferences["mcp-score/profileNonce"] = uuid.uuid4().hex
    for name in PATH_PREFERENCES:
        destination = layout.plugins_dir if name == "myPlugins" else layout.root / name
        preferences[f"application/paths/{name}"] = str(destination)
    layout.preferences.write(preferences)

    extensions_dir = layout.data_dir / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    enabled = [
        {"uri": uri, "actions": [{"code": "main", "exec_point": "manually"}]}
        for uri in PLUGIN_URIS
    ]
    (extensions_dir / "config.json").write_text(json.dumps(enabled))

    # A leftover session from a previous (killed) run makes MuseScore ask
    # whether to restore it, and that modal dialog steals focus.
    shutil.rmtree(layout.data_dir / "session", ignore_errors=True)


def child_environment(settings: Settings) -> dict[str, str]:
    """Isolate child-process application paths without changing HOME."""
    root = settings.layout.root
    environment = dict(os.environ)
    environment["TMPDIR"] = str(root / "tmp")
    environment["TMP"] = str(root / "tmp")
    environment["TEMP"] = str(root / "tmp")
    if settings.system == "Darwin":
        environment["CFFIXED_USER_HOME"] = str(root)
    elif settings.system == "Windows":
        # Qt itself uses WIN_PORTABLE paths, not these environment variables;
        # these also keep environment-based library caches in the private root.
        environment["APPDATA"] = str(root / "Roaming")
        environment["LOCALAPPDATA"] = str(root / "Local")
    else:
        environment.update(
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DISPLAY": XVFB_DISPLAY,
            }
        )
    return environment


def claim_profile(layout: Layout) -> None:
    """Reserve a new private directory, refusing to adopt existing user data."""
    root = layout.root
    marker = root / PROFILE_MARKER
    if root.is_symlink():
        raise HarnessError("The test profile must not be a symlink")
    for path in (
        layout.preferences.file,
        layout.data_dir,
        layout.plugins_dir,
        layout.stdout_log,
        *(layout.root / name for name in PATH_PREFERENCES),
    ):
        if not path.resolve().is_relative_to(root.resolve()):
            raise HarnessError(f"Profile path escapes the private root: {path}")
    if root.exists():
        for path in root.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                raise HarnessError(f"Private profile symlink escapes its root: {path}")
    if marker.exists():
        if marker.read_text() != PROFILE_MARKER_CONTENT:
            raise HarnessError("Unrecognized test profile ownership marker")
        return
    if root.exists() and any(root.iterdir()):
        raise HarnessError("Refusing to adopt a nonempty unowned profile directory")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(PROFILE_MARKER_CONTENT)


@dataclass(frozen=True)
class OwnedProcess:
    """A launched PID plus start time and executable; PID alone is not ownership."""

    pid: int
    created: float
    executable: str
    kind: str

    def __post_init__(self) -> None:
        if (
            type(self.pid) is not int
            or self.pid <= 0
            or type(self.created) not in {int, float}
            or not math.isfinite(self.created)
            or self.created <= 0
            or not isinstance(self.executable, str)
            or not self.executable
            or self.kind not in {"musescore", "xvfb"}
        ):
            raise ValueError("Invalid process identity")


def identify(process: psutil.Process, kind: str) -> OwnedProcess:
    return OwnedProcess(process.pid, process.create_time(), process.exe(), kind)


def matching_process(identity: OwnedProcess) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if (
            process.create_time() != identity.created
            or os.path.normcase(process.exe()) != os.path.normcase(identity.executable)
            or process.status() == psutil.STATUS_ZOMBIE
        ):
            return None
        return process
    except psutil.NoSuchProcess, psutil.ZombieProcess:
        return None
    except psutil.AccessDenied as error:
        raise HarnessError(f"Cannot verify ownership of PID {identity.pid}") from error


def read_owned(layout: Layout) -> list[OwnedProcess]:
    if not layout.state_file.exists():
        return []
    try:
        data = json.loads(layout.state_file.read_text())
        if data["version"] != PROCESS_STATE_VERSION:
            raise ValueError("Unknown process-state version")
        return [OwnedProcess(**item) for item in data["processes"]]
    except (ValueError, TypeError, KeyError) as error:
        raise HarnessError(
            "Invalid private process ownership record; refusing cleanup"
        ) from error


def save_owned(layout: Layout, identities: list[OwnedProcess]) -> None:
    temporary = layout.state_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": PROCESS_STATE_VERSION,
                "processes": [asdict(identity) for identity in identities],
            }
        )
    )
    temporary.replace(layout.state_file)


def track_process(layout: Layout, process: subprocess.Popen[bytes], kind: str) -> None:
    identities = read_owned(layout)
    try:
        identity = identify(psutil.Process(process.pid), kind)
    except psutil.NoSuchProcess:
        return
    if identity not in identities:
        identities.append(identity)
    save_owned(layout, identities)
    refresh_owned(layout)


def refresh_owned(layout: Layout) -> None:
    """Capture descendants only through an identity-verified launched process."""
    identities = read_owned(layout)
    additions: list[OwnedProcess] = []
    for identity in identities:
        process = matching_process(identity)
        if process is None:
            continue
        try:
            for child in process.children(recursive=True):
                child_identity = identify(child, identity.kind)
                if child_identity not in identities and child_identity not in additions:
                    additions.append(child_identity)
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
    if additions:
        save_owned(layout, identities + additions)


def musescore_running(settings: Settings) -> bool:
    for process in psutil.process_iter(["name", "exe"]):
        try:
            name = str(process.info.get("name") or "").lower()
            executable = str(process.info.get("exe") or "").lower()
            if (
                name in MUSESCORE_PROCESS_NAMES
                or Path(executable).name in MUSESCORE_PROCESS_NAMES
            ):
                return True
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
    return False


def preflight(settings: Settings) -> None:
    """Refuse occupied applications/endpoints before any configuration writes."""
    if musescore_running(settings):
        raise HarnessError("MuseScore is already running; no profile was changed")
    if any(
        matching_process(identity) is not None
        for identity in read_owned(settings.layout)
    ):
        raise HarnessError(
            "This test profile still owns running processes; run stop first"
        )
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family) as probe:
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((host, BRIDGE_PORT))
        except OSError as error:
            unsupported = {
                errno.EAFNOSUPPORT,
                errno.EPROTONOSUPPORT,
                errno.EADDRNOTAVAIL,
            }
            if family == socket.AF_INET6 and error.errno in unsupported:
                continue
            raise HarnessError(
                "Bridge port is unavailable; no profile was changed"
            ) from error
    if settings.system == "Linux":
        socket_path = Path("/tmp/.X11-unix") / f"X{XVFB_DISPLAY[1:]}"
        if socket_path.exists():
            raise HarnessError(
                "The selected display is already in use; no profile was changed"
            )
        for process in psutil.process_iter(["name", "cmdline"]):
            if process.info.get("name") == "Xvfb" and XVFB_DISPLAY in (
                process.info.get("cmdline") or []
            ):
                raise HarnessError(
                    "The selected display is already in use; no profile was changed"
                )


def prepare_directories(layout: Layout) -> None:
    for path in (layout.root / "tmp", layout.root / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)


def start_xvfb(settings: Settings) -> None:
    """Create a new owned display; never reuse or kill an unrelated server."""
    socket_path = Path("/tmp/.X11-unix") / f"X{XVFB_DISPLAY[1:]}"
    with (settings.layout.root / "xvfb.log").open("wb") as output:
        process = subprocess.Popen(
            ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_SCREEN],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    track_process(settings.layout, process, "xvfb")
    deadline = time.monotonic() + XVFB_STARTUP_SECONDS
    while not socket_path.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            raise HarnessError(f"Owned Xvfb did not come up on {XVFB_DISPLAY}")
        time.sleep(PROCESS_POLL_SECONDS)


def launch(settings: Settings, score: Path) -> subprocess.Popen[bytes]:
    """Launch in the private profile and record ownership before waiting."""
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        process = _start(settings, score)
        track_process(settings.layout, process, "musescore")
        deadline = time.monotonic() + STARTUP_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            refresh_owned(settings.layout)
            time.sleep(PROCESS_POLL_SECONDS)
        if process.poll() is None:
            return process
        logger.warning("MuseScore exited during startup (attempt %d)", attempt)
    print_log_tails(settings.layout)
    raise HarnessError("MuseScore exited during startup")


def _start(settings: Settings, score: Path) -> subprocess.Popen[bytes]:
    logger.info("launching MuseScore with private profile %s", settings.layout.root)
    command = [str(settings.executable), str(score)]
    if settings.system == "Linux":
        # Avoid recording the transient /usr/bin/env from AppRun's shebang.
        # The stable bash launcher waits for its real application child.
        command.insert(0, "bash")
    with settings.layout.stdout_log.open("wb") as output:
        return subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=child_environment(settings),
        )


def owned_musescore_pid(layout: Layout) -> int:
    """Resolve the actual owned application, not an AppImage shell wrapper."""
    refresh_owned(layout)
    candidates = []
    for identity in read_owned(layout):
        if identity.kind != "musescore":
            continue
        process = matching_process(identity)
        if process is not None and process.name().lower() in MUSESCORE_PROCESS_NAMES:
            candidates.append(process.pid)
    if len(candidates) != 1:
        raise HarnessError("Cannot uniquely identify the owned MuseScore application")
    return candidates[0]


def trigger_plugin(settings: Settings) -> None:
    """Open the enabled bridge through MuseScore's Plugins menu.

    The disposable profile enables only this plugin. Linux and Windows use
    menu navigation; macOS invokes named native menu items. Custom shortcut
    registration and absolute screen coordinates are not involved.
    """
    logger.info("opening the bridge through the Plugins menu")
    owned_pid = owned_musescore_pid(settings.layout)
    if settings.system == "Windows":
        script = WINDOWS_TRIGGER_SCRIPT.replace("OWNED_PROCESS_ID", str(owned_pid))
        run(["powershell", "-NoProfile", "-Command", script])
    elif settings.system == "Darwin":
        script = MACOS_TRIGGER_SCRIPT.replace("OWNED_PROCESS_ID", str(owned_pid))
        run(["osascript", "-e", script])
    else:
        env = {**os.environ, "DISPLAY": XVFB_DISPLAY}
        found = run(
            [
                "xdotool",
                "search",
                "--onlyvisible",
                "--pid",
                str(owned_pid),
                "--class",
                "mscore|MuseScore",
            ],
            env=env,
            check=False,
        )
        windows = found.stdout.split()
        if not windows:
            print_log_tails(settings.layout)
            raise HarnessError(f"no MuseScore window found on {XVFB_DISPLAY}")
        # The score title need not contain MuseScore; helper windows may.
        window = windows[-1]
        # Without a window manager (bare Xvfb) windowactivate fails; fall back
        # to windowfocus before opening the menu.
        if run(
            ["xdotool", "windowactivate", "--sync", window], env=env, check=False
        ).returncode:
            run(["xdotool", "windowfocus", "--sync", window], env=env, check=False)
        run(["xdotool", "key", "--clearmodifiers", "Escape"], env=env)
        time.sleep(DIALOG_DISMISS_SECONDS)
        run(["xdotool", "key", "--clearmodifiers", "alt+p"], env=env)
        time.sleep(MENU_OPEN_SECONDS)
        # Manage plugins is first; the sole enabled category is last.
        run(["xdotool", "key", "--clearmodifiers", "End", "Right"], env=env)
        time.sleep(MENU_OPEN_SECONDS)
        run(["xdotool", "key", "--clearmodifiers", "Return"], env=env)


WINDOWS_TRIGGER_SCRIPT = r"""
Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms
$process = Get-Process -Id OWNED_PROCESS_ID | Where-Object { $_.MainWindowHandle -ne 0 }
if (-not $process) { throw "no MuseScore window" }
[Microsoft.VisualBasic.Interaction]::AppActivate($process.Id)
Start-Sleep -Seconds 1
[System.Windows.Forms.SendKeys]::SendWait("{ESC}")
Start-Sleep -Seconds 2
[System.Windows.Forms.SendKeys]::SendWait("%p")
Start-Sleep -Seconds 1
[System.Windows.Forms.SendKeys]::SendWait("{END}{RIGHT}")
Start-Sleep -Seconds 1
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
"""

MACOS_TRIGGER_SCRIPT = """
tell application "System Events"
    tell (first application process whose unix id is OWNED_PROCESS_ID)
        set frontmost to true
        delay 1
        key code 53
        delay 2
        set pluginsMenu to menu bar item "Plugins" of menu bar 1
        click pluginsMenu
        delay 1
        set categoryMenu to menu 1 of pluginsMenu
        set bridgeCategory to menu item "Composing/arranging tools" of categoryMenu
        click bridgeCategory
        delay 1
        click menu item "MCP Score Bridge" of menu 1 of bridgeCategory
    end tell
end tell
"""


async def ping_once(uri: str) -> bool:
    """Return True if the bridge at *uri* answers a ping with ``pong``."""
    try:
        async with websockets.connect(
            uri, open_timeout=BRIDGE_ROUND_TRIP_SECONDS
        ) as connection:
            await connection.send(json.dumps({"command": "ping"}))
            raw = await asyncio.wait_for(
                connection.recv(), timeout=BRIDGE_ROUND_TRIP_SECONDS
            )
    except UNREACHABLE_ERRORS:
        return False
    try:
        response: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return response.get("result") == "pong"


async def wait_for_bridge(uri: str, timeout_seconds: float) -> bool:
    """Poll with a real ping: the port can be open before the plugin answers."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if await ping_once(uri):
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(BRIDGE_POLL_INTERVAL_SECONDS)


def verify_private_paths(settings: Settings) -> None:
    """Verify actual application log and preference writes in the private root."""
    launched = [
        item.created for item in read_owned(settings.layout) if item.kind == "musescore"
    ]
    current_logs = [
        path
        for path in settings.layout.log_dir.glob("*.log")
        if launched and path.stat().st_mtime >= min(launched)
    ]
    if not current_logs:
        raise HarnessError("MuseScore did not create current private application logs")
    if settings.system == "Darwin":
        with settings.layout.preferences.file.open("rb") as source:
            values = plistlib.load(source)
        persisted = bool(values.get("cloud.clientId"))
    else:
        values_ini = configparser.RawConfigParser()
        values_ini.read(settings.layout.preferences.file)
        persisted = bool(values_ini.get("cloud", "clientId", fallback=""))
    if not persisted:
        raise HarnessError(
            "MuseScore did not persist new preferences in the private profile"
        )


def verify_macos_preferences(settings: Settings) -> None:
    """Prove native preference reads and Foundation paths before launching Qt."""
    if settings.system != "Darwin":
        return
    with settings.layout.preferences.file.open("rb") as source:
        expected_nonce = plistlib.load(source)["mcp-score.profileNonce"]
    result = run(
        ["defaults", "read", MACOS_PREFERENCES_DOMAIN, "mcp-score.profileNonce"],
        env=child_environment(settings),
    )
    if result.stdout.strip() != expected_nonce:
        raise HarnessError(
            "CoreFoundation did not resolve the private preference domain"
        )
    foundation = run(
        [
            "osascript",
            "-l",
            "JavaScript",
            "-e",
            'ObjC.import("Foundation"); '
            "ObjC.unwrap($.NSSearchPathForDirectoriesInDomains(14, 1, true)"
            ".objectAtIndex(0))",
        ],
        env=child_environment(settings),
    )
    expected_data = settings.layout.root / "Library/Application Support"
    if Path(foundation.stdout.strip()).resolve() != expected_data.resolve():
        raise HarnessError("Foundation did not resolve private application data")


def verify_windows_portable_binary(settings: Settings) -> None:
    """Reject a nonportable binary before it can access native user folders.

    WIN_PORTABLE's deployed data-path literal is shared by QSettings and the
    application data resolver. Normal MSI binaries compile out this branch.
    """
    if settings.system != "Windows":
        return
    literal = "%1/../../../Data/settings"
    binary = settings.executable.read_bytes()
    if literal.encode() not in binary and literal.encode("utf-16-le") not in binary:
        raise HarnessError("Refusing a Windows binary without WIN_PORTABLE data paths")


def start(settings: Settings, score: Path) -> None:
    if not settings.executable.exists():
        raise HarnessError(f"{settings.executable} not found; run 'install' first")
    if not score.is_file():
        raise HarnessError(f"Score does not exist: {score}")
    verify_windows_portable_binary(settings)
    preflight(settings)
    claim_profile(settings.layout)
    with profile_lock(settings.layout):
        preflight(settings)
        prepare_directories(settings.layout)
        save_owned(settings.layout, [])
        seed_configuration(settings.layout)
        verify_macos_preferences(settings)
        try:
            if settings.system == "Linux":
                start_xvfb(settings)
            launch(settings, score.resolve())
            trigger_plugin(settings)
            uri = f"ws://localhost:{BRIDGE_PORT}"
            if not asyncio.run(wait_for_bridge(uri, BRIDGE_TIMEOUT_SECONDS)):
                print_log_tails(settings.layout)
                raise HarnessError(f"the bridge plugin did not answer at {uri}")
            refresh_owned(settings.layout)
            verify_private_paths(settings)
        except BaseException:
            stop_owned(settings.layout)
            raise
    logger.info("bridge ready in private profile %s", settings.layout.root)


@contextlib.contextmanager
def profile_lock(layout: Layout) -> Iterator[None]:
    """Hold an OS lock so concurrent start/stop cannot race on one profile."""
    lock_file = layout.root.parent / f".{layout.root.name}.harness.lock"
    with lock_file.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                if lock.read(1) == b"":
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise HarnessError("Another harness operation owns this profile") from error
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def stop_owned(layout: Layout) -> None:
    refresh_owned(layout)
    identities = read_owned(layout)
    for identity in reversed(identities):
        process = matching_process(identity)
        if process is None:
            continue
        try:
            process.terminate()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except psutil.TimeoutExpired:
            current = matching_process(identity)
            if current is not None:
                current.kill()
                current.wait(timeout=STOP_TIMEOUT_SECONDS)
        except psutil.NoSuchProcess, psutil.ZombieProcess:
            continue
    layout.state_file.unlink(missing_ok=True)


def stop(settings: Settings) -> None:
    """Stop identity-verified owned PIDs only, retaining private logs/profile."""
    if not settings.layout.root.exists():
        return
    marker = settings.layout.root / PROFILE_MARKER
    if not marker.exists() or marker.read_text() != PROFILE_MARKER_CONTENT:
        raise HarnessError("Refusing to stop processes for an unowned profile")
    with profile_lock(settings.layout):
        stop_owned(settings.layout)


def collect_logs(settings: Settings, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in (
        settings.layout.log_dir,
        settings.layout.stdout_log,
        settings.layout.root / "xvfb.log",
    ):
        if path.is_dir():
            shutil.copytree(path, destination / path.name, dirs_exist_ok=True)
        elif path.is_file():
            shutil.copy2(path, destination / path.name)


def clean(settings: Settings) -> None:
    """Remove only a marked private profile, after stopping its owned PIDs."""
    if not settings.layout.root.exists():
        return
    marker = settings.layout.root / PROFILE_MARKER
    if not marker.exists() or marker.read_text() != PROFILE_MARKER_CONTENT:
        raise HarnessError("Refusing to remove an unowned profile")
    claim_profile(settings.layout)
    with profile_lock(settings.layout):
        stop_owned(settings.layout)
        shutil.rmtree(settings.layout.root)


def normal_profile_paths(system: str) -> list[Path]:
    home = Path.home()
    documents = home / "Documents/MuseScore4"
    if system == "Windows":
        return [
            Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "MuseScore",
            Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "MuseScore",
            documents,
        ]
    if system == "Darwin":
        return [
            home / "Library/Preferences" / f"{MACOS_PREFERENCES_DOMAIN}.plist",
            home / "Library/Application Support/MuseScore",
            home / "Library/Caches/MuseScore",
            documents,
        ]
    return [
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "MuseScore",
        Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "MuseScore",
        Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "MuseScore",
        documents,
    ]


def normal_profile_snapshot(system: str) -> dict[str, str]:
    """Read normal profile contents for CI non-interference assertions."""
    contents: dict[str, str] = {}
    for root in normal_profile_paths(system):
        paths = [root, *root.rglob("*")] if root.is_dir() else [root]
        for path in paths:
            if path.is_symlink():
                contents[str(path)] = "symlink:" + os.readlink(path)
            elif path.is_file():
                contents[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_dir():
                contents[str(path)] = "directory"
    return contents


def snapshot_normal_profile(system: str, destination: Path) -> None:
    destination.write_text(json.dumps(normal_profile_snapshot(system), sort_keys=True))


def verify_normal_profile(system: str, baseline: Path) -> None:
    expected = json.loads(baseline.read_text())
    actual = normal_profile_snapshot(system)
    if expected != actual:
        changed = sorted(
            key
            for key in expected.keys() | actual.keys()
            if expected.get(key) != actual.get(key)
        )
        raise HarnessError(
            "The normal MuseScore profile changed: " + ", ".join(changed)
        )
    logger.info("normal MuseScore profile is unchanged")


# ── Helpers ───────────────────────────────────────────────────────────


def run(
    command: list[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        command, check=False, cwd=cwd, env=env, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise HarnessError(
            f"{command[0]} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def newest_log(layout: Layout) -> Path | None:
    logs = sorted(layout.log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def print_log_tails(layout: Layout) -> None:
    for path in (newest_log(layout), layout.stdout_log):
        if path is None or not path.exists():
            continue
        lines = path.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
        logger.info("tail of %s:\n%s", path, "\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install")
    start_parser = commands.add_parser("start")
    start_parser.add_argument("score", type=Path)
    commands.add_parser("stop")
    commands.add_parser("clean")
    logs_parser = commands.add_parser("collect-logs")
    logs_parser.add_argument("destination", type=Path)
    for name in ("snapshot-normal-profile", "verify-normal-profile"):
        profile_parser = commands.add_parser(name)
        profile_parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    settings = Settings.from_environment()
    try:
        if args.command == "install":
            print(install(settings))  # noqa: T201
        elif args.command == "start":
            start(settings, args.score)
        elif args.command == "stop":
            stop(settings)
        elif args.command == "clean":
            clean(settings)
        elif args.command == "collect-logs":
            collect_logs(settings, args.destination)
        elif args.command == "snapshot-normal-profile":
            snapshot_normal_profile(settings.system, args.file)
        else:
            verify_normal_profile(settings.system, args.file)
    except HarnessError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
