"""The emulator's gRPC screenshots and frame stream, against a fake EmulatorController."""

from __future__ import annotations

import sys
import threading
import time
from concurrent import futures
from pathlib import Path

import cv2
import grpc
import numpy as np
import pytest
from fastapi.testclient import TestClient

from dokkan_farmer.context import AppContext
from dokkan_farmer.device import AdbDevice, DeviceError, EmulatorGrpc, SimConfig, SimDevice, SimGame
from dokkan_farmer.device import emulator_grpc as eg
from dokkan_farmer.settings import Settings
from dokkan_farmer.web.app import create_app


def varint(value: int) -> bytes:
    return eg._varint(value)


def field(number: int, payload: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def int_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def image_message(rgb: np.ndarray, seq: int = 1, pad: int = 0) -> bytes:
    """An ``Image`` message the way the emulator sends it: RGB888, top row first."""
    height, width = rgb.shape[:2]
    rows = rgb.reshape(height, width * 3)
    if pad:
        rows = np.hstack([rows, np.zeros((height, pad), np.uint8)])
    fmt = int_field(1, eg.RGB888) + field(2, int_field(1, 0)) + int_field(3, width)
    fmt += int_field(4, height)
    return field(1, fmt) + field(4, rows.tobytes()) + int_field(5, seq) + int_field(6, 123)


def screen(width: int = 30, height: int = 40, top=(255, 0, 0), bottom=(0, 0, 255)) -> np.ndarray:
    """RGB: the top half one colour, the bottom half another (catches upside-down frames)."""
    rgb = np.zeros((height, width, 3), np.uint8)
    rgb[: height // 2] = top
    rgb[height // 2 :] = bottom
    return rgb


class FakeEmulator:
    """getScreenshot and streamScreenshot of android.emulation.control.EmulatorController."""

    def __init__(self, frames: int = 50, interval: float = 0.01) -> None:
        self.frames = frames
        self.interval = interval
        self.requests: list[bytes] = []
        self.streams_open = 0
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        handlers = {
            "getScreenshot": grpc.unary_unary_rpc_method_handler(self._shot),
            "streamScreenshot": grpc.unary_stream_rpc_method_handler(self._stream),
        }
        self.server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "android.emulation.control.EmulatorController", handlers
                ),
            )
        )
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()

    def _shot(self, request: bytes, context) -> bytes:
        self.requests.append(request)
        return image_message(screen())

    def _stream(self, request: bytes, context):
        self.requests.append(request)
        self.streams_open += 1
        try:
            for seq in range(1, self.frames + 1):
                if not context.is_active():
                    return
                yield image_message(screen(20, 27), seq=seq)
                time.sleep(self.interval)
        finally:
            self.streams_open -= 1

    def close(self) -> None:
        self.server.stop(None)


@pytest.fixture
def fake():
    server = FakeEmulator()
    yield server
    server.close()


# ------------------------------------------------------------------ messages
def test_image_format_encoding():
    assert eg.encode_image_format() == int_field(1, eg.RGB888)
    assert eg.encode_image_format(width=540) == int_field(1, 2) + int_field(3, 540)
    assert eg.encode_image_format(0, 0, fmt=0) == b""  # proto3: zeros are left out
    assert eg._varint(300) == b"\xac\x02"


def test_decoding_keeps_rows_upright_and_drops_padding():
    rgb = screen(5, 4)
    for pad in (0, 2):
        frame = eg.decode_image(image_message(rgb, seq=7, pad=pad))
        assert frame is not None and (frame.width, frame.height, frame.seq) == (5, 4, 7)
        bgr = frame.to_bgr()
        assert bgr.shape == (4, 5, 3)
        assert tuple(bgr[0, 0]) == (0, 0, 255)  # red on top (BGR)
        assert tuple(bgr[-1, -1]) == (255, 0, 0)  # blue at the bottom


