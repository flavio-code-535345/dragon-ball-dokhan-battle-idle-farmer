"""Active-hours windows such as ``"22:00-07:30, 13:00-14:00"``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_DAY = 24 * 60


def _in_window(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # window crosses midnight


@dataclass(frozen=True)
class ActiveHours:
    """A set of daily time windows. No windows means "always active"."""

    windows: tuple[tuple[int, int], ...] = ()

    @classmethod
    def parse(cls, text: str | None) -> ActiveHours:
        if not text or not text.strip():
            return cls()
        windows: list[tuple[int, int]] = []
        for part in re.split(r"[,;]", text):
            if not part.strip():
                continue
            match = _WINDOW_RE.match(part)
            if not match:
                raise ValueError(f"Invalid window {part.strip()!r}: expected HH:MM-HH:MM")
            h1, m1, h2, m2 = (int(g) for g in match.groups())
            start, end = h1 * 60 + m1, h2 * 60 + m2
            if m1 > 59 or m2 > 59 or start >= _DAY or end > _DAY:
                raise ValueError(f"Invalid time in window {part.strip()!r}")
            if start == end:
                raise ValueError(f"Window {part.strip()!r} is empty")
            windows.append((start, end))
        return cls(tuple(windows))

    def __bool__(self) -> bool:
        return bool(self.windows)

    def __str__(self) -> str:
        if not self.windows:
            return "always"
        return ", ".join(
            f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in self.windows
        )

    def is_active(self, now: datetime) -> bool:
        if not self.windows:
            return True
        minute = now.hour * 60 + now.minute
        return any(_in_window(minute, start, end) for start, end in self.windows)

    def next_start(self, now: datetime) -> datetime | None:
        """The next minute at which a window is open (``None`` if always active)."""
        if not self.windows:
            return None
        base = now.replace(second=0, microsecond=0)
        for step in range(1, _DAY + 1):
            candidate = base + timedelta(minutes=step)
            if self.is_active(candidate):
                return candidate
        return None
