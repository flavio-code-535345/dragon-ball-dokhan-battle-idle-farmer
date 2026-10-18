"""The bundled "Dokkan Extreme Z-Battle" routine, played against a small fake event."""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from dokkan_farmer.device.sim import Box, _button, _canvas, _text
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
BACK = Box(20, 1170, 140, 90)
HOME_START = Box(240, 800, 240, 140)
MENU_EVENT = Box(200, 380, 320, 100)
TABS = Box(20, 150, 400, 70)
Z_TAB = Box(560, 150, 140, 70)  # (0.89, 0.14) of 720x1280 is inside
RINGS = [(153, 1058, 79), (355, 1034, 75), (565, 1009, 67)]  # the three team slots
RING_BGR = {"str": (40, 40, 220), "int": (190, 50, 150), "agl": (220, 120, 30)}
ARROWS = Box(216, 1024, 72, 58)
BADGE = Box(30, 40, 90, 60)
SKIP = Box(621, 721, 72, 74)
SLOTS = 2  # banners in view
BANNER_TOP, BANNER_STEP = 260, 260


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


class EventsGame(ExtremeGame):
    """The same event pages, reached from the home screen through the Z-Battle list.

    ``events`` are the strongest level each team can beat, in list order; ``nexts`` are the
    levels the banners say are next (default: all fresh, level 1).
    """

    def __init__(self, events: list[int], nexts: list[int] | None = None, screen="home") -> None:
        super().__init__(strongest=0)
        self.events = events
        self.next_levels = list(nexts or [1] * len(events))
        self.screen, self.tab, self.offset, self.current = screen, "story", 0, 0
        self.played: list[list[int]] = [[] for _ in events]

    @staticmethod
    def banner(slot: int) -> Box:
        return Box(40, BANNER_TOP + slot * BANNER_STEP, 640, 200)

    @staticmethod
    def label(slot: int) -> Box:
        return Box(60, BANNER_TOP + slot * BANNER_STEP + 120, 300, 60)

    def render(self):
        if self.screen in ("home", "menu", "list"):
            img = _canvas((30, 30, 50))
            if self.screen == "home":
                _button(img, HOME_START, "START", "start")
            elif self.screen == "menu":
                _button(img, MENU_EVENT, "EVENT", "nav")
            else:
                _button(img, TABS, "STORY BONUS GROWTH", "nav")
                _button(img, Z_TAB, "Z-BATTLE", "nav")
                if self.tab == "z":
                    for slot in range(SLOTS):
                        i = self.offset + slot
                        if i < len(self.events):
                            _button(img, self.banner(slot), f"EVENT {i}", "ok")
                            _text(img, f"NEXT Lv. {self.next_levels[i]}", self.label(slot).center)
            return img
        img = super().render()
        if self.screen in ("stage", "team"):
            _button(img, BACK, "<<<", "nav")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "home":
            if HOME_START.contains(x, y):
                self.screen = "menu"
        elif self.screen == "menu":
            if MENU_EVENT.contains(x, y):
                self.screen, self.tab = "list", "story"
        elif self.screen == "list":
            if Z_TAB.contains(x, y):
                self.tab = "z"
            elif self.tab == "z":
                for slot in range(SLOTS):
                    i = self.offset + slot
                    if i < len(self.events) and self.banner(slot).contains(x, y):
                        self.current, self.strongest = i, self.events[i]
                        self.level, self.screen = self.next_levels[i], "stage"
        elif self.screen in ("stage", "team") and BACK.contains(x, y):
            self.screen = "list" if self.screen == "stage" else "stage"
        else:
            before = len(self.won)
            super().tap(x, y)
            if len(self.won) > before:
                self.played[self.current].append(self.won[-1])
            self.next_levels[self.current] = self.level

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen == "list" and self.tab == "z":
            step = 1 if y1 > y2 else -1
            self.offset = max(0, min(len(self.events) - SLOTS, self.offset + step))


class OrderGame(ExtremeGame):
    """One battle on the planning screen: three ringed units, swap arrows, the enemy's badge.

    ``order`` are the unit types of the slots (a swipe onto the next slot swaps the two);
    ``order_at_tap`` records the order whenever the ki spheres are tapped."""

    def __init__(self, order: list[str], animating: bool = False) -> None:
        super().__init__(strongest=99)
        self.order, self.screen = order, "animation" if animating else "battle"
        self.order_at_tap: list[list[str]] = []
        self.skipped = 0

    def render(self):
        if self.screen not in ("battle", "animation"):
            return super().render()
        img = _canvas((60, 40, 30))
        _button(img, ITEM, "ITEM", "nav")
        _button(img, BADGE, "PHY", "danger")
        for (cx, cy, r), kind in zip(RINGS, self.order, strict=True):
            cv2.circle(img, (cx, cy), r - 5, RING_BGR[kind], 10)
        if self.screen == "battle":
            _button(img, ARROWS, "<>", "ok")
            _button(img, SPHERE, "KI", "auto_on")
        else:
            _button(img, SKIP, "FF", "start")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "animation" and SKIP.contains(x, y):
            self.skipped += 1
            self.screen = "battle"
        elif self.screen == "battle" and SPHERE.contains(x, y):
            self.order_at_tap.append(list(self.order))
            super().tap(x, y)
        elif self.screen not in ("battle", "animation"):
            super().tap(x, y)

    def swipe_xy(self, x1: float, y1: float, x2: float, y2: float) -> None:
        if self.screen != "battle" or y1 < 1000:
            return
        slot = lambda x: min(range(3), key=lambda i: abs(RINGS[i][0] - x))  # noqa: E731
        a, b = slot(x1), slot(x2)
        if abs(a - b) == 1:
            self.order[a], self.order[b] = self.order[b], self.order[a]


