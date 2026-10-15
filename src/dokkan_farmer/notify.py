"""Push notifications through Apprise (Discord, Telegram, ntfy, Gotify, e-mail, ...)."""

from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
from typing import Any

import numpy as np

from .vision import encode_jpeg

log = logging.getLogger(__name__)

_STOP = object()


class Notifier:
    """Sends notifications from a background thread so the farm loop never blocks."""

    def __init__(self, urls: str = "", app_name: str = "Dokkan Farmer") -> None:
        self.urls = urls.strip()
        self.app_name = app_name
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.sent: list[tuple[str, str]] = []  # recent messages, shown in the UI

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    def send(self, title: str, body: str, image: np.ndarray | None = None) -> None:
        self.sent.append((title, body))
        del self.sent[:-20]
        if not self.enabled:
            return
        self._ensure_worker()
        attachment = encode_jpeg(image, quality=70, max_width=720) if image is not None else None
        try:
            self._queue.put_nowait((f"{self.app_name}: {title}", body, attachment))
        except queue.Full:
            log.warning("Notification queue is full, dropping %r", title)

    def send_now(self, title: str, body: str) -> bool:
        """Synchronous send (used by the "test notification" button)."""
        if not self.enabled:
            raise RuntimeError("No NOTIFY_URLS configured")
        return self._deliver(f"{self.app_name}: {title}", body, None)

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            self._queue.put(_STOP)
            thread.join(timeout=timeout)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            title, body, attachment = item
            try:
                self._deliver(title, body, attachment)
            except Exception:  # pragma: no cover - never kill the worker
                log.exception("Notification failed")

    def _deliver(self, title: str, body: str, attachment: bytes | None) -> bool:
        import apprise  # imported lazily: only needed when notifications are configured

        client = apprise.Apprise()
        if not client.add(self.urls):
            log.error("NOTIFY_URLS contains no valid Apprise URL")
            return False
        path = None
        try:
            if attachment:
                fd, path = tempfile.mkstemp(suffix=".jpg", prefix="dokkan-farmer-")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(attachment)
            ok = bool(client.notify(title=title, body=body, attach=path))
            if not ok:
                log.warning("Notification %r was not delivered", title)
            return ok
        finally:
            if path:
                os.unlink(path)
