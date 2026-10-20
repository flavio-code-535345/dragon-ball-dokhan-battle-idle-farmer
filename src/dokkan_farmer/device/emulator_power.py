"""Start, stop and restart the Android emulator of the emulator stack.

The farmer cannot stop the emulator's process itself (it runs in the other container), so
the emulator container runs a small supervisor (docker-compose.emulator.yml) that watches
two files in the folder both containers share: the farmer writes a command (``start``,
``stop`` or ``restart``) to ``emulator.ctl``, the supervisor does it and writes ``running``,
``stopping`` or ``stopped`` to ``emulator.state``. No state file = no supervisor (an older
stack): the farmer then never shuts the emulator down.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

COMMANDS = ("start", "stop", "restart")
STATES = ("running", "stopping", "stopped")
#: a state file older than this and still saying "running" is fine (the supervisor only
#: writes on changes); this is only how long a command may wait before it is sent again
RESEND_SECONDS = 15.0


class EmulatorPower:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.control = self.directory / "emulator.ctl"
        self.state_file = self.directory / "emulator.state"

    @property
    def available(self) -> bool:
        """The supervisor is there (it writes the state file as soon as it starts)."""
        return self.state_file.is_file()

    def state(self) -> str | None:
        """running, stopping or stopped; None without a supervisor."""
        try:
            text = self.state_file.read_text().strip()
        except OSError:
            return None
        return text if text in STATES else None

    def send(self, command: str) -> None:
        if command not in COMMANDS:
            raise ValueError(f"Unknown emulator command {command!r}")
        if not self.available:
            raise RuntimeError(
                "The emulator has no power control: update the stack (docker-compose.emulator.yml)"
            )
        tmp = self.control.with_suffix(".tmp")
        tmp.write_text(command + "\n")
        os.replace(tmp, self.control)  # the supervisor never reads half a command

    def wait_for(self, state: str, timeout: float, *, command: str | None = None) -> bool:
        """Wait until the supervisor reports ``state``; resend ``command`` now and then."""
        deadline = time.monotonic() + timeout
        resent = time.monotonic()
        while time.monotonic() < deadline:
            if self.state() == state:
                return True
            if command and time.monotonic() - resent >= RESEND_SECONDS:
                resent = time.monotonic()
                with contextlib.suppress(OSError, RuntimeError):
                    self.send(command)
            time.sleep(0.1)
        return self.state() == state
