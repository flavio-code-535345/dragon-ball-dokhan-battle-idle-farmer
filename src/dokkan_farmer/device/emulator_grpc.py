"""Frames straight from the Android emulator's gRPC interface.

The emulator's ``EmulatorController`` service hands out the screen as raw pixels, already
scaled: on request (``getScreenshot``) or each time Android draws a new frame
(``streamScreenshot``). There is no PNG in between, so the Screen tab's live view runs at the
game's own frame rate. The emulator stack opens the service without a token (``-grpc``),
reachable only inside the stack's network.

Taps and swipes can go the same way (``sendTouch``): a few milliseconds instead of the few
hundred ``adb shell input`` takes to start.

The messages used are encoded by hand (a few protobuf fields of
``emulator_controller.proto``) to keep protobuf out of the dependencies.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import grpc
import numpy as np

from .base import DeviceError

log = logging.getLogger(__name__)

_SERVICE = "/android.emulation.control.EmulatorController/"
#: ImageFormat.ImgFormat.RGB888: 3 bytes a pixel, no compression
RGB888 = 2
MAX_MESSAGE = 64 << 20
#: A stream nobody read from for this long stops (a browser tab closed without notice).
IDLE_SECONDS = 30.0
#: The game's sound: 16-bit stereo PCM at this rate (AudioFormat of the proto).
AUDIO_RATE = 44100
AUDIO_CHANNELS = 2
#: ``Touch.pressure`` of a finger that is down (0 = lifted).
TOUCH_PRESSURE = 1024
#: A touch call takes milliseconds: waiting long for a dead emulator would stall the farm.
TOUCH_TIMEOUT = 3.0


# ------------------------------------------------------------------ protobuf
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: memoryview, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(data: memoryview) -> Iterator[tuple[int, int | memoryview]]:
    """(field number, value) of a protobuf message: ints, or memoryviews of length fields."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        value: int | memoryview
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value, pos = data[pos : pos + length], pos + length
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, value


def encode_fields(*pairs: tuple[int, int]) -> bytes:
    """A message of integer fields ``(number, value)`` (zeros left out, as in proto3)."""
    return b"".join(_varint(n << 3) + _varint(v) for n, v in pairs if v)


def encode_image_format(width: int = 0, height: int = 0, fmt: int = RGB888) -> bytes:
    """An ``ImageFormat`` message: format = 1, width = 3, height = 4 (0 = not scaled)."""
    out = b""
    for number, value in ((1, fmt), (3, width), (4, height)):
        if value:  # proto3 leaves zeros out
            out += _varint(number << 3) + _varint(value)
    return out


def encode_touch_event(x: int, y: int, pressed: bool, identifier: int = 0) -> bytes:
    """A ``TouchEvent`` with one ``Touch`` (x = 1, y = 2, identifier = 3, pressure = 4)."""
    touch = encode_fields(
        (1, max(0, int(x))),
        (2, max(0, int(y))),
        (3, identifier),
        (4, TOUCH_PRESSURE if pressed else 0),
    )
    return _varint(1 << 3 | 2) + _varint(len(touch)) + touch  # touches = 1 (repeated)


@dataclass(frozen=True)
class Frame:
    """One ``Image`` message: RGB888 pixels as the emulator sent them."""

    width: int
    height: int
    pixels: memoryview
    seq: int = 0

    def to_bgr(self) -> np.ndarray:
        stride = len(self.pixels) // self.height  # rows may be padded
        rows = np.frombuffer(self.pixels, np.uint8, count=stride * self.height)
        rgb = rows.reshape(self.height, stride)[:, : self.width * 3].reshape(
            self.height, self.width, 3
        )
        # Top row first: emulator_controller.proto says "bottom up", but compared with
        # Android's own screencap (emulator 36.5) the rows come top-down.
        return np.ascontiguousarray(rgb[:, :, ::-1])


def decode_image(data: bytes) -> Frame | None:
    """An ``Image`` message (format = 1, image = 4, seq = 5); ``None`` when it is empty."""
    width = height = seq = 0
    old_width = old_height = 0
    pixels: memoryview | None = None
    for number, value in _fields(memoryview(data)):
        if number == 1 and isinstance(value, memoryview):  # ImageFormat
            for inner, inner_value in _fields(value):
                if inner == 3 and isinstance(inner_value, int):
                    width = inner_value
                elif inner == 4 and isinstance(inner_value, int):
                    height = inner_value
        elif number == 2 and isinstance(value, int):  # deprecated width
            old_width = value
        elif number == 3 and isinstance(value, int):  # deprecated height
            old_height = value
        elif number == 4 and isinstance(value, memoryview):
            pixels = value
        elif number == 5 and isinstance(value, int):
            seq = value
    width, height = width or old_width, height or old_height
    # An empty image: the display is off or not visible.
    if not pixels or not width or not height or len(pixels) < width * height * 3:
        return None
    return Frame(width, height, pixels, seq)


