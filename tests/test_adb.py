"""AdbDevice against a fake ``adb`` executable that records its arguments."""

from __future__ import annotations

import json
import os
import struct
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
    raw = args[1:] == ["screencap"] and os.environ.get("FAKE_ADB_RAW")
    with open(raw or os.environ["FAKE_ADB_PNG"], "rb") as fh:
        sys.stdout.buffer.write(fh.read())
elif cmd == "pair":
    print("Successfully paired to %s [guid=adb-123]" % args[1])
elif cmd in ("install", "install-multiple"):
    print("Performing Streamed Install")
    if mode == "install_fail":
        print("adb: failed to install x.apk: Failure [INSTALL_FAILED_NO_MATCHING_ABIS: "
              "Failed to extract native libraries, res=-113]")
        sys.exit(1)
    print("Success")
elif cmd == "uninstall":
    if mode == "uninstall_fail":
        print("Failure [DELETE_FAILED_INTERNAL_ERROR]"); sys.exit(1)
    print("Success")
elif cmd == "push":
    print("%s: 1 file pushed, 0 skipped." % args[1])
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
    elif script.startswith("dumpsys package"):
        if "dbzdokkanww" in script:
            print("    primaryCpuAbi=arm64-v8a")
        else:
            print("    primaryCpuAbi=null")
    elif "dumpsys" in script:
        print("  topResumedActivity=ActivityRecord{1a2b u0 com.bandainamcogames.dbzdokkanww/"
              "com.bandainamcogames.dbzdokkan.MainActivity t42}")
    elif "lib64/arm64" in script:
        if mode == "redroid":
            print("x86_64,arm64-v8a,x86,armeabi-v7a,armeabi\nlibnb.so\narm64-libs")
        elif mode == "emulator-odd":
            print("x86_64,x86,arm64-v8a,armeabi-v7a\nlibhoudini.so")
        else:
            print("arm64-v8a,armeabi-v7a,armeabi\n0")
    elif script == "getprop sys.boot_completed":
        print("" if mode == "booting" else "1")
    elif "getprop" in script:
        if mode == "redroid":
            print("redroid\nredroid14_x86_64\n14\n34\nx86_64\n"
                  "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi\nlibnb.so\nPhysical size: 720x1280")
        else:
            print("Google\nPixel 7\n14\n34\narm64-v8a\narm64-v8a,armeabi-v7a,armeabi\n\n"
                  "Physical size: 1080x2400\nOverride size: 720x1600")
    elif script.startswith("pm path"):
        if "dbzdokkanww" in script:
            print("package:/data/app/~~x==/com.bandainamcogames.dbzdokkanww-y==/base.apk")
        else:
            sys.exit(1)
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
    launch = next(c for c in calls() if "monkey" in c)
    assert launch[3:7] == ["monkey", "--pct-syskeys", "0", "-p"]
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
    assert info["abi"] == "arm64-v8a" and info["resolution"] == "720x1600"
    assert info["abilist"] == "arm64-v8a,armeabi-v7a,armeabi" and info["native_bridge"] == ""
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
    monkeypatch.setenv("FAKE_ADB_MODE", "booting")
    with pytest.raises(DeviceError, match="still starting Android"):
        AdbDevice("redroid:5555", adb_path=path).connect()


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


def test_info_redroid_reports_arm_translation(fake_adb, monkeypatch):
    path, _ = fake_adb
    monkeypatch.setenv("FAKE_ADB_MODE", "redroid")
    info = AdbDevice("redroid:5555", adb_path=path).info()
    assert info["abi"] == "x86_64" and "arm64-v8a" in info["abilist"]
    assert info["native_bridge"] == "libnb.so" and info["resolution"] == "720x1280"


def test_type_text(fake_adb):
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    device.text("goku@example.com")
    device.text("p4ss w'rd;$(x)")
    device.text("")
    shell_texts = [c[5:] for c in calls() if c[2:5] == ["shell", "input", "text"]]
    # Spaces become %s for `input text`; everything is quoted for the device shell.
    assert shell_texts == [["goku@example.com"], ["'p4ss%sw'\"'\"'rd;$(x)'"]]
    for bad in ("über", "line\nbreak", "x" * 501):
        with pytest.raises(ValueError):
            device.text(bad)


def test_a_typed_percent_s_is_not_a_space(fake_adb):
    # `input text` turns any "%s" into a space: a real one is typed as "...%" + "s...".
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    device.text("Ab%s9 x!")
    device.text("%s")
    shells = [c[3:] for c in calls() if c[2:3] == ["shell"]]
    assert shells == [
        ["input text Ab% && input text 's9%sx!'"],
        ["input text % && input text s"],
    ]


def test_held_keys_are_pressed_in_one_command(fake_adb):
    path, calls = fake_adb
    AdbDevice("192.168.1.50:5555", adb_path=path).press("DEL", 3)
    assert calls()[-1][2:] == ["shell", "input", "keyevent", *["KEYCODE_DEL"] * 3]


