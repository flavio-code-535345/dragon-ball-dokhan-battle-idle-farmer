from __future__ import annotations

import time
from datetime import datetime

import pytest

from dokkan_farmer.device.sim import B, SimConfig
from dokkan_farmer.engine import Farmer, FarmState, FrameStore, to_pixels
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.vision import Matcher


def test_farm_loop_counts_runs_and_handles_popups(make_sim):
    env = make_sim()
    summary = env.run(minutes=6)
    game = env.game
    assert game.auto, "the farmer should have turned Auto on"
    assert summary["runs"] >= 8
    # A run counts when the results screen appears after a new Start: never more than clears.
    assert game.clears - 1 <= summary["runs"] <= game.clears
    assert "friend" in game.history  # friend requests happened...
    assert game.stones == 5 and game.items == 2  # ...and nothing was spent
    assert not summary["error"]
    assert env.state.get("lifetime")["runs"] == summary["runs"]
    assert env.state.get("was_running") is False


def test_taps_land_inside_the_buttons(make_sim):
    env = make_sim()
    env.run(minutes=3)
    button = B["attempt_again"]
    attempt_taps = [t for t in env.device.taps if button.y - 5 <= t[1] <= button.y + button.h + 5]
    assert attempt_taps
    assert all(button.contains(x, y) for x, y in attempt_taps)


def test_taps_go_to_the_templates_own_tap_point(make_sim):
    env = make_sim()
    meta = env.store.get("attempt_again")[0]
    meta.tap = [0.0, 1.0]  # one button height below the button's centre
    env.store.version += 1
    env.run(minutes=1)
    button = B["attempt_again"]
    below = [t for t in env.device.taps if button.contains(t[0], t[1] - button.h)]
    assert below, "a tap one button height below Attempt Again"
    # (A tap in the button itself would also be the home screen's Events button.)


def test_stop_after_runs(make_sim):
    env = make_sim()
    summary = env.run(minutes=30, options={"max_runs": 3})
    assert summary["runs"] == 3
    assert "reached 3 runs" in summary["reason"]
    assert env.clock.t - 1_000_000 < 30 * 60  # stopped early


def test_soft_stop_finishes_the_run_in_progress(make_sim):
    env = make_sim()
    farmer = env.make_farmer()
    asked: list[int] = []

    def ask_once() -> None:
        session = farmer._session
        if not asked and session is not None and session.runs >= 2:
            asked.append(session.runs)
            assert farmer.soft_stop() is True
            assert farmer.snapshot()["soft_stop"] is True

    summary = env.run(minutes=30, farmer=farmer, on_tick=ask_once)
    assert asked, "the soft stop was asked for"
    # Stopped by the next finished run, not at once and not never.
    assert summary["runs"] == asked[0] + 1
    assert summary["reason"].startswith("Soft stop: the run in progress is finished")
    assert not summary["error"]
    assert env.clock.t - 1_000_000 < 30 * 60
    assert farmer.snapshot()["state"] == "stopped"
    assert farmer.soft_stop() is False  # no session any more


def test_a_cancelled_soft_stop_goes_on(make_sim):
    env = make_sim()
    farmer = env.make_farmer()
    steps: list[str] = []

    def toggle() -> None:
        session = farmer._session
        if session is None:
            return
        if not steps and session.runs >= 1:
            steps.append("asked")
            farmer.soft_stop()
        elif steps == ["asked"]:
            steps.append("cancelled")
            assert farmer.soft_stop(False) is False
            assert farmer.snapshot()["soft_stop"] is False

    summary = env.run(minutes=8, farmer=farmer, on_tick=toggle)
    assert steps == ["asked", "cancelled"]
    assert summary["runs"] >= 3 and "Soft stop" not in summary["reason"]


def test_soft_stop_while_waiting_stops_at_once(make_sim):
    env = make_sim(SimConfig(act_max=10, act_cost=10, act_regen_seconds=10_000))
    farmer = env.make_farmer()
    seen: list[str] = []

    sleep = farmer._sleeper

    def sleep_and_ask(seconds: float) -> None:
        # The wait is inside one tick: ask from the sleep itself.
        if farmer.snapshot()["state"] == "waiting" and not seen:
            seen.append("waiting")
            farmer.soft_stop()
        sleep(seconds)

    farmer._sleeper = sleep_and_ask
    summary = env.run(minutes=30, options={"act_wait_minutes": 20}, farmer=farmer)
    assert seen and summary["reason"] == "Soft stop: nothing was in progress"
    assert env.clock.t - 1_000_000 < 30 * 60


