"""The bundled "Dokkan Collect Rewards" routine, played against a small fake home screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from dokkan_farmer.device.sim import WIDTH, Box, _button, _canvas
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.vision import Matcher, TemplateStore
from test_clear_new_stages import Clock, QuestDevice

ROUTINE = "dokkan_collect_rewards"
TITLE = Box(0, 0, WIDTH, 120)
MISSIONS = Box(40, 1100, 300, 100)
GIFTS = Box(380, 1100, 300, 100)
TABS = ("DAILY", "MAIN", "EVENT")
CLAIM = Box(220, 1000, 280, 100)
BACK = Box(20, 1160, 160, 90)
OK = Box(240, 780, 240, 90)


def tab_box(index: int) -> Box:
    return Box(20 + index * 230, 160, 210, 90)


class RewardsGame:
    """Home → Missions (tabs, Claim all, reward popup) and Present Box (Accept all, confirm)."""

    def __init__(self, claimable: set[int], presents: int = 0) -> None:
        self.claimable = set(claimable)  # tabs with finished missions
        self.presents = presents
        self.screen, self.tab, self.dialogs = "home", 0, 0
        self.claimed: list[str] = []

    def render(self):
        img = _canvas((40, 30, 30))
        if self.dialogs:
            _button(img, OK, "OK", "ok")
        elif self.screen == "home":
            _button(img, TITLE, "HOME", "nav")
            _button(img, MISSIONS, "MISSIONS", "start")
            _button(img, GIFTS, "GIFTS", "retry")
        elif self.screen == "missions":
            _button(img, TITLE, "MISSIONS", "auto_on")
            for i, name in enumerate(TABS):
                _button(img, tab_box(i), name, "danger" if i == self.tab else "nav")
            if self.tab in self.claimable:
                _button(img, CLAIM, "CLAIM ALL", "ok")
            _button(img, BACK, "BACK", "no")
        elif self.screen == "presents":
            _button(img, TITLE, "PRESENTS", "auto_off")
            if self.presents:
                _button(img, CLAIM, "ACCEPT ALL", "start")
            _button(img, BACK, "BACK", "no")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.dialogs:
            if OK.contains(x, y):
                self.dialogs -= 1
        elif self.screen == "home":
            if MISSIONS.contains(x, y):
                self.screen, self.tab = "missions", 0
            elif GIFTS.contains(x, y):
                self.screen = "presents"
        elif BACK.contains(x, y):
            self.screen = "home"
        elif self.screen == "missions":
            hit = [i for i in range(len(TABS)) if tab_box(i).contains(x, y)]
            if hit:
                self.tab = hit[0]
            elif CLAIM.contains(x, y) and self.tab in self.claimable:
                self.claimable.discard(self.tab)
                self.claimed.append(TABS[self.tab])
                self.presents += 1  # mission rewards land in the Present Box
                self.dialogs = 1
        elif self.screen == "presents" and CLAIM.contains(x, y) and self.presents:
            self.claimed.append("PRESENTS")
            self.presents = 0
            self.dialogs = 2  # "Accept all?" and the result

    def swipe(self, y1: float, y2: float) -> None:
        pass

    def back(self) -> None:
        if not self.dialogs:
            self.screen = "home"


def capture_templates(store: TemplateStore, *, back_button: bool = True) -> None:
    game = RewardsGame({0, 1, 2}, presents=1)
    shots = [
        ("home", 0, "home_screen", TITLE, False),
        ("home", 0, "home_missions", MISSIONS, False),
        ("home", 0, "home_presents", GIFTS, False),
        ("missions", 0, "missions_screen", TITLE, False),
        ("missions", 0, "claim_all", CLAIM, False),
        ("missions", 1, "mission_tab_1", tab_box(0), False),
        ("missions", 0, "mission_tab_2", tab_box(1), False),
        ("missions", 0, "mission_tab_3", tab_box(2), False),
        ("presents", 0, "present_screen", TITLE, False),
        ("presents", 0, "accept_all", CLAIM, False),
    ]
    if back_button:
        shots.append(("presents", 0, "back_button", BACK, False))
    for screen, tab, name, box, search_full in shots:
        game.screen, game.tab = screen, tab
        store.create_from_frame(
            name, game.render(), box.as_tuple(), threshold=0.9, search_full=search_full
        )
    game.dialogs = 1
    store.create_from_frame("ok_button", game.render(), OK.as_tuple(), search_full=True)


@pytest.fixture
def play(tmp_path: Path):
    def run(game: RewardsGame, back_button: bool = True) -> dict:
        settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
        store = TemplateStore(settings.templates_dir)
        capture_templates(store, back_button=back_button)
        library = RoutineLibrary(settings.routines_dir)
        library.install_defaults()
        routine = library.load(ROUTINE)
        clock = Clock()
        farmer = Farmer(
            QuestDevice(game),
            Matcher(store, settings.match_scale),
            settings,
            clock=clock,
            wall=clock,
            sleeper=clock.sleep,
        )
        end = clock.t + 30 * 60
        return farmer.run_session(
            ROUTINE, routine, routine.option_values({}), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)


@pytest.mark.parametrize("back_button", [True, False])
def test_claims_every_tab_then_the_present_box_and_stops(play, back_button):
    game = RewardsGame({0, 2}, presents=3)  # MAIN has nothing to claim
    summary = play(game, back_button)
    assert game.claimed == ["DAILY", "EVENT", "PRESENTS"]
    assert game.presents == 0 and game.screen == "home"
    assert "Rewards collected (3 claims)" in summary["reason"] and not summary["error"]


def test_nothing_to_collect_stops_quickly(play):
    game = RewardsGame(set())
    summary = play(game)
    assert game.claimed == [] and "Rewards collected (0 claims)" in summary["reason"]
