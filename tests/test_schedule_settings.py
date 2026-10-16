from datetime import datetime
from pathlib import Path

import pytest

from dokkan_farmer.schedule import ActiveHours
from dokkan_farmer.settings import GAME_PACKAGES, Settings


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 15, hour, minute)


def test_no_windows_means_always_active():
    hours = ActiveHours.parse("")
    assert not hours
    assert hours.is_active(at(3))
    assert hours.next_start(at(3)) is None
    assert str(hours) == "always"


def test_simple_and_overnight_windows():
    hours = ActiveHours.parse("22:00-07:30, 13:00-14:00")
    assert hours.is_active(at(23, 15))
    assert hours.is_active(at(2))
    assert hours.is_active(at(7, 29))
    assert not hours.is_active(at(7, 30))
    assert hours.is_active(at(13, 30))
    assert not hours.is_active(at(18))
    assert hours.next_start(at(18)) == at(22)
    assert hours.next_start(at(8)) == at(13)
    assert str(hours) == "22:00-07:30, 13:00-14:00"


def test_full_day_window():
    assert ActiveHours.parse("00:00-24:00").is_active(at(12))


@pytest.mark.parametrize("text", ["7-9", "25:00-26:00", "10:00-10:00", "10:61-11:00", "abc"])
def test_invalid_windows(text):
    with pytest.raises(ValueError):
        ActiveHours.parse(text)


def test_settings_from_env_defaults():
    settings = Settings.from_env({})
    assert settings.device_backend == "adb"
    assert settings.game_package == GAME_PACKAGES["global"]
    assert settings.autostart == "resume"
    assert settings.templates_dir == Path("/data/templates")


def test_settings_from_env_values():
    settings = Settings.from_env(
        {
            "DEVICE_BACKEND": "sim",
            "GAME_PACKAGE": "jp",
            "AUTOSTART": "yes",
            "WEB_PORT": "9000",
            "MATCH_SCALE": "5",
            "DATA_DIR": "/tmp/farm",
            "NOTIFY_EVERY_RUNS": "25",
        }
    )
    assert settings.device_backend == "sim"
    assert settings.game_package == GAME_PACKAGES["jp"]
    assert settings.autostart == "true"
    assert settings.web_port == 9000
    assert settings.match_scale == 1.0  # clamped
    assert settings.templates_dir == Path("/tmp/farm/sim-templates")
    assert settings.notify_every_runs == 25
    assert "web_password" not in settings.public_dict()


def test_custom_package_and_bad_values():
    assert (
        Settings.from_env({"GAME_PACKAGE": "com.example.game"}).game_package == "com.example.game"
    )
    with pytest.raises(ValueError):
        Settings.from_env({"DEVICE_BACKEND": "usb"})
    with pytest.raises(ValueError):
        Settings.from_env({"WEB_PORT": "eighty"})
    with pytest.raises(ValueError):
        Settings.from_env({"AUTOSTART": "sometimes"})


def test_power_save_is_automatic_unless_set():
    assert Settings.from_env({}).power_save is None  # automatic: the emulator stack's emulator
    assert Settings.from_env({"POWER_SAVE": "auto"}).power_save is None
    assert Settings.from_env({"POWER_SAVE": "false"}).power_save is False
    on = Settings.from_env({"POWER_SAVE": "yes", "SCREEN_OFF_MINUTES": "0"})
    assert on.power_save is True and on.screen_off_minutes == 0
    assert Settings.from_env({}).screen_off_minutes == 10
    assert Settings.from_env({}).public_dict()["power_save"] == "auto"
