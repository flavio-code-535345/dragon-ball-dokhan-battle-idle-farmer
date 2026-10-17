"""REST API + static single-page UI."""

from __future__ import annotations

import base64
import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..context import AppContext
from ..device import AdbDevice, DeviceError, normalize_key
from ..device.adb import validate_package
from ..engine import WAKE_SETTLE_UI
from ..installer import MAX_DOWNLOAD, InstallError
from ..library import validate_routine_id
from ..live import BOUNDARY, mjpeg_part, open_feed
from ..logging_setup import LOG_BUFFER, ensure_log_buffer
from ..routine import Routine, RoutineError, parse_routine
from ..vision import encode_png
from .mcp import McpServer, ToolError
from .mcp import image as mcp_image
from .mcp import text as mcp_text

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
#: /mcp/<token> checks its own secret: MCP clients cannot do Basic auth. They also probe
#: /.well-known/ for OAuth: a plain 404 there, not a Basic auth 401, says "no login needed".
PUBLIC_PATHS = ("/api/health", "/manifest.webmanifest", "/static/icon", "/mcp/", "/.well-known/")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SameOriginMiddleware:
    """Reject state-changing requests sent by other web sites (CSRF).

    Browsers label every request with ``Sec-Fetch-Site`` (or at least ``Origin``);
    non-browser clients such as curl send neither and are allowed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            if not self._same_origin(headers):
                response = JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _same_origin(headers: dict[str, str]) -> bool:
        fetch_site = headers.get("sec-fetch-site")
        if fetch_site is not None:
            return fetch_site in ("same-origin", "none")
        origin = headers.get("origin")
        if origin is None:
            return True
        hosts = {headers.get("host"), headers.get("x-forwarded-host")}
        return urlsplit(origin).netloc in hosts - {None}


class BasicAuthMiddleware:
    """HTTP Basic auth for everything except health checks and app icons."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(PUBLIC_PATHS) or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Dokkan Farmer", charset="UTF-8"'},
        )
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        for key, value in scope.get("headers", []):
            if key != b"authorization":
                continue
            scheme, _, encoded = value.decode("latin-1").partition(" ")
            if scheme.lower() != "basic":
                return False
            try:
                user, _, password = base64.b64decode(encoded, validate=True).partition(b":")
            except ValueError:
                return False
            user_ok = hmac.compare_digest(user, self.username)
            password_ok = hmac.compare_digest(password, self.password)
            return user_ok and password_ok
        return False


# ------------------------------------------------------------------ requests
class StartRequest(BaseModel):
    routine: str
    options: dict[str, Any] = {}


class OptionsRequest(BaseModel):
    options: dict[str, Any]


class TapRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    #: 2 = a double tap (a double-click in the web UI)
    count: int = Field(1, ge=1, le=2)


class SwipeRequest(BaseModel):
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)
    ms: int = Field(300, ge=1, le=5000)


class KeyRequest(BaseModel):
    key: str
    #: a key held down in the web UI: its repeats, sent together
    count: int = Field(1, ge=1, le=50)


class AppRequest(BaseModel):
    action: Literal["launch", "stop", "restart"]
    #: Another app than the game, e.g. "com.aurora.store" to open Aurora Store.
    package: str | None = None


class ScreenPowerRequest(BaseModel):
    on: bool


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class InstallRequest(BaseModel):
    #: An http(s) link to an .apk / .xapk / .apks file, "fdroid:<package>" or
    #: "apkpure:<package>[@abi]".
    source: str = Field(min_length=3, max_length=2000)


class ConnectRequest(BaseModel):
    serial: str


class PairRequest(BaseModel):
    address: str
    code: str


class TemplateCreate(BaseModel):
    name: str
    frame_id: int | None = None
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    threshold: float = Field(0.85, ge=0.5, le=0.99)
    mode: Literal["gray", "color"] = "gray"
    search_full: bool = False
    description: str = ""
    overwrite: bool = False
    #: Where to tap when a routine taps this template (screen fractions, like x/y); empty =
    #: the middle of the box. Stored relative to the box, so it moves with the template.
    tap_x: float | None = Field(None, ge=0, le=1)
    tap_y: float | None = Field(None, ge=0, le=1)


class TemplateUpdate(BaseModel):
    threshold: float | None = Field(None, ge=0.5, le=0.99)
    mode: Literal["gray", "color"] | None = None
    search_full: bool | None = None
    description: str | None = None
    #: True: tap the template's centre again (forget its tap point)
    reset_tap: bool = False


class RoutineText(BaseModel):
    yaml: str = Field(max_length=200_000)