def test_act_wait_policy(make_sim):
    env = make_sim(SimConfig(act_max=20, act_cost=10, act_regen_seconds=120))
    summary = env.run(minutes=30, options={"act_wait_minutes": 10})
    assert summary["counters"].get("act_waits", 0) >= 1
    assert summary["runs"] >= 3
    assert env.game.stones == 5 and env.game.items == 2


def test_act_stone_policy_respects_the_limit(make_sim):
    env = make_sim(SimConfig(act_max=10, act_cost=10, act_regen_seconds=10_000))
    summary = env.run(
        minutes=20,
        options={"act_policy": "stone", "max_stone_refills": 2, "act_wait_minutes": 5},
    )
    assert env.game.stones == 3, "exactly two Dragon Stones may be spent"
    assert summary["counters"]["stone_refills"] == 2
    assert summary["counters"].get("act_waits", 0) >= 1  # then it falls back to waiting


def test_act_item_policy(make_sim):
    env = make_sim(SimConfig(act_max=10, act_cost=10, act_regen_seconds=10_000))
    summary = env.run(minutes=10, options={"act_policy": "item", "act_wait_minutes": 5})
    assert env.game.items == 0
    assert summary["counters"]["item_refills"] == 2
    assert env.game.stones == 5


def test_act_stop_policy(make_sim):
    env = make_sim(SimConfig(act_max=20, act_cost=10, act_regen_seconds=10_000))
    summary = env.run(minutes=30, options={"act_policy": "stop"})
    assert summary["reason"].startswith("Out of ACT")
    assert summary["runs"] == 2


def test_defeat_gives_up_and_stops_without_paying(make_sim):
    env = make_sim(SimConfig(defeat_chance=1.0))
    summary = env.run(minutes=10)
    assert "defeated" in summary["reason"]
    assert env.game.revives == 0 and env.game.stones == 5
    assert env.game.defeats == 1


def test_defeat_can_continue(make_sim):
    env = make_sim(SimConfig(defeat_chance=0.5, seed=3))
    summary = env.run(minutes=15, options={"on_defeat": "continue"})
    assert env.game.defeats >= 1
    assert env.game.revives == 0 and env.game.stones == 5
    assert summary["runs"] >= 1
    assert summary["counters"]["defeats"] == env.game.defeats


def test_connection_errors_are_retried(make_sim):
    env = make_sim(SimConfig(error_chance=1.0))
    summary = env.run(minutes=5)
    assert "error" in env.game.history
    assert summary["runs"] >= 5


def test_crash_is_detected_and_the_game_is_navigated_back(make_sim):
    env = make_sim()
    crashed = {"done": False}

    def crash_after_two_runs() -> None:
        if not crashed["done"] and env.game.clears >= 2 and env.game.screen == "team":
            env.game.crash()
            crashed["done"] = True

    summary = env.run(minutes=12, on_tick=crash_after_two_runs)
    assert crashed["done"]
    history = env.game.history
    for screen in ("title", "news", "home", "events", "stages", "difficulty", "support"):
        assert screen in history
    assert summary["runs"] >= 8


def test_crash_mid_battle_resumes_the_quest(make_sim):
    env = make_sim()
    crashed = {"done": False}

    def crash_in_battle() -> None:
        if not crashed["done"] and env.game.clears >= 1 and env.game.screen == "battle":
            env.game.crash()
            crashed["done"] = True

    summary = env.run(minutes=8, on_tick=crash_in_battle)
    assert crashed["done"]
    assert "resume" in env.game.history
    assert summary["runs"] >= 5


def test_stuck_screen_escalates_to_restart_and_gives_up(make_sim):
    env = make_sim()
    text = env.library.read_text("dokkan_auto_farm")
    env.library.save_text(
        "dokkan_auto_farm", text.replace("static_seconds: 180", "static_seconds: 30")
    )
    env.store.delete("nav_1")  # nothing on the home screen is known any more
    env.game.screen = "home"
    summary = env.run(minutes=120)
    assert summary["error"]
    assert summary["reason"].startswith("Gave up")
    assert summary["restarts"] == 4
    assert summary["recoveries"] >= 8
    assert "BACK" in env.device.keys
    shots = list(env.settings.screenshots_dir.glob("*.jpg"))
    assert shots, "stuck screens are saved for debugging"


