"""The MCP endpoint for AI assistants, against the simulator."""

from __future__ import annotations

import base64
import threading

import pytest
from fastapi.testclient import TestClient

from dokkan_farmer.context import AppContext
from dokkan_farmer.device.sim import SimConfig, SimDevice, SimGame
from dokkan_farmer.settings import Settings
from dokkan_farmer.web import create_app

TOKEN = "test-token-0123456789"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", device_backend="sim", mcp_token=TOKEN, web_password="pw"
    )
    device = SimDevice(SimGame(SimConfig(battle_seconds=1.0)), package=settings.game_package)
    ctx = AppContext(settings, device=device)
    with TestClient(create_app(ctx)) as test_client:
        for thread in threading.enumerate():  # the device connects at startup
            if thread.name == "startup":
                thread.join(10)
        yield test_client
    ctx.farmer.stop()


def rpc(client, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    response = client.post(f"/mcp/{TOKEN}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def call(client, tool, **arguments):
    result = rpc(client, "tools/call", {"name": tool, "arguments": arguments})["result"]
    return result["content"], result["isError"]


def test_handshake_and_tool_list(client):
    init = rpc(client, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"]["tools"] is not None
    assert "fractions" in init["instructions"]
    note = client.post(
        f"/mcp/{TOKEN}", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert note.status_code == 202
    names = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
    assert {"screenshot", "tap", "create_template", "find_templates", "start_farm"} <= names
    assert rpc(client, "nope")["error"]["code"] == -32601
    # Basic auth does not apply, the token does.
    assert client.post("/mcp/wrong-token-0123456789", json={}).status_code == 404
    assert client.get("/api/status").status_code == 401
    assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_look_tap_and_crop_a_template(client):
    content, error = call(client, "screenshot", width=300)
    assert not error and content[1]["type"] == "image"
    assert base64.b64decode(content[1]["data"])[:2] == b"\xff\xd8"  # a JPEG
    frame_id = int(content[0]["text"].split()[1].rstrip(","))
    content, error = call(
        client, "create_template", name="mcp_title", frame_id=frame_id, x=0, y=0, w=1, h=0.09
    )
    assert not error and '"name": "mcp_title"' in content[0]["text"]
    content, _ = call(client, "find_templates")
    assert '"mcp_title"' in content[0]["text"]
    content, error = call(client, "tap", x=0.5, y=0.5, wait=0)
    assert not error and content[0]["text"].startswith("Tapped") and content[-1]["type"] == "image"
    content, error = call(client, "tap", x=2, y=0.5)
    assert error  # validation errors come back as tool errors
    content, error = call(client, "template_image", name="mcp_title")
    assert not error and content[0]["mimeType"] == "image/png"
    content, error = call(client, "delete_template", name="missing")
    assert error and "No template" in content[0]["text"]


def test_routines_farm_and_logs(client):
    content, _ = call(client, "list_routines")
    assert "dokkan_collect_rewards" in content[0]["text"]
    content, _ = call(client, "get_routine", id="dokkan_auto_farm")
    assert content[0]["text"].startswith("# Dokkan Auto Farm")
    content, error = call(client, "start_farm", routine="dokkan_auto_farm")
    assert not error and '"Starting Dokkan Auto Farm"' in content[0]["text"]
    content, error = call(client, "stop_farm")
    assert not error
    content, _ = call(client, "logs", lines=5)
    assert content[0]["text"]


def test_token_setting():
    assert Settings.from_env({"MCP_TOKEN": TOKEN}).mcp_token == TOKEN
    assert Settings.from_env({}).public_dict()["mcp_enabled"] is False
    with pytest.raises(ValueError, match="MCP_TOKEN"):
        Settings.from_env({"MCP_TOKEN": "short"})
