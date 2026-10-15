"""Routine files on disk: bundled examples plus the user's own copies."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .routine import Routine, RoutineError, parse_routine
from .util import atomic_write

log = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).parent / "routines"
ROUTINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def validate_routine_id(routine_id: str) -> str:
    if not ROUTINE_ID_RE.match(routine_id or ""):
        raise RoutineError("Routine ids may only use letters, digits, '_' and '-' (max 64)")
    return routine_id


@dataclass
class RoutineInfo:
    id: str
    name: str
    description: str
    bundled: bool
    modified: bool
    valid: bool
    error: str | None


class RoutineLibrary:
    def __init__(self, directory: Path, bundled_dir: Path = BUNDLED_DIR) -> None:
        self.directory = Path(directory)
        self.bundled_dir = Path(bundled_dir)
        self.directory.mkdir(parents=True, exist_ok=True)

    def install_defaults(self) -> list[str]:
        """Copy bundled routines that the user does not have yet (never overwrites)."""
        installed = []
        for src in sorted(self.bundled_dir.glob("*.yaml")):
            dest = self.directory / src.name
            if not dest.exists():
                shutil.copyfile(src, dest)
                installed.append(src.stem)
        if installed:
            log.info("Installed bundled routine(s): %s", ", ".join(installed))
        return installed

    def path(self, routine_id: str) -> Path:
        return self.directory / f"{validate_routine_id(routine_id)}.yaml"

    def bundled_path(self, routine_id: str) -> Path:
        return self.bundled_dir / f"{validate_routine_id(routine_id)}.yaml"

    def exists(self, routine_id: str) -> bool:
        return self.path(routine_id).exists()

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.yaml") if ROUTINE_ID_RE.match(p.stem))

    def list(self) -> list[RoutineInfo]:
        infos = []
        for routine_id in self.ids():
            text = self.read_text(routine_id)
            bundled = self.bundled_path(routine_id)
            modified = bundled.exists() and bundled.read_text(encoding="utf-8") != text
            try:
                routine = parse_routine(text)
                infos.append(
                    RoutineInfo(
                        routine_id,
                        routine.name,
                        routine.description,
                        bundled.exists(),
                        modified,
                        True,
                        None,
                    )
                )
            except RoutineError as exc:
                infos.append(
                    RoutineInfo(
                        routine_id, routine_id, "", bundled.exists(), modified, False, str(exc)
                    )
                )
        return infos

    def read_text(self, routine_id: str) -> str:
        path = self.path(routine_id)
        if not path.exists():
            raise KeyError(routine_id)
        return path.read_text(encoding="utf-8")

    def load(self, routine_id: str) -> Routine:
        return parse_routine(self.read_text(routine_id))

    def save_text(self, routine_id: str, text: str) -> Routine:
        routine = parse_routine(text)  # never save something that does not load
        atomic_write(self.path(routine_id), text.encode("utf-8"))
        return routine

    def delete(self, routine_id: str) -> None:
        path = self.path(routine_id)
        if not path.exists():
            raise KeyError(routine_id)
        path.unlink()

    def restore(self, routine_id: str) -> Routine:
        bundled = self.bundled_path(routine_id)
        if not bundled.exists():
            raise KeyError(routine_id)
        return self.save_text(routine_id, bundled.read_text(encoding="utf-8"))
