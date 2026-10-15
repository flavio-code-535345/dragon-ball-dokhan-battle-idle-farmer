"""Small JSON file with UI choices, lifetime statistics and session history."""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .util import atomic_write

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "selected_routine": None,
    "options": {},
    "adb_serial": None,
    "was_running": False,
    "lifetime": {"runs": 0, "sessions": 0, "seconds": 0},
    "sessions": [],
}
MAX_SESSIONS = 30


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("Could not read %s (%s); starting fresh", self.path, exc)
                return
            if isinstance(loaded, dict):
                merged = copy.deepcopy(DEFAULTS)
                merged.update(loaded)
                merged["lifetime"] = {**DEFAULTS["lifetime"], **loaded.get("lifetime", {})}
                self.data = merged

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self.data, indent=2, sort_keys=True).encode("utf-8")
        atomic_write(self.path, payload)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def update(self, **values: Any) -> None:
        with self._lock:
            self.data.update(values)
            self.save()

    def routine_options(self, routine_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("options", {}).get(routine_id, {}))

    def set_routine_options(self, routine_id: str, options: dict[str, Any]) -> None:
        with self._lock:
            self.data.setdefault("options", {})[routine_id] = dict(options)
            self.save()

    def add_runs(self, count: int = 1) -> None:
        with self._lock:
            self.data["lifetime"]["runs"] = int(self.data["lifetime"].get("runs", 0)) + count
            self.save()

    def record_session(self, summary: dict[str, Any]) -> None:
        with self._lock:
            lifetime = self.data["lifetime"]
            lifetime["sessions"] = int(lifetime.get("sessions", 0)) + 1
            lifetime["seconds"] = int(lifetime.get("seconds", 0)) + int(summary.get("seconds", 0))
            sessions = self.data.setdefault("sessions", [])
            sessions.append(summary)
            del sessions[:-MAX_SESSIONS]
            self.save()
