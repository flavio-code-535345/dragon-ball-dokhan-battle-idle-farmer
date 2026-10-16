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
    """An adb that logs its arguments and answers every command with a blue PNG."""
    adb = tmp_path / "adb"
    png = tmp_path / "screen.png"
    image = np.zeros((20, 10, 3), np.uint8)
    image[:, :, 0] = 150  # blue: from adb
    cv2.imwrite(str(png), image)
    log = tmp_path / "adb.log"
    adb.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.buffer.write(open({str(png)!r}, 'rb').read())\n"
    )
    adb.chmod(0o755)
    return adb, log


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_adb_device_prefers_the_console_and_falls_back(screens, tmp_path):
    adb, log = fake_adb(tmp_path)
    fake = FakeConsole()
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    device = AdbDevice("127.0.0.1:5555", adb_path=str(adb), console=console)
    assert device.screenshot()[0, 0, 2] == 200  # red: from the console
    assert not log.exists()  # adb not used

    fake.close()
    console.close()
    console.timeout = 1
    assert device.screenshot()[0, 0, 0] == 150  # console gone: adb
    log.unlink()
    device.screenshot()  # no new console attempt for a minute
    assert "screencap" in log.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_the_console_only_serves_the_emulator(screens, tmp_path):
    adb, _ = fake_adb(tmp_path)
    fake = FakeConsole()
    console = EmulatorConsole(f"127.0.0.1:{fake.port}", screens)
    # The Device tab switched to a phone: its screen, not the emulator's.
    device = AdbDevice("192.168.1.50:5555", adb_path=str(adb), console=console)
    assert device.screenshot()[0, 0, 0] == 150
    assert fake.commands == []
    device.set_serial("127.0.0.1:5555")  # back to the emulator
    assert device.screenshot()[0, 0, 2] == 200
    fake.close()


def test_settings_build_the_console(tmp_path):
    settings = Settings.from_env({"DATA_DIR": str(tmp_path), "EMULATOR_CONSOLE": "android:5554"})
    assert settings.emulator_console == "android:5554"
    assert settings.emulator_screenshots == "/screens"
    device = create_device(settings, serial="android:5555")
    assert isinstance(device, AdbDevice) and device.console is not None
    assert device.console.address == "android:5554"
    assert create_device(Settings(data_dir=tmp_path)).console is None
