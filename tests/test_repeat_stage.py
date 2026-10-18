"""The bundled "Dokkan Repeat Stage" routine, played against a small fake event stage."""

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

ROUTINE = "dokkan_repeat_stage"
HEADER = Box(40, 340, 400, 40)
BAR = Box(20, 250, 680, 80)
BOOST = Box(410, 1050, 100, 70)
MISSION = Box(520, 1050, 180, 70)
TITLE = Box(180, 220, 360, 50)
ITEM_ROW = Box(60, 610, 500, 50)
ITEM_SELECT = Box(520, 760, 140, 60)
STONE_SELECT = Box(520, 370, 140, 60)
CANCEL = Box(200, 980, 320, 90)
USE_LABEL = Box(180, 600, 360, 40)
MINUS = Box(190, 660, 70, 70)
OK = Box(380, 820, 260, 80)
START = Box(380, 1150, 300, 100)
ITEM = Box(560, 1100, 140, 80)
SPHERE = Box(240, 520, 200, 200)
RESULT = Box(100, 300, 520, 100)
MTITLE = Box(200, 60, 320, 60)
ACCEPT = Box(380, 1000, 260, 90)
MCANCEL = Box(60, 1000, 260, 90)
EVENT_ROW = Box(40, 400, 640, 140)
ROW_HEADER = Box(50, 470, 360, 41)
TAPS_TO_WIN = 3
MEAT = 60  # stamina of one piece


class StageGame:
    """Details page → (refill dialogs) → team → battle → results → details; missions."""

    def __init__(self, stamina: int, boosts: int = 0) -> None:
        self.sta, self.boosts = stamina, boosts
        self.boost_on = False
        self.screen, self.taps, self.pieces = "details", 0, 2
        self.cost = 40
        self.clears = 0
        self.boosted_runs = 0
        self.refills = 0
        self.meat_used = 0
        self.stones_used = 0
        self.missions_accepted = False
        self.origin = "details"

    def render(self):
        img = _canvas((30, 60, 30))
        s = self.screen
        if s == "details":
            _button(img, HEADER, "CLEAR REWARD FEATURED", "nav")
            _button(img, BAR, "HARD 40", "retry")
            _button(img, BOOST, "BOOST ON" if self.boost_on else "BOOST OFF", self.boost_style)
            _button(img, MISSION, "MISSION", "ok")
        elif s == "event":
            _button(img, EVENT_ROW, "STAGE", "retry")
            _button(img, ROW_HEADER, "CLEAR REWARD FEATURED", "nav")
            _button(img, MISSION, "MISSION", "ok")
        elif s == "restore":
            _button(img, TITLE, "RESTORE STA", "no")
            _button(img, STONE_SELECT, "STONE SELECT", "danger")
            _button(img, ITEM_ROW, "RESTORE WITH ITEM", "nav")
            _button(img, ITEM_SELECT, "SELECT", "ok")
            _button(img, CANCEL, "CANCEL", "cancel")
        elif s == "items":
            _button(img, TITLE, "RESTORE STA", "no")
            _button(img, USE_LABEL, "ITEMS TO USE", "nav")
            _button(img, MINUS, "-", "start")
            _button(img, OK, "OK", "ok")
            _button(img, CANCEL, "CANCEL", "cancel")
        elif s in ("confirm", "restored", "accepted"):
            _button(img, OK, "OK", "ok")
        elif s == "team":
            _button(img, START, "START!", "danger")
        elif s == "battle":
            _button(img, ITEM, "ITEM", "nav")
            _button(img, SPHERE, "KI", "auto_on")
        elif s == "result":
            _button(img, RESULT, "OBTAINED ITEM", "auto_on")
        elif s == "missions":
            _button(img, MTITLE, "MISSIONS", "no")
            _button(img, ACCEPT, "ACCEPT ALL", "ok")
            _button(img, MCANCEL, "CANCEL", "cancel")
        elif s == "mission_confirm":
            _button(img, OK, "OK", "ok")
            _button(img, MCANCEL, "CANCEL", "cancel")
        return img

    @property
    def boost_style(self) -> str:
        return "auto_on" if self.boost_on else "auto_off"

    def tap(self, x: float, y: float) -> None:
        s = self.screen
        if s == "details":
            if BOOST.contains(x, y) and self.boosts > 0:
                self.boost_on = not self.boost_on
            elif BAR.contains(x, y):
                self.cost = 80 if self.boost_on else 40
                self.screen = "restore" if self.sta < self.cost else "team"
            elif MISSION.contains(x, y):
                self.origin, self.screen = "details", "missions"
        elif s == "restore":
            if ITEM_SELECT.contains(x, y):
                self.screen, self.pieces = "items", 2
            elif STONE_SELECT.contains(x, y):
                self.stones_used += 1
            elif CANCEL.contains(x, y):
                self.screen = "details"
        elif s == "items":
            if MINUS.contains(x, y):
                self.pieces = max(1, self.pieces - 1)
            elif OK.contains(x, y):
                self.screen = "confirm"
            elif CANCEL.contains(x, y):
                self.screen = "restore"
        elif s == "confirm" and OK.contains(x, y):
            self.sta += MEAT * self.pieces
            self.refills += 1
            self.meat_used += self.pieces
            self.screen = "restored"
        elif s == "restored" and OK.contains(x, y):
            self.screen = "details"
        elif s == "team" and START.contains(x, y):
            self.sta -= self.cost
            if self.boost_on:
                self.boosts -= 1
                self.boosted_runs += 1
            self.screen, self.taps = "battle", 0
        elif s == "battle" and SPHERE.contains(x, y):
            self.taps += 1
            if self.taps >= TAPS_TO_WIN:
                self.screen = "result"
        elif s == "result":
            self.clears += 1
            self.boost_on = False
            self.screen = "event"
        elif s == "event":
            if EVENT_ROW.contains(x, y) and not MISSION.contains(x, y):
                self.screen = "details"
            elif MISSION.contains(x, y):
                self.origin, self.screen = "event", "missions"
        elif s == "missions":
            if ACCEPT.contains(x, y) and not self.missions_accepted:
                self.screen = "mission_confirm"
            elif MCANCEL.contains(x, y):
                self.screen = self.origin
        elif s == "mission_confirm":
            if OK.contains(x, y):
                self.missions_accepted = True
                self.screen = "accepted"
            elif MCANCEL.contains(x, y):
                self.screen = "missions"
        elif s == "accepted" and OK.contains(x, y):
            self.screen = "missions"

    def swipe(self, y1: float, y2: float) -> None:
        pass

    def back(self) -> None:
        pass


