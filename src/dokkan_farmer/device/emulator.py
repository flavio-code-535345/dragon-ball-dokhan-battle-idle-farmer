"""Screenshots straight from the Android emulator instead of through Android.

`adb exec-out screencap` makes Android copy each frame out of the emulated GPU, which takes
a second or more in the emulator. The emulator's console command
`screenrecord screenshot <dir>` saves the frame it already has as a PNG in about 0.1 s. The
directory is a volume shared by the emulator's and the farmer's containers
(docker-compose.emulator.yml), at the same path in both.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .base import DeviceError

log = logging.getLogger(__name__)


class EmulatorConsole:
    """A connection to the emulator console (telnet protocol, port 5554 by default)."""

    def __init__(self, address: str, directory: str | Path, timeout: float = 5.0) -> None:
        host, sep, port = address.strip().rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"EMULATOR_CONSOLE must look like android:5554, got {address!r}")
        self.host = host
        self.port = int(port)
        self.directory = Path(directory)
        self.timeout = timeout
        #: how long to wait for the PNG after the console said OK
        self.wait = 2.0
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()
        #: one screenshot at a time: each clears the folder the other waits on
        self._screenshot_lock = threading.Lock()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        self._buffer = b""

    # ------------------------------------------------------------- protocol
    def _reply(self) -> list[str]:
        """The lines of one answer, which ends with "OK" (or "KO: why")."""
        assert self._sock is not None
        lines: list[str] = []
        while True:
            while b"\n" not in self._buffer:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError("the emulator closed its console")
                self._buffer += chunk
            raw, _, self._buffer = self._buffer.partition(b"\n")
            line = raw.decode("utf-8", "replace").strip()
            if line == "OK":
                return lines
            if line.startswith("KO"):
                raise DeviceError(f"Emulator console: {line}")
            lines.append(line)

    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock, self._buffer = sock, b""
        if "Authentication required" in " ".join(self._reply()):
            self.close()
            raise DeviceError(
                "The emulator console wants a password: start the emulator with an empty "
                "~/.emulator_console_auth_token (docker-compose.emulator.yml does)"
            )

    def command(self, line: str) -> list[str]:
        with self._lock:
            for attempt in (1, 2):  # a dropped connection is reopened once
                try:
                    if self._sock is None:
                        self._connect()
                    assert self._sock is not None
                    self._sock.sendall(line.encode("utf-8") + b"\n")
                    return self._reply()
                except (OSError, ConnectionError) as exc:
                    self.close()
                    if attempt == 2:
                        raise DeviceError(f"Emulator console {self.address}: {exc}") from exc
            raise AssertionError("unreachable")

    # ----------------------------------------------------------- screenshot
    def _pngs(self) -> list[Path]:
        try:
            return list(self.directory.glob("*.png"))
        except OSError:
            return []

    def _clear(self) -> None:
        for path in self._pngs():
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # stale files would be read as new screenshots
                raise DeviceError(f"Cannot clean {self.directory}: {exc}") from exc

    def screenshot(self) -> np.ndarray:
        with self._screenshot_lock:
            return self._screenshot()

    def _screenshot(self) -> np.ndarray:
        self._clear()
        self.command(f"screenrecord screenshot {self.directory}")
        deadline = time.monotonic() + self.wait
        while True:
            for path in self._pngs():
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    self._clear()
                    return image
            if time.monotonic() > deadline:
                raise DeviceError(
                    f"The emulator saved no screenshot in {self.directory}: is that folder "
                    "shared with the emulator's container?"
                )
            time.sleep(0.02)