def test_device_disconnect_reconnects(make_sim):
    env = make_sim()
    start = env.clock.t
    farmer = env.make_farmer()
    statuses = []
    farmer.on_device_status = lambda ok, err: statuses.append(ok)

    def flaky() -> None:
        env.device.reachable = not (start + 60 <= env.clock.t < start + 240)

    summary = env.run(minutes=8, farmer=farmer, on_tick=flaky)
    assert False in statuses and statuses[-1] is True
    assert summary["runs"] >= 5


def test_missing_required_templates_refuse_to_start(make_sim):
    env = make_sim()
    env.store.delete("start_button")
    summary = env.run(minutes=1)
    assert summary["error"]
    assert "start_button" in summary["reason"]
    assert env.device.taps == []


def test_rule_without_effect_is_paused_so_fallbacks_run(make_sim):
    env = make_sim()
    routine = parse_routine(
        """
name: Stubborn
rules:
  - {name: Useless, when: {visible: attempt_again}, do: [{tap_xy: [0.5, 0.04]}]}
  - {name: Useful, when: {visible: attempt_again}, do: [{tap: attempt_again}]}
"""
    )
    farmer = env.make_farmer()
    end = env.clock.t + 60
    farmer.run_session(
        "stubborn", routine, {}, until=lambda: env.game.screen != "results" or env.clock.t >= end
    )
    assert env.game.screen == "act_confirm"


def test_some_missing_templates_only_skip_their_rules(make_sim):
    env = make_sim()
    env.store.delete("friend_request")
    farmer = env.make_farmer()
    summary = env.run(minutes=6, farmer=farmer)
    # Without the friend popup template the farmer still progresses: the popup's
    # OK button is a generic ok_button and gets confirmed.
    assert summary["runs"] >= 8


def test_outside_active_hours_nothing_happens(make_sim):
    env = make_sim(active_hours="01:00-02:00")
    farmer = env.make_farmer(now=lambda: datetime(2026, 1, 1, 12, 0))
    env.run(minutes=5, farmer=farmer)
    assert env.device.taps == []


def test_edge_counter_without_rearm_counts_appearances(make_sim):
    env = make_sim()
    routine = parse_routine(
        """
name: Count clears
counters:
  results: {appears: attempt_again}
rules:
  - {name: Again, when: {visible: attempt_again}, do: [{tap: attempt_again}], cooldown: 5}
  - {name: OK, when: {visible: ok_button, not_visible: attempt_again}, do: [{tap: ok_button}]}
  - {name: Start, when: {visible: start_button}, do: [{tap: start_button}]}
  - {name: Auto, when: {visible: auto_off}, do: [{tap: auto_off}]}
  - {name: Cancel, when: {visible: friend_request}, do: [{tap: cancel_button}]}
  - {name: Continue, when: {static_for: 4}, do: [{tap_xy: [0.5, 0.15]}], cooldown: 3}
settings:
  run_counter: results
"""
    )
    farmer = env.make_farmer()
    end = env.clock.t + 300
    summary = farmer.run_session("custom", routine, {}, until=lambda: env.clock.t >= end)
    assert summary["runs"] >= 5
    assert abs(summary["runs"] - env.game.clears) <= 1


def test_threaded_start_pause_stop(make_sim):
    env = make_sim(SimConfig(battle_seconds=1.0))
    farmer = Farmer(env.device, Matcher(env.store), env.settings, state=env.state)
    routine = env.routine
    farmer.start("dokkan_auto_farm", routine, {"continue_after": 2})
    with pytest.raises(RuntimeError):
        farmer.start("dokkan_auto_farm", routine, {})
    deadline = time.monotonic() + 10
    while farmer.snapshot()["state"] != FarmState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.05)
    farmer.pause()
    deadline = time.monotonic() + 10
    while farmer.snapshot()["state"] != FarmState.PAUSED and time.monotonic() < deadline:
        time.sleep(0.05)
    assert farmer.snapshot()["state"] == FarmState.PAUSED
    farmer.resume()
    farmer.stop()
    assert not farmer.is_running
    snapshot = farmer.snapshot()
    assert snapshot["state"] == FarmState.STOPPED
    assert snapshot["last_session"]["reason"] == "Stopped by user"
    assert farmer.frames.latest_id() is not None


def test_shutdown_keeps_resume_flag(make_sim):
    env = make_sim()
    farmer = Farmer(env.device, Matcher(env.store), env.settings, state=env.state)
    farmer.start("dokkan_auto_farm", env.routine, {})
    time.sleep(0.3)
    farmer.stop("Container stopped", resume_later=True)
    assert env.state.get("was_running") is True


