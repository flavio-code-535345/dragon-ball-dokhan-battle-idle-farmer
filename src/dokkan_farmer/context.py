"""Wires settings, storage, device, matcher and farmer together for the web app."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .device import (
    AdbDevice,
    Device,
    DeviceError,
    EmulatorPower,
    SimDevice,
    create_device,
    install_sim_templates,
)
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
#: How often the idle screen-off of power saving is checked.
POWER_CHECK_SECONDS = 30
#: How long the emulator gets to confirm "start" before the farm waits for the device anyway.
EMULATOR_START_SECONDS = 20


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
        serial = self._device_serial(settings)
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
        #: power control of the emulator stack (needs its supervisor, see EmulatorPower)
        self.emulator: EmulatorPower | None = None
        if settings.emulator_console:
            self.emulator = EmulatorPower(settings.emulator_screenshots)
        self._boot_lock = threading.Lock()

    # ------------------------------------------------------------------ device
    def _device_serial(self, settings: Settings) -> str:
        """The address set in the web UI, unless ADB_SERIAL was changed since then."""
        saved = self.state.get("adb_serial")
        env = settings.adb_serial
        if saved and env and saved != env and env != self.state.get("adb_serial_env"):
            # e.g. the stack switched from a phone to the emulator: the stack wins.
            log.info("ADB_SERIAL changed to %s: using it instead of %s set in the UI", env, saved)
            saved = None
        self.state.update(adb_serial=saved, adb_serial_env=env)
        return saved or env

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
        self.ensure_emulator_on()
        self.farmer.start(routine_id, routine, values)
        return routine

    # ------------------------------------------------------------ emulator power
    def emulator_status(self) -> dict[str, Any]:
        """For the web UI: whether the emulator can be started and stopped, and its state."""
        state = self.emulator.state() if self.emulator is not None else None
        minutes = self.settings.emulator_off_minutes
        return {
            "available": state is not None,
            "state": state,
            "off_minutes": minutes if state is not None else 0,
            "busy": self.farmer.is_running,
        }

    def emulator_command(self, action: str) -> dict[str, Any]:
        """Start, stop or restart the emulator (the Device tab's buttons)."""
        if self.emulator is None or self.emulator.state() is None:
            raise RuntimeError(
                "The emulator has no power control: update the stack (docker-compose.emulator.yml)"
            )
        if action in ("stop", "restart") and self.farmer.is_running:
            raise RuntimeError("Stop the farm first")
        if action == "start" and self.emulator.state() == "running":
            return self.emulator_status()
        self.farmer.mark_watched()  # a started emulator is not shut down at once again
        self.emulator.send(action)
        if action == "stop":
            self._on_device_status(False, "The emulator is shut down")
        else:
            self._spawn_boot_watch()
        return self.emulator_status()

    def ensure_emulator_on(self) -> None:
        """Start the emulator if it was shut down; the farm then waits for the device."""
        if self.emulator is None or self.emulator.state() in (None, "running"):
            return
        log.info("The emulator is shut down: starting it for the farm")
        self.farmer.mark_watched()
        self.emulator.send("start")
        self.emulator.wait_for("running", EMULATOR_START_SECONDS, command="start")
        self._spawn_boot_watch()

    def check_emulator_idle(self) -> None:
        """Shut the emulator down after EMULATOR_OFF_MINUTES without a farm or web UI use."""
        minutes = self.settings.emulator_off_minutes
        if not minutes or self.emulator is None or self.emulator.state() != "running":
            return
        if self.farmer.is_running or self.installer.busy or self._boot_lock.locked():
            return
        if self.farmer.idle_seconds() < minutes * 60:
            return
        log.info("Shutting the emulator down to save power (idle for %d min)", minutes)
        try:
            self.emulator.send("stop")
        except (OSError, RuntimeError) as exc:
            log.warning("Could not shut the emulator down: %s", exc)
            return
        self._on_device_status(False, "The emulator is shut down")

    def _spawn_boot_watch(self) -> None:
        threading.Thread(target=self._boot_watch, name="emulator-boot", daemon=True).start()

    def _boot_watch(self) -> None:
        """After a start or restart: connect as soon as Android is up."""
        if not self._boot_lock.acquire(blocking=False):
            return
        try:
            time.sleep(3)  # a restart: let the old Android go before looking for the new one
            status = self._connect_with_retries()
            if status and status["connected"] and self.farmer.power_save:
                self.farmer.wake_at_startup()
        finally:
            self._boot_lock.release()

    # ---------------------------------------------------------------- lifecycle
    def startup(self) -> None:
        threading.Thread(target=self._startup_worker, name="startup", daemon=True).start()
        if self.settings.power_save is not False or self.emulator is not None:
            threading.Thread(target=self._power_worker, name="power-save", daemon=True).start()

    def _power_worker(self) -> None:
        while not self._closing.wait(POWER_CHECK_SECONDS):
            try:
                self.farmer.check_idle()
                self.check_emulator_idle()
            except Exception:
                log.exception("Power saving check failed")

    def _connect_with_retries(self) -> dict[str, Any] | None:
        """Try the device for STARTUP_CONNECT_SECONDS (Android needs a minute or two to boot)."""
        status = self.refresh_device()
        deadline = time.monotonic() + STARTUP_CONNECT_SECONDS
        if not status["connected"]:
            log.info("Waiting for the device (%s)...", status["error"])
        while not status["connected"] and time.monotonic() < deadline:
            if self._closing.wait(STARTUP_RETRY_SECONDS):
                return None
            status = self.refresh_device()
        return status

    def _startup_worker(self) -> None:
        status = self._connect_with_retries()
        if status is None:
            return
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
        if self.farmer.power_save and status["connected"]:
            self.farmer.wake_at_startup()
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
