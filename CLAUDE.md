# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

dokkan-idle-farmer is a self-hosted Docker bot that idle-farms Dragon Ball Z Dokkan Battle: YAML routines
look at screenshots (OpenCV template matching) and tap through adb. The web UI is FastAPI plus build-less
vanilla JS. Users deploy it with Portainer from the prebuilt image `flavio11113/dokkan-idle-farmer`.

## Commands

- Setup: `.venv` here was created by uv and has no pip: `uv pip install -p .venv -r requirements-dev.txt`.
  Fresh: `python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`. The pinned deps need
  Python 3.12+ (CI and the image use 3.13); syntax stays 3.11-safe (ruff `py311`). The package is never
  installed: pytest gets `pythonpath = src` from `pyproject.toml`, the app needs `PYTHONPATH=src`.
- Tests: `.venv/bin/python -m pytest` (226 tests, about 3 min). Fast loop without the simulator end-to-end
  files (184 tests, about 20 s): `.venv/bin/python -m pytest --ignore=tests/test_engine.py --ignore=tests/test_clear_new_stages.py`.
  One test: `.venv/bin/python -m pytest tests/test_engine.py::test_device_disconnect_reconnects` or
  `.venv/bin/python -m pytest tests/test_web.py -k health` (an engine test alone takes about 10 s).
- Lint, as CI runs it: `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && node --check src/dokkan_farmer/web/static/app.js`
  (`.venv/bin/ruff format src tests` fixes formatting).
- Run against the built-in fake game (no device), from the repo root:
  `rm -rf /tmp/dokkan-data && PYTHONPATH=src DEVICE_BACKEND=sim AUTOSTART=false DATA_DIR=/tmp/dokkan-data WEB_PORT=8080 .venv/bin/python -m dokkan_farmer`
  then http://localhost:8080 (API docs at `/api/docs`). Use a fresh `DATA_DIR` (default `/data`): bundled
  routines are only copied when missing, and with the default `AUTOSTART=resume` a farm that was running
  when the app stopped starts again by itself.
- Image: `docker build -t dokkan-farmer:dev .`. Cloud sandboxes have no Docker daemon; validate compose
  edits with `docker compose -f docker-compose.emulator.yml config -q`.

## Architecture

**Wiring.** `__main__.py` → `Settings.from_env()` (`settings.py`, the only place that reads env vars;
errors there exit 2) → `AppContext` (`context.py`, the composition root: StateStore, RoutineLibrary,
TemplateStore, Device, Matcher, Notifier, one Farmer, InstallManager) → `web/app.py:create_app` → uvicorn.
Startup never blocks HTTP: `ctx.startup()` starts a thread that retries the device (10 s steps, 300 s) and
then applies `AUTOSTART`, plus a power-save thread unless `POWER_SAVE=false`. Everything the UI changes
(adb serial, selected routine, options, stats) lives in `DATA_DIR/state.json` (`state.py`); routines,
templates (`*.png` + `templates.json`) and debug screenshots are folders in `DATA_DIR`.

