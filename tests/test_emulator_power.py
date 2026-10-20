"""Emulator power control: the farmer's side of the supervisor in docker-compose.emulator.yml."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from dokkan_farmer import context as context_module
from dokkan_farmer.context import AppContext
from dokkan_farmer.device import EmulatorPower
from dokkan_farmer.device.sim import SimConfig, SimDevice, SimGame
from dokkan_farmer.settings import Settings
from dokkan_farmer.web import create_app


class FakeSupervisor:
    """What the emulator container's supervisor does with the two files."""

    def __init__(self, directory, state: str | None = "running") -> None:
        self.directory = directory
        self.commands: list[str] = []
        if state:
            (directory / "emulator.state").write_text(state + "\n")
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        ctl = self.directory / "emulator.ctl"
        while not self._done.wait(0.02):
            if not ctl.is_file():
                continue
            command = ctl.read_text().strip()
            ctl.unlink()
            self.commands.append(command)
            state = "stopped" if command == "stop" else "running"
            (self.directory / "emulator.state").write_text(state + "\n")

    def close(self) -> None:
        self._done.set()
        self._thread.join(timeout=2)


@pytest.fixture
def shared(tmp_path):
    folder = tmp_path / "screens"
    folder.mkdir()
    return folder


def make_ctx(tmp_path, shared, **overrides) -> AppContext:
    settings = Settings(
        data_dir=tmp_path / "data",
        device_backend="sim",
        emulator_console="android:5554",
        emulator_screenshots=str(shared),
        **overrides,
    )
    device = SimDevice(SimGame(SimConfig(battle_seconds=1.0)), package=settings.game_package)
    return AppContext(settings, device=device)


def test_power_files(shared):
    power = EmulatorPower(shared)
    assert not power.available and power.state() is None
    with pytest.raises(RuntimeError, match="power control"):
        power.send("stop")
    (shared / "emulator.state").write_text("running\n")
    assert power.available and power.state() == "running"
    with pytest.raises(ValueError):
        power.send("explode")
    power.send("restart")
    assert (shared / "emulator.ctl").read_text().strip() == "restart"
    (shared / "emulator.state").write_text("garbage\n")
    assert power.state() is None


def test_wait_for_resends_the_command(shared, monkeypatch):
    monkeypatch.setattr("dokkan_farmer.device.emulator_power.RESEND_SECONDS", 0.1)
    power = EmulatorPower(shared)
    (shared / "emulator.state").write_text("stopped\n")
    assert not power.wait_for("running", 0.05)

    def late_supervisor():
        time.sleep(0.3)
        (shared / "emulator.state").write_text("running\n")

    threading.Thread(target=late_supervisor, daemon=True).start()
    assert power.wait_for("running", 5, command="start")
    assert (shared / "emulator.ctl").read_text().strip() == "start"


def test_idle_emulator_is_shut_down(tmp_path, shared):
    supervisor = FakeSupervisor(shared)
    ctx = make_ctx(tmp_path, shared, emulator_off_minutes=30)
    try:
        ctx.check_emulator_idle()  # just started: not idle yet
        assert supervisor.commands == []
        ctx.farmer._last_use = ctx.farmer.clock() - 31 * 60
        ctx.check_emulator_idle()
        deadline = time.monotonic() + 2
        while not supervisor.commands and time.monotonic() < deadline:
            time.sleep(0.02)
        assert supervisor.commands == ["stop"]
        assert not ctx.device_status["connected"]
        assert "shut down" in ctx.device_status["error"]
        assert ctx.emulator_status()["state"] == "stopped"
    finally:
        supervisor.close()


def test_idle_shutdown_is_off_when_it_must_be(tmp_path, shared):
    supervisor = FakeSupervisor(shared)
    try:
        never = make_ctx(tmp_path, shared, emulator_off_minutes=0)
        never.farmer._last_use = never.farmer.clock() - 10**6
        never.check_emulator_idle()
        busy = make_ctx(tmp_path, shared, emulator_off_minutes=5)
        busy.farmer._last_use = busy.farmer.clock() - 10**6
        busy.farmer.start("r", busy.library.load("dokkan_auto_farm"), {})
        try:
            busy.check_emulator_idle()
        finally:
            busy.farmer.stop()
        time.sleep(0.2)
        assert supervisor.commands == []
    finally:
        supervisor.close()


def test_no_supervisor_means_no_shutdown(tmp_path, shared):
    ctx = make_ctx(tmp_path, shared, emulator_off_minutes=1)
    ctx.farmer._last_use = ctx.farmer.clock() - 10**6
    ctx.check_emulator_idle()
    assert not (shared / "emulator.ctl").exists()
    assert ctx.emulator_status() == {
        "available": False,
        "state": None,
        "off_minutes": 0,
        "busy": False,
    }
    with pytest.raises(RuntimeError, match="power control"):
        ctx.emulator_command("stop")
    ctx.ensure_emulator_on()  # nothing to start, nothing breaks


def test_farm_start_wakes_a_shut_down_emulator(tmp_path, shared, monkeypatch):
    monkeypatch.setattr(context_module, "EMULATOR_START_SECONDS", 5)
    supervisor = FakeSupervisor(shared, state="stopped")
    ctx = make_ctx(tmp_path, shared)
    try:
        ctx.start_farm("dokkan_auto_farm")
        assert supervisor.commands == ["start"]
        assert ctx.emulator_status()["state"] == "running"
    finally:
        ctx.farmer.stop()
        supervisor.close()


def test_buttons_through_the_api(tmp_path, shared):
    supervisor = FakeSupervisor(shared)
    ctx = make_ctx(tmp_path, shared)
    try:
        with TestClient(create_app(ctx)) as client:
            status = client.get("/api/status").json()
            assert status["emulator"]["state"] == "running"
            assert status["settings"]["emulator_off_minutes"] == 30
            assert client.post("/api/emulator", json={"action": "bogus"}).status_code == 422
            assert client.post("/api/emulator", json={"action": "stop"}).json()["available"]
            deadline = time.monotonic() + 2
            while client.get("/api/emulator").json()["state"] != "stopped":
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert client.post("/api/emulator", json={"action": "start"}).status_code == 200
            deadline = time.monotonic() + 2
            while client.get("/api/emulator").json()["state"] != "running":
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert supervisor.commands == ["stop", "start"]
            ctx.farmer.start("dokkan_auto_farm", ctx.library.load("dokkan_auto_farm"), {})
            try:
                busy = client.post("/api/emulator", json={"action": "restart"})
                assert busy.status_code == 409 and "farm" in busy.json()["detail"]
            finally:
                ctx.farmer.stop()
    finally:
        supervisor.close()


def test_api_without_supervisor_answers_409(tmp_path, shared):
    ctx = make_ctx(tmp_path, shared)
    with TestClient(create_app(ctx)) as client:
        assert client.get("/api/status").json()["emulator"]["available"] is False
        assert client.post("/api/emulator", json={"action": "stop"}).status_code == 409


def test_setting_from_env():
    assert Settings.from_env({}).emulator_off_minutes == 30
    assert Settings.from_env({"EMULATOR_OFF_MINUTES": "0"}).emulator_off_minutes == 0
    assert Settings.from_env({"EMULATOR_OFF_MINUTES": "-4"}).emulator_off_minutes == 0
