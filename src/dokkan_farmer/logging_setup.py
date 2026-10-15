"""Logging: console + rotating file + an in-memory ring buffer for the web UI."""

from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log records so the web UI can tail them."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)
            return
        with self._buffer_lock:
            self._records.append(
                {
                    "id": self._next_id,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": message,
                }
            )
            self._next_id += 1

    def since(self, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._buffer_lock:
            items = [r for r in self._records if r["id"] > after_id]
        return items[-limit:]


LOG_BUFFER = RingBufferHandler()
LOG_BUFFER.setFormatter(logging.Formatter("%(message)s"))


def ensure_log_buffer() -> None:
    """Let the web UI show our logs even when :func:`setup_logging` was not called."""
    if LOG_BUFFER in logging.getLogger().handlers:
        return
    package = logging.getLogger("dokkan_farmer")
    if LOG_BUFFER not in package.handlers:
        package.addHandler(LOG_BUFFER)
    if package.level == logging.NOTSET:
        package.setLevel(logging.INFO)


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    logging.getLogger("dokkan_farmer").removeHandler(LOG_BUFFER)
    root.addHandler(LOG_BUFFER)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "farmer.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)

    # The UI polls the API every couple of seconds; keep the access log quiet.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