# ------------------------------------------------------------------- helpers
def _routine_summary(ctx: AppContext, routine_id: str, routine: Routine) -> dict[str, Any]:
    store = ctx.store
    current = routine.option_values(routine.sanitize_options(ctx.state.routine_options(routine_id)))
    options = [
        {"key": key, **spec.model_dump(), "value": current.get(key, spec.default)}
        for key, spec in routine.options.items()
    ]
    templates = [
        {
            "name": name,
            "level": doc.level,
            "description": doc.description,
            "captured": store.has(name),
        }
        for name, doc in routine.template_checklist().items()
    ]
    rules = []
    for rule in routine.rules:
        missing = sorted(
            n for n in (set(rule.when.visible) | rule.tap_templates()) if not store.has(n)
        )
        rules.append(
            {
                "name": rule.name,
                "enabled": rule.enabled,
                "templates": sorted(rule.templates()),
                "missing": missing,
                "actions": [a.describe() for a in rule.do],
            }
        )
    required = [t for t in templates if t["level"] == "required"]
    return {
        "id": routine_id,
        "name": routine.name,
        "description": routine.description,
        "options": options,
        "templates": templates,
        "rules": rules,
        "ready": all(t["captured"] for t in required),
        "required_missing": [t["name"] for t in required if not t["captured"]],
    }


def _frame_or_404(ctx: AppContext, frame_id: int | None) -> tuple[int, Any]:
    entry = ctx.farmer.frames.get(frame_id)
    if entry is None:
        raise HTTPException(409, "That screenshot is no longer available, capture a new one")
    return entry[0], entry[1]


