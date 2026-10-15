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


def test_stop_after_runs(make_sim):
    env = make_sim()
    summary = env.run(minutes=30, options={"max_runs": 3})
    assert summary["runs"] == 3
    assert "reached 3 runs" in summary["reason"]
    assert env.clock.t - 1_000_000 < 30 * 60  # stopped early


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
