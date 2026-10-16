"""Which device address the farmer uses: the web UI's or the stack's ADB_SERIAL."""

from __future__ import annotations

from dokkan_farmer.context import AppContext
from dokkan_farmer.settings import Settings
from dokkan_farmer.state import StateStore


def context(tmp_path, adb_serial: str) -> AppContext:
    return AppContext(Settings(data_dir=tmp_path / "data", adb_serial=adb_serial))


def save_ui_address(tmp_path, address: str) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    StateStore(settings.state_file).update(adb_serial=address)


def test_a_changed_adb_serial_beats_an_older_address_from_the_ui(tmp_path):
    # Saved by an older version through the Device tab, before switching stacks.
    save_ui_address(tmp_path, "192.168.1.50:5555")
    assert context(tmp_path, "android:5555").device.serial == "android:5555"
    # Restarting with the same ADB_SERIAL keeps it.
    assert context(tmp_path, "android:5555").device.serial == "android:5555"


def test_the_ui_address_survives_restarts_until_adb_serial_changes(tmp_path):
    context(tmp_path, "192.168.1.50:5555")
    save_ui_address(tmp_path, "192.168.1.50:41234")  # new wireless debugging port
    assert context(tmp_path, "192.168.1.50:5555").device.serial == "192.168.1.50:41234"
    assert context(tmp_path, "android:5555").device.serial == "android:5555"


def test_without_adb_serial_the_ui_address_is_used(tmp_path):
    save_ui_address(tmp_path, "10.0.0.8:5555")
    assert context(tmp_path, "").device.serial == "10.0.0.8:5555"