def test_uninstall(fake_adb, monkeypatch):
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    device.uninstall("com.bandainamcogames.dbzdokkanww")
    assert calls()[-1][2:] == ["uninstall", "com.bandainamcogames.dbzdokkanww"]
    monkeypatch.setenv("FAKE_ADB_MODE", "uninstall_fail")
    with pytest.raises(DeviceError, match="DELETE_FAILED_INTERNAL_ERROR"):
        device.uninstall("com.bandainamcogames.dbzdokkanww")
    with pytest.raises(ValueError):
        device.uninstall("not a package")


def test_install_push_and_is_installed(fake_adb, monkeypatch, tmp_path):
    path, calls = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    base, split = tmp_path / "base.apk", tmp_path / "split_config.arm64_v8a.apk"
    assert "Success" in device.install([base])
    assert "Success" in device.install([base, split])
    log = calls()
    assert ["-s", "192.168.1.50:5555", "install", "-r", "-g", str(base)] in log
    assert ["-s", "192.168.1.50:5555", "install-multiple", "-r", "-g", str(base), str(split)] in log
    with pytest.raises(ValueError):
        device.install([])
    device.push(tmp_path / "main.obb", "/sdcard/Android/obb/x/main.obb")
    assert [
        "-s",
        "192.168.1.50:5555",
        "push",
        str(tmp_path / "main.obb"),
        "/sdcard/Android/obb/x/main.obb",
    ] in calls()
    assert device.is_installed("com.bandainamcogames.dbzdokkanww") is True
    assert device.is_installed("com.aurora.store") is False
    with pytest.raises(ValueError):
        device.is_installed("bad package")
    monkeypatch.setenv("FAKE_ADB_MODE", "install_fail")
    with pytest.raises(DeviceError, match="INSTALL_FAILED_NO_MATCHING_ABIS"):
        device.install([base])


def test_runnable_abis_and_app_abi(fake_adb, monkeypatch):
    path, _ = fake_adb
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    assert device.runnable_abis() == ["arm64-v8a", "armeabi-v7a", "armeabi"]  # a phone
    assert device.app_abi("com.bandainamcogames.dbzdokkanww") == "arm64-v8a"
    assert device.app_abi("com.aurora.store") is None
    monkeypatch.setenv("FAKE_ADB_MODE", "redroid")
    # redroid 14 lists 32-bit ARM, but its translator only covers 64-bit ARM.
    assert device.runnable_abis() == ["x86_64", "x86", "arm64-v8a"]
    monkeypatch.setenv("FAKE_ADB_MODE", "emulator-odd")
    assert device.runnable_abis() is None


def raw_screencap(path, header_size: int = 16, fmt: int = 1):
    """What `adb exec-out screencap` prints: width, height, format[, colour space], RGBA."""
    rgba = np.zeros((40, 30, 4), np.uint8)
    rgba[:, :, 0] = 200  # red
    rgba[:, :, 3] = 255
    header = struct.pack("<III", 30, 40, fmt) + b"\0" * (header_size - 12)
    path.write_bytes(header + rgba.tobytes())


@pytest.mark.parametrize("header_size", [12, 16])
def test_raw_screenshots_skip_png(fake_adb, monkeypatch, tmp_path, header_size):
    path, calls = fake_adb
    raw = tmp_path / "screen.raw"
    raw_screencap(raw, header_size)
    monkeypatch.setenv("FAKE_ADB_RAW", str(raw))
    device = AdbDevice("redroid:5555", adb_path=path)
    for _ in range(2):
        image = device.screenshot()
        assert image.shape == (40, 30, 3) and tuple(image[0, 0]) == (0, 0, 200)  # BGR
    assert device.capture_method == "raw"
    assert ["-s", "redroid:5555", "exec-out", "screencap", "-p"] not in calls()


def test_unreadable_raw_output_falls_back_to_png(fake_adb, monkeypatch, tmp_path):
    path, calls = fake_adb
    raw = tmp_path / "screen.raw"
    raw_screencap(raw, fmt=4)  # RGB_565: not handled
    monkeypatch.setenv("FAKE_ADB_RAW", str(raw))
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    assert device.screenshot()[0, 0, 2] == 200  # from the PNG
    assert device.capture_method == "png"
    device.screenshot()
    raw_calls = [c for c in calls() if c[2:] == ["exec-out", "screencap"]]
    assert len(raw_calls) == 1  # not retried
    device.set_serial("192.168.1.51:5555")
    assert device.capture_method is None


def test_slow_raw_screenshots_are_compared_with_png(fake_adb, monkeypatch, tmp_path):
    from dokkan_farmer.device import adb

    path, calls = fake_adb
    raw = tmp_path / "screen.raw"
    raw_screencap(raw)
    monkeypatch.setenv("FAKE_ADB_RAW", str(raw))
    monkeypatch.setattr(adb, "RAW_FAST_ENOUGH", 0.0)
    device = AdbDevice("192.168.1.50:5555", adb_path=path)
    assert device.screenshot().shape == (40, 30, 3)
    assert device.capture_method in ("raw", "png")
    assert ["-s", "192.168.1.50:5555", "exec-out", "screencap", "-p"] in calls()
