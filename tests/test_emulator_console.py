"""Screenshots through the Android emulator's console, against a fake console."""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from dokkan_farmer.device import AdbDevice, DeviceError, EmulatorConsole, create_device
from dokkan_farmer.settings import Settings

BANNER = b"Android Console: type 'help' for a list of commands\r\nOK\r\n"
AUTH_BANNER = (
    b"Android Console: Authentication required\r\n"
    b"Android Console: type 'auth <auth_token>' to authenticate\r\nOK\r\n"
)


class FakeConsole:
    """Answers `screenrecord screenshot <dir>` by writing a red PNG into <dir>."""

    def __init__(self, banner: bytes = BANNER, write: bool = True, hang_up_after: int = 0):
        self.banner = banner
        self.write = write
        self.hang_up_after = hang_up_after
        self.connections = 0
        self.commands: list[str] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.sendall(self.banner)
            buffer, answered = b"", 0
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    command = line.decode().strip()
                    self.commands.append(command)
                    if command.startswith("screenrecord screenshot "):
                        if self.write:
                            image = np.zeros((40, 30, 3), np.uint8)
                            image[:, :, 2] = 200
                            target = Path(command.split(" ", 2)[2]) / "Screenshot_1.png"
                            cv2.imwrite(str(target), image)
                        conn.sendall(b"OK\r\n")
                    else:
                        conn.sendall(b"KO: unknown command, try 'help'\r\n")
                    answered += 1
                    if self.hang_up_after and answered >= self.hang_up_after:
                        return

    def close(self) -> None:
        # shutdown() wakes the thread blocked in accept(); close() alone keeps listening.
        with contextlib.suppress(OSError):
            self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()


@pytest.fixture
def screens(tmp_path) -> Path:
    directory = tmp_path / "screens"
    directory.mkdir()
    return directory


def test_screenshots_come_from_the_console(screens):
    fake = FakeConsole()
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    for _ in range(3):
        image = console.screenshot()
        assert image.shape == (40, 30, 3) and image[0, 0, 2] == 200
    assert fake.connections == 1  # one connection, reused
    assert fake.commands == [f"screenrecord screenshot {screens}"] * 3
    assert list(screens.iterdir()) == []  # nothing left behind
    console.close()
    fake.close()


def test_a_dropped_connection_is_reopened(screens):
    fake = FakeConsole(hang_up_after=1)
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    console.screenshot()
    assert console.screenshot().shape == (40, 30, 3)
    assert fake.connections == 2
    fake.close()


def test_console_errors(screens):
    fake = FakeConsole(banner=AUTH_BANNER)
    with pytest.raises(DeviceError, match="password"):
        EmulatorConsole(f"127.0.0.1:{fake.port}", screens).screenshot()
    fake.close()

    fake = FakeConsole()
    with pytest.raises(DeviceError, match="KO: unknown command"):
        EmulatorConsole(f"127.0.0.1:{fake.port}", screens).command("nonsense")
    fake.close()

    fake = FakeConsole(write=False)  # folder not shared with the emulator
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    console.wait = 0.2
    with pytest.raises(DeviceError, match="shared"):
        console.screenshot()
    fake.close()

    with pytest.raises(DeviceError, match="Emulator console"):
        EmulatorConsole(f"127.0.0.1:{fake.port}", screens, timeout=1).screenshot()

    for bad in ("android", "android:console", ":5554"):
        with pytest.raises(ValueError):
            EmulatorConsole(bad, screens)


def fake_adb(tmp_path) -> tuple[Path, Path]:
    """An adb that logs its arguments and answers screencap with a blue PNG.

    `dumpsys` prints tmp_path/rotation (Android's screen rotation); while tmp_path/offline
    exists every command fails like a dropped device.
    """
    adb = tmp_path / "adb"
    png = tmp_path / "screen.png"
    image = np.zeros((20, 10, 3), np.uint8)
    image[:, :, 0] = 150  # blue: from adb
    cv2.imwrite(str(png), image)
    (tmp_path / "rotation").write_text("      SurfaceOrientation: 0\n")
    log = tmp_path / "adb.log"
    adb.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"os.chdir({str(tmp_path)!r})\n"
        "args = ' '.join(sys.argv[1:])\n"
        "open('adb.log', 'a').write(args + '\\n')\n"
        "if os.path.exists('offline'):\n"
        "    sys.exit('error: device offline')\n"
        "if 'dumpsys' in args:\n"
        "    sys.stdout.write(open('rotation').read())\n"
        "else:\n"
        "    sys.stdout.buffer.write(open('screen.png', 'rb').read())\n"
    )
    adb.chmod(0o755)
    return adb, log


