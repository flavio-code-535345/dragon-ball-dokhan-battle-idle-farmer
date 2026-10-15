"""Wires settings, storage, device, matcher and farmer together for the web app."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .device import AdbDevice, Device, DeviceError, SimDevice, create_device, install_sim_templates
from .engine import Farmer, missing_required_templates
from .installer import InstallManager
from .library import RoutineLibrary
from .notify import Notifier
from .routine import Routine, RoutineError
from .settings import Settings
from .state import StateStore
from .vision import Matcher, TemplateStore

log = logging.getLogger(__name__)

#: At startup, keep trying to reach the device for this long (Android in a
#: container needs a minute or two to boot).
STARTUP_CONNECT_SECONDS = 300
STARTUP_RETRY_SECONDS = 10


class AppContext:
    def __init__(
        self,
        settings: Settings,
        *,
        device: Device | None = None,
        farmer_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(settings.state_file)
        self.library = RoutineLibrary(settings.routines_dir)
        self.library.install_defaults()
        self.store = TemplateStore(settings.templates_dir)
        serial = self.state.get("adb_serial") or settings.adb_serial
        self.device = device or create_device(settings, serial=serial)
        if isinstance(self.device, SimDevice):
            # Keep the demo templates in sync with the simulator's drawings.
            install_sim_templates(self.store)
        self.matcher = Matcher(self.store, settings.match_scale)
        self.notifier = Notifier(settings.notify_urls)
        self._device_lock = threading.Lock()
        self._device_status: dict[str, Any] = {
            "connected": False,
            "error": None,
            "info": {},
            "checked_at": None,
        }
        self.farmer = Farmer(
            self.device,
            self.matcher,
            settings,
            notifier=self.notifier,
            state=self.state,
            on_device_status=self._on_device_status,
            **(farmer_kwargs or {}),
        )
        # Installs run for minutes: they bypass the device lock so the live
        # screen and touch control keep working meanwhile (adb runs both at once).
        self.installer = InstallManager(settings.data_dir / "apk-tmp", lambda fn: fn(self.device))
        self._closing = threading.Event()

    # ------------------------------------------------------------------ device
    @property
    def device_status(self) -> dict[str, Any]:
        with self._device_lock:
            status = dict(self._device_status)
        status["serial"] = self.device.serial
        status["backend"] = self.device.backend
        return status

    def _on_device_status(self, connected: bool, error: str | None) -> None:
        with self._device_lock:
            self._device_status.update(connected=connected, error=error, checked_at=time.time())

    def refresh_device(self) -> dict[str, Any]:
        try:
            with self.farmer.device_lock:
                self.device.connect()
                info = self.device.info()
        except (DeviceError, ValueError) as exc:
            with self._device_lock:
                self._device_status.update(
                    connected=False, error=str(exc), info={}, checked_at=time.time()
                )
        else:
            with self._device_lock:
                self._device_status.update(
                    connected=True, error=None, info=info, checked_at=time.time()
                )
        return self.device_status

    def set_serial(self, serial: str) -> dict[str, Any]:
        if not isinstance(self.device, AdbDevice):
            raise ValueError("The simulator has no serial to change")
        if self.farmer.is_running:
            raise RuntimeError("Stop the farm before switching devices")
        with self.farmer.device_lock:
            self.device.set_serial(serial)
        self.state.update(adb_serial=self.device.serial or None)
        return self.refresh_device()

    # ------------------------------------------------------------------- farm
    def selected_routine(self) -> str:
        selected = self.state.get("selected_routine")
        if selected and self.library.exists(selected):
            return selected
        if self.library.exists(self.settings.routine):
            return self.settings.routine
        ids = self.library.ids()
        return ids[0] if ids else self.settings.routine

    def start_farm(self, routine_id: str, options: dict[str, Any] | None = None) -> Routine:
        routine = self.library.load(routine_id)
        missing = missing_required_templates(routine, self.store)
        if missing:
            raise RoutineError(
                f"Capture the required template(s) first: {', '.join(missing)} (Templates tab)"
            )
        saved = routine.sanitize_options(self.state.routine_options(routine_id))
        merged = {**saved, **(options or {})}
        values = routine.option_values(merged)
        self.state.set_routine_options(routine_id, values)
        self.state.update(selected_routine=routine_id)
        self.farmer.start(routine_id, routine, values)
        return routine

    # ---------------------------------------------------------------- lifecycle
    def startup(self) -> None:
        threading.Thread(target=self._startup_worker, name="startup", daemon=True).start()

    def _startup_worker(self) -> None:
        status = self.refresh_device()
        deadline = time.monotonic() + STARTUP_CONNECT_SECONDS
        if not status["connected"]:
            log.info("Waiting for the device (%s)...", status["error"])
        while not status["connected"] and time.monotonic() < deadline:
            if self._closing.wait(STARTUP_RETRY_SECONDS):
                return
            status = self.refresh_device()
        if status["connected"]:
            info = status["info"]
            log.info(
                "Device %s connected (%s %s, Android %s, %s)",
                self.device.serial or "(default)",
                info.get("manufacturer", ""),
                info.get("model", ""),
                info.get("android", "?"),
                info.get("resolution", "?"),
            )
        else:
            log.warning("Device not connected yet: %s", status["error"])
        mode = self.settings.autostart
        resume = mode == "resume" and bool(self.state.get("was_running"))
        if mode == "true" or resume:
            routine_id = self.selected_routine()
            try:
                log.info("Autostart (%s): starting routine %s", mode, routine_id)
                self.start_farm(routine_id)
            except Exception as exc:
                log.error("Autostart failed: %s", exc)

    def shutdown(self) -> None:
        self._closing.set()
        if self.farmer.is_running:
            self.farmer.stop("Container stopped", resume_later=True)
        self.notifier.close()
        self.device.close()