def test_frame_store_pins_frames():
    import numpy as np

    store = FrameStore(keep=2)
    first = store.add(np.zeros((4, 4, 3), np.uint8), 0)
    assert store.pin(first)
    for _ in range(5):
        store.add(np.zeros((4, 4, 3), np.uint8), 0)
    assert store.get(first) is not None  # pinned frames survive
    assert store.get(first + 1) is None
    assert store.jpeg()[1][:2] == b"\xff\xd8"
    assert not store.pin(999)


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [(0.5, 0.5, (360, 640)), (1.0, 1.0, (719, 1279)), (100, 200, (100, 200)), (5000, -3, (719, 0))],
)
def test_to_pixels(x, y, expected):
    assert to_pixels(x, y, 720, 1280) == expected


def test_ui_device_actions_reconnect_once(make_sim):
    from dokkan_farmer.device.base import DeviceError
    from dokkan_farmer.device.sim import SimDevice

    class DroppedConnection(SimDevice):
        """Like a network phone after a restart: commands fail until `adb connect`."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.connected = False
            self.connects = 0

        def connect(self):
            self.connects += 1
            self.connected = True

        def screenshot(self):
            if not self.connected:
                raise DeviceError("device '192.168.1.50:5555' not found")
            return super().screenshot()

    env = make_sim()
    device = DroppedConnection(env.game, package=env.settings.game_package)
    statuses = []
    farmer = Farmer(
        device,
        Matcher(env.store),
        env.settings,
        on_device_status=lambda ok, err: statuses.append(ok),
    )
    assert farmer.capture() is not None
    assert device.connects == 1 and statuses[-1] is True
    farmer.capture()  # still connected: no second connect needed
    assert device.connects == 1


# --------------------------------------------------------------- power saving
class ScreenLog:
    """Records the farmer's screen-off and screen-on calls on the simulator."""

    def __init__(self, device):
        self.calls = []
        sleep, wake = device.sleep_screen, device.wake_screen
        device.sleep_screen = lambda: (self.calls.append("off"), sleep())[1]
        device.wake_screen = lambda: (self.calls.append("on"), wake())[1]


def power_farmer(env, **kwargs):
    """A farmer whose only way to wake the screen is power saving's own (no dark-frame WAKEUP)."""
    farmer = env.make_farmer(**kwargs)
    farmer._wake_if_dark = lambda session, frame: None
    return farmer


def wait_routine(action: str):
    return parse_routine(f"""
name: Waits
rules:
  - name: Wait
    when: {{always: true}}
    do: [{action}]
""")


def test_power_save_turns_the_screen_off_while_act_regenerates(make_sim):
    env = make_sim(SimConfig(act_max=20, act_cost=10, act_regen_seconds=120), power_save=True)
    screen = ScreenLog(env.device)
    farmer = power_farmer(env)
    summary = env.run(minutes=40, options={"act_wait_minutes": 10}, farmer=farmer)
    waits = summary["counters"].get("act_waits", 0)
    assert waits >= 2
    assert screen.calls == ["off", "on"] * waits  # off for each wait, on again after it
    assert summary["runs"] >= 3  # with 2+ waits: farming went on after waking
    assert not farmer.screen_off and not env.game.screen_off


def test_short_waits_keep_the_screen_on(make_sim):
    env = make_sim(SimConfig(act_max=20, act_cost=10, act_regen_seconds=120), power_save=True)
    screen = ScreenLog(env.device)
    summary = env.run(minutes=20, options={"act_wait_minutes": 1}, farmer=power_farmer(env))
    assert summary["counters"].get("act_waits", 0) >= 1 and screen.calls == []


def test_only_wait_minutes_turns_the_screen_off(make_sim):
    env = make_sim(power_save=True)
    screen = ScreenLog(env.device)
    farmer = power_farmer(env)
    # A plain wait may be letting the game play (Auto Mode): never with the screen off.
    farmer.run_session("w", wait_routine("{wait: 600}"), {}, max_ticks=2)
    assert screen.calls == []
    farmer.run_session("w", wait_routine("{wait_minutes: 5}"), {}, max_ticks=2)
    assert screen.calls == ["off", "on", "off", "on"]


