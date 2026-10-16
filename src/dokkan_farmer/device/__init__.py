"""Device backends: real Android devices over ADB, or the built-in simulator."""

from __future__ import annotations

from ..settings import Settings
from .adb import AdbDevice
from .base import KEYCODES, Device, DeviceError, normalize_key
from .emulator import EmulatorConsole
from .emulator_grpc import EmulatorGrpc
from .sim import SimConfig, SimDevice, SimGame, install_sim_templates

__all__ = [
    "KEYCODES",
    "AdbDevice",
    "Device",
    "DeviceError",
    "EmulatorConsole",
    "EmulatorGrpc",
    "SimConfig",
    "SimDevice",
    "SimGame",
    "create_device",
    "install_sim_templates",
    "normalize_key",
]


def create_device(settings: Settings, serial: str | None = None) -> Device:
    if settings.device_backend == "sim":
        return SimDevice(package=settings.game_package)
    console = grpc = None
    if settings.emulator_console:
        console = EmulatorConsole(settings.emulator_console, settings.emulator_screenshots)
    if settings.emulator_grpc:
        grpc = EmulatorGrpc(settings.emulator_grpc)
    return AdbDevice(
        serial=serial or settings.adb_serial,
        adb_path=settings.adb_path,
        console=console,
        grpc=grpc,
    )
