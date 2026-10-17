"""Runtime configuration, read from environment variables (Portainer-friendly)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Android package names of the two Dokkan Battle releases.
GAME_PACKAGES = {
    "global": "com.bandainamcogames.dbzdokkanww",
    "jp": "com.bandainamcogames.dbzdokkan",
}

AUTOSTART_MODES = ("false", "true", "resume")


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, got {value!r}") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def resolve_game_package(value: str) -> str:
    """Accept ``global``/``jp`` shortcuts or a full Android package name."""
    key = value.strip().lower()
    return GAME_PACKAGES.get(key, value.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    device_backend: str = "adb"
    adb_serial: str = ""
    adb_path: str = "adb"
    #: host:port of the Android emulator's console (emulator stack): fast screenshots
    emulator_console: str = ""
    #: folder shared with the emulator for those screenshots (same path in both containers)
    emulator_screenshots: str = "/screens"
    #: host:port of the emulator's gRPC port (emulator stack): live view at the game's frame rate
    emulator_grpc: str = ""
    game_package: str = GAME_PACKAGES["global"]
    routine: str = "dokkan_auto_farm"
    autostart: str = "resume"
    active_hours: str = ""
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_username: str = "admin"
    web_password: str = ""
    #: secret of the MCP endpoint /mcp/<token> for AI assistants; empty = no MCP endpoint
    mcp_token: str = ""
    notify_urls: str = ""
    notify_every_runs: int = 0
    match_scale: float = 0.5
    tap_jitter: float = 0.15
    keep_screen_on: bool = True
    #: turn Android's screen off while nothing happens (long waits, idle): the emulator then
    #: stops drawing the game, its biggest power draw. None = automatic: only while the device
    #: is the emulator stack's emulator (a phone may have a lock screen with a PIN).
    power_save: bool | None = None
    #: with power_save: screen off after this many minutes without a farm and without anyone
    #: looking at or touching the device in the web UI (0 = only during the farm's waits)
    screen_off_minutes: int = 10
    log_level: str = "INFO"
    screenshot_keep: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        backend = _text(env, "DEVICE_BACKEND", "adb").lower()
        if backend not in {"adb", "sim"}:
            raise ValueError(f"DEVICE_BACKEND must be 'adb' or 'sim', got {backend!r}")
        autostart = _text(env, "AUTOSTART", "resume").lower()
        if autostart in {"1", "yes", "on"}:
            autostart = "true"
        elif autostart in {"0", "no", "off"}:
            autostart = "false"
        if autostart not in AUTOSTART_MODES:
            raise ValueError(f"AUTOSTART must be one of {', '.join(AUTOSTART_MODES)}")
        power_save = _text(env, "POWER_SAVE", "auto").lower()
        mcp_token = (env.get("MCP_TOKEN", "") or "").strip()
        if mcp_token and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", mcp_token):
            raise ValueError("MCP_TOKEN must be 16-128 letters, digits, '-' or '_'")
        return cls(
            data_dir=Path(_text(env, "DATA_DIR", "/data")),
            device_backend=backend,
            adb_serial=_text(env, "ADB_SERIAL", ""),
            adb_path=_text(env, "ADB_PATH", "adb"),
            emulator_console=_text(env, "EMULATOR_CONSOLE", ""),
            emulator_screenshots=_text(env, "EMULATOR_SCREENSHOTS", "/screens"),
            emulator_grpc=_text(env, "EMULATOR_GRPC", ""),
            game_package=resolve_game_package(_text(env, "GAME_PACKAGE", "global")),
            routine=_text(env, "ROUTINE", "dokkan_auto_farm"),
            autostart=autostart,
            active_hours=_text(env, "ACTIVE_HOURS", ""),
            web_host=_text(env, "WEB_HOST", "0.0.0.0"),
            web_port=_int(env, "WEB_PORT", 8080),
            web_username=_text(env, "WEB_USERNAME", "admin"),
            web_password=env.get("WEB_PASSWORD", "") or "",
            mcp_token=mcp_token,
            notify_urls=_text(env, "NOTIFY_URLS", ""),
            notify_every_runs=max(0, _int(env, "NOTIFY_EVERY_RUNS", 0)),
            match_scale=min(1.0, max(0.2, _float(env, "MATCH_SCALE", 0.5))),
            tap_jitter=min(0.45, max(0.0, _float(env, "TAP_JITTER", 0.15))),
            keep_screen_on=_bool(env, "KEEP_SCREEN_ON", True),
            power_save=None if power_save == "auto" else _bool(env, "POWER_SAVE", False),
            screen_off_minutes=max(0, _int(env, "SCREEN_OFF_MINUTES", 10)),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
            screenshot_keep=max(0, _int(env, "SCREENSHOT_KEEP", 30)),
        )

    @property
    def templates_dir(self) -> Path:
        # The simulator generates its own templates; keep them away from real ones.
        return self.data_dir / ("sim-templates" if self.device_backend == "sim" else "templates")

    @property
    def routines_dir(self) -> Path:
        return self.data_dir / "routines"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def state_file(self) -> Path:
        return self.data_dir / ("state-sim.json" if self.device_backend == "sim" else "state.json")

    def public_dict(self) -> dict[str, object]:
        """Settings that are safe to show in the web UI (no secrets)."""
        return {
            "device_backend": self.device_backend,
            "adb_serial": self.adb_serial,
            "emulator_console": self.emulator_console,
            "emulator_grpc": self.emulator_grpc,
            "game_package": self.game_package,
            "routine": self.routine,
            "autostart": self.autostart,
            "active_hours": self.active_hours,
            "notify_enabled": bool(self.notify_urls),
            "notify_every_runs": self.notify_every_runs,
            "match_scale": self.match_scale,
            "tap_jitter": self.tap_jitter,
            "keep_screen_on": self.keep_screen_on,
            "power_save": "auto" if self.power_save is None else self.power_save,
            "screen_off_minutes": self.screen_off_minutes,
            "auth_enabled": bool(self.web_password),
            "mcp_enabled": bool(self.mcp_token),
            "data_dir": str(self.data_dir),
        }