def test_no_power_save_keeps_the_screen_on(make_sim):
    env = make_sim(SimConfig(act_max=20, act_cost=10, act_regen_seconds=120), power_save=False)
    screen = ScreenLog(env.device)
    env.run(minutes=20, options={"act_wait_minutes": 10})
    assert screen.calls == []


def test_power_save_outside_active_hours(make_sim):
    env = make_sim(active_hours="01:00-02:00", power_save=True)
    hour = [12]
    farmer = power_farmer(env, now=lambda: datetime(2026, 1, 1, hour[0], 30))
    screen = ScreenLog(env.device)
    start = env.clock.t
    touched = []

    def on_tick():
        elapsed = env.clock.t - start
        if elapsed >= 120 and not touched:  # someone taps in the Screen tab
            touched.append(farmer.note_use(touch=True))
        if 0 < elapsed < 300:  # (from the first tick on)
            assert farmer.screen_off == (not touched)  # stays on after the touch, no re-off
        elif elapsed < 15 * 60:
            farmer.check_idle()  # the power-save thread; 10 min without use: off again
        elif hour[0] == 12:
            hour[0] = 1  # active hours begin

    env.run(minutes=20, farmer=farmer, on_tick=on_tick)
    assert touched == [True]
    assert screen.calls[:4] == ["off", "on", "off", "on"]
    assert env.device.taps, "farming started once active hours began"
    assert not farmer.screen_off


def test_idle_screen_off_and_waking_from_the_web_ui(make_sim):
    env = make_sim(power_save=True, screen_off_minutes=10)
    farmer = env.make_farmer()
    screen = ScreenLog(env.device)
    env.clock.sleep(9 * 60)
    farmer.check_idle()
    assert screen.calls == []  # not idle long enough
    farmer.mark_watched()  # an open live view
    env.clock.sleep(9 * 60)
    farmer.check_idle()
    assert screen.calls == []
    env.clock.sleep(2 * 60)
    farmer.check_idle()
    assert screen.calls == ["off"] and farmer.screen_off and env.game.screen_off
    assert farmer.snapshot()["screen_off"]
    farmer.check_idle()
    assert screen.calls == ["off"]  # only once
    assert farmer.note_use(touch=False)  # looking, no farm running: back on
    assert screen.calls == ["off", "on"] and not env.game.screen_off
    assert not farmer.note_use(touch=False)  # already on


def test_a_farm_waiting_with_the_screen_off_wakes_only_on_touch(make_sim, monkeypatch):
    env = make_sim(power_save=True)
    farmer = env.make_farmer()
    screen = ScreenLog(env.device)
    farmer.screen_sleep("test")
    monkeypatch.setattr(Farmer, "is_running", property(lambda self: True))
    farmer.note_use(touch=False)  # the live view while the farm waits: stays off
    assert screen.calls == ["off"]
    farmer.note_use(touch=True)
    assert screen.calls == ["off", "on"]
    env.clock.sleep(3600)
    farmer.check_idle()  # a farm at work never gets the idle screen-off
    assert screen.calls == ["off", "on"]
    farmer.pause()
    farmer._pause_event.set()  # is_running is faked: set the pause by hand
    farmer.check_idle()  # paused and nobody watching: off
    assert screen.calls == ["off", "on", "off"]


def test_a_wake_that_fails_is_tried_again(make_sim):
    from dokkan_farmer.device.base import DeviceError

    env = make_sim(power_save=True)
    farmer = env.make_farmer()
    farmer.screen_sleep("test")
    wake, fails = env.device.wake_screen, [2]

    def flaky_wake():
        if fails[0]:
            fails[0] -= 1
            raise DeviceError("device 'android:5555' not found")
        wake()

    env.device.wake_screen = flaky_wake
    assert not farmer.note_use(touch=True)  # failed (also after one reconnect)
    assert farmer.screen_off and farmer.snapshot()["screen_off"]  # the UI keeps its notice
    assert farmer.note_use(touch=True) and not farmer.screen_off and not env.game.screen_off


def test_power_save_is_automatic_for_the_emulator_only(make_sim):
    env = make_sim()  # POWER_SAVE=auto: the simulator is no emulator
    farmer = env.make_farmer()
    screen = ScreenLog(env.device)
    assert env.settings.power_save is None and not farmer.power_save
    farmer.screen_sleep("test")
    assert screen.calls == []
    type(env.device).is_emulator = property(lambda self: True)
    try:
        assert farmer.power_save
        farmer.screen_sleep("test")
        assert screen.calls == ["off"]
    finally:
        del type(env.device).is_emulator