def test_empty_and_broken_images():
    assert eg.decode_image(b"") is None  # the display is off
    assert eg.decode_image(field(1, int_field(3, 10) + int_field(4, 10))) is None
    with pytest.raises(ValueError):
        eg.decode_image(b"\x0a\xff")  # a length past the end
    # The deprecated width/height of Image (fields 2 and 3) still work.
    old = int_field(2, 2) + int_field(3, 1) + field(4, bytes(6))
    assert eg.decode_image(old).to_bgr().shape == (1, 2, 3)


@pytest.mark.parametrize("bad", ["android", "android:grpc", ":8554"])
def test_bad_addresses(bad):
    with pytest.raises(ValueError):
        EmulatorGrpc(bad)


# -------------------------------------------------------------------- client
def test_screenshot_and_stream(fake):
    client = EmulatorGrpc(f"127.0.0.1:{fake.port}")
    image = client.screenshot()
    assert image.shape == (40, 30, 3) and tuple(image[0, 0]) == (0, 0, 255)

    stream = client.stream(width=20)
    count, frame = stream.next(0, timeout=5)
    assert count >= 1 and frame is not None and frame.to_bgr().shape == (27, 20, 3)
    assert fake.requests[-1] == eg.encode_image_format(width=20)
    later, _ = stream.next(count, timeout=5)
    assert later > count  # only frames newer than the one asked after
    stream.close()
    deadline = time.monotonic() + 5
    while fake.streams_open and time.monotonic() < deadline:
        time.sleep(0.05)
    assert fake.streams_open == 0  # closing cancels the call on the emulator too
    client.close()


def test_a_finished_or_unreachable_stream_raises(fake):
    fake.frames = 2
    client = EmulatorGrpc(f"127.0.0.1:{fake.port}")
    stream = client.stream(width=20)
    count = 0
    with pytest.raises(DeviceError, match="ended"):
        for _ in range(10):
            count, _ = stream.next(count, timeout=5)
    client.close()

    gone = EmulatorGrpc(f"127.0.0.1:{fake.port}", timeout=1)
    fake.close()
    with pytest.raises(DeviceError, match="Emulator gRPC"):
        gone.screenshot()


# -------------------------------------------------------------------- device
FAKE_ADB = """
import sys
args = " ".join(sys.argv[1:])
if "dumpsys" in args:
    print("SurfaceOrientation: 0")
elif "exec-out" in args:
    sys.stdout.buffer.write(open({png!r}, "rb").read())
"""


@pytest.fixture
def adb(tmp_path) -> Path:
    png = tmp_path / "adb.png"
    cv2.imwrite(str(png), np.full((40, 30, 3), 90, np.uint8))  # grey: from adb
    script = tmp_path / "adb"
    script.write_text(f"#!{sys.executable}\n" + FAKE_ADB.format(png=str(png)))
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="fake adb is a POSIX script")
def test_adb_device_prefers_grpc(fake, adb):
    device = AdbDevice(
        "127.0.0.1:5555", adb_path=str(adb), grpc=EmulatorGrpc(f"127.0.0.1:{fake.port}")
    )
    assert tuple(device.screenshot()[0, 0]) == (0, 0, 255)  # red: from gRPC
    stream = device.live_stream(20)
    assert stream is not None
    stream.close()
    device.set_serial("192.168.1.50:5555")  # a phone: its own screen, no emulator stream
    assert device.live_stream(20) is None
    assert tuple(device.screenshot()[0, 0]) == (90, 90, 90)
    device.close()


# ----------------------------------------------------------------------- web
class StreamingAdb(AdbDevice):
    """An emulator whose screenshots and frame stream come from the fake gRPC server."""

    def __init__(self, port: int) -> None:
        super().__init__("127.0.0.1:5555", adb_path="adb-is-not-called")
        self.grpc = EmulatorGrpc(f"127.0.0.1:{port}")

    def connect(self) -> None:
        pass

    def info(self) -> dict:
        return {}

    def _screen_rotation(self) -> int:
        return 0

    def wake_screen(self) -> None:
        pass  # power saving wakes the emulator at startup (adb is not called here)


def parts(body: bytes) -> list[bytes]:
    return [p for p in body.split(b"--frame\r\n") if p]


