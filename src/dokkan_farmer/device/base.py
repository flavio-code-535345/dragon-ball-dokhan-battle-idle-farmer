"""Common device interface shared by the ADB and simulator backends."""

from __future__ import annotations

import abc
from typing import Any

import numpy as np


class DeviceError(RuntimeError):
    """The Android device could not be reached or rejected a command."""


#: Friendly key names accepted by routines and the web UI.
KEYCODES = {
    "BACK": "KEYCODE_BACK",
    "HOME": "KEYCODE_HOME",
    "ENTER": "KEYCODE_ENTER",
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

    @abc.abstractmethod
    def launch_app(self, package: str) -> None: ...

    @abc.abstractmethod
    def stop_app(self, package: str) -> None: ...

    @abc.abstractmethod
    def foreground_package(self) -> str | None:
        """Package of the app in front, or ``None`` if it cannot be determined."""

    def info(self) -> dict[str, Any]:
        return {}

    def set_stay_awake(self, enabled: bool) -> None:  # noqa: B027 - optional hook
        """Keep the screen on while farming (no-op where unsupported)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""