# --------------------------------------------------------------------- client
class EmulatorGrpc:
    """A connection to the emulator's gRPC port (``-grpc``), e.g. ``android:8554``."""

    def __init__(self, address: str, timeout: float = 10.0) -> None:
        host, sep, port = address.strip().rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"EMULATOR_GRPC must look like android:8554, got {address!r}")
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._channel: grpc.Channel | None = None
        self._lock = threading.Lock()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def _rpc_channel(self) -> grpc.Channel:
        with self._lock:
            if self._channel is None:
                self._channel = grpc.insecure_channel(
                    self.address,
                    options=[
                        ("grpc.max_receive_message_length", MAX_MESSAGE),
                        ("grpc.max_send_message_length", MAX_MESSAGE),
                    ],
                )
            return self._channel

    def close(self) -> None:
        with self._lock:
            if self._channel is not None:
                self._channel.close()
            self._channel = None

    def _error(self, exc: grpc.RpcError) -> DeviceError:
        code = exc.code() if hasattr(exc, "code") else None
        details = exc.details() if hasattr(exc, "details") else str(exc)
        name = code.name if code is not None else "error"
        return DeviceError(f"Emulator gRPC {self.address}: {name}: {details}")

    def screenshot(self) -> np.ndarray:
        """The whole screen, full size, as a BGR image."""
        call = self._rpc_channel().unary_unary(_SERVICE + "getScreenshot")
        try:
            data = call(encode_image_format(), timeout=self.timeout)
        except grpc.RpcError as exc:
            raise self._error(exc) from exc
        frame = decode_image(data)
        if frame is None:
            raise DeviceError("The emulator sent an empty screenshot (is its display on?)")
        return frame.to_bgr()

    def touch(self, x: int, y: int, pressed: bool) -> None:
        """A finger down (or moved, while it is down) at pixel (x, y), or lifted there."""
        call = self._rpc_channel().unary_unary(_SERVICE + "sendTouch")
        try:
            call(encode_touch_event(x, y, pressed), timeout=min(self.timeout, TOUCH_TIMEOUT))
        except grpc.RpcError as exc:
            raise self._error(exc) from exc

    def audio(self) -> Iterator[bytes]:
        """The game's sound as it plays: 16-bit little-endian stereo PCM at AUDIO_RATE.

        ``streamAudio`` with an ``AudioFormat``: samplingRate = 1, channels = 2 (Stereo),
        format = 3 (S16), mode = 4 (real time); each ``AudioPacket`` has its samples in 3.
        Closing the iterator cancels the call.
        """
        request = encode_fields((1, AUDIO_RATE), (2, 1), (3, 1), (4, 1))
        call = self._rpc_channel().unary_stream(_SERVICE + "streamAudio")(request)
        try:
            for data in call:
                for number, value in _fields(memoryview(data)):
                    if number == 3 and isinstance(value, memoryview) and len(value):
                        yield bytes(value)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.CANCELLED:
                raise self._error(exc) from exc
        finally:
            call.cancel()

    def stream(self, width: int) -> FrameStream:
        """Frames at most ``width`` pixels wide (the emulator keeps the proportions), each
        time Android draws a new one."""
        call = self._rpc_channel().unary_stream(_SERVICE + "streamScreenshot")
        return FrameStream(call(encode_image_format(width=width)), self.address)


class FrameStream:
    """Reads a ``streamScreenshot`` call in the background and keeps only the newest frame."""

    def __init__(self, call: grpc.Call, address: str) -> None:
        self._call = call
        self._address = address
        self._cond = threading.Condition()
        self._latest: bytes | None = None
        self._count = 0
        self._done = False
        self._error: str | None = None
        self._polled = time.monotonic()
        self._thread = threading.Thread(target=self._read, name="emulator-stream", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            for data in self._call:
                with self._cond:
                    self._latest = data
                    self._count += 1
                    self._cond.notify_all()
                if time.monotonic() - self._polled > IDLE_SECONDS:
                    self._call.cancel()
                    break
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.CANCELLED:
                self._error = f"Emulator gRPC {self._address}: {exc.code().name}: {exc.details()}"
        finally:
            with self._cond:
                self._done = True
                self._cond.notify_all()

    def next(self, after: int, timeout: float) -> tuple[int, Frame | None]:
        """The newest frame after frame number ``after``: ``(number, frame)``.

        Waits up to ``timeout`` seconds; ``(after, None)`` if nothing newer came. Raises
        :class:`DeviceError` once the stream has ended.
        """
        self._polled = time.monotonic()
        deadline = self._polled + timeout
        with self._cond:
            while self._count <= after and not self._done:
                left = deadline - time.monotonic()
                if left <= 0:
                    return after, None
                self._cond.wait(left)
            if self._count <= after:
                raise DeviceError(self._error or f"The emulator's stream ({self._address}) ended")
            count, data = self._count, self._latest
        assert data is not None
        frame = decode_image(data)
        return count, frame

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._call.cancel()
