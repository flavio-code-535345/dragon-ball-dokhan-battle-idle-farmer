"""AdbDevice against a fake ``adb`` executable that records its arguments."""

from __future__ import annotations

import json
import os
import sys
import textwrap

import cv2
import numpy as np
import pytest

from dokkan_farmer.device.adb import AdbDevice, validate_serial
from dokkan_farmer.device.base import DeviceError

FAKE_ADB = r"""
import json, os, sys, time

args = sys.argv[1:]
with open(os.environ["FAKE_ADB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_ADB_MODE", "ok")
if args[:1] == ["-s"]:
    args = args[2:]
cmd = args[0] if args else ""
out = sys.stdout

if mode == "hang":
    time.sleep(5)
if cmd == "start-server":
    sys.exit(0)
if cmd == "connect":
    print("failed to connect to '%s': Connection refused" % args[1] if mode == "refuse"
          else "connected to %s" % args[1])
elif cmd == "get-state":
    if mode == "unauthorized":
        sys.stderr.write("error: device unauthorized.\n"); sys.exit(1)
    print("device")
elif cmd == "exec-out":
    with open(os.environ["FAKE_ADB_PNG"], "rb") as fh:
        sys.stdout.buffer.write(fh.read())
elif cmd == "pair":
    print("Successfully paired to %s [guid=adb-123]" % args[1])
elif cmd == "devices":
    print("List of devices attached")
    print("192.168.1.50:5555       device product:x model:Pixel_7 device:panther transport_id:1")
    print("emulator-5554           offline")
elif cmd == "shell":
    script = " ".join(args[1:])
    if script.startswith("monkey"):
        if "unknown.pkg" in script:
            print("** No activities found to run, monkey aborted."); sys.exit(251)
        print("Events injected: 1")
    elif "dumpsys" in script:
        print("  topResumedActivity=ActivityRecord{1a2b u0 com.bandainamcogames.dbzdokkanww/"
              "com.bandainamcogames.dbzdokkan.MainActivity t42}")
    elif "getprop" in script:
        print("Google\nPixel 7\n14\n34\narm64-v8a\nPhysical size: 1080x2400")
sys.exit(0)
"""


@pytest.fixture
def fake_adb(tmp_path, monkeypatch):
    script = tmp_path / "adb"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE_ADB))
    script.chmod(0o755)
    log = tmp_path / "adb.log"
    image = np.zeros((40, 30, 3), np.uint8)
    image[:, :, 2] = 200
    png = tmp_path / "screen.png"
    cv2.imwrite(str(png), image)
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    monkeypatch.setenv("FAKE_ADB_PNG", str(png))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return str(script), calls


def test_connect_screenshot_and_input(fake_adb):
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    device.connect()
    assert ["connect", "192.168.1.50:5555"] in calls()
    image = device.screenshot()
    assert image.shape == (40, 30, 3) and image[0, 0, 2] == 200
    device.tap(10, 20)
    device.swipe(1, 2, 3, 4, 250)
    device.key("back")
    log = calls()
    assert ["-s", "192.168.1.50:5555", "shell", "input", "tap", "10", "20"] in log
    assert ["-s", "192.168.1.50:5555", "shell", "input", "swipe", "1", "2", "3", "4", "250"] in log
    assert ["-s", "192.168.1.50:5555", "shell", "input", "keyevent", "KEYCODE_BACK"] in log


def test_apps_and_foreground(fake_adb):
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    device.launch_app("com.bandainamcogames.dbzdokkanww")
    with pytest.raises(DeviceError, match="not installed"):
        device.launch_app("unknown.pkg")
    with pytest.raises(ValueError):
        device.launch_app("not a package; rm -rf /")
    device.stop_app("com.bandainamcogames.dbzdokkanww")
    assert [
        "-s",
        "192.168.1.50:5555",
        "shell",
        "am",
        "force-stop",
        "com.bandainamcogames.dbzdokkanww",
    ] in calls()
    assert device.foreground_package() == "com.bandainamcogames.dbzdokkanww"


def test_info_devices_pair(fake_adb):
    path, _ = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    info = device.info()
    assert info["model"] == "Pixel 7" and info["android"] == "14"
    assert info["abi"] == "arm64-v8a" and info["resolution"] == "1080x2400"
    devices = device.devices()
    assert devices[0]["serial"] == "192.168.1.50:5555" and devices[0]["model"] == "Pixel_7"
    assert devices[1]["state"] == "offline"
    assert "Successfully paired" in device.pair("192.168.1.50:37000", "123456")
    with pytest.raises(ValueError):
        device.pair("192.168.1.50", "123456")
    with pytest.raises(ValueError):
        device.pair("192.168.1.50:37000", "12ab56")


def test_connection_failures(fake_adb, monkeypatch):
    path, _ = fake_adb
    monkeypatch.setenv("FAKE_ADB_MODE", "refuse")
    with pytest.raises(DeviceError, match="Connection refused"):
        AdbDevice("192.168.1.50:5555", adb_path=path).connect()
    monkeypatch.setenv("FAKE_ADB_MODE", "unauthorized")
    with pytest.raises(DeviceError, match="Allow USB debugging"):
        AdbDevice("SERIAL123", adb_path=path).connect()


def test_timeout_and_missing_binary(fake_adb, monkeypatch):
    path, _ = fake_adb
    monkeypatch.setenv("FAKE_ADB_MODE", "hang")
    with pytest.raises(DeviceError, match="timed out"):
        AdbDevice("x:1", adb_path=path, timeout=0.5).screenshot()
    with pytest.raises(DeviceError, match="not found"):
        AdbDevice("x:1", adb_path=os.devnull + "/nope").tap(1, 1)


@pytest.mark.parametrize("serial", ["-o rm", "a b", "", "x" * 200])
def test_invalid_serials(serial):
    with pytest.raises(ValueError):
        validate_serial(serial)
