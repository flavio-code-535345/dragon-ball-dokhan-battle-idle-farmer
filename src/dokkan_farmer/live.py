"""The Screen tab's live view as one continuous MJPEG stream.

The emulator stack sends frames as Android draws them (gRPC). Other devices give
screenshots, taken here at the requested rate (while farming: the farm loop's frames).
Either way the browser keeps one connection open instead of asking for each frame, so the
delay of a proxy such as Cloudflare counts once instead of per frame.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from .device.base import DeviceError
from .vision import encode_jpeg

if TYPE_CHECKING:
    from .device.emulator_grpc import FrameStream
    from .engine import Farmer

#: Without a new frame (a still screen), the last one is sent again after this long: proxies
#: keep the connection open, and a closed browser tab is noticed at the next write.
KEEPALIVE = 5.0
JPEG_QUALITY = 70
BOUNDARY = "frame"


class Feed(Protocol):
    def next_jpeg(self) -> bytes: ...

    def close(self) -> None: ...


def mjpeg_part(jpeg: bytes) -> bytes:
    head = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
    return head.encode() + jpeg + b"\r\n"


class StreamFeed:
    """Frames of the emulator's gRPC stream, at most ``fps`` a second."""

    def __init__(self, stream: FrameStream, fps: float, width: int) -> None:
        self.stream = stream
        self.interval = 1.0 / fps
        self.width = width
        self._count = 0
        self._jpeg: bytes | None = None
        self._next_at = 0.0

    def next_jpeg(self) -> bytes:
        wait = self._next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # The first frame may take a moment; later ones are resent while the screen is still.
        count, frame = self.stream.next(self._count, KEEPALIVE if self._jpeg else 15.0)
        if count != self._count:
            self._count = count
            if frame is not None:  # None: an empty image (display off): keep the last one
                self._jpeg = encode_jpeg(frame.to_bgr(), JPEG_QUALITY, max_width=self.width)
        if self._jpeg is None:
            raise DeviceError("The emulator sent no picture")
        self._next_at = time.monotonic() + self.interval
        return self._jpeg

    def close(self) -> None:
        self.stream.close()


class CaptureFeed:
    """Screenshots at most ``fps`` a second (phones, redroid, the emulator without gRPC)."""

    def __init__(self, farmer: Farmer, fps: float, width: int) -> None:
        self.farmer = farmer
        self.interval = 1.0 / fps
        self.width = width
        self._frame_id: int | None = None
        self._jpeg: bytes | None = None
        self._next_at = 0.0

    def next_jpeg(self) -> bytes:
        wait = self._next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resend_at = time.monotonic() + KEEPALIVE
        while True:
            # While farming this is the farm loop's newest frame, otherwise a new screenshot.
            frame_id = self.farmer.capture()
            if frame_id != self._frame_id or self._jpeg is None:
                break
            if time.monotonic() >= resend_at:
                return self._jpeg
            time.sleep(min(self.interval, 0.5))
        result = self.farmer.frames.jpeg(frame_id, max_width=self.width)
        if result is None:
            raise DeviceError("No screenshot yet")
        self._frame_id, self._jpeg = result
        self._next_at = time.monotonic() + self.interval
        return self._jpeg

    def close(self) -> None:
        pass


def open_feed(farmer: Farmer, width: int, fps: float) -> Feed:
    """The emulator's frame stream where there is one, otherwise screenshots."""
    stream = farmer.device_call(lambda d: d.live_stream(width))
    if stream is not None:
        return StreamFeed(stream, fps, width)
    return CaptureFeed(farmer, fps, width)
