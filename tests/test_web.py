from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient

from dokkan_farmer.context import AppContext
from dokkan_farmer.device.sim import SimConfig, SimDevice, SimGame
from dokkan_farmer.settings import Settings
from dokkan_farmer.web import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", device_backend="sim")


@pytest.fixture
def client(settings):
    device = SimDevice(SimGame(SimConfig(battle_seconds=1.0)), package=settings.game_package)
    ctx = AppContext(settings, device=device)
    with TestClient(create_app(ctx)) as test_client:
        test_client.ctx = ctx
        yield test_client
    ctx.farmer.stop()


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_health_status_and_index(client):
    assert client.get("/api/health").json()["ok"] is True
    status = client.get("/api/status").json()
    assert status["settings"]["device_backend"] == "sim"
    assert status["farm"]["state"] == "stopped"
    assert status["selected_routine"] == "dokkan_auto_farm"
    assert wait_for(lambda: client.get("/api/status").json()["device"]["connected"])
    page = client.get("/")
    assert page.status_code == 200 and "Dokkan Farmer" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/manifest.webmanifest").json()["short_name"] == "Farmer"


def test_basic_auth(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", device_backend="sim", web_password="s3cret")
    ctx = AppContext(settings)
    with TestClient(create_app(ctx)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/static/icon-192.png").status_code == 200
        denied = client.get("/api/status")
        assert denied.status_code == 401
        assert "Basic" in denied.headers["www-authenticate"]
        assert client.get("/", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/status", auth=("admin", "s3cret")).status_code == 200
        bad = {"Authorization": "Basic " + base64.b64encode(b"nonsense").decode()}
        assert client.get("/api/status", headers=bad).status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_screen_capture_and_analyze(client):
    capture = client.post("/api/screen/capture").json()
    assert capture["width"] == 720 and capture["height"] == 1280
    jpeg = client.get(f"/api/screen.jpg?frame={capture['frame_id']}&w=360")
    assert jpeg.headers["content-type"] == "image/jpeg"
    assert jpeg.content[:2] == b"\xff\xd8"
    found = {r["name"] for r in client.get("/api/screen/analyze").json()["results"] if r["found"]}
    assert "attempt_again" in found  # the simulator starts on the results screen
    assert client.get("/api/screen.jpg?frame=99999").status_code == 404


def test_device_endpoints(client):
    client.post("/api/screen/capture")
    tap = client.post("/api/device/tap", json={"x": 0.5, "y": 0.79}).json()  # attempt again
    assert tap == {"x": 360, "y": 1011}
    assert client.ctx.device.game.screen == "act_confirm"
    assert client.post("/api/device/key", json={"key": "back"}).json()["ok"]
    assert client.ctx.device.game.screen == "results"
    assert client.post("/api/device/key", json={"key": "VOLUME_UP"}).status_code == 400
    assert client.post("/api/device/tap", json={"x": 2, "y": 0}).status_code == 422
    client.post("/api/device/app", json={"action": "stop"})
    assert not client.ctx.device.game.running
    client.post("/api/device/app", json={"action": "launch"})
    assert client.ctx.device.game.screen == "title"
    assert client.post(
        "/api/device/swipe", json={"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2}
    ).json()["ok"]
    assert client.post("/api/device/connect", json={"serial": "1.2.3.4:5555"}).status_code == 400
    assert (
        client.post("/api/device/pair", json={"address": "1.2.3.4:5", "code": "123456"}).status_code
        == 400
    )
    assert client.get("/api/device/devices").json()["devices"][0]["serial"] == "simulator"


def test_template_lifecycle(client):
    frame_id = client.post("/api/screen/capture?pin=true").json()["frame_id"]
    body = {"name": "Results_Header", "frame_id": frame_id, "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.09}
    created = client.post("/api/templates", json=body)
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["template"]["name"] == "results_header"
    assert data["score"] > 0.9
    assert client.post("/api/templates", json={**body, "name": "results_header"}).status_code == 409
    assert (
        client.post(
            "/api/templates", json={**body, "name": "results_header", "overwrite": True}
        ).status_code
        == 200
    )
    names = [t["name"] for t in client.get("/api/templates").json()]
    assert "results_header" in names and "attempt_again" in names
    png = client.get("/api/templates/results_header.png")
    assert png.content[:4] == b"\x89PNG"
    patched = client.patch(
        "/api/templates/results_header", json={"threshold": 0.7, "search_full": True}
    )
    assert patched.json()["threshold"] == 0.7 and patched.json()["region"] is None
    assert client.delete("/api/templates/results_header").json()["ok"]
    assert client.delete("/api/templates/results_header").status_code == 404
    assert client.get("/api/templates/nope.png").status_code == 404
    tiny = {**body, "name": "tiny", "w": 0.001, "h": 0.001}
    assert client.post("/api/templates", json=tiny).status_code == 400
    bad_name = {**body, "name": "bad name!"}
    assert client.post("/api/templates", json=bad_name).status_code == 400
    assert client.post("/api/templates", json={**body, "frame_id": 99999}).status_code == 409


def test_routine_endpoints(client):
    routines = client.get("/api/routines").json()
    assert [r["id"] for r in routines] == ["dokkan_auto_farm"]
    detail = client.get("/api/routines/dokkan_auto_farm").json()
    assert detail["valid"] and detail["ready"] and detail["bundled"]
    assert {o["key"] for o in detail["options"]} >= {"max_runs", "act_policy"}
    assert any(t["name"] == "attempt_again" and t["captured"] for t in detail["templates"])
    text = detail["yaml"]
    assert client.post("/api/routines/validate", json={"yaml": text}).json()["valid"]
    invalid = client.post("/api/routines/validate", json={"yaml": "name: x"}).json()
    assert not invalid["valid"] and "rules" in invalid["error"]
    assert client.put("/api/routines/mine", json={"yaml": "rules: nope"}).status_code == 400
    saved = client.put(
        "/api/routines/mine", json={"yaml": text.replace("name: Dokkan Auto Farm", "name: Mine")}
    )
    assert saved.status_code == 200 and saved.json()["name"] == "Mine"
    assert client.post("/api/routines/mine/restore").status_code == 404
    assert client.delete("/api/routines/mine").json()["ok"]
    assert client.get("/api/routines/mine").status_code == 404
    assert client.get("/api/routines/..%2Fetc").status_code in (400, 404)
    options = client.put(
        "/api/routines/dokkan_auto_farm/options", json={"options": {"max_runs": 3}}
    )
    assert options.json()["options"]["max_runs"] == 3
    bad = client.put("/api/routines/dokkan_auto_farm/options", json={"options": {"max_runs": -1}})
    assert bad.status_code == 400
    restored = client.post("/api/routines/dokkan_auto_farm/restore")
    assert restored.status_code == 200


def test_farm_start_and_stop(client):
    response = client.post(
        "/api/farm/start", json={"routine": "dokkan_auto_farm", "options": {"continue_after": 2}}
    )
    assert response.status_code == 200, response.text
    assert client.post("/api/farm/start", json={"routine": "dokkan_auto_farm"}).status_code == 409
    assert wait_for(lambda: client.get("/api/status").json()["farm"]["state"] == "running")
    assert wait_for(lambda: client.get("/api/status").json()["farm"].get("last_rule") is not None)
    farm = client.get("/api/status").json()["farm"]
    assert farm["routine_id"] == "dokkan_auto_farm" and farm["options"]["continue_after"] == 2
    assert client.post("/api/farm/pause").json()["state"] in ("running", "paused")
    client.post("/api/farm/resume")
    stopped = client.post("/api/farm/stop").json()
    assert stopped["state"] == "stopped"
    assert stopped["last_session"]["reason"] == "Stopped by user"
    stats = client.get("/api/stats").json()
    assert stats["lifetime"]["sessions"] == 1 and len(stats["sessions"]) == 1
    lines = client.get("/api/logs").json()["lines"]
    assert any("Farm started" in line["msg"] for line in lines)


def test_farm_start_errors(client):
    assert client.post("/api/farm/start", json={"routine": "missing"}).status_code == 404
    bad = client.post(
        "/api/farm/start", json={"routine": "dokkan_auto_farm", "options": {"act_policy": "yolo"}}
    )
    assert bad.status_code == 400 and "one of" in bad.json()["detail"]
    client.ctx.store.delete("attempt_again")
    missing = client.post("/api/farm/start", json={"routine": "dokkan_auto_farm"})
    assert missing.status_code == 400 and "attempt_again" in missing.json()["detail"]


def test_notify_test_without_urls(client):
    assert client.post("/api/notify/test").status_code == 400


def test_autostart_resumes_after_restart(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
    first = AppContext(settings)
    first.state.update(was_running=True, selected_routine="dokkan_auto_farm")
    second = AppContext(settings)
    with TestClient(create_app(second)) as client:
        assert wait_for(
            lambda: client.get("/api/status").json()["farm"]["state"] in ("starting", "running")
        )
    # Leaving the TestClient shuts the app down like `docker stop`: resume next time.
    assert second.state.get("was_running") is True


def test_cross_site_requests_are_blocked(client):
    url = "/api/farm/stop"
    assert client.post(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post(url, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.post(url, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post(url).status_code == 200  # curl & co. send neither header
    assert client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_device_errors_are_503_and_update_the_status(client):
    def connected() -> bool:
        return client.get("/api/status").json()["device"]["connected"]

    assert wait_for(connected)  # the startup check has finished
    client.ctx.device.reachable = False
    response = client.post("/api/screen/capture")
    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"]
    assert connected() is False
    client.ctx.device.reachable = True
    assert client.post("/api/screen/capture").status_code == 200
    assert connected() is True


def test_static_files_revalidate_and_manifest_sends_credentials(client):
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert 'rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"' in (
        client.get("/").text
    )