def screencaps(log: Path) -> int:
    return log.read_text().count("screencap") if log.exists() else 0


@pytest.fixture
def emulator(screens, tmp_path):
    """An AdbDevice for 127.0.0.1:5555 whose console is a FakeConsole."""
    adb, log = fake_adb(tmp_path)
    fake = FakeConsole()
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    device = AdbDevice("127.0.0.1:5555", adb_path=str(adb), console=console)
    yield device, fake, log
    fake.close()


def source(image: np.ndarray) -> str:
    """Where a screenshot came from: the fake console draws red, the fake adb blue."""
    return "console" if image[0, 0, 2] == 200 else "adb"


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_adb_device_prefers_the_console_and_falls_back(emulator):
    device, fake, log = emulator
    assert source(device.screenshot()) == "console"
    assert screencaps(log) == 0  # adb only checked the rotation
    assert source(device.screenshot()) == "console"
    assert log.read_text().count("dumpsys") == 1  # rotation checked every few seconds

    fake.close()
    device.console.close()
    device.console.timeout = 1
    assert source(device.screenshot()) == "adb"  # console gone: adb
    assert source(device.screenshot()) == "adb"  # no new console attempt for a minute
    assert screencaps(log) >= 2


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_a_rotated_screen_goes_through_adb(emulator, tmp_path):
    device, _, _ = emulator
    assert source(device.screenshot()) == "console"
    # The emulator's extra touchscreens (for displays that do not exist) always say 0.
    (tmp_path / "rotation").write_text("      SurfaceOrientation: 0\n      SurfaceOrientation: 1\n")
    device._rotation_at = float("-inf")  # the next check is due
    assert source(device.screenshot()) == "adb"
    (tmp_path / "rotation").write_text("      SurfaceOrientation: 0\n")
    device._rotation_at = float("-inf")
    assert source(device.screenshot()) == "console"


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_after_an_adb_failure_screenshots_need_adb(emulator, tmp_path):
    # The console keeps working when adb drops: its screenshots must not hide that,
    # or the farm would never notice (and never notify) an unreachable device.
    device, _, _ = emulator
    assert source(device.screenshot()) == "console"
    (tmp_path / "offline").touch()
    with pytest.raises(DeviceError):
        device.tap(10, 10)
    with pytest.raises(DeviceError, match="offline"):
        device.screenshot()
    with pytest.raises(DeviceError, match="offline"):
        device.screenshot()
    (tmp_path / "offline").unlink()
    assert source(device.screenshot()) == "adb"  # adb answers again...
    assert source(device.screenshot()) == "console"  # ...so the console is trusted again


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_the_console_only_serves_the_emulator(emulator):
    device, fake, _ = emulator
    # The Device tab switched to a phone: its screen, not the emulator's.
    device.set_serial("192.168.1.50:5555")
    assert source(device.screenshot()) == "adb"
    assert fake.commands == []
    device.set_serial("127.0.0.1:5555")  # back to the emulator
    assert source(device.screenshot()) == "console"


def test_settings_build_the_console(tmp_path):
    settings = Settings.from_env({"DATA_DIR": str(tmp_path), "EMULATOR_CONSOLE": "android:5554"})
    assert settings.emulator_console == "android:5554"
    assert settings.emulator_screenshots == "/screens"
    device = create_device(settings, serial="android:5555")
    assert isinstance(device, AdbDevice) and device.console is not None
    assert device.console.address == "android:5554"
    assert create_device(Settings(data_dir=tmp_path)).console is None
