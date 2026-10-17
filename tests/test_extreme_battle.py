"""The bundled "Dokkan Extreme Z-Battle" routine, played against a small fake event."""

from __future__ import annotations

from pathlib import Path

import pytest

from dokkan_farmer.device.sim import Box, _button, _canvas
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.vision import Matcher, TemplateStore
from test_clear_new_stages import Clock, QuestDevice

ROUTINE = "dokkan_extreme_battle"
FIGHT = Box(180, 700, 360, 120)
START = Box(380, 1150, 300, 100)
ITEM = Box(560, 1100, 140, 80)
SPHERE = Box(240, 520, 200, 200)  # (0.47, 0.47) of 720x1280 is inside
TITLE = Box(60, 60, 600, 100)
OK = Box(240, 1180, 240, 80)
TAPS_TO_WIN = 3


class ExtremeGame:
    """Stage page → team page → battle → results → friend popup → next level."""

    def __init__(self, strongest: int) -> None:
        self.strongest = strongest  # levels above it are lost
        self.level, self.screen, self.taps = 1, "stage", 0
        self.won: list[int] = []
        self.lost: list[int] = []

    def render(self):
        img = _canvas((30, 30, 50))
        if self.screen == "stage":
            _button(img, FIGHT, f"FIGHT STA 0 LV {self.level}", "start")
        elif self.screen == "team":
            _button(img, START, "START", "ok")
        elif self.screen == "battle":
            _button(img, ITEM, "ITEM", "nav")
            _button(img, SPHERE, "KI", "auto_on")
        elif self.screen == "congrats":
            _button(img, TITLE, "CONGRATULATIONS", "danger")
        elif self.screen in ("results", "friend"):
            _button(img, OK, "OK", "ok")
        elif self.screen == "defeat":
            _button(img, TITLE, "DEFEATED", "no")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "stage" and FIGHT.contains(x, y):
            self.screen = "team"
        elif self.screen == "team" and START.contains(x, y):
            self.screen, self.taps = "battle", 0
        elif self.screen == "battle" and SPHERE.contains(x, y):
            self.taps += 1
            if self.taps >= TAPS_TO_WIN:
                if self.level <= self.strongest:
                    self.won.append(self.level)
                    self.screen = "congrats"
                else:
                    self.lost.append(self.level)
                    self.screen = "defeat"
        elif self.screen == "congrats":
            self.screen = "results"
        elif self.screen == "results" and OK.contains(x, y):
            self.screen = "friend"
        elif self.screen == "friend" and OK.contains(x, y):
            self.level += 1
            self.screen = "next"
        elif self.screen in ("next", "defeat"):
            self.screen = "stage"

    def swipe(self, y1: float, y2: float) -> None:
        pass

    def back(self) -> None:
        pass


def capture_templates(store: TemplateStore) -> None:
    game = ExtremeGame(strongest=9)
    for screen, name, box in [
        ("stage", "xb_fight", FIGHT),
        ("team", "xb_start", START),
        ("battle", "xb_battle", ITEM),
        ("congrats", "xb_congrats", TITLE),
    ]:
        game.screen = screen
        store.create_from_frame(name, game.render(), box.as_tuple(), threshold=0.9)
    game.screen = "results"
    store.create_from_frame("ok_button", game.render(), OK.as_tuple(), search_full=True)


@pytest.fixture
def play(tmp_path: Path):
    def run(game: ExtremeGame, options: dict | None = None) -> dict:
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
        end = clock.t + 60 * 60
        return farmer.run_session(
            ROUTINE, routine, routine.option_values(options or {}), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)


def test_plays_level_after_level_up_to_the_limit(play):
    game = ExtremeGame(strongest=99)
    summary = play(game, {"max_battles": 3})
    assert game.won == [1, 2, 3] and not game.lost and not summary["error"]
    assert summary["runs"] == 3


def test_stops_when_a_level_is_too_hard(play):
    game = ExtremeGame(strongest=2)
    summary = play(game, {"max_battles": 0, "tries": 2})
    assert game.won == [1, 2] and game.lost == [3, 3]
    assert "Lost 2 battles in a row after 2 won" in summary["reason"]
