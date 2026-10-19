"""The bundled "Dokkan Growth Sweep" routine: the Event menu's Bonus list, event after event."""

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
from test_repeat_stage import MISSION, START, StageGame
from test_repeat_stage import capture_templates as capture_stage_templates

ROUTINE = "dokkan_growth_sweep"
HOME_START = Box(200, 800, 320, 100)
MENU_EVENT = Box(60, 300, 600, 120)
NAV = Box(300, 1050, 120, 90)
BACK = Box(20, 1060, 150, 80)
REQ_TITLE = Box(160, 380, 400, 60)
REQ_OK = Box(380, 820, 260, 80)  # like the OK of the stage fakes (the ok_button template)
FULL = 4  # taps on an event's stage row that never start it before the sweep leaves


class SweepGame(StageGame):
    """Home → START menu → Event menu (a wheel of entries) → an event's page → details → team →
    battle → results → event page. Entries: attempts left (None = unlimited), restricted."""

    def __init__(self, entries, stamina: int = 1000) -> None:
        super().__init__(stamina, 0)
        self.screen = "home"
        self.entries = [dict(e) for e in entries]
        self.selected = len(entries) - 1  # the wheel starts at the end of the list
        self.current = None
        self.tab = None
        self.visited: list[int] = []
        self.restricted_hits = 0

    def entry(self) -> dict:
        return self.entries[self.current]

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
            _text(img, f"ENTRY {self.selected}", (200, 520), 1.5)
            return img
        if s == "req":
            img = _canvas((40, 40, 40))
            _button(img, REQ_TITLE, "REQUIREMENT NOT MET", "start")
            _button(img, REQ_OK, "OK", "ok")
            return img
        img = super().render()
        if s in ("event", "details", "team"):
            _button(img, BACK, "<<<", "nav")
        return img

    def tap(self, x: float, y: float) -> None:
        s = self.screen
        if s == "home":
            if HOME_START.contains(x, y):
                self.screen = "menu"
        elif s == "menu":
            if MENU_EVENT.contains(x, y):
                self.screen = "eventmenu"
        elif s == "eventmenu":
            if 690 <= y <= 700 and x < 300:
                self.tab = "bonus"
            elif 800 <= y <= 920:
                self.current = self.selected
                self.visited.append(self.selected)
                self.screen = "event"
        elif s == "req":
            if REQ_OK.contains(x, y):
                self.screen = "team"
        elif s == "event":
            if BACK.contains(x, y):
                self.screen = "eventmenu"
            elif MISSION.contains(x, y) or self.entry()["attempts"] != 0:
                super().tap(x, y)  # (a row without attempts left does not open)
        elif s == "details":
            if BACK.contains(x, y):
                self.screen = "event"
            else:
                super().tap(x, y)
        elif s == "team":
            if BACK.contains(x, y):
                self.screen = "details"
            elif START.contains(x, y) and self.entry().get("restricted"):
                self.restricted_hits += 1
                self.screen = "req"
            else:
                super().tap(x, y)
        elif s == "result":
            super().tap(x, y)
            if self.entry()["attempts"] is not None:
                self.entry()["attempts"] -= 1
        else:
            super().tap(x, y)

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen == "eventmenu":
            step = -1 if y1 < y2 else 1
            size = 1 if abs(y2 - y1) < 250 else 3  # 0.15 → one entry, 0.3 → three
            self.selected = min(max(self.selected + step * size, 0), len(self.entries) - 1)


def capture_templates(store: TemplateStore) -> None:
    capture_stage_templates(store)
    game = SweepGame([{"attempts": 1}])
    game.selected = 0
    for screen, name, box, kwargs in (
        ("home", "home_screen", HOME_START, {}),
        ("menu", "menu_event", MENU_EVENT, {}),
        ("eventmenu", "navigator_open", NAV, {}),
        ("event", "back_button", BACK, {}),
        ("req", "requirement_not_met", REQ_TITLE, {"mode": "color"}),
    ):
        game.screen = screen
        game.current = 0
        store.create_from_frame(
            name, game.render(), box.as_tuple(), threshold=0.9, search_full=True, **kwargs
        )


@pytest.fixture
def play(tmp_path: Path):
    def run(game: SweepGame, options: dict | None = None, hours: float = 4) -> dict:
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
    assert {"event_stage_row", "back_button", "navigator_open", "stage_details"} <= required


def test_visits_every_entry_plays_what_is_left_and_stops(play):
    # Entry 2 has no attempt left today, entry 3 is unlimited (one clear per visit is asked).
    game = SweepGame([{"attempts": 1}, {"attempts": 1}, {"attempts": 0}, {"attempts": None}])
    summary = play(game, {"max_entries": 4})
    assert game.visited == [0, 1, 2, 3]
    assert game.clears == 3 and not summary["error"]
    assert game.missions_accepted
    assert "Growth sweep done: 4 events" in summary["reason"]


def test_an_event_with_a_team_restriction_is_given_up_and_the_sweep_goes_on(play):
    game = SweepGame([{"attempts": 1}, {"attempts": 1, "restricted": True}, {"attempts": 1}])
    summary = play(game, {"max_entries": 3, "claim_missions": "no"})
    assert game.visited == [0, 1, 2]
    assert game.restricted_hits == 1 and game.clears == 2
    assert "Growth sweep done: 3 events" in summary["reason"]


def test_per_event_asks_for_more_clears_of_an_unlimited_event(play):
    game = SweepGame([{"attempts": None}, {"attempts": None}])
    play(game, {"max_entries": 2, "per_event": 2, "claim_missions": "no"})
    assert game.visited == [0, 1] and game.clears == 4


def test_stops_after_max_entries(play):
    game = SweepGame([{"attempts": 1}] * 6)
    summary = play(game, {"max_entries": 2, "claim_missions": "no"})
    assert game.visited == [0, 1] and "2 events" in summary["reason"]


def test_started_on_an_event_page_it_goes_back_to_the_menu_first(play):
    game = SweepGame([{"attempts": 1}, {"attempts": 1}])
    game.screen, game.current = "event", 0
    summary = play(game, {"max_entries": 2, "claim_missions": "no"})
    assert game.visited[-2:] == [0, 1] and "Growth sweep done" in summary["reason"]


def test_a_screen_nobody_knows_ends_in_a_stop(play):
    game = SweepGame([{"attempts": 1}])
    game.screen = "nowhere"
    game.render = lambda: _canvas((80, 20, 60))
    game.back = lambda: None
    summary = play(game, hours=1)
    assert "Lost on a screen" in summary["reason"]
