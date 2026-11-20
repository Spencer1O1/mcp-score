"""Fixed, read-only Accessibility probe for an owned hosted-runner application.

No Apple Events, UI actions, global window lists, screenshots or permission
requests. The harness supplies one recorded identity, never arbitrary code.
"""

from __future__ import annotations

import ctypes
import json
import sys
from typing import Any

__all__ = ["inspect_windows", "probe_owned"]

MAX_WINDOWS = 6
MAX_SHEETS = 2
MAX_TITLE_UNITS = 256
MAX_INPUT_BYTES = 8192
AX_TIMEOUT_SECONDS = 0.15


class ProbeError(Exception):
    """The native API could not supply verified, bounded metadata."""


class OwnershipChangedError(ProbeError):
    """Discard metadata instead of inspecting a different application's UI."""


class CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


class Accessibility:
    """The small native read surface; every returned CF object is released."""

    def __init__(self) -> None:
        self.ax = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self.cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        pointer = ctypes.c_void_p
        signatures = {
            "AXIsProcessTrusted": ([], ctypes.c_bool),
            "AXUIElementGetTypeID": ([], ctypes.c_ulong),
            "AXUIElementCreateApplication": ([ctypes.c_int], pointer),
            "AXUIElementGetPid": (
                [pointer, ctypes.POINTER(ctypes.c_int)],
                ctypes.c_int,
            ),
            "AXUIElementSetMessagingTimeout": ([pointer, ctypes.c_float], ctypes.c_int),
            "AXUIElementGetAttributeValueCount": (
                [pointer, pointer, ctypes.POINTER(ctypes.c_long)],
                ctypes.c_int,
            ),
            "AXUIElementCopyAttributeValues": (
                [
                    pointer,
                    pointer,
                    ctypes.c_long,
                    ctypes.c_long,
                    ctypes.POINTER(pointer),
                ],
                ctypes.c_int,
            ),
            "AXUIElementCopyAttributeValue": (
                [pointer, pointer, ctypes.POINTER(pointer)],
                ctypes.c_int,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.ax, name)
            function.argtypes, function.restype = arguments, result
        signatures = {
            "CFRelease": ([pointer], None),
            "CFRetain": ([pointer], pointer),
            "CFGetTypeID": ([pointer], ctypes.c_ulong),
            "CFArrayGetTypeID": ([], ctypes.c_ulong),
            "CFStringGetTypeID": ([], ctypes.c_ulong),
            "CFBooleanGetTypeID": ([], ctypes.c_ulong),
            "CFArrayGetCount": ([pointer], ctypes.c_long),
            "CFArrayGetValueAtIndex": ([pointer, ctypes.c_long], pointer),
            "CFStringCreateWithCString": (
                [pointer, ctypes.c_char_p, ctypes.c_uint32],
                pointer,
            ),
            "CFStringGetLength": ([pointer], ctypes.c_long),
            "CFStringCreateWithSubstring": ([pointer, pointer, CFRange], pointer),
            "CFStringGetCString": (
                [pointer, pointer, ctypes.c_long, ctypes.c_uint32],
                ctypes.c_bool,
            ),
            "CFBooleanGetValue": ([pointer], ctypes.c_bool),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.cf, name)
            function.argtypes, function.restype = arguments, result

    def trusted(self) -> bool:
        # Unlike requesting trust, this query never asks the user for a grant.
        return bool(self.ax.AXIsProcessTrusted())

    def application(self, pid: int) -> int:
        element = self.ax.AXUIElementCreateApplication(pid)
        if not element:
            raise ProbeError("No application Accessibility object")
        return element

    def release(self, element: int) -> None:
        self.cf.CFRelease(element)

    def owner(self, element: int) -> int:
        pid = ctypes.c_int()
        self._check(self.ax.AXUIElementGetPid(element, ctypes.byref(pid)), "owner")
        return pid.value

    def timeout(self, element: int) -> None:
        self._check(
            self.ax.AXUIElementSetMessagingTimeout(element, AX_TIMEOUT_SECONDS),
            "timeout",
        )

    @staticmethod
    def _check(code: int, attribute: str) -> None:
        if code:
            raise ProbeError(f"{attribute} unavailable (AXError {code})")

    def _key(self, name: str) -> int:
        value = self.cf.CFStringCreateWithCString(
            None, name.encode("ascii"), 0x08000100
        )
        if not value:
            raise ProbeError("Cannot create Accessibility attribute key")
        return value

    def children(self, element: int, name: str, limit: int) -> tuple[list[int], bool]:
        key, value = self._key(name), ctypes.c_void_p()
        children: list[int] = []
        try:
            count = ctypes.c_long()
            self._check(
                self.ax.AXUIElementGetAttributeValueCount(
                    element, key, ctypes.byref(count)
                ),
                name,
            )
            if count.value < 0:
                raise ProbeError("Invalid Accessibility array size")
            if count.value == 0:
                return [], False
            self._check(
                self.ax.AXUIElementCopyAttributeValues(
                    element, key, 0, min(count.value, limit), ctypes.byref(value)
                ),
                name,
            )
            if (
                not value.value
                or self.cf.CFGetTypeID(value) != self.cf.CFArrayGetTypeID()
            ):
                raise ProbeError("Invalid Accessibility array")
            actual = self.cf.CFArrayGetCount(value)
            if actual < 0 or actual > limit:
                raise ProbeError("Accessibility array exceeds its requested bound")
            for index in range(actual):
                child = self.cf.CFArrayGetValueAtIndex(value, index)
                if (
                    not child
                    or self.cf.CFGetTypeID(child) != self.ax.AXUIElementGetTypeID()
                ):
                    raise ProbeError("Invalid Accessibility child")
                children.append(self.cf.CFRetain(child))
            return children, count.value > limit
        except BaseException:
            for child in children:
                self.release(child)
            raise
        finally:
            self.cf.CFRelease(key)
            if value.value:
                self.cf.CFRelease(value)

    def attribute(self, element: int, name: str) -> str | bool | dict[str, object]:
        key, value = self._key(name), ctypes.c_void_p()
        try:
            code = self.ax.AXUIElementCopyAttributeValue(
                element, key, ctypes.byref(value)
            )
            if code or not value.value:
                return {"status": "unavailable", "ax_error": code}
            kind = self.cf.CFGetTypeID(value)
            if kind == self.cf.CFBooleanGetTypeID():
                return bool(self.cf.CFBooleanGetValue(value))
            if kind != self.cf.CFStringGetTypeID():
                return {"status": "unsupported_type"}
            length = self.cf.CFStringGetLength(value)
            if length < 0:
                raise ProbeError("Invalid Accessibility string length")
            short = self.cf.CFStringCreateWithSubstring(
                None, value, CFRange(0, min(length, MAX_TITLE_UNITS))
            )
            if not short:
                raise ProbeError("Cannot bound Accessibility string")
            try:
                buffer = ctypes.create_string_buffer(MAX_TITLE_UNITS * 4 + 1)
                if not self.cf.CFStringGetCString(
                    short, buffer, len(buffer), 0x08000100
                ):
                    return {"status": "encoding_unavailable"}
                text = buffer.value.decode("utf-8")
                if length > MAX_TITLE_UNITS:
                    return {"status": "truncated", "value": text}
                return text
            finally:
                self.cf.CFRelease(short)
        finally:
            self.cf.CFRelease(key)
            if value.value:
                self.cf.CFRelease(value)


def inspect_windows(api: Any, pid: int) -> dict[str, object]:
    """Read only this application's bounded windows and direct sheets."""
    if not api.trusted():
        return {"status": "permission_denied", "windows": []}
    application = api.application(pid)
    windows: list[int] = []
    try:
        if api.owner(application) != pid:
            raise OwnershipChangedError("Application owner changed")
        api.timeout(application)
        windows, truncated = api.children(application, "AXWindows", MAX_WINDOWS)
        result = []
        for window in windows:
            if api.owner(window) != pid:
                raise OwnershipChangedError(
                    "Window owner differs from recorded application"
                )
            api.timeout(window)
            entry = {
                name: api.attribute(window, attribute)
                for name, attribute in (
                    ("title", "AXTitle"),
                    ("role", "AXRole"),
                    ("subrole", "AXSubrole"),
                    ("modal", "AXModal"),
                )
            }
            entry["pid"] = pid
            sheets = []
            try:
                sheets, limited = api.children(window, "AXSheets", MAX_SHEETS)
                entry["sheets_truncated"] = limited
                entries = []
                for sheet in sheets:
                    if api.owner(sheet) != pid:
                        raise OwnershipChangedError(
                            "Sheet owner differs from recorded application"
                        )
                    api.timeout(sheet)
                    entries.append(
                        {
                            "pid": pid,
                            **{
                                name: api.attribute(sheet, attribute)
                                for name, attribute in (
                                    ("title", "AXTitle"),
                                    ("role", "AXRole"),
                                    ("subrole", "AXSubrole"),
                                    ("modal", "AXModal"),
                                )
                            },
                        }
                    )
                entry["sheets"] = entries
            except OwnershipChangedError:
                raise
            except ProbeError as error:
                entry["sheets_status"] = str(error)
            finally:
                for sheet in sheets:
                    api.release(sheet)
            result.append(entry)
        return {
            "status": "captured" if result else "no_windows",
            "windows": result,
            "truncated": truncated,
        }
    finally:
        for window in windows:
            api.release(window)
        api.release(application)


def probe_owned(request: dict[str, Any]) -> dict[str, object]:
    # Loaded after startup script location is established; no native API loads
    # until the same hosted-runner and persisted ownership guards pass.
    import musescore_harness as harness

    settings = harness.Settings.from_environment()
    harness.require_macos_runner(settings)
    harness.validate_startup_diagnostic_paths(settings.layout)
    if request.get("root") != str(settings.layout.root.absolute()):
        raise ProbeError("Profile differs from the configured owned root")
    identity = harness.OwnedProcess(**request["identity"])
    if (
        identity.kind != "musescore"
        or identity not in harness.read_owned(settings.layout)
        or identity.executable != str(settings.executable.resolve())
        or identity.handoff_executable not in {None, str(settings.executable.resolve())}
        or harness.matching_process(identity) is None
    ):
        raise ProbeError("Application identity is not currently owned")
    result = inspect_windows(Accessibility(), identity.pid)
    if harness.matching_process(identity) is None:
        return {"status": "identity_changed", "windows": []}
    return result


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ProbeError("Probe request exceeds its bound")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"root", "identity"}:
            raise ProbeError("Invalid fixed probe request")
        result = probe_owned(request)
    except Exception as error:
        result = {"status": "unavailable", "reason": str(error)[:256], "windows": []}
    print(json.dumps(result, ensure_ascii=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
