"""A small MCP server (Model Context Protocol, "Streamable HTTP" transport) for AI assistants.

It lets e.g. Claude look at the device and use the farmer: screenshots, taps, templates,
routines, the farm and its log. JSON-RPC over plain HTTP POSTs answered with JSON (no SSE
stream, no sessions): ``initialize``, ``ping``, ``tools/list`` and ``tools/call``. The tools
themselves are the web API's route functions, registered by :func:`..app.create_app`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from .. import __version__
from ..device import DeviceError
from ..routine import RoutineError

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
Controls the Dokkan Idle Farmer: an Android device (usually an emulator) running Dragon Ball
Z Dokkan Battle, and a bot that farms it with YAML routines and screenshot templates.
Screen coordinates are fractions: x and y from 0 to 1, (0, 0) is the top left corner.
Look with `screenshot`, act with `tap`/`swipe`/`key` (they return the screen afterwards).
Templates are crops of a screenshot that routines look for: crop them with
`create_template` from the frame_id of a screenshot; check them with `find_templates`.
Do not spend Dragon Stones or other paid currency unless the user asked for it."""


class ToolError(Exception):
    """A tool failed in a way the assistant should read (returned with isError)."""


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, Any]
    required: list[str]
    run: Callable[[dict[str, Any]], list[dict[str, Any]]]

    def spec(self) -> dict[str, Any]:
        schema = {"type": "object", "properties": self.params, "required": self.required}
        return {"name": self.name, "description": self.description, "inputSchema": schema}


def text(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)
    return {"type": "text", "text": value}


def image(data: str, mime: str = "image/jpeg") -> dict[str, Any]:
    return {"type": "image", "data": data, "mimeType": mime}


class McpServer:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def tool(
        self, description: str, params: dict[str, Any] | None = None, required: tuple = ()
    ) -> Callable[[Callable], Callable]:
        def register(fn: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> Callable:
            name = fn.__name__.removeprefix("mcp_")  # mcp_tap → the tool "tap"
            self.tools[name] = Tool(name, description, params or {}, list(required), fn)
            return fn

        return register

    def handle(self, message: Any) -> dict[str, Any] | None:
        """One JSON-RPC message → its response (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid request")
        msg_id, method = message.get("id"), message.get("method")
        if msg_id is None:  # a notification, e.g. notifications/initialized
            return None
        params = message.get("params") or {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dokkan-farmer", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": [t.spec() for t in self.tools.values()]})
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if tool is None:
                return _error(msg_id, -32602, f"Unknown tool: {params.get('name')}")
            return _result(msg_id, self._call(tool, params.get("arguments") or {}))
        return _error(msg_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _call(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"content": tool.run(args), "isError": False}
        except HTTPException as exc:
            message = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        except (ToolError, DeviceError, RoutineError, ValueError, RuntimeError) as exc:
            message = str(exc) or type(exc).__name__
        except Exception as exc:  # the assistant should see it, not a broken connection
            log.exception("MCP tool %s failed", tool.name)
            message = f"{type(exc).__name__}: {exc}"
        return {"content": [text(message)], "isError": True}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