def test_the_live_stream_from_the_emulator(fake, tmp_path):
    ctx = AppContext(Settings(data_dir=tmp_path / "data"), device=StreamingAdb(fake.port))
    with TestClient(create_app(ctx)) as client:
        res = client.get("/api/screen/stream.mjpg?w=120&fps=30&n=3")
        assert res.status_code == 200
        assert res.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
        frames = parts(res.content)
        assert len(frames) == 3
        head, _, jpeg = frames[0].partition(b"\r\n\r\n")
        assert b"Content-Type: image/jpeg" in head and jpeg[:2] == b"\xff\xd8"
        image = cv2.imdecode(np.frombuffer(jpeg.rstrip(b"\r\n"), np.uint8), cv2.IMREAD_COLOR)
        assert image.shape[1] == 20 and image[1, 10, 2] > 200  # red on top: upright


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
    device = SimDevice(SimGame(SimConfig(battle_seconds=1.0)), package=settings.game_package)
    with TestClient(create_app(AppContext(settings, device=device))) as test_client:
        yield test_client


def test_the_live_stream_from_screenshots(client):
    # The simulator has no frame stream: screenshots, paced to fps.
    started = time.monotonic()
    res = client.get("/api/screen/stream.mjpg?w=240&fps=10&n=3")
    assert res.status_code == 200 and len(parts(res.content)) == 3
    assert time.monotonic() - started >= 0.15  # two waits of 1/10 s between three frames
    assert client.get("/api/screen/stream.mjpg?fps=0").status_code == 422


def test_a_still_screen_is_resent(monkeypatch, tmp_path):
    # No new frame for KEEPALIVE seconds: the last one goes out again.
    from dokkan_farmer import live

    monkeypatch.setattr(live, "KEEPALIVE", 0.2)
    calls = threading.Event()

    class Farmer:
        frames = None

        def capture(self) -> int:
            calls.set()
            return 1

    class Frames:
        def jpeg(self, frame_id, max_width):
            return frame_id, b"\xff\xd8jpeg"

    farmer = Farmer()
    farmer.frames = Frames()
    feed = live.CaptureFeed(farmer, fps=30, width=100)
    assert feed.next_jpeg() == b"\xff\xd8jpeg"
    started = time.monotonic()
    assert feed.next_jpeg() == b"\xff\xd8jpeg"  # same frame id: resent after KEEPALIVE
    assert time.monotonic() - started >= 0.2 and calls.is_set()


def test_only_the_emulator_counts_as_emulator():
    from dokkan_farmer.device.emulator import EmulatorConsole

    device = AdbDevice("android:5555", adb_path="adb", grpc=EmulatorGrpc("android:8554"))
    assert device.is_emulator  # automatic power saving applies
    device.set_serial("192.168.1.50:5555")  # the Device tab now points at a phone
    assert not device.is_emulator
    console = EmulatorConsole("android:5554", "/screens")
    assert AdbDevice("android:5555", adb_path="adb", console=console).is_emulator
    assert not AdbDevice("android:5555", adb_path="adb").is_emulator


# --------------------------------------------------------------------- sound
def test_sound_from_the_emulator(tmp_path):
    requests = []

    def stream_audio(request, context):
        requests.append(request)
        for chunk in (b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"):
            yield field(1, request) + int_field(2, 7) + field(3, chunk)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    handlers = {"streamAudio": grpc.unary_stream_rpc_method_handler(stream_audio)}
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "android.emulation.control.EmulatorController", handlers
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        ctx = AppContext(Settings(data_dir=tmp_path / "data"), device=StreamingAdb(port))
        with TestClient(create_app(ctx)) as client:
            res = client.get("/api/audio.pcm")
            assert res.status_code == 200 and res.headers["x-sample-rate"] == "44100"
            assert res.content == b"\x01\x00\x02\x00\x03\x00\x04\x00"
        assert requests[0] == eg.encode_fields((1, 44100), (2, 1), (3, 1), (4, 1))
    finally:
        server.stop(None)


def test_no_sound_without_the_emulator(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
    device = SimDevice(SimGame(SimConfig()), package=settings.game_package)
    with TestClient(create_app(AppContext(settings, device=device))) as client:
        assert client.get("/api/audio.pcm").status_code == 404
