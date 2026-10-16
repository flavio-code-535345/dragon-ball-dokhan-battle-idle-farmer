"""Common device interface shared by the ADB and simulator backends."""

from __future__ import annotations

import abc
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np


class DeviceError(RuntimeError):
    """The Android device could not be reached or rejected a command."""


#: Friendly key names accepted by routines and the web UI.
KEYCODES = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
    "DEL": "KEYCODE_DEL",
    "FORWARD_DEL": "KEYCODE_FORWARD_DEL",
    "TAB": "KEYCODE_TAB",
    "DPAD_UP": "KEYCODE_DPAD_UP",
    "DPAD_DOWN": "KEYCODE_DPAD_DOWN",
    "DPAD_LEFT": "KEYCODE_DPAD_LEFT",
    "DPAD_RIGHT": "KEYCODE_DPAD_RIGHT",
    "ESCAPE": "KEYCODE_ESCAPE",
    "MENU": "KEYCODE_MENU",
    "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "WAKEUP": "KEYCODE_WAKEUP",
    "POWER": "KEYCODE_POWER",
}


def normalize_key(name: str) -> str:
    key = name.strip().upper()
    if key.startswith("KEYCODE_"):
        key = key[len("KEYCODE_") :]
    if key not in KEYCODES:
        raise ValueError(f"Unknown key {name!r}; use one of: {', '.join(KEYCODES)}")
    return key


class Device(abc.ABC):
    """An Android screen we can look at and touch."""

    backend = "base"

    @property
    @abc.abstractmethod
    def serial(self) -> str: ...

    @abc.abstractmethod
    def connect(self) -> None:
        """Make sure the device is reachable; raise :class:`DeviceError` otherwise."""

    @abc.abstractmethod
    def screenshot(self) -> np.ndarray:
        """Return the current screen as a BGR ``uint8`` image."""

    @abc.abstractmethod
    def tap(self, x: int, y: int) -> None: ...

    @abc.abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...

    @abc.abstractmethod
    def key(self, name: str) -> None: ...

    def press(self, name: str, times: int = 1) -> None:
        """Press a key several times (a key held down in the web UI)."""
        for _ in range(times):
            self.key(name)

    @abc.abstractmethod
    def launch_app(self, package: str) -> None: ...

    @abc.abstractmethod
    def stop_app(self, package: str) -> None: ...

    @abc.abstractmethod
    def foreground_package(self) -> str | None:
        """Package of the app in front, or ``None`` if it cannot be determined."""

    def info(self) -> dict[str, Any]:
        return {}

    def text(self, value: str) -> None:
        """Type text into the focused input field."""
        raise DeviceError("This device cannot type text")

    def install(self, apks: Sequence[Path]) -> str:
        """Install an APK, or the split APKs of one app together."""
        raise DeviceError("This device cannot install apps")

    def push(self, local: Path, remote: str) -> None:
        """Copy a file to the device."""
        raise DeviceError("This device cannot receive files")

    def uninstall(self, package: str) -> None:
        """Remove an app and its data from the device."""
        raise DeviceError("This device cannot uninstall apps")

    def is_installed(self, package: str) -> bool | None:
        """Whether an app is installed (``None`` if unknown)."""
        return None

    def app_abi(self, package: str) -> str | None:
        """The CPU ABI an installed app runs with, e.g. ``arm64-v8a`` (``None`` if unknown)."""
        return None

    def runnable_abis(self) -> list[str] | None:
        """ABIs whose native code really runs here, translation included (``None`` if unknown)."""
        return None

    def android_log(self, package: str) -> str:
        """What Android logged about an app: its crash reports and the lines naming it."""
        raise DeviceError("This device has no Android log")

    def set_stay_awake(self, enabled: bool) -> None:  # noqa: B027 - optional hook
        """Keep the screen on while farming (no-op where unsupported)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""
