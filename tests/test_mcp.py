"""The MCP endpoint for AI assistants, against the simulator."""

from __future__ import annotations

import base64
import json
import logging
import threading

import cv2
import numpy as np
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
        test_client.app_ctx = ctx
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
    content, error = call(client, "screenshot")
    assert not error and content[1]["type"] == "image"
    jpeg = base64.b64decode(content[1]["data"])
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG, 360 px wide by default
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[1] == 360
    frame_id = int(content[0]["text"].split()[1].rstrip(","))
    content, error = call(
        client, "create_template", name="mcp_title", frame_id=frame_id, x=0, y=0, w=1, h=0.09
    )
    assert not error and content[0]["text"].startswith("Saved mcp_title: score ")
    found = json.loads(call(client, "find_templates")[0][0]["text"])
    assert "mcp_title" in [row[0] for row in found["found"]]
    assert len(found["near"]) <= 3 and all(row[1] >= 0.5 for row in found["near"])
    names = client.app_ctx.store.names()
    assert len(found["found"]) + len(found["near"]) < len(names)  # the rest is left out
    picked = json.loads(
        call(client, "find_templates", names=["mcp_title", "attempt_again", "nope"])[0][0]["text"]
    )
    assert {row[0] for row in picked["templates"]} == {"mcp_title", "attempt_again"}
    assert picked["unknown"] == ["nope"]
    content, error = call(client, "tap", x=0.5, y=0.5, wait=0)
    assert not error and content[0]["text"].startswith("Tapped") and content[-1]["type"] == "image"
    content, error = call(client, "tap", x=2, y=0.5)
    assert error  # validation errors come back as tool errors
    content, error = call(client, "template_image", name="mcp_title")
    assert not error and content[0]["mimeType"] == "image/png"
    content, _ = call(client, "update_template", name="mcp_title", mode="color", threshold=0.7)
    assert content[0]["text"] == "mcp_title: 0.7 color near tap=None"
    lines = call(client, "list_templates")[0][0]["text"].splitlines()
    assert "mcp_title 0.7 color near" in lines
    assert call(client, "delete_template", name="mcp_title")[0][0]["text"] == "Deleted mcp_title"
    content, error = call(client, "delete_template", name="missing")
    assert error and "No template" in content[0]["text"]


def test_routines_in_short_and_edits(client):
    rows = call(client, "list_routines")[0][0]["text"].splitlines()
    assert "dokkan_collect_rewards: Dokkan Collect Rewards (bundled)" in rows
    brief = call(client, "get_routine", id="dokkan_auto_farm")
    assert len(brief[0]) == 1  # no YAML unless asked
    text = brief[0][0]["text"]
    assert text.startswith("Dokkan Auto Farm (dokkan_auto_farm): valid, ") and "max_runs=0" in text
    content, _ = call(client, "get_routine", id="dokkan_auto_farm", yaml=True)
    assert content[1]["text"].startswith("# Dokkan Auto Farm")
    edit = {"old": "    default: 6\n", "new": "    default: 7\n"}
    content, error = call(client, "save_routine", id="dokkan_auto_farm", edits=[edit])
    assert not error and content[0]["text"].startswith("Saved dokkan_auto_farm: valid, ")
    assert "\n" not in content[0]["text"]
    assert "continue_after=7" in call(client, "get_routine", id="dokkan_auto_farm")[0][0]["text"]
    content, error = call(client, "save_routine", id="dokkan_auto_farm", edits=[edit])
    assert error and "found 0 times" in content[0]["text"]
    # Some clients send lists as JSON text.
    back = {"old": "    default: 7\n", "new": "    default: 6\n"}
    content, error = call(client, "save_routine", id="dokkan_auto_farm", edits=json.dumps([back]))
    assert not error, content
    content, error = call(client, "save_routine", id="dokkan_auto_farm", edits="[oops")
    assert error and "edits must be a JSON array" in content[0]["text"]
    content, error = call(client, "save_routine", id="dokkan_auto_farm")
    assert error and "yaml or edits" in content[0]["text"]
    bad = {"old": "rules:", "new": "rulez:"}
    content, error = call(client, "save_routine", id="dokkan_auto_farm", edits=[bad])
    assert error
    yaml = "name: Mine\nrules:\n  - name: Wait\n    when: {always: true}\n    do: [{wait: 1}]\n"
    content, error = call(client, "save_routine", id="mine", yaml=yaml)
    assert content[0]["text"] == "Saved mine: valid, 1 rules"


def test_farm_status_and_logs(client):
    content, error = call(client, "start_farm", routine="dokkan_auto_farm")
    line = content[0]["text"]
    assert not error and "\n" not in line
    assert line.startswith(("starting: Dokkan Auto Farm (dokkan_auto_farm)", "running: "))
    status = json.loads(call(client, "farm_status")[0][0]["text"])
    assert status["device"] == "connected" and "settings" not in status
    assert set(status) >= {"farm", "counters", "selected_routine"}
    content, error = call(client, "stop_farm")
    assert not error and content[0]["text"].startswith("stopped")
    first = call(client, "logs")[0][0]["text"].splitlines()
    assert len(first) <= 21 and first[-1].startswith("(last_id ")
    last_id = int(first[-1].split()[1].rstrip(")"))
    again = call(client, "logs", after=last_id)[0][0]["text"]
    assert again == f"(no new lines, last_id {last_id})"
    logging.getLogger("dokkan_farmer.test").warning("a new line")
    newer = call(client, "logs", after=last_id)[0][0]["text"].splitlines()
    assert newer[0].endswith("WARNING a new line") and len(newer) == 2


def test_token_setting():
    assert Settings.from_env({"MCP_TOKEN": TOKEN}).mcp_token == TOKEN
    assert Settings.from_env({}).public_dict()["mcp_enabled"] is False
    with pytest.raises(ValueError, match="MCP_TOKEN"):
        Settings.from_env({"MCP_TOKEN": "short"})
