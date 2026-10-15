"""ADB backend: drives any Android phone, emulator or redroid container through ``adb``."""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

from .base import KEYCODES, Device, DeviceError, normalize_key

log = logging.getLogger(__name__)

SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,127}$")
HOST_PORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-\[\]:]*:\d{1,5}$")
PAIR_CODE_RE = re.compile(r"^\d{6}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_CONNECTED_RE = re.compile(r"^(already )?connected to ", re.MULTILINE)
_RESUMED_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:]\s*ActivityRecord\{[^}]*?\s"
    r"([A-Za-z][\w.]*)/"
)
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z][\w.]*)/")


def validate_serial(serial: str) -> str:
    serial = serial.strip()
    if not SERIAL_RE.match(serial):
        raise ValueError("Invalid device serial (expected e.g. 192.168.1.50:5555 or a USB serial)")
    return serial


def validate_package(package: str) -> str:
    if not PACKAGE_RE.match(package):
        raise ValueError(f"Invalid Android package name: {package!r}")
    return package


def _output(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()


def _decode_png(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return None
    return image


def _state_hint(state: str, serial: str) -> str:
    target = serial or "the device"
    hints = {
        "unauthorized": f"{target} is unauthorized: accept the 'Allow USB debugging' prompt "
        "on the device (tick 'Always allow').",
        "offline": f"{target} is offline: toggle USB/wireless debugging or restart adb.",
        "missing": f"{target} not found: check ADB_SERIAL, that debugging is enabled and the "
        "device is reachable from the container.",
        "multiple": "More than one device is attached: set ADB_SERIAL to pick one.",
    }
    return hints.get(state, f"{target} is not ready ({state})")


class AdbDevice(Device):
    backend = "adb"

    def __init__(self, serial: str = "", adb_path: str = "adb", timeout: float = 20.0) -> None:
        self._serial = validate_serial(serial) if serial.strip() else ""
        self.adb_path = adb_path
        self.timeout = timeout

    # ------------------------------------------------------------------ plumbing
    @property
    def serial(self) -> str:
        return self._serial

    def set_serial(self, serial: str) -> None:
        self._serial = validate_serial(serial) if serial.strip() else ""

    @property
    def is_network(self) -> bool:
        return bool(self._serial) and bool(HOST_PORT_RE.match(self._serial))

    def _exec(
        self,
        args: Sequence[str],
        *,
        use_serial: bool = True,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = [self.adb_path]
        if use_serial and self._serial:
            cmd += ["-s", self._serial]
        cmd += list(args)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout or self.timeout, check=False
            )
        except OSError as exc:  # missing, not executable, bad path...
            raise DeviceError(f"adb binary not found or not runnable: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            what = " ".join(args[:2])
            raise DeviceError(f"adb {what} timed out after {exc.timeout:.0f}s") from exc
        if check and proc.returncode != 0:
            raise DeviceError(_output(proc) or f"adb {args[0]} failed ({proc.returncode})")
        return proc

    def shell(self, *args: str, timeout: float | None = None) -> str:
        proc = self._exec(["shell", *args], timeout=timeout)
        return proc.stdout.decode("utf-8", "replace")

    # ---------------------------------------------------------------- lifecycle
    def state(self) -> str:
        proc = self._exec(["get-state"], timeout=10, check=False)
        text = _output(proc)
        if proc.returncode == 0:
            return text.splitlines()[-1].strip() if text else "unknown"
        lowered = text.lower()
        for key in ("unauthorized", "offline"):
            if key in lowered:
                return key
        if "more than one" in lowered:
            return "multiple"
        if "not found" in lowered or "no devices" in lowered:
            return "missing"
        return text or "unknown"

    def connect(self) -> None:
        self._exec(["start-server"], use_serial=False, timeout=30, check=False)
        if self.is_network:
            proc = self._exec(["connect", self._serial], use_serial=False, timeout=15, check=False)
            text = _output(proc)
            if not _CONNECTED_RE.search(text):
                raise DeviceError(f"adb connect {self._serial}: {text or 'failed'}")
        state = self.state()
        if state != "device":
            raise DeviceError(_state_hint(state, self._serial))

    def disconnect(self) -> None:
        if self.is_network:
            self._exec(["disconnect", self._serial], use_serial=False, timeout=10, check=False)

    def pair(self, host_port: str, code: str) -> str:
        """Pair with Android 11+ wireless debugging ("Pair device with pairing code")."""
        host_port, code = host_port.strip(), code.strip()
        if not HOST_PORT_RE.match(host_port):
            raise ValueError("Pairing address must look like 192.168.1.50:37123")
        if not PAIR_CODE_RE.match(code):
            raise ValueError("Pairing code must be the 6-digit code shown on the device")
        proc = self._exec(["pair", host_port, code], use_serial=False, timeout=30, check=False)
        text = _output(proc)
        if "successfully paired" not in text.lower():
            raise DeviceError(text or "Pairing failed")
        return text

    def devices(self) -> list[dict[str, str]]:
        proc = self._exec(["devices", "-l"], use_serial=False, timeout=15, check=False)
        result = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            entry = {"serial": parts[0], "state": parts[1]}
            for extra in parts[2:]:
                if ":" in extra:
                    key, _, value = extra.partition(":")
                    entry[key] = value
            result.append(entry)
        return result

    # ------------------------------------------------------------------- screen
    def screenshot(self) -> np.ndarray:
        proc = self._exec(["exec-out", "screencap", "-p"])
        image = _decode_png(proc.stdout)
        if image is None:
            # Very old adb/device combos mangle binary output; undo CRLF translation.
            image = _decode_png(proc.stdout.replace(b"\r\n", b"\n"))
        if image is None:
            raise DeviceError("screencap returned no image (is the device unlocked?)")
        return image

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        coords = [str(int(v)) for v in (x1, y1, x2, y2, max(1, duration_ms))]
        self.shell("input", "swipe", *coords)

    def key(self, name: str) -> None:
        self.shell("input", "keyevent", KEYCODES[normalize_key(name)])

    # --------------------------------------------------------------------- apps
    def launch_app(self, package: str) -> None:
        validate_package(package)
        proc = self._exec(
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            check=False,
        )
        text = _output(proc)
        if "no activities found" in text.lower():
            raise DeviceError(f"{package} is not installed on {self._serial or 'the device'}")
        if proc.returncode != 0:
            raise DeviceError(text or f"Could not launch {package}")

    def stop_app(self, package: str) -> None:
        validate_package(package)
        self.shell("am", "force-stop", package)

    def foreground_package(self) -> str | None:
        # One round-trip; grep keeps the output small. "; true" because grep exits 1 on no match.
        out = self.shell(
            "dumpsys activity activities | grep -E 'ResumedActivity'; "
            "dumpsys window | grep -E 'mCurrentFocus'; true",
            timeout=15,
        )
        for pattern in (_RESUMED_RE, _FOCUS_RE):
            match = pattern.search(out)
            if match:
                return match.group(1)
        return None

    def info(self) -> dict[str, Any]:
        out = self.shell(
            "getprop ro.product.manufacturer; getprop ro.product.model; "
            "getprop ro.build.version.release; getprop ro.build.version.sdk; "
            "getprop ro.product.cpu.abi; wm size"
        )
        lines = [line.strip() for line in out.splitlines()]
        info: dict[str, Any] = {}
        keys = ["manufacturer", "model", "android", "sdk", "abi"]
        for key, value in zip(keys, lines, strict=False):
            info[key] = value
        sizes = re.findall(r"(Physical|Override) size:\s*(\d+)x(\d+)", out)
        if sizes:
            _, w, h = sizes[-1]
            info["resolution"] = f"{w}x{h}"
        return info

    def set_stay_awake(self, enabled: bool) -> None:
        try:
            self.shell("svc", "power", "stayon", "true" if enabled else "false")
        except DeviceError as exc:
            log.debug("Could not change stay-awake setting: %s", exc)
