"""The bundled "Dokkan Skill Orbs" routine: from the home screen through the Event menu's Bonus
list to the stage, then the same grind as Repeat Stage."""

from __future__ import annotations

from pathlib import Path

import pytest

from dokkan_farmer.device.sim import Box, _button, _canvas, _text
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.vision import Matcher, TemplateStore
from test_clear_new_stages import Clock, QuestDevice
from test_repeat_stage import StageGame
from test_repeat_stage import capture_templates as capture_stage_templates

ROUTINE = "dokkan_skill_orbs"
HOME_START = Box(200, 800, 320, 100)
MENU_EVENT = Box(60, 300, 600, 120)
NAV = Box(300, 1050, 120, 90)
BANNER = Box(72, 442, 576, 147)
ENTRIES = 8


class SkillGame(StageGame):
    """Home → START menu → Event menu (a wheel of entries, the target's banner shows while it
    is selected) → the stage's event page → details → ... like Repeat Stage."""

    def __init__(self, stamina: int, boosts: int = 0, start: int = 5, target: int = 1) -> None:
        super().__init__(stamina, boosts)
        self.screen = "home"
        self.selected, self.target = start, target
        self.tab = None
        self.opened_other = False

    def render(self):
        s = self.screen
        if s == "home":
            img = _canvas((20, 60, 20))
            _button(img, HOME_START, "START", "start")
            return img
        if s == "menu":
            img = _canvas((50, 50, 20))
            _button(img, MENU_EVENT, "EVENT", "retry")
            return img
        if s == "eventmenu":
            img = _canvas((30, 30, 60))
            _button(img, NAV, "STONE", "danger")
            if self.selected == self.target:
                _button(img, BANNER, "GOD LEVEL TRAINING", "retry")
            else:
                _text(img, f"OTHER {self.selected}", (200, 520), 1.5)
            return img
        if s == "other":
            return _canvas((90, 20, 20))
        return super().render()

    def tap(self, x: float, y: float) -> None:
        s = self.screen
        if s == "home":
            if HOME_START.contains(x, y):
                self.screen = "menu"
        elif s == "menu":
            if MENU_EVENT.contains(x, y):
                self.screen = "eventmenu"
        elif s == "eventmenu":
            if 690 <= y <= 700 and x < 300:  # the Bonus tab, at 0.3 x 0.544
                self.tab = "bonus"
            elif 800 <= y <= 920:  # the selected entry
                if self.selected == self.target:
                    self.screen = "event"
                else:
                    self.screen, self.opened_other = "other", True
        else:
            super().tap(x, y)

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen == "eventmenu":
            step = -1 if y1 < y2 else 1  # a swipe down moves the wheel up the list
            self.selected = min(max(self.selected + step, 0), ENTRIES - 1)

    def back(self) -> None:
        if self.screen == "other":
            self.screen = "eventmenu"


def capture_templates(store: TemplateStore) -> None:
    capture_stage_templates(store)
    game = SkillGame(stamina=0, target=1, start=1)
    for screen, name, box in (
        ("home", "home_screen", HOME_START),
        ("menu", "menu_event", MENU_EVENT),
        ("eventmenu", "navigator_open", NAV),
        ("eventmenu", "skill_orb_banner", BANNER),
    ):
        game.screen = screen
        store.create_from_frame(
            name, game.render(), box.as_tuple(), threshold=0.9, search_full=True
        )


@pytest.fixture
def play(tmp_path: Path):
    def run(game: SkillGame, options: dict | None = None, hours: float = 3) -> dict:
        settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
        store = TemplateStore(settings.templates_dir)
        capture_templates(store)
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
        end = clock.t + hours * 3600
        return farmer.run_session(
            ROUTINE, routine, routine.option_values(options or {}), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)
    required = {n for n, doc in routine.templates.items() if doc.level == "required"}
    assert {"skill_orb_banner", "stage_details", "start_button"} <= required


def test_walks_from_the_home_screen_to_the_stage_and_clears_it(play):
    game = SkillGame(stamina=1000, boosts=2, start=5, target=1)  # the wheel starts further down
    summary = play(game, {"max_clears": 3, "boost": "on", "claim_missions": "no"})
    assert game.tab == "bonus" and not game.opened_other
    assert game.clears == 3 and game.boosted_runs == 2 and not summary["error"]


def test_finds_the_stage_when_the_wheel_starts_above_it(play):
    game = SkillGame(stamina=1000, start=0, target=4)
    play(game, {"max_clears": 1, "claim_missions": "no"})
    assert game.clears == 1 and not game.opened_other


def test_refills_with_one_meat_and_accepts_the_missions(play):
    game = SkillGame(stamina=100, start=1, target=1)
    summary = play(game, {"max_clears": 3, "boost": "off"})
    assert game.clears == 3 and game.refills == 1 and game.meat_used == 1
    assert game.stones_used == 0 and game.missions_accepted
    assert "mission rewards accepted" in summary["reason"]


def test_stops_when_the_stage_is_not_in_the_list_today(play):
    game = SkillGame(stamina=1000, start=3, target=99)
    summary = play(game, {"max_scrolls": 4})
    assert "not in the Event menu" in summary["reason"] and not game.opened_other


def test_a_screen_nobody_knows_ends_in_a_stop_instead_of_a_loop(play):
    game = SkillGame(stamina=1000)
    game.screen = "other"
    game.back = lambda: None  # Back does not help here
    summary = play(game, hours=1)
    assert "Lost on a screen" in summary["reason"]