def create_app(ctx: AppContext) -> FastAPI:
    ensure_log_buffer()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ctx.startup()
        yield
        ctx.shutdown()

    app = FastAPI(
        title="Dokkan Idle Farmer",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(SameOriginMiddleware)
    if ctx.settings.web_password:
        app.add_middleware(
            BasicAuthMiddleware,
            username=ctx.settings.web_username,
            password=ctx.settings.web_password,
        )

    @app.exception_handler(RoutineError)
    async def _routine_error(_: Any, exc: RoutineError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(DeviceError)
    async def _device_error(_: Any, exc: DeviceError) -> JSONResponse:
        # 503 rather than 502: a 502 looks like a broken reverse proxy (Cloudflare, nginx...).
        return JSONResponse({"detail": f"Device error: {exc}"}, status_code=503)

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Make browsers and CDNs (e.g. Cloudflare) pick up new versions after an update.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ----------------------------------------------------------------- status
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "settings": ctx.settings.public_dict(),
            "device": ctx.device_status,
            "farm": ctx.farmer.snapshot(),
            "lifetime": ctx.state.get("lifetime"),
            "selected_routine": ctx.selected_routine(),
            "notify_enabled": ctx.notifier.enabled,
        }

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return {"lifetime": ctx.state.get("lifetime"), "sessions": ctx.state.get("sessions")}

    @app.get("/api/logs")
    def logs(after: int = 0, limit: int = Query(300, ge=1, le=2000)) -> dict[str, Any]:
        lines = LOG_BUFFER.since(after, limit)
        return {"lines": lines, "last_id": lines[-1]["id"] if lines else after}

    # --------------------------------------------------------------- routines
    @app.get("/api/routines")
    def list_routines() -> list[dict[str, Any]]:
        return [asdict(info) for info in ctx.library.list()]

    @app.get("/api/routines/{routine_id}")
    def get_routine(routine_id: str) -> dict[str, Any]:
        validate_routine_id(routine_id)
        try:
            text = ctx.library.read_text(routine_id)
        except KeyError:
            raise HTTPException(404, f"No routine called {routine_id}") from None
        bundled = ctx.library.bundled_path(routine_id).exists()
        try:
            routine = parse_routine(text)
        except RoutineError as exc:
            return {
                "id": routine_id,
                "yaml": text,
                "valid": False,
                "error": str(exc),
                "bundled": bundled,
            }
        return {
            **_routine_summary(ctx, routine_id, routine),
            "yaml": text,
            "valid": True,
            "error": None,
            "bundled": bundled,
        }

    @app.put("/api/routines/{routine_id}")
    def save_routine(routine_id: str, body: RoutineText) -> dict[str, Any]:
        routine = ctx.library.save_text(validate_routine_id(routine_id), body.yaml)
        log.info("Routine %s saved", routine_id)
        return {
            **_routine_summary(ctx, routine_id, routine),
            "yaml": body.yaml,
            "valid": True,
            "error": None,
            "bundled": ctx.library.bundled_path(routine_id).exists(),
        }

    @app.post("/api/routines/validate")
    def validate_routine(body: RoutineText) -> dict[str, Any]:
        try:
            routine = parse_routine(body.yaml)
        except RoutineError as exc:
            return {"valid": False, "error": str(exc)}
        return {"valid": True, "error": None, "name": routine.name, "rules": len(routine.rules)}

    @app.post("/api/routines/{routine_id}/restore")
    def restore_routine(routine_id: str) -> dict[str, Any]:
        try:
            ctx.library.restore(validate_routine_id(routine_id))
        except KeyError:
            raise HTTPException(404, "This routine is not a bundled one") from None
        return get_routine(routine_id)

    @app.delete("/api/routines/{routine_id}")
    def delete_routine(routine_id: str) -> dict[str, Any]:
        try:
            ctx.library.delete(validate_routine_id(routine_id))
        except KeyError:
            raise HTTPException(404, f"No routine called {routine_id}") from None
        return {"ok": True}

    @app.put("/api/routines/{routine_id}/options")
    def save_options(routine_id: str, body: OptionsRequest) -> dict[str, Any]:
        routine = ctx.library.load(validate_routine_id(routine_id))
        values = routine.option_values(body.options)
        ctx.state.set_routine_options(routine_id, values)
        ctx.state.update(selected_routine=routine_id)
        return {"options": values}

    # ------------------------------------------------------------------- farm
    @app.post("/api/farm/start")
    def farm_start(body: StartRequest) -> dict[str, Any]:
        validate_routine_id(body.routine)
        if not ctx.library.exists(body.routine):
            raise HTTPException(404, f"No routine called {body.routine}")
        try:
            ctx.start_farm(body.routine, body.options)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return ctx.farmer.snapshot()

    @app.post("/api/farm/stop")
    def farm_stop() -> dict[str, Any]:
        ctx.farmer.stop("Stopped by user")
        return ctx.farmer.snapshot()

    @app.post("/api/farm/pause")
    def farm_pause() -> dict[str, Any]:
        ctx.farmer.pause()
        return ctx.farmer.snapshot()

    @app.post("/api/farm/resume")
    def farm_resume() -> dict[str, Any]:
        ctx.farmer.resume()
        return ctx.farmer.snapshot()

    # ----------------------------------------------------------------- screen
    @app.get("/api/screen.jpg")
    def screen_jpeg(frame: int | None = None, w: int = Query(720, ge=120, le=2160)) -> Response:
        result = ctx.farmer.frames.jpeg(frame, max_width=w)
        if result is None:
            raise HTTPException(404, "No screenshot yet")
        frame_id, data = result
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Frame-Id": str(frame_id)},
        )

    @app.get("/api/screen.png")
    def screen_png(frame: int | None = None) -> Response:
        entry = ctx.farmer.frames.get(frame)
        if entry is None:
            raise HTTPException(404, "No screenshot yet")
        frame_id, image, _ = entry
        return Response(
            encode_png(image),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="screen-{frame_id}.png"',
            },
        )

    @app.get("/api/screen/live.jpg")
    def screen_live(w: int = Query(720, ge=120, le=2160), after: int | None = None) -> Response:
        """A fresh screenshot as JPEG in one request (the farm loop's latest while farming).

        ``after``: the frame the viewer already shows; answered with 204 when nothing newer
        exists yet (while farming, frames come at the farm loop's pace).
        """
        if ctx.farmer.note_use(touch=False):
            time.sleep(WAKE_SETTLE_UI)  # the screen was off: let the game draw first
        frame_id = ctx.farmer.capture()
        if after is not None and frame_id == after:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        result = ctx.farmer.frames.jpeg(frame_id, max_width=w)
        if result is None:
            raise HTTPException(404, "No screenshot yet")
        frame_id, data = result
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Frame-Id": str(frame_id)},
        )

    @app.get("/api/audio.pcm")
    def audio_stream() -> StreamingResponse:
        """The emulator's sound while the browser listens: raw 16-bit little-endian stereo
        PCM (rate in X-Sample-Rate). 404 for devices without it (phones, redroid)."""
        from ..device.emulator_grpc import AUDIO_CHANNELS, AUDIO_RATE

        source = ctx.device.audio_stream()
        if source is None:
            raise HTTPException(404, "Sound needs the emulator stack (EMULATOR_GRPC)")
        ctx.farmer.mark_watched()

        def chunks():
            try:
                yield from source
            except DeviceError as exc:
                log.info("Sound stream ended: %s", exc)
            finally:
                source.close()

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Sample-Rate": str(AUDIO_RATE),
                "X-Channels": str(AUDIO_CHANNELS),
            },
        )

    @app.get("/api/screen/stream.mjpg")
    async def screen_stream(
        w: int = Query(540, ge=120, le=1080),
        fps: float = Query(10, ge=0.5, le=30),
        n: int = Query(0, ge=0, le=100000),
    ) -> StreamingResponse:
        """The live view as one MJPEG stream, at most ``fps`` frames a second.

        ``n``: stop after that many frames (0 = until the browser closes it).
        """
        if await anyio.to_thread.run_sync(lambda: ctx.farmer.note_use(touch=False)):
            await anyio.sleep(WAKE_SETTLE_UI)
        feed = await anyio.to_thread.run_sync(open_feed, ctx.farmer, w, fps)

        async def parts() -> AsyncIterator[bytes]:
            sent = 0
            try:
                while not n or sent < n:
                    # abandon_on_cancel: a closed tab must not wait for the next frame.
                    jpeg = await anyio.to_thread.run_sync(feed.next_jpeg, abandon_on_cancel=True)
                    ctx.farmer.mark_watched()  # an open live view keeps the screen on
                    yield mjpeg_part(jpeg)
                    sent += 1
            except DeviceError as exc:
                log.info("Live view stream ended: %s", exc)
            finally:
                feed.close()

        return StreamingResponse(
            parts(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/screen/capture")
    def screen_capture(pin: bool = False) -> dict[str, Any]:
        if ctx.farmer.note_use(touch=False):
            time.sleep(WAKE_SETTLE_UI)
        frame_id = ctx.farmer.capture()
        if pin:
            ctx.farmer.frames.pin(frame_id)
        _, frame = _frame_or_404(ctx, frame_id)
        height, width = frame.shape[:2]
        return {"frame_id": frame_id, "width": width, "height": height}

    @app.get("/api/screen/analyze")
    def screen_analyze(frame: int | None = None) -> dict[str, Any]:
        frame_id, image = _frame_or_404(ctx, frame)
        height, width = image.shape[:2]
        analysis = ctx.matcher.analyze(image)
        results = []
        for name in ctx.store.names():
            match = analysis.find(name)
            if match is not None:
                results.append(match.to_dict(width, height))
        return {"frame_id": frame_id, "width": width, "height": height, "results": results}

    # ----------------------------------------------------------------- device
    @app.post("/api/device/tap")
    def device_tap(body: TapRequest) -> dict[str, Any]:
        if ctx.farmer.note_use(touch=True):
            time.sleep(WAKE_SETTLE_UI)  # a tap on a screen still turning on gets lost
        px, py = ctx.farmer.tap_relative(body.x, body.y, body.count)
        return {"x": px, "y": py}

    @app.post("/api/device/swipe")
    def device_swipe(body: SwipeRequest) -> dict[str, Any]:
        if ctx.farmer.note_use(touch=True):
            time.sleep(WAKE_SETTLE_UI)
        ctx.farmer.swipe_relative(body.x1, body.y1, body.x2, body.y2, body.ms)
        return {"ok": True}

    @app.post("/api/device/screen")
    def device_screen(body: ScreenPowerRequest) -> dict[str, Any]:
        """The Screen off/on button: Android's screen off saves the emulator's drawing."""
        ctx.farmer.mark_watched()
        if body.on:
            if not ctx.farmer.screen_wake():
                ctx.farmer.device_call(lambda d: d.wake_screen())
        else:
            ctx.farmer.screen_sleep("turned off in the web UI", force=True)
        return {"screen_off": ctx.farmer.screen_off}

    @app.post("/api/device/key")
    def device_key(body: KeyRequest) -> dict[str, Any]:
        try:
            key = normalize_key(body.key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if ctx.farmer.note_use(touch=True):
            time.sleep(WAKE_SETTLE_UI)
        ctx.farmer.device_call(lambda d: d.press(key, body.count))
        return {"ok": True}

    @app.post("/api/device/text")
    def device_text(body: TextRequest) -> dict[str, Any]:
        if ctx.farmer.note_use(touch=True):
            time.sleep(WAKE_SETTLE_UI)
        try:
            ctx.farmer.device_call(lambda d: d.text(body.text))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    @app.post("/api/device/app")
    def device_app(body: AppRequest) -> dict[str, Any]:
        package = ctx.settings.game_package
        if body.package:
            try:
                package = validate_package(body.package.strip())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        ctx.farmer.note_use(touch=True)
        if body.action in ("stop", "restart"):
            ctx.farmer.device_call(lambda d: d.stop_app(package))
        if body.action in ("launch", "restart"):
            ctx.farmer.device_call(lambda d: d.launch_app(package))
        return {"ok": True, "package": package}

    @app.post("/api/device/refresh")
    def device_refresh() -> dict[str, Any]:
        return ctx.refresh_device()

    @app.post("/api/device/connect")
    def device_connect(body: ConnectRequest) -> dict[str, Any]:
        try:
            return ctx.set_serial(body.serial)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/device/pair")
    def device_pair(body: PairRequest) -> dict[str, Any]:
        if not isinstance(ctx.device, AdbDevice):
            raise HTTPException(400, "Pairing is only available with the adb backend")
        try:
            message = ctx.device.pair(body.address, body.code)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("Paired with %s", body.address)
        return {"ok": True, "message": message}

    @app.get("/api/device/devices")
    def device_list() -> dict[str, Any]:
        if not isinstance(ctx.device, AdbDevice):
            return {"devices": [{"serial": ctx.device.serial, "state": "device"}]}
        return {"devices": ctx.device.devices()}

    # ------------------------------------------------------------------- apps
    def _require_adb() -> None:
        if not isinstance(ctx.device, AdbDevice):
            raise HTTPException(400, "Installing apps needs a real Android device (adb backend)")

    @app.get("/api/apps")
    def apps_status() -> dict[str, Any]:
        package = ctx.settings.game_package
        installed = abi = runnable = None
        if ctx.device_status["connected"]:
            try:
                installed = ctx.farmer.device_call(lambda d: d.is_installed(package))
                runnable = ctx.farmer.device_call(lambda d: d.runnable_abis())
                if installed:
                    abi = ctx.farmer.device_call(lambda d: d.app_abi(package))
            except DeviceError:
                pass
        return {
            "package": package,
            "installed": installed,
            # The game's CPU code, and whether this Android can run it (None = unknown).
            "abi": abi,
            "abi_ok": None if abi is None or runnable is None else abi in runnable,
            # The CPU code this Android runs (APKPure only has Dokkan's 32-bit ARM build).
            "runnable_abis": runnable,
            # The emulator's ARM translation (libndk_translation 0.2.2) installs Dokkan's 64-bit
            # build but lacks an AArch64 instruction it uses: the game dies (SIGILL) as it starts
            # drawing. Its 32-bit build runs.
            "crashes": abi == "arm64-v8a"
            and ctx.device_status["info"].get("native_bridge") == "libndk_translation.so",
            "can_install": isinstance(ctx.device, AdbDevice),
            "job": ctx.installer.current(),
        }

    @app.get("/api/apps/job")
    def apps_job() -> dict[str, Any]:
        return {"job": ctx.installer.current()}

    @app.post("/api/apps/install")
    def apps_install(body: InstallRequest) -> dict[str, Any]:
        _require_adb()
        try:
            return {"job": ctx.installer.start_url(body.source)}
        except InstallError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/device/android-log")
    def device_android_log() -> dict[str, Any]:
        """Android's crash reports and log lines about the game, to copy into a bug report."""
        package = ctx.settings.game_package
        return {
            "package": package,
            "text": ctx.farmer.device_call(lambda d: d.android_log(package)),
        }

    @app.post("/api/apps/uninstall")
    def apps_uninstall() -> dict[str, Any]:
        """Remove the game (e.g. a build this Android cannot run) and its data."""
        _require_adb()
        if ctx.farmer.is_running:
            raise HTTPException(409, "Stop the farm first")
        if ctx.installer.busy:
            raise HTTPException(409, "An install is still running")
        package = ctx.settings.game_package
        ctx.farmer.device_call(lambda d: d.uninstall(package))
        log.info("Uninstalled %s", package)
        return {"ok": True, "package": package}

    @app.put("/api/apps/upload")
    async def apps_upload(
        request: Request, filename: str = Query(min_length=1, max_length=200)
    ) -> dict[str, Any]:
        _require_adb()
        if ctx.installer.busy:
            raise HTTPException(409, "Another install is still running")
        path = ctx.installer.upload_path(filename)
        size = 0
        try:
            with open(path, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise HTTPException(413, "The file is too large")
                    fh.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if size == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "The upload was empty")
        try:
            return {"job": ctx.installer.start_file(path, filename)}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    # -------------------------------------------------------------- templates
    @app.get("/api/templates")
    def list_templates() -> list[dict[str, Any]]:
        return [meta.to_dict() for meta in ctx.store.metas()]

    @app.get("/api/templates/export")
    def export_templates() -> JSONResponse:
        return JSONResponse(
            ctx.store.export_bundle(),
            headers={"Content-Disposition": 'attachment; filename="dokkan-templates.json"'},
        )

    @app.post("/api/templates/import")
    def import_templates(body: dict[str, Any]) -> dict[str, Any]:
        try:
            names = ctx.store.import_bundle(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("Imported templates: %s", ", ".join(names))
        return {"imported": names}

    @app.get("/api/templates/{name}.png")
    def template_png(name: str) -> Response:
        try:
            data = ctx.store.png_bytes(name)
        except (KeyError, ValueError):
            raise HTTPException(404, f"No template called {name}") from None
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.post("/api/templates")
    def create_template(body: TemplateCreate) -> dict[str, Any]:
        frame_id, frame = _frame_or_404(ctx, body.frame_id)
        height, width = frame.shape[:2]
        try:
            name = body.name.strip().lower()
            if ctx.store.has(name) and not body.overwrite:
                raise HTTPException(409, f"A template called {name} already exists")
            box = (body.x * width, body.y * height, body.w * width, body.h * height)
            tap = None
            if body.tap_x is not None and body.tap_y is not None:
                tap = [(body.tap_x - body.x) / body.w - 0.5, (body.tap_y - body.y) / body.h - 0.5]
            meta = ctx.store.create_from_frame(
                name,
                frame,
                box,
                threshold=body.threshold,
                mode=body.mode,
                search_full=body.search_full,
                description=body.description,
                tap=tap,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        analysis = ctx.matcher.analyze(frame)
        match = analysis.find(meta.name)
        warnings = []
        if match is None or not match.found:
            warnings.append("The new template is not found again on this screenshot.")
        candidates = analysis.candidates(meta.name, 2)
        if len(candidates) > 1 and candidates[1][0] >= meta.threshold - 0.05:
            warnings.append(
                f"It also matches another spot (score {candidates[1][0]:.2f}): crop a more "
                "distinctive area or raise the threshold."
            )
        log.info("Template %s saved from frame %d", meta.name, frame_id)
        return {
            "template": meta.to_dict(),
            "score": round(match.score, 3) if match else 0.0,
            "warnings": warnings,
        }

    @app.patch("/api/templates/{name}")
    def update_template(name: str, body: TemplateUpdate) -> dict[str, Any]:
        try:
            meta = ctx.store.update(
                name,
                threshold=body.threshold,
                mode=body.mode,
                search_full=body.search_full,
                description=body.description,
                reset_tap=body.reset_tap,
            )
        except KeyError:
            raise HTTPException(404, f"No template called {name}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return meta.to_dict()

    @app.delete("/api/templates/{name}")
    def delete_template(name: str) -> dict[str, Any]:
        try:
            ctx.store.delete(name)
        except KeyError:
            raise HTTPException(404, f"No template called {name}") from None
        return {"ok": True}

    # ------------------------------------------------------------- notify
    @app.post("/api/notify/test")
    def notify_test() -> dict[str, Any]:
        if not ctx.notifier.enabled:
            raise HTTPException(400, "Set NOTIFY_URLS (Apprise URLs) to enable notifications")
        ok = ctx.notifier.send_now("Test", "Notifications from Dokkan Farmer work!")
        if not ok:
            raise HTTPException(502, "The notification could not be delivered, check the logs")
        return {"ok": True}

    # ----------------------------------------------------------------- static
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "Dokkan Idle Farmer",
                "short_name": "Farmer",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#101217",
                "theme_color": "#101217",
                "icons": [
                    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            },
            media_type="application/manifest+json",
        )

    # -------------------------------------------------------------------- MCP
    # Answers are kept short: every word an assistant reads costs it tokens.
    if ctx.settings.mcp_token:
        server = McpServer()

        def shot(args: dict[str, Any]) -> list[dict[str, Any]]:
            if ctx.farmer.note_use(touch=False):
                time.sleep(WAKE_SETTLE_UI)
            frame_id = ctx.farmer.capture()
            ctx.farmer.frames.pin(frame_id)  # kept for create_template
            width = int(min(1080, max(240, args.get("width") or 360)))
            _, frame = _frame_or_404(ctx, frame_id)
            _, data = ctx.farmer.frames.jpeg(frame_id, max_width=width)
            height, full_width = frame.shape[:2]
            info = f"frame_id {frame_id}, device screen {full_width}x{height} px"
            return [mcp_text(info), mcp_image(base64.b64encode(data).decode())]

        def then_shot(args: dict[str, Any], done: str) -> list[dict[str, Any]]:
            if not args.get("screenshot", True):
                return [mcp_text(done)]
            time.sleep(min(10.0, max(0.0, float(args.get("wait", 1.5)))))
            return [mcp_text(done), *shot(args)]

        def box(values: list[float]) -> list[float]:
            return [round(v, 3) for v in values]

        def farm_line() -> str:
            farm = ctx.farmer.snapshot()
            line = f"{farm['state']}: {farm.get('routine_name') or '-'}"
            if farm.get("routine_id"):
                line += f" ({farm['routine_id']})"
            if farm.get("message"):
                line += f" - {farm['message']}"
            disabled = [r["name"] for r in farm.get("disabled_rules") or []]
            if disabled:
                line += f"; disabled rules: {', '.join(disabled)}"
            return line

        after = {
            "screenshot": {"type": "boolean", "description": "Return the screen afterwards"},
            "wait": {"type": "number", "description": "Seconds before that screenshot (1.5)"},
            "width": {"type": "integer", "description": "Screenshot width in px (360)"},
        }
        frac = {"type": "number", "minimum": 0, "maximum": 1}

        @server.tool(
            "Take a fresh screenshot of the device. Returns its frame_id (for create_template). "
            "To learn only which screen is up, find_templates is cheaper.",
            {"width": after["width"]},
        )
        def mcp_screenshot(args: dict[str, Any]) -> list[dict[str, Any]]:
            return shot(args)

        @server.tool(
            "Tap the screen at x, y (0..1 fractions of width and height).",
            {"x": frac, "y": frac, "double": {"type": "boolean"}, **after},
            ("x", "y"),
        )
        def mcp_tap(args: dict[str, Any]) -> list[dict[str, Any]]:
            body = TapRequest(x=args["x"], y=args["y"], count=2 if args.get("double") else 1)
            point = device_tap(body)
            done = f"Tapped {body.x:.3f}, {body.y:.3f} = pixel {point['x']}, {point['y']}"
            return then_shot(args, done)

        @server.tool(
            "Swipe from x1, y1 to x2, y2 (fractions). Swiping up (y1 > y2) scrolls down a list.",
            {"x1": frac, "y1": frac, "x2": frac, "y2": frac, "ms": {"type": "integer"}, **after},
            ("x1", "y1", "x2", "y2"),
        )
        def mcp_swipe(args: dict[str, Any]) -> list[dict[str, Any]]:
            fields = ("x1", "y1", "x2", "y2", "ms")
            device_swipe(SwipeRequest(**{k: args[k] for k in fields if k in args}))
            return then_shot(args, "Swiped")

        @server.tool(
            "Press an Android key: BACK, HOME, ENTER, APP_SWITCH, ...",
            {"key": {"type": "string"}, **after},
            ("key",),
        )
        def mcp_key(args: dict[str, Any]) -> list[dict[str, Any]]:
            device_key(KeyRequest(key=args["key"]))
            return then_shot(args, f"Pressed {args['key']}")

        @server.tool(
            "Launch, stop or restart the game app.",
            {"action": {"type": "string", "enum": ["launch", "stop", "restart"]}, **after},
            ("action",),
        )
        def mcp_game(args: dict[str, Any]) -> list[dict[str, Any]]:
            device_app(AppRequest(action=args["action"]))
            return then_shot({"wait": 5, **args}, f"Game: {args['action']}")

        @server.tool(
            "Which templates a fresh screenshot shows: the found ones and at most 3 near misses "
            "(score >= 0.5), as [name, score, box x/y/w/h]. names: only these, found or not. "
            "Its frame_id works for create_template. Templates have a mode (gray/color).",
            {"names": {"type": "array", "items": {"type": "string"}}},
        )
        def mcp_find_templates(args: dict[str, Any]) -> list[dict[str, Any]]:
            frame_id = ctx.farmer.capture()
            ctx.farmer.frames.pin(frame_id)  # kept for create_template
            results = sorted(screen_analyze(frame_id)["results"], key=lambda r: -r["score"])
            out: dict[str, Any] = {"frame_id": frame_id}
            if args.get("names"):
                wanted = set(args["names"])
                out["templates"] = [
                    [r["name"], r["score"], r["found"], box(r["box"])]
                    for r in results
                    if r["name"] in wanted
                ]
                out["unknown"] = sorted(wanted - {r["name"] for r in results}) or None
            else:
                out["found"] = [
                    [r["name"], r["score"], box(r["box"])] for r in results if r["found"]
                ]
                near = [r for r in results if not r["found"] and r["score"] >= 0.5][:3]
                out["near"] = [[r["name"], r["score"], box(r["box"])] for r in near]
            return [mcp_text({k: v for k, v in out.items() if v is not None})]

        @server.tool(
            "All templates, one per line: name, threshold, mode, search area (near = where it "
            "was cropped, full = whole screen), tap point if any, description."
        )
        def mcp_list_templates(args: dict[str, Any]) -> list[dict[str, Any]]:
            lines = []
            for t in list_templates():
                area = "full" if t["region"] is None else "near"
                line = f"{t['name']} {t['threshold']} {t['mode']} {area}"
                if t.get("tap"):
                    line += f" tap={t['tap']}"
                if t.get("description"):
                    line += f": {t['description']}"
                lines.append(line)
            return [mcp_text("\n".join(lines) or "(no templates)")]

        @server.tool("Look at one template's image.", {"name": {"type": "string"}}, ("name",))
        def mcp_template_image(args: dict[str, Any]) -> list[dict[str, Any]]:
            data = template_png(args["name"]).body
            return [mcp_image(base64.b64encode(data).decode(), "image/png")]

        @server.tool(
            "Crop a template from a screenshot (frame_id from `screenshot`). x, y, w, h: the box "
            "as fractions; crop something only that screen or button shows (its text). "
            "search_full: look for it anywhere (else only near where it was cropped). "
            "tap_x/tap_y: where `tap: name` taps (default: the box centre).",
            {
                "name": {"type": "string", "description": "a-z, 0-9, _ and -"},
                "frame_id": {"type": "integer"},
                "x": frac,
                "y": frac,
                "w": frac,
                "h": frac,
                "threshold": {"type": "number", "description": "0.5-0.99 (0.85)"},
                "mode": {"type": "string", "enum": ["gray", "color"]},
                "search_full": {"type": "boolean"},
                "tap_x": frac,
                "tap_y": frac,
                "description": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            ("name", "frame_id", "x", "y", "w", "h"),
        )
        def mcp_create_template(args: dict[str, Any]) -> list[dict[str, Any]]:
            result = create_template(TemplateCreate(**args))
            meta = result["template"]
            line = f"Saved {meta['name']}: score {result['score']} on its screenshot"
            line += f" (threshold {meta['threshold']}), bbox {meta['bbox']}"
            if meta.get("tap"):
                line += f", tap {meta['tap']}"
            return [mcp_text(" ".join([line, *result["warnings"]]))]

        @server.tool(
            "Change a template's threshold, mode (color tells an orange button from a "
            "greyed-out one), search area, description, or forget its tap point.",
            {
                "name": {"type": "string"},
                "mode": {"type": "string", "enum": ["gray", "color"]},
                "threshold": {"type": "number"},
                "search_full": {"type": "boolean"},
                "description": {"type": "string"},
                "reset_tap": {"type": "boolean"},
            },
            ("name",),
        )
        def mcp_update_template(args: dict[str, Any]) -> list[dict[str, Any]]:
            name = args.pop("name")
            t = update_template(name, TemplateUpdate(**args))
            area = "full" if t["region"] is None else "near"
            return [mcp_text(f"{name}: {t['threshold']} {t['mode']} {area} tap={t['tap']}")]

        @server.tool("Delete a template.", {"name": {"type": "string"}}, ("name",))
        def mcp_delete_template(args: dict[str, Any]) -> list[dict[str, Any]]:
            delete_template(args["name"])
            return [mcp_text(f"Deleted {args['name']}")]

        @server.tool(
            "The farm in short: state, routine, counters, runs, last rule, device connected."
        )
        def mcp_farm_status(args: dict[str, Any]) -> list[dict[str, Any]]:
            farm = ctx.farmer.snapshot()
            device = ctx.device_status
            out = {
                "farm": farm_line(),
                "counters": farm.get("counters") or {},
                "runs": farm.get("runs"),
                "elapsed_s": round(farm["elapsed"]) if farm.get("elapsed") else None,
                "last_rule": farm.get("last_rule"),
                "screen_off": farm.get("screen_off"),
                "device": "connected" if device.get("connected") else device.get("error"),
                "selected_routine": ctx.selected_routine(),
            }
            if farm["state"] == "stopped" and farm.get("last_session"):
                last = farm["last_session"]
                out["last_session"] = {k: last.get(k) for k in ("routine", "reason", "runs")}
            return [mcp_text({k: v for k, v in out.items() if v is not None})]

        @server.tool("The routines, one per line: id, name, bundled / modified, invalid.")
        def mcp_list_routines(args: dict[str, Any]) -> list[dict[str, Any]]:
            rows = []
            for r in list_routines():
                flags = [f for f in ("bundled", "modified") if r.get(f)]
                row = f"{r['id']}: {r['name']}" + (f" ({', '.join(flags)})" if flags else "")
                rows.append(row if r.get("valid") else f"{row} INVALID: {r.get('error')}")
            return [mcp_text("\n".join(rows))]

        def routine_brief(detail: dict[str, Any]) -> str:
            rules = detail.get("rules") or []
            head = f"{detail.get('name')} ({detail['id']}): "
            if not detail.get("valid", True):
                return head + f"INVALID: {detail.get('error')}"
            head += f"valid, {len(rules)} rules, " + (
                "ready" if detail.get("ready") else "not ready"
            )
            lines = [head]
            options = ", ".join(f"{o['key']}={o['value']}" for o in detail.get("options") or [])
            if options:
                lines.append(f"options: {options}")
            missing = [t for t in detail.get("templates") or [] if not t["captured"]]
            captured = [t["name"] for t in detail.get("templates") or [] if t["captured"]]
            if captured:
                lines.append(f"captured: {', '.join(captured)}")
            for t in missing:
                lines.append(f"missing {t['level']} {t['name']}: {t['description']}")
            return "\n".join(lines)

        @server.tool(
            "A routine in short: options and their values, captured templates, the missing ones "
            "with what to capture. yaml: true also returns its YAML.",
            {"id": {"type": "string"}, "yaml": {"type": "boolean"}},
            ("id",),
        )
        def mcp_get_routine(args: dict[str, Any]) -> list[dict[str, Any]]:
            detail = get_routine(args["id"])
            out = [mcp_text(routine_brief(detail))]
            if args.get("yaml"):
                out.append(mcp_text(detail.get("yaml", "")))
            return out

        @server.tool(
            "Save a routine (validated; a bundled one can be restored later): the whole yaml, or "
            "edits to the saved one, each {old, new} where old occurs exactly once.",
            {
                "id": {"type": "string"},
                "yaml": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                        "required": ["old", "new"],
                    },
                },
            },
            ("id",),
        )
        def mcp_save_routine(args: dict[str, Any]) -> list[dict[str, Any]]:
            routine_id = validate_routine_id(args["id"])
            text = args.get("yaml")
            if text is None:
                if not args.get("edits"):
                    raise ToolError("Give yaml or edits")
                try:
                    text = ctx.library.read_text(routine_id)
                except KeyError:
                    raise ToolError(f"No routine called {routine_id}") from None
            for i, edit in enumerate(args.get("edits") or [], 1):
                found = text.count(edit["old"])
                if found != 1:
                    raise ToolError(f"Edit {i}: old text found {found} times (needs exactly 1)")
                text = text.replace(edit["old"], edit["new"])
            detail = save_routine(routine_id, RoutineText(yaml=text))
            missing = detail.get("required_missing") or []
            line = f"Saved {routine_id}: valid, {len(detail.get('rules') or [])} rules"
            line += f"; missing required templates: {', '.join(missing)}" if missing else ""
            return [mcp_text(line)]

        @server.tool(
            "Start farming with a routine and options (see get_routine for them).",
            {"routine": {"type": "string"}, "options": {"type": "object"}},
            ("routine",),
        )
        def mcp_start_farm(args: dict[str, Any]) -> list[dict[str, Any]]:
            farm_start(StartRequest(routine=args["routine"], options=args.get("options") or {}))
            return [mcp_text(farm_line())]

        @server.tool("Stop farming.")
        def mcp_stop_farm(args: dict[str, Any]) -> list[dict[str, Any]]:
            farm_stop()
            return [mcp_text(farm_line())]

        @server.tool(
            "Log lines (what the farm did and why), oldest first. after: the last_id of the "
            "previous call, to get only newer lines. lines: how many at most (20, the newest).",
            {"after": {"type": "integer"}, "lines": {"type": "integer"}},
        )
        def mcp_logs(args: dict[str, Any]) -> list[dict[str, Any]]:
            limit = int(min(500, max(1, args.get("lines") or 20)))
            after_id = int(args.get("after") or 0)
            records = LOG_BUFFER.since(after_id, limit)
            out = [
                f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['level']} {r['msg']}"
                for r in records
            ]
            last_id = records[-1]["id"] if records else after_id
            out.append(f"(last_id {last_id})" if out else f"(no new lines, last_id {last_id})")
            return [mcp_text("\n".join(out))]

        @app.post("/mcp/{token}", include_in_schema=False)
        async def mcp_endpoint(token: str, request: Request) -> Response:
            if not hmac.compare_digest(token.encode(), ctx.settings.mcp_token.encode()):
                raise HTTPException(404, "Not Found")
            try:
                message = await request.json()
            except ValueError:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                    status_code=400,
                )
            answer = await anyio.to_thread.run_sync(server.handle, message)
            if answer is None:
                return Response(status_code=202)
            return JSONResponse(answer)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