def capture_templates(store: TemplateStore) -> None:
    game = StageGame(stamina=0, boosts=1)

    def crop(name: str, screen: str, box: Box, **kwargs) -> None:
        game.screen = screen
        store.create_from_frame(name, game.render(), box.as_tuple(), threshold=0.9, **kwargs)

    crop("stage_details", "details", HEADER)
    crop("boost_off", "details", BOOST, mode="color")  # grey; boost on is green
    game.boost_on = True
    crop("boost_on", "details", BOOST, mode="color")
    game.boost_on = False
    crop("event_mission", "details", MISSION)
    crop("event_stage_row", "event", ROW_HEADER)
    crop("restore_with_item", "restore", ITEM_ROW)
    crop("restore_sta", "restore", TITLE)
    crop("restore_items_to_use", "items", USE_LABEL)
    crop("start_button", "team", START)
    crop("battle_screen", "battle", ITEM)
    crop("stage_result", "result", RESULT)
    crop("mission_dialog", "missions", MTITLE)
    crop("mission_accept_all", "missions", ACCEPT, search_full=True)
    crop("cancel_button", "missions", MCANCEL, search_full=True)
    crop("ok_button", "accepted", OK, search_full=True)


@pytest.fixture
def play(tmp_path: Path):
    def run(game: StageGame, options: dict | None = None) -> dict:
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
        end = clock.t + 3 * 60 * 60
        return farmer.run_session(
            ROUTINE, routine, routine.option_values(options or {}), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)


def test_repeats_the_stage_refills_with_one_meat_and_accepts_the_missions(play):
    game = StageGame(stamina=100)
    summary = play(game, {"max_clears": 3})
    assert game.clears == 3 and not summary["error"]
    assert game.refills == 1 and game.meat_used == 1  # 100 - 80 = 20 < 40: one piece
    assert game.stones_used == 0
    assert game.missions_accepted
    assert "mission rewards accepted" in summary["reason"]


def test_boost_is_used_as_long_as_charges_last(play):
    game = StageGame(stamina=1000, boosts=2)
    play(game, {"max_clears": 4, "boost": "on", "claim_missions": "no"})
    assert game.clears == 4 and game.boosted_runs == 2


def test_stops_instead_of_refilling_when_asked(play):
    game = StageGame(stamina=100)
    summary = play(game, {"max_clears": 5, "refill": "stop"})
    assert game.clears == 2 and game.refills == 0 and game.stones_used == 0
    assert "Out of stamina after 2 clears" in summary["reason"]