**Farm loop (`engine.py`).** `Farmer.start` runs `run_session` in a thread. `_prepare` runs once: rules
whose `visible`/`tap` templates are missing are disabled, edge counters are set up. Each `_tick`: pause and
active-hours checks, screenshot into the `FrameStore`, lazy cached matching (`vision.FrameAnalysis`),
counters, limits, foreground check, then the **first** rule (top to bottom) whose `when` holds fires its
`do` actions. Safety nets: stuck detection (`static_seconds`/`idle_seconds` → recovery actions → game
restart → give up), no-effect suppression (a rule fired 3 times on an unchanged 32x32 thumbnail is paused
120 s; the scrolling rules of `dokkan_clear_new_stages.yaml` rely on it), endless back-off on
`DeviceError`. Routine, options and disabled rules are fixed at farm start; template edits apply on the
next tick. The `Farmer` takes injectable `clock`/`wall`/`now`/`sleeper`/`rng`: engine code must use
`self.clock`/`self._sleep`, never `time.*` (tests run on `tests/conftest.py`'s `VirtualClock`).

**Power saving.** With `POWER_SAVE` (`auto` = only while `device.is_emulator`) the farmer sends Android's
SLEEP key during `wait_minutes` of 2+ minutes, once when active hours end, and from `Farmer.check_idle`
(no farm, paused or idle wait, and no web UI use for `SCREEN_OFF_MINUTES`). Endpoints that take a fresh
screenshot or send input call `farmer.note_use(touch=...)` and wait `WAKE_SETTLE_UI` if it woke the screen.

**Routines and templates.** `routine.py`: pydantic models (`extra="forbid"`), `parse_action` normalises
YAML shorthands, `$option` references are validated. Bundled routines (`src/dokkan_farmer/routines/*.yaml`)
are copied into `DATA_DIR/routines` only if missing, never overwritten (the UI's "Restore default").
`vision.py` matches with `TM_CCOEFF_NORMED` on the frame scaled by `MATCH_SCALE`; a template is rescaled
by screen width / capture width and searched near where it was captured (±6 % of the screen) unless
`search_full`; peaks much darker or brighter than the template (a copy behind a popup) are skipped.
Templates are shared by all routines; a template's optional `tap` offset is where `tap: name` taps when the
routine gives no `offset`.

**Device layer (`device/`).** `Device` (`base.py`): `screenshot()` returns top-row-first BGR in the pixel
space of `input tap`. `create_device` gives `SimDevice` (`DEVICE_BACKEND=sim`) or `AdbDevice`, optionally
with `EmulatorGrpc` and `EmulatorConsole`. `AdbDevice.screenshot()` tries the emulator's gRPC
`getScreenshot`, then its console (`screenrecord screenshot` into the shared `EMULATOR_SCREENSHOTS` folder,
which it empties of `*.png`), then adb `screencap`; the fast paths need the serial's host to equal the
port's host (`_is_emulator_host`, also behind `is_emulator`), rotation 0 and a working adb.
`emulator_grpc.py` hand-encodes the few protobuf fields it needs (no protobuf dependency); its frames come
top-down although the proto says bottom-up. UI device access goes through `Farmer.device_call` (lock + one
reconnect), the farm loop through `_with_device` (lock only); the gRPC stream reader and the installer
deliberately skip the lock. `installer.py` installs APKs/bundles from `apkpure:`, `fdroid:` or URLs.

**Web.** Routes are sync (threadpool; `time.sleep` is fine) except `stream.mjpg`, which uses
`anyio.to_thread` for device work. Global handlers: `DeviceError` → 503, `RoutineError` → 400; routes map
`RuntimeError` (farm running, installer busy) to 409 locally. `DeviceError` is a `RuntimeError` and
`RoutineError` a `ValueError`, so keep device calls out of `except RuntimeError` blocks. The live view is
one MJPEG stream (`live.py`: the emulator's gRPC frame stream, else screenshots); the client falls back to
polling `/api/screen/live.jpg` after 8 s. API coordinates are 0..1 fractions; in routines a point is
fractional only if both values are ≤ 1. `static/app.js` is one IIFE bound to `index.html` ids (renaming an
id breaks it silently; `node --check` only checks syntax); all device input goes through one FIFO queue
(taps wait 250 ms to detect a double tap). With `MCP_TOKEN`, `web/mcp.py` serves MCP (JSON-RPC over POST, no SSE) at
`/mcp/<token>`; its tools in `create_app` call the route functions (named `mcp_*` so they don't shadow
them) and it skips Basic auth.

**Deployment and CI.** The image (`Dockerfile`) holds only `requirements.txt`, `src/` and `docker/*.sh`;
runtime files must live under `src/dokkan_farmer/`. Stacks: `docker-compose.yml` (phone over adb),
`docker-compose.build.yml` (local build), `docker-compose.redroid.yml` (privileged `host-setup` loads binder,
then redroid), `docker-compose.emulator.yml` (Google's emulator in KVM: 720x1280 at 15 Hz, host renderer,
gRPC 8554, console 5554, tmpfs `/screens` shared with the farmer). The stacks have no `env_file`: a setting
reaches a container only if it is listed in that service's `environment:`. `ci.yml` runs lint and tests,
an amd64 smoke test in demo mode (`/api/health`, `"connected":true`, farm start, `"state":"running"`), then
builds amd64+arm64 and, except on PRs, pushes to GHCR and Docker Hub (`latest` on `main`, the commit SHA,
semver from `v*` tags). **A push to `main` is a release**: the stacks pull `:latest` with
`pull_policy: always`. `emulator-e2e.yml` and `android-e2e.yml` run only on `workflow_dispatch` with a
real emulator/redroid on GitHub runners (inputs such as `emulator_flags`, `android_env`); the emulator one
installs Dokkan through the farmer and logs CPU use as `POWER` lines. Only they catch real-device,
protocol or download-source regressions.

## Gotchas

- The dev dependency is `httpx2` (Starlette's TestClient imports it), not `httpx`.
- Tests pin the bundled routines: required template sets, the exact list of bundled routine ids
  (`tests/test_web.py`), literal YAML strings (e.g. `static_seconds: 180` in `tests/test_engine.py`).
  `tests/test_docs.py` parses the YAML blocks of `docs/ROUTINES.md` that start with `name:`.
- The simulator (`sim.py`) only plays `dokkan_auto_farm` (its templates come from `install_sim_templates`).
  `dokkan_clear_new_stages` is tested against `QuestGame` in `tests/test_clear_new_stages.py` (and
  `dokkan_collect_rewards` against `RewardsGame` in `tests/test_collect_rewards.py`), which reuse
  sim.py's private drawing helpers (`_button`, `_canvas`, `_text`, `Box`).
- Other fakes: a generated fake `adb` script (`tests/test_adb.py`), a real-socket console and a real
  `grpc.server` emulator (`tests/test_emulator_console.py`, `tests/test_emulator_grpc.py`), an injected HTTP
  opener for the installer, `HOST_SETUP_*` path overrides for `docker/host-setup.sh`.
- New settings: field and parsing in `Settings.from_env`, `public_dict()` if the UI shows it (no secrets),
  the list in `app.js` `renderDevice`, the README table, `.env.example` and the compose `environment:`.
- The version is in both `pyproject.toml` and `src/dokkan_farmer/__init__.py`.
- Serials and package names are regex-checked before they reach a device shell (`device/adb.py`); new shell
  commands must do the same.
- Dokkan on the emulator: only the 32-bit build runs (the farmer installs it from APKPure; the Play
  Store's 64-bit build crashes with SIGILL in the ARM translation), and only the `host` renderer draws it
  right (the image's `swiftshader_indirect` breaks its shaders). The e2e `apkpure:` install and
  `POWER` measurements are how such claims were checked.
