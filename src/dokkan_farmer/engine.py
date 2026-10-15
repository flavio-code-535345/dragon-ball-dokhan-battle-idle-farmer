"""The farm loop: look at the screen, fire the first matching rule, repeat.

On top of the routine's rules the engine adds the "idle farmer" safety net:
run counting, stop conditions, keeping the game in the foreground, waking
the screen, stuck detection with escalating recovery (BACK -> restart the
game -> give up), device reconnects with back-off and active hours.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .device.base import Device, DeviceError
from .notify import Notifier
from .routine import Action, Condition, Routine, format_text, loose_equal, resolve_number
from .schedule import ActiveHours
from .settings import Settings
from .state import StateStore
from .util import format_duration
from .vision import FrameAnalysis, Matcher, TemplateStore, encode_jpeg

log = logging.getLogger("dokkan_farmer.engine")

#: Mean absolute difference (0-255) of a 32x32 thumbnail above which the screen "changed".
CHANGE_THRESHOLD = 2.0
#: Frames darker than this (mean brightness) look like a switched-off screen.
DARK_THRESHOLD = 3.0
MAX_ERRORS_IN_ROW = 10
#: A rule that fires this many times in a row without changing the screen is paused...
NO_EFFECT_LIMIT = 3
#: ...for this many seconds, so that lower-priority rules (fallbacks) get a chance.
NO_EFFECT_PAUSE = 120.0


class FarmState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class FrameStore:
    """The latest screenshots, addressable by id (live view and template capture)."""

    def __init__(self, keep: int = 6, keep_pinned: int = 4) -> None:
        self._frames: OrderedDict[int, tuple[np.ndarray, float]] = OrderedDict()
        self._pinned: OrderedDict[int, tuple[np.ndarray, float]] = OrderedDict()
        self._jpeg: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._next = 1
        self._keep = keep
        self._keep_pinned = keep_pinned
        self._lock = threading.Lock()

    def add(self, frame: np.ndarray, timestamp: float) -> int:
        with self._lock:
            frame_id = self._next
            self._next += 1
            self._frames[frame_id] = (frame, timestamp)
            while len(self._frames) > self._keep:
                self._frames.popitem(last=False)
            return frame_id

    def pin(self, frame_id: int) -> bool:
        """Keep a frame around while the user draws a template on it."""
        with self._lock:
            entry = self._frames.get(frame_id) or self._pinned.get(frame_id)
            if entry is None:
                return False
            self._pinned[frame_id] = entry
            self._pinned.move_to_end(frame_id)
            while len(self._pinned) > self._keep_pinned:
                self._pinned.popitem(last=False)
            return True

    def get(self, frame_id: int | None = None) -> tuple[int, np.ndarray, float] | None:
        with self._lock:
            if frame_id is None:
                if not self._frames:
                    return None
                frame_id = next(reversed(self._frames))
            entry = self._frames.get(frame_id) or self._pinned.get(frame_id)
            return None if entry is None else (frame_id, entry[0], entry[1])

    def latest_id(self) -> int | None:
        with self._lock:
            return next(reversed(self._frames)) if self._frames else None

    def jpeg(self, frame_id: int | None = None, max_width: int = 720) -> tuple[int, bytes] | None:
        entry = self.get(frame_id)
        if entry is None:
            return None
        frame_id, frame, _ = entry
        key = (frame_id, max_width)
        with self._lock:
            cached = self._jpeg.get(key)
        if cached is None:
            cached = encode_jpeg(frame, quality=72, max_width=max_width)
            with self._lock:
                self._jpeg[key] = cached
                while len(self._jpeg) > 8:
                    self._jpeg.popitem(last=False)
        return frame_id, cached


@dataclass
class Session:
    routine_id: str
    routine: Routine
    options: dict[str, Any]
    started: float
    started_wall: float
    counters: dict[str, int] = field(default_factory=dict)
    rule_fired_at: dict[int, float] = field(default_factory=dict)
    disabled_rules: dict[int, str] = field(default_factory=dict)
    edge_counters: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    counter_armed: dict[str, bool] = field(default_factory=dict)
    counter_seen: dict[str, bool] = field(default_factory=dict)
    last_rule_at: float = 0.0
    last_change_at: float = 0.0
    prev_thumb: np.ndarray | None = None
    recoveries: int = 0
    recoveries_since_run: int = 0
    restarts: int = 0
    restarts_without_progress: int = 0
    last_foreground_check: float = float("-inf")
    foreground_misses: int = 0
    dark_frames: int = 0
    streak_rule: int | None = None
    streak_count: int = 0
    streak_thumb: np.ndarray | None = None
    suppressed_until: dict[int, float] = field(default_factory=dict)
    device_errors: int = 0
    errors_in_row: int = 0
    last_rule: str | None = None
    last_rule_wall: float | None = None
    last_run_wall: float | None = None
    next_milestone: int = 0
    stop_reason: str | None = None
    stop_is_error: bool = False

    @property
    def run_counter(self) -> str:
        return self.routine.settings.run_counter

    @property
    def runs(self) -> int:
        return self.counters.get(self.run_counter, 0)


def missing_required_templates(routine: Routine, store: TemplateStore) -> list[str]:
    return [
        name
        for name, doc in routine.templates.items()
        if doc.level == "required" and not store.has(name)
    ]


def to_pixels(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """Values within 0..1 are fractions of the screen, larger values are pixels."""
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        x, y = x * width, y * height
    return int(min(width - 1, max(0, round(x)))), int(min(height - 1, max(0, round(y))))


class Farmer:
    def __init__(
        self,
        device: Device,
        matcher: Matcher,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
        state: StateStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        now: Callable[[], datetime] = datetime.now,
        sleeper: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        on_device_status: Callable[[bool, str | None], None] | None = None,
    ) -> None:
        self.device = device
        self.matcher = matcher
        self.settings = settings
        self.notifier = notifier or Notifier("")
        self.state = state
        self.clock, self.wall, self.now = clock, wall, now
        self._sleeper = sleeper
        self.rng = rng or random.Random()
        self.on_device_status = on_device_status
        self.active_hours = ActiveHours.parse(settings.active_hours)
        self.device_lock = threading.RLock()
        self.frames = FrameStore()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._state = FarmState.STOPPED
        self._message = "Idle"
        self._wait_until: float | None = None
        self._detections: list[dict[str, Any]] = []
        self._session: Session | None = None
        self._last_summary: dict[str, Any] | None = None
        self._stop_request: str | None = None
        self._resume_later = False

    # ================================================================ public API
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self, routine_id: str, routine: Routine, options: dict[str, Any] | None = None
    ) -> None:
        if self.is_running:
            raise RuntimeError("The farm is already running")
        values = routine.option_values(options)
        self._stop_event.clear()
        self._pause_event.clear()
        self._stop_request = None
        self._resume_later = False
        self._set_state(FarmState.STARTING, f"Starting {routine.name}")
        self._thread = threading.Thread(
            target=self._thread_main, args=(routine_id, routine, values), name="farmer", daemon=True
        )
        self._thread.start()

    def stop(
        self, reason: str = "Stopped by user", *, resume_later: bool = False, wait: float = 15.0
    ) -> None:
        self._stop_request = reason
        self._resume_later = resume_later
        self._stop_event.set()
        self._pause_event.clear()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait)

    def pause(self) -> None:
        if self.is_running:
            self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            data: dict[str, Any] = {
                "state": self._state.value,
                "message": self._message,
                "wait_until": self._wait_until,
                "detections": list(self._detections),
                "frame_id": self.frames.latest_id(),
                "active_hours": str(self.active_hours),
                "active_now": self.active_hours.is_active(self.now()),
            }
            session = self._session
        if session is not None:
            elapsed = self.clock() - session.started
            routine = session.routine
            data.update(
                routine_id=session.routine_id,
                routine_name=routine.name,
                options=dict(session.options),
                started_at=session.started_wall,
                elapsed=elapsed,
                counters=dict(session.counters),
                run_counter=session.run_counter,
                runs=session.runs,
                runs_per_hour=round(session.runs / max(elapsed / 3600.0, 1 / 60), 1),
                last_rule=session.last_rule,
                last_rule_at=session.last_rule_wall,
                last_run_at=session.last_run_wall,
                recoveries=session.recoveries,
                restarts=session.restarts,
                disabled_rules=[
                    {"name": routine.rules[i].name, "reason": why}
                    for i, why in sorted(session.disabled_rules.items())
                ],
            )
        data["last_session"] = self._last_summary
        return data

    # ------------------------------------------------------- device helpers (UI)
    def capture(self) -> int:
        """Take a fresh screenshot (or reuse the farm loop's latest one)."""
        if self.is_running and self.frames.latest_id() is not None:
            return self.frames.latest_id()  # type: ignore[return-value]
        frame = self.device_call(lambda d: d.screenshot())
        return self.frames.add(frame, self.wall())

    def tap_relative(self, x: float, y: float) -> tuple[int, int]:
        entry = self.frames.get()
        if entry is None:
            self.capture()
            entry = self.frames.get()
        assert entry is not None
        height, width = entry[1].shape[:2]
        px, py = to_pixels(x, y, width, height)
        self.device_call(lambda d: d.tap(px, py))
        return px, py

    def swipe_relative(self, x1: float, y1: float, x2: float, y2: float, ms: int = 300) -> None:
        entry = self.frames.get()
        if entry is None:
            self.capture()
            entry = self.frames.get()
        assert entry is not None
        height, width = entry[1].shape[:2]
        a, b = to_pixels(x1, y1, width, height), to_pixels(x2, y2, width, height)
        self.device_call(lambda d: d.swipe(a[0], a[1], b[0], b[1], ms))

    def device_call(self, fn: Callable[[Device], Any]) -> Any:
        """Run a device action for the web UI, reconnecting once if the device dropped."""
        try:
            result = self._with_device(fn)
        except DeviceError:
            try:
                self._with_device(lambda d: d.connect())
            except DeviceError as exc:
                self._device_status(False, str(exc))
                raise
            # Reachable again; if the action still fails it is the action's fault.
            self._device_status(True, None)
            return self._with_device(fn)
        self._device_status(True, None)
        return result

    def _with_device(self, fn: Callable[[Device], Any]) -> Any:
        with self.device_lock:
            return fn(self.device)

    # =============================================================== the loop
    def _thread_main(self, routine_id: str, routine: Routine, values: dict[str, Any]) -> None:
        try:
            self.run_session(routine_id, routine, values)
        except Exception:  # pragma: no cover - last line of defence
            log.exception("The farm loop crashed")
            self._set_state(FarmState.ERROR, "The farm loop crashed, see the logs")

    def run_session(
        self,
        routine_id: str,
        routine: Routine,
        values: dict[str, Any],
        *,
        max_ticks: int | None = None,
        until: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run one farming session synchronously (the thread target; used by tests)."""
        now = self.clock()
        session = Session(routine_id, routine, values, started=now, started_wall=self.wall())
        session.last_rule_at = session.last_change_at = now
        session.next_milestone = self.settings.notify_every_runs
        with self._status_lock:
            self._session = session
        self._prepare(session)
        if session.stop_reason is None:
            options = ", ".join(f"{k}={v}" for k, v in values.items()) or "defaults"
            log.info("Farm started: %s (%s)", routine.name, options)
            self.notifier.send("Farm started", f"{routine.name}\nOptions: {options}")
            if self.state is not None:
                self.state.update(was_running=True, selected_routine=routine_id)
            if self.settings.keep_screen_on:
                self._quietly(lambda d: d.set_stay_awake(True))
            self._set_state(FarmState.RUNNING, "Farming")
        ticks = 0
        try:
            while session.stop_reason is None and not self._stop_event.is_set():
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if until is not None and until():
                    break
                ticks += 1
                try:
                    self._tick(session)
                    session.errors_in_row = 0
                except DeviceError as exc:
                    self._device_error(session, exc)
                except Exception as exc:
                    session.errors_in_row += 1
                    log.exception("Unexpected error in the farm loop")
                    if session.errors_in_row >= MAX_ERRORS_IN_ROW:
                        session.stop_reason = f"Too many errors in a row (last: {exc})"
                        session.stop_is_error = True
                    else:
                        self._sleep(5.0)
        finally:
            summary = self._finish(session)
        return summary

    def _prepare(self, session: Session) -> None:
        store = self.matcher.store
        routine = session.routine
        for index, rule in enumerate(routine.rules):
            if not rule.enabled:
                session.disabled_rules[index] = "disabled in the routine"
                continue
            missing = sorted(
                n for n in (set(rule.when.visible) | rule.tap_templates()) if not store.has(n)
            )
            if rule.when.any_visible and not any(store.has(n) for n in rule.when.any_visible):
                missing += sorted(rule.when.any_visible)
            if missing:
                session.disabled_rules[index] = "missing template(s): " + ", ".join(missing)
        for name, spec in routine.counters.items():
            if spec.appears is None:
                continue
            if not store.has(spec.appears):
                log.warning("Counter %r is inactive: template %r is missing", name, spec.appears)
                continue
            rearm = spec.rearm if spec.rearm and store.has(spec.rearm) else None
            if spec.rearm and rearm is None:
                log.warning(
                    "Counter %r: rearm template %r missing, counting every appearance",
                    name,
                    spec.rearm,
                )
            session.edge_counters[name] = (spec.appears, rearm)
            session.counter_armed[name] = False
            session.counter_seen[name] = True  # whatever is on screen at start does not count
        for index, reason in sorted(session.disabled_rules.items()):
            log.info("Rule %r skipped: %s", routine.rules[index].name, reason)
        missing = missing_required_templates(routine, store)
        if missing:
            session.stop_reason = (
                f"Missing required template(s): {', '.join(missing)}. "
                "Capture them in the Templates tab first."
            )
            session.stop_is_error = True
        elif len(session.disabled_rules) == len(routine.rules):
            session.stop_reason = "No usable rules: capture the routine's templates first"
            session.stop_is_error = True

    def _tick(self, session: Session) -> None:
        settings = session.routine.settings
        tick = resolve_number(settings.tick_interval, session.options)
        if self._pause_event.is_set():
            self._set_state(FarmState.PAUSED, "Paused")
            self._sleep(0.5)
            self._reset_timers(session)
            return
        if not self.active_hours.is_active(self.now()):
            resume_at = self.active_hours.next_start(self.now())
            when = resume_at.strftime("%H:%M") if resume_at else "later"
            self._set_state(FarmState.WAITING, f"Outside active hours, resuming at {when}")
            self._sleep(30.0)
            self._reset_timers(session)
            return

        frame = self._capture()
        if session.device_errors:
            log.info("Device is reachable again")
            session.device_errors = 0
            self._device_status(True, None)
        now = self.clock()
        if self._state not in (FarmState.RUNNING,):
            self._set_state(FarmState.RUNNING, "Farming")
        analysis = self.matcher.analyze(frame)
        self._track_change(session, frame, now)
        self._wake_if_dark(session, frame)
        self._update_counters(session, analysis)
        if self._check_limits(session, now):
            self._publish(analysis)
            return
        if self._ensure_foreground(session, now):
            self._publish(analysis)
            return

        index = self._select_rule(session, analysis, now)
        if index is not None and self._had_no_effect(session, index, now):
            index = self._select_rule(session, analysis, now)
        self._publish(analysis)
        if index is not None:
            self._fire(session, index, analysis)
            self._publish(analysis)
        if session.stop_reason is None and not self._stop_event.is_set():
            self._check_stuck(session, analysis)
        if session.stop_reason is None:
            self._sleep(tick)

    # ------------------------------------------------------------- perception
    def _capture(self) -> np.ndarray:
        frame = self._with_device(lambda d: d.screenshot())
        self.frames.add(frame, self.wall())
        return frame

    def _track_change(self, session: Session, frame: np.ndarray, now: float) -> None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.int16)
        prev = session.prev_thumb
        if prev is None or float(np.abs(thumb - prev).mean()) > CHANGE_THRESHOLD:
            session.last_change_at = now
        session.prev_thumb = thumb

    def _wake_if_dark(self, session: Session, frame: np.ndarray) -> None:
        if float(frame.mean()) < DARK_THRESHOLD:
            session.dark_frames += 1
            if session.dark_frames >= 5:
                log.info("The screen looks switched off, sending WAKEUP")
                self._with_device(lambda d: d.key("WAKEUP"))
                session.dark_frames = 0
        else:
            session.dark_frames = 0

    def _update_counters(self, session: Session, analysis: FrameAnalysis) -> None:
        for name, (on, rearm) in session.edge_counters.items():
            if rearm is not None and analysis.visible(rearm):
                session.counter_armed[name] = True
            visible = analysis.visible(on)
            if rearm is not None:
                if visible and session.counter_armed[name]:
                    session.counter_armed[name] = False
                    self._increment(session, name, 1)
            elif visible and not session.counter_seen[name]:
                self._increment(session, name, 1)
            session.counter_seen[name] = visible

    def _increment(self, session: Session, counter: str, by: int) -> None:
        session.counters[counter] = session.counters.get(counter, 0) + by
        if counter != session.run_counter:
            return
        runs = session.runs
        session.last_run_wall = self.wall()
        session.restarts_without_progress = 0
        session.recoveries_since_run = 0
        log.info("Run #%d finished", runs)
        if self.state is not None:
            self.state.add_runs(by)
        every = self.settings.notify_every_runs
        if every and runs >= session.next_milestone:
            elapsed = format_duration(self.clock() - session.started)
            self.notifier.send("Progress", f"{runs} runs in {elapsed} ({session.routine.name})")
            session.next_milestone = (runs // every + 1) * every

    def _check_limits(self, session: Session, now: float) -> bool:
        settings = session.routine.settings
        max_runs = resolve_number(settings.stop_after_runs, session.options)
        if max_runs > 0 and session.runs >= max_runs:
            session.stop_reason = f"Finished: reached {int(max_runs)} runs"
            return True
        minutes = resolve_number(settings.stop_after_minutes, session.options)
        if minutes > 0 and now - session.started >= minutes * 60:
            session.stop_reason = f"Finished: time limit of {minutes:g} min reached"
            return True
        return False

    def _ensure_foreground(self, session: Session, now: float) -> bool:
        settings = session.routine.settings
        if not settings.ensure_foreground:
            return False
        if now - session.last_foreground_check < settings.foreground_check_seconds:
            return False
        session.last_foreground_check = now
        try:
            package = self._with_device(lambda d: d.foreground_package())
        except DeviceError as exc:
            log.debug("Foreground check failed: %s", exc)
            return False
        if package is None or package == self.settings.game_package:
            session.foreground_misses = 0
            return False
        session.foreground_misses += 1
        if session.foreground_misses < 2:
            # Double-check a few seconds later before relaunching.
            session.last_foreground_check = now - settings.foreground_check_seconds + 5
            return False
        log.warning("The game is not in the foreground (%s): launching it", package)
        session.foreground_misses = 0
        self._launch_game(session)
        return True

    # --------------------------------------------------------------- decisions
    def _select_rule(self, session: Session, analysis: FrameAnalysis, now: float) -> int | None:
        for index, rule in enumerate(session.routine.rules):
            if index in session.disabled_rules or session.suppressed_until.get(index, 0) > now:
                continue
            last = session.rule_fired_at.get(index)
            if last is not None and now - last < resolve_number(rule.cooldown, session.options):
                continue
            if self._conditions_met(session, rule.when, analysis, now):
                return index
        return None

    def _conditions_met(
        self, session: Session, when: Condition, analysis: FrameAnalysis, now: float
    ) -> bool:
        options = session.options
        for key, expected in when.option.items():
            if not loose_equal(options.get(key), expected):
                return False
        for counter, limit in when.counter_below.items():
            if session.counters.get(counter, 0) >= resolve_number(limit, options):
                return False
        for counter, limit in when.counter_at_least.items():
            if session.counters.get(counter, 0) < resolve_number(limit, options):
                return False
        if when.static_for is not None and (
            now - session.last_change_at < resolve_number(when.static_for, options)
        ):
            return False
        if when.idle_for is not None and (
            now - session.last_rule_at < resolve_number(when.idle_for, options)
        ):
            return False
        if not all(analysis.visible(name) for name in when.visible):
            return False
        if when.any_visible and not any(analysis.visible(name) for name in when.any_visible):
            return False
        return not any(analysis.visible(name) for name in when.not_visible)

    def _had_no_effect(self, session: Session, index: int, now: float) -> bool:
        """Pause a rule that keeps firing while the screen stays exactly the same."""
        thumb = session.prev_thumb
        same_screen = (
            session.streak_rule == index
            and thumb is not None
            and session.streak_thumb is not None
            and float(np.abs(thumb - session.streak_thumb).mean()) <= CHANGE_THRESHOLD
        )
        session.streak_count = session.streak_count + 1 if same_screen else 1
        session.streak_rule, session.streak_thumb = index, thumb
        if session.streak_count <= NO_EFFECT_LIMIT:
            return False
        log.warning(
            "Rule %r had no visible effect %d times in a row: skipping it for %.0fs",
            session.routine.rules[index].name,
            NO_EFFECT_LIMIT,
            NO_EFFECT_PAUSE,
        )
        session.suppressed_until[index] = now + NO_EFFECT_PAUSE
        session.streak_rule, session.streak_count, session.streak_thumb = None, 0, None
        return True

    def _fire(self, session: Session, index: int, analysis: FrameAnalysis) -> None:
        rule = session.routine.rules[index]
        now = self.clock()
        session.rule_fired_at[index] = now
        session.last_rule_at = now
        session.last_rule = rule.name
        session.last_rule_wall = self.wall()
        log.info("Rule: %s", rule.name)
        self._set_state(FarmState.RUNNING, rule.name)
        self._run_actions(session, rule.do, analysis)
        if session.stop_reason is None and not self._stop_event.is_set():
            delay = rule.delay if rule.delay is not None else session.routine.settings.action_delay
            self._sleep(resolve_number(delay, session.options))

    def _check_stuck(self, session: Session, analysis: FrameAnalysis) -> None:
        now = self.clock()
        stuck = session.routine.settings.stuck
        static_limit = resolve_number(stuck.static_seconds, session.options)
        idle_limit = resolve_number(stuck.idle_seconds, session.options)
        static_for = now - session.last_change_at
        idle_for = now - session.last_rule_at
        if static_limit > 0 and static_for >= static_limit:
            why = f"screen unchanged for {format_duration(static_for)}"
        elif idle_limit > 0 and idle_for >= idle_limit:
            why = f"no rule matched for {format_duration(idle_for)}"
        else:
            return
        session.recoveries += 1
        session.recoveries_since_run += 1
        self._save_screenshot(analysis.frame, "stuck")
        if session.recoveries_since_run > stuck.restart_after:
            session.recoveries_since_run = 0
            self._restart_game(session, why, analysis.frame)
        else:
            log.warning("Looks stuck (%s): recovery attempt %d", why, session.recoveries_since_run)
            self._set_state(FarmState.RUNNING, f"Recovering ({why})")
            self._run_actions(session, stuck.recovery, None)
        self._reset_timers(session)

    # ----------------------------------------------------------------- actions
    def _run_actions(
        self, session: Session, actions: list[Action], analysis: FrameAnalysis | None
    ) -> None:
        for action in actions:
            if session.stop_reason is not None or self._stop_event.is_set():
                return
            self._run_action(session, action, analysis)

    def _frame_size(self, analysis: FrameAnalysis | None) -> tuple[int, int]:
        if analysis is not None:
            return analysis.width, analysis.height
        entry = self.frames.get()
        frame = self._capture() if entry is None else entry[1]
        height, width = frame.shape[:2]
        return width, height

    def _text_values(self, session: Session) -> dict[str, Any]:
        return {
            **session.options,
            **session.counters,
            "runs": session.runs,
            "routine": session.routine.name,
            "elapsed": format_duration(self.clock() - session.started),
        }

    def _run_action(self, session: Session, action: Action, analysis: FrameAnalysis | None) -> None:
        kind, args, options = action.kind, action.args, session.options
        if kind == "tap":
            match = analysis.find(args["template"]) if analysis is not None else None
            if match is None or not match.found:
                log.info("Tap on %s skipped: it is not on screen", args["template"])
                return
            x, y, w, h = match.box
            dx, dy = args.get("offset", [0.0, 0.0])
            jitter = self.settings.tap_jitter
            cx = x + w * (0.5 + dx + self.rng.uniform(-jitter, jitter))
            cy = y + h * (0.5 + dy + self.rng.uniform(-jitter, jitter))
            width, height = self._frame_size(analysis)
            px = int(min(width - 1, max(0, round(cx))))
            py = int(min(height - 1, max(0, round(cy))))
            self._with_device(lambda d: d.tap(px, py))
        elif kind == "tap_xy":
            width, height = self._frame_size(analysis)
            px, py = to_pixels(
                resolve_number(args["x"], options),
                resolve_number(args["y"], options),
                width,
                height,
            )
            self._with_device(lambda d: d.tap(px, py))
        elif kind == "swipe":
            width, height = self._frame_size(analysis)
            a = to_pixels(
                resolve_number(args["x1"], options),
                resolve_number(args["y1"], options),
                width,
                height,
            )
            b = to_pixels(
                resolve_number(args["x2"], options),
                resolve_number(args["y2"], options),
                width,
                height,
            )
            ms = int(resolve_number(args["ms"], options))
            self._with_device(lambda d: d.swipe(a[0], a[1], b[0], b[1], ms))
        elif kind == "key":
            self._with_device(lambda d: d.key(args["key"]))
        elif kind == "wait":
            self._wait(session, resolve_number(args["seconds"], options))
        elif kind == "wait_minutes":
            minutes = resolve_number(args["minutes"], options)
            self._wait(session, minutes * 60, f"Waiting {minutes:g} min")
        elif kind == "count":
            self._increment(session, args["counter"], int(args.get("by", 1)))
        elif kind == "reset":
            session.counters[args["counter"]] = 0
        elif kind == "notify":
            text = format_text(args["text"], self._text_values(session))
            image = analysis.frame if analysis is not None else None
            self.notifier.send(session.routine.name, text, image=image)
            log.info("Notification: %s", text)
        elif kind == "log":
            log.info(format_text(args["text"], self._text_values(session)))
        elif kind == "stop":
            session.stop_reason = format_text(args["text"], self._text_values(session)) or (
                "Stopped by the routine"
            )
        elif kind == "launch_app":
            self._launch_game(session)
        elif kind == "stop_app":
            self._with_device(lambda d: d.stop_app(self.settings.game_package))
        elif kind == "restart_app":
            frame = analysis.frame if analysis is not None else None
            self._restart_game(session, "requested by the routine", frame)
        elif kind == "screenshot":
            frame = analysis.frame if analysis is not None else self._capture()
            self._save_screenshot(frame, args.get("label") or "routine")

    def _wait(self, session: Session, seconds: float, message: str | None = None) -> None:
        if seconds <= 0:
            return
        if seconds >= 30:
            until = self.wall() + seconds
            self._set_state(
                FarmState.WAITING,
                message or f"Waiting {format_duration(seconds)}",
                wait_until=until,
            )
            log.info("%s", message or f"Waiting {format_duration(seconds)}")
        self._sleep(seconds)
        self._reset_timers(session)
        if not self._stop_event.is_set():
            self._set_state(FarmState.RUNNING, "Farming")

    def _launch_game(self, session: Session) -> None:
        self._with_device(lambda d: d.launch_app(self.settings.game_package))
        self._sleep(5.0)
        self._reset_timers(session)

    def _restart_game(self, session: Session, reason: str, frame: np.ndarray | None) -> None:
        stuck = session.routine.settings.stuck
        session.restarts += 1
        session.restarts_without_progress += 1
        if stuck.max_restarts and session.restarts_without_progress > stuck.max_restarts:
            session.stop_reason = (
                f"Gave up: restarted the game {stuck.max_restarts}x without finishing a run "
                f"({reason})"
            )
            session.stop_is_error = True
            return
        log.warning("Restarting the game (%s)", reason)
        self._set_state(FarmState.RUNNING, f"Restarting the game ({reason})")
        self.notifier.send("Restarting the game", reason, image=frame)
        self._with_device(lambda d: d.stop_app(self.settings.game_package))
        self._sleep(3.0)
        self._launch_game(session)

    def _device_error(self, session: Session, exc: DeviceError) -> None:
        session.device_errors += 1
        delay = min(60.0, 5.0 * 2 ** min(session.device_errors - 1, 4))
        log.warning("Device error: %s (retrying in %.0fs)", exc, delay)
        self._set_state(FarmState.RECONNECTING, f"Device unreachable: {exc}")
        self._device_status(False, str(exc))
        if session.device_errors == 3:
            self.notifier.send("Device unreachable", str(exc))
        self._sleep(delay)
        try:
            self._with_device(lambda d: d.connect())
        except DeviceError as again:
            log.debug("Reconnect failed: %s", again)
        self._reset_timers(session)

    # ------------------------------------------------------------- bookkeeping
    def _finish(self, session: Session) -> dict[str, Any]:
        reason = session.stop_reason or self._stop_request or "Stopped"
        elapsed = self.clock() - session.started
        summary = {
            "routine_id": session.routine_id,
            "routine": session.routine.name,
            "started_at": session.started_wall,
            "ended_at": self.wall(),
            "seconds": int(elapsed),
            "runs": session.runs,
            "counters": dict(session.counters),
            "recoveries": session.recoveries,
            "restarts": session.restarts,
            "reason": reason,
            "error": session.stop_is_error,
        }
        state = FarmState.ERROR if session.stop_is_error else FarmState.STOPPED
        level = logging.WARNING if session.stop_is_error else logging.INFO
        log.log(
            level,
            "Farm stopped: %s (%d runs in %s)",
            reason,
            session.runs,
            format_duration(elapsed),
        )
        self.notifier.send(
            "Farm stopped",
            f"{reason}\n{session.runs} runs in {format_duration(elapsed)}",
            image=self._latest_frame() if session.stop_is_error else None,
        )
        if self.state is not None:
            self.state.record_session(summary)
            self.state.update(was_running=self._resume_later)
        if self.settings.keep_screen_on:
            self._quietly(lambda d: d.set_stay_awake(False))
        with self._status_lock:
            self._last_summary = summary
            self._session = None
            self._state = state
            self._message = reason
            self._wait_until = None
        return summary

    def _latest_frame(self) -> np.ndarray | None:
        entry = self.frames.get()
        return None if entry is None else entry[1]

    def _save_screenshot(self, frame: np.ndarray | None, label: str) -> None:
        keep = self.settings.screenshot_keep
        if frame is None or keep <= 0:
            return
        directory: Path = self.settings.screenshots_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(self.wall()).strftime("%Y%m%d-%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
            cv2.imwrite(str(directory / f"{stamp}-{safe}.jpg"), frame)
            shots = sorted(directory.glob("*.jpg"))
            for old in shots[:-keep]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("Could not save screenshot: %s", exc)

    def _publish(self, analysis: FrameAnalysis) -> None:
        detections = [
            m.to_dict(analysis.width, analysis.height) for m in analysis.matches() if m.found
        ]
        with self._status_lock:
            self._detections = detections

    def _reset_timers(self, session: Session) -> None:
        now = self.clock()
        session.last_rule_at = session.last_change_at = now
        session.prev_thumb = None

    def _set_state(self, state: FarmState, message: str, wait_until: float | None = None) -> None:
        with self._status_lock:
            self._state = state
            self._message = message
            self._wait_until = wait_until

    def _device_status(self, ok: bool, error: str | None) -> None:
        if self.on_device_status is not None:
            self.on_device_status(ok, error)

    def _quietly(self, fn: Callable[[Device], Any]) -> None:
        try:
            self._with_device(fn)
        except DeviceError as exc:
            log.debug("Ignored device error: %s", exc)

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
        else:
            self._stop_event.wait(seconds)