class OrderDevice(QuestDevice):
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.game.swipe_xy(x1, y1, x2, y2)


def capture_templates(store: TemplateStore, events: bool = False, battle: bool = False) -> None:
    game = EventsGame([9]) if events else ExtremeGame(strongest=9)
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
    if battle:
        game = OrderGame(["agl", "agl", "agl"])
        store.create_from_frame("xb_planning", game.render(), ARROWS.as_tuple(), threshold=0.9)
        store.create_from_frame("enemy_phy", game.render(), BADGE.as_tuple(), threshold=0.9)
        game.screen = "animation"
        store.create_from_frame("xb_skip", game.render(), SKIP.as_tuple(), threshold=0.9)
    if not events:
        return
    for screen, name, box in [
        ("home", "home_screen", HOME_START),
        ("menu", "menu_event", MENU_EVENT),
        ("list", "xb_tabs", Box(TABS.x, TABS.y, 300, TABS.h)),
    ]:
        game.screen = screen
        store.create_from_frame(name, game.render(), box.as_tuple(), threshold=0.9)
    game.screen = "stage"
    store.create_from_frame("back_button", game.render(), BACK.as_tuple(), threshold=0.9)
    game.screen, game.tab = "list", "z"
    label = game.label(0)
    box = Box(label.x + 35, label.y, 230, label.h)
    store.create_from_frame(
        "xb_lv1", game.render(), box.as_tuple(), threshold=0.9, search_full=True
    )


@pytest.fixture
def play(tmp_path: Path):
    def run(game: ExtremeGame, options: dict | None = None, minutes: int = 60) -> dict:
        settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
        store = TemplateStore(settings.templates_dir)
        capture_templates(store, isinstance(game, EventsGame), isinstance(game, OrderGame))
        library = RoutineLibrary(settings.routines_dir)
        library.install_defaults()
        routine = library.load(ROUTINE)
        clock = Clock()
        farmer = Farmer(
            (OrderDevice if isinstance(game, OrderGame) else QuestDevice)(game),
            Matcher(store, settings.match_scale),
            settings,
            clock=clock,
            wall=clock,
            sleeper=clock.sleep,
        )
        end = clock.t + minutes * 60
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


def test_plays_every_fresh_event_then_stops(play):
    # The third event was played before (next level 4): it is skipped.
    game = EventsGame([2, 1, 99], nexts=[1, 1, 4])
    summary = play(game, {"events": "all", "tries": 2}, minutes=240)
    assert game.played == [[1, 2], [1], []]
    assert "No Extreme Z-Battle event with a fresh level 1 is left" in summary["reason"]
    assert game.next_levels == [3, 2, 4]


def test_finds_events_further_down_the_list(play):
    game = EventsGame([1, 1, 1, 1], nexts=[3, 3, 3, 1], screen="list")
    summary = play(game, {"events": "all", "tries": 2}, minutes=240)
    assert game.played == [[], [], [], [1]]
    assert "No Extreme Z-Battle event with a fresh level 1 is left" in summary["reason"]


def test_stops_when_an_event_cannot_be_started(play):
    game = EventsGame([0, 5])
    summary = play(game, {"events": "all", "tries": 2}, minutes=240)
    assert game.played == [[], []] and game.lost == [1, 1]
    assert "An event was lost 2 times at its first level" in summary["reason"]


def test_swaps_the_strong_unit_to_the_front(play):
    game = OrderGame(["int", "agl", "str"])
    play(game, {"smart_order": "by_type", "max_battles": 1})
    assert game.order_at_tap[0] == ["str", "int", "agl"]


def test_moves_a_unit_the_enemy_beats_out_of_the_front(play):
    game = OrderGame(["int", "agl", "agl"])
    play(game, {"smart_order": "by_type", "max_battles": 1})
    assert game.order_at_tap[0] == ["agl", "int", "agl"]


def test_leaves_the_order_alone_by_default_or_when_it_is_fine(play):
    game = OrderGame(["int", "agl", "str"])
    play(game, {"max_battles": 1})
    assert game.order_at_tap[0] == ["int", "agl", "str"]
    game = OrderGame(["str", "int", "agl"])
    play(game, {"smart_order": "by_type", "max_battles": 1})
    assert game.order_at_tap[0] == ["str", "int", "agl"]


def test_fast_forwards_the_animations(play):
    game = OrderGame(["agl", "agl", "agl"], animating=True)
    play(game, {"max_battles": 1})
    assert game.skipped >= 1
