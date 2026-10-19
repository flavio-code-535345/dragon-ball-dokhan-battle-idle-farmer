"""The bundled "Dokkan Clear New Stages" routine, played against a small fake quest game."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from dokkan_farmer.device.base import Device
from dokkan_farmer.device.sim import WIDTH, Box, _button, _canvas, _text
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.vision import Matcher, TemplateStore

ROUTINE = "dokkan_clear_new_stages"
PACKAGE = "com.bandainamcogames.dbzdokkanww"

TITLE = Box(0, 0, WIDTH, 120)
ROWS = 4  # stage rows on screen at once
ROW_TOP, ROW_HEIGHT = 200, 200
DIFFICULTIES = ("NORMAL", "Z-HARD")
SUPPORT = Box(40, 300, 640, 140)
START = Box(420, 1120, 260, 100)
PAUSE = Box(560, 30, 140, 70)
CLEAR = Box(60, 500, 600, 160)
OK = Box(240, 780, 240, 90)
GIVE_UP = Box(240, 740, 240, 90)
#: Like Dokkan's bottom bar (HP, Dokkan meter, NEXT UP): on the map and in battles.
HUD = Box(0, 1180, 720, 100)
#: The bright blue of Dokkan's path arrows (HSV about 104, 247, 224).
ARROW_BGR = (224, 125, 7)


def new_badge(row_top: int) -> Box:
    return Box(520, row_top + 50, 140, 80)


def difficulty_bar(row: int) -> Box:
    return Box(20, ROW_TOP + row * ROW_HEIGHT, 680, 90)


class QuestGame:
    """Like Dokkan: a stage list where only never-cleared stages are NEW, a difficulty screen
    without NEW marks, then support, team, map, battle and results."""

    def __init__(self, cleared: dict[int, set[str]], stages: int = 8, losing: set = ()) -> None:
        self.stages = stages
        self.cleared = {s: set(cleared.get(s, ())) for s in range(stages)}
        self.losing = set(losing)  # (stage, difficulty) the team cannot win
        self.screen = "list"
        self.offset = 0
        self.stage = self.difficulty = None
        self.steps = self.hp = 0
        self.played: list[tuple[int, str]] = []
        self.paid = False
        self.team_tapped = False
        self.forked = False  # the path fork of the current map was taken
        self.difficulties = DIFFICULTIES  # Quest stages start at HARD, without NORMAL

    def new(self, stage: int) -> bool:
        return not self.cleared[stage]  # one cleared difficulty: CLEARED!, no NEW mark

    # ------------------------------------------------------------------ drawing
    def render(self) -> np.ndarray:
        img = _canvas((40, 30, 30))
        screen = self.screen
        if screen == "list":
            _button(img, TITLE, "AREA 1", "nav")
            for row in range(ROWS):
                stage = self.offset + row
                top = ROW_TOP + row * ROW_HEIGHT
                cv2.rectangle(img, (20, top), (700, top + 180), (90, 70, 60), -1)
                _text(img, f"STAGE {stage + 1}", (220, top + 90), 1.2)
                if self.new(stage):
                    _button(img, new_badge(top), "NEW", "danger")
        elif screen == "difficulty":
            _button(img, TITLE, f"STAGE {self.stage + 1}", "retry")
            for row, name in enumerate(self.difficulties):
                _button(img, difficulty_bar(row), name, "nav" if row == 0 else "danger")
                top = ROW_TOP + row * ROW_HEIGHT
                _text(img, "CLEAR REWARD", (220, top + 140), 0.8)
        elif screen == "support":
            _button(img, SUPPORT, "SUPPORT GOKU", "nav")
        elif screen == "team":
            _button(img, START, "START!", "start")
        elif screen == "map":
            _button(img, TITLE, "MAP", "auto_on")
            _button(img, HUD, "NEXT UP", "auto_on")
            _text(img, f"step {self.steps}", (360, 600), 2.0)
            if self.fork_open():
                cv2.fillPoly(img, [self.arrow()], ARROW_BGR)
        elif screen == "battle":
            _button(img, PAUSE, "II", "auto_off")
            _button(img, HUD, "NEXT UP", "auto_on")
            _text(img, f"enemy HP {self.hp}", (360, 300), 1.5)
        elif screen == "clear":
            _button(img, CLEAR, "STAGE CLEAR", "ok")
        elif screen == "results":
            _button(img, OK, "OK", "ok")
        elif screen == "defeat":
            _button(img, Box(60, 500, 600, 160), "DEFEATED", "no")
            _button(img, GIVE_UP, "GIVE UP", "danger")
            _button(img, Box(240, 900, 240, 90), "REVIVE", "start")
        return img

    # ----------------------------------------------------------------- touching
    def tap(self, x: float, y: float) -> None:
        screen = self.screen
        row = int((y - ROW_TOP) // ROW_HEIGHT) if y >= ROW_TOP else -1
        if screen == "list" and 0 <= row < ROWS:
            self.stage, self.screen = self.offset + row, "difficulty"
        elif screen == "difficulty" and 0 <= row < len(self.difficulties):
            self.difficulty, self.screen = self.difficulties[row], "support"
        elif screen == "support" and SUPPORT.contains(x, y):
            self.screen = "team"
        elif screen == "team" and START.contains(x, y):
            self.screen, self.steps, self.forked = "map", 0, False
            self.played.append((self.stage, self.difficulty))
        elif screen == "map" and self.fork_open():
            x0, y0, w, h = cv2.boundingRect(self.arrow())
            self.forked = x0 <= x < x0 + w and y0 <= y < y0 + h  # only the arrow goes on
        elif screen == "map" and 1000 < y < HUD.y:
            self.steps += 1
            if self.steps == 3:
                self.screen, self.hp = "battle", 4
        elif screen == "battle" and 1000 < y < HUD.y:
            self.team_tapped = True  # the move spot: a character, not a ki sphere
        elif screen == "battle" and 450 <= y <= 700:
            self.hp -= 1
            if self.hp == 0:
                lost = (self.stage, self.difficulty) in self.losing
                self.screen = "defeat" if lost else "clear"
        elif screen == "clear":
            self.screen = "results"
        elif screen == "results" and OK.contains(x, y):
            self.cleared[self.stage].add(self.difficulty)
            if self.difficulty == self.difficulties[-1]:  # the highest pays the lower ones too
                self.cleared[self.stage].update(self.difficulties)
            self.screen = "list"
        elif screen == "defeat":
            if GIVE_UP.contains(x, y):
                self.screen = "list"
            elif Box(240, 900, 240, 90).contains(x, y):
                self.paid = True

    def fork_open(self) -> bool:
        """After the first move the path forks: a blue arrow must be tapped."""
        return self.screen == "map" and self.steps == 1 and not self.forked

    def arrow(self) -> np.ndarray:
        """The fork's arrow, pointing another way (and elsewhere) on every stage."""
        cx, cy = (480, 560) if (self.stage or 0) % 2 else (230, 430)
        flip = -1 if (self.stage or 0) % 2 else 1
        points = [(-60, -20), (10, -20), (10, -45), (60, 0), (10, 45), (10, 20), (-60, 20)]
        return np.array([(cx + flip * dx, cy + dy) for dx, dy in points], np.int32)

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen != "list":
            return
        step = 2 if y1 > y2 else -2  # swiping up shows the stages further down
        self.offset = min(max(self.offset + step, 0), self.stages - ROWS)

    def back(self) -> None:
        if self.screen == "difficulty":
            self.screen = "list"


class EventGame(QuestGame):
    """An event's stage list: no area title, every row has the Clear Reward / Featured Reward
    heading, and the newest (NEW) stage is at the top."""

    HEADING = Box(40, ROW_TOP + 130, 320, 40)

    def __init__(self, cleared: dict[int, set[str]], stages: int = 8) -> None:
        super().__init__(cleared, stages)
        self.swipes = 0

    def render(self) -> np.ndarray:
        img = super().render()
        if self.screen == "list":
            _button(img, TITLE, "EVENT", "retry")  # not the quest list's title
            for row in range(ROWS):
                top = ROW_TOP + row * ROW_HEIGHT
                _text(img, "CLEAR REWARD FEATURED", (200, top + 150), 0.8)
        return img

    def swipe(self, y1: float, y2: float) -> None:
        self.swipes += 1
        super().swipe(y1, y2)


NAV_BUTTON = Box(590, 130, 100, 60)
NAV_TITLE = Box(60, 40, 600, 100)


def nav_entry(index: int) -> Box:
    """The navigator's rows, centred at 24 %, 45 % and 66 % of the screen height."""
    return Box(40, int(1280 * (0.24 + 0.21 * index)) - 70, 640, 140)


class NavGame(EventGame):
    """Several events behind a Dragon Stone Navigator. A finished event leaves the navigator,
    except the ``sticky`` ones (their remaining stones need another team)."""

    def __init__(self, events: list[dict[int, set[str]]], sticky: set[int] = frozenset()) -> None:
        super().__init__(events[0])
        self.events = [{s: set(c.get(s, ())) for s in range(self.stages)} for c in events]
        self.cleared = self.events[0]
        self.sticky = set(sticky)
        self.current = 0
        self.opened: list[int] = []  # the events opened from the navigator

    def entries(self) -> list[int]:
        return [
            i
            for i, cleared in enumerate(self.events)
            if i in self.sticky or any(not cleared[s] for s in range(self.stages))
        ]

    def render(self) -> np.ndarray:
        if self.screen == "navigator":
            img = _canvas((30, 40, 60))
            _button(img, NAV_TITLE, "DRAGON STONE NAVIGATOR", "nav")
            for row, _ in enumerate(self.entries()[:3]):
                _button(img, nav_entry(row), f"EVENT {row + 1}", "retry")
            return img
        img = super().render()
        if self.screen == "list":
            _button(img, NAV_BUTTON, "STONE", "danger")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "navigator":
            entries = self.entries()
            for row, event in enumerate(entries[:3]):
                if nav_entry(row).contains(x, y):
                    self.current = event
                    self.cleared = self.events[event]
                    self.opened.append(event)
                    self.offset, self.screen = 0, "list"
            return
        if self.screen == "list" and NAV_BUTTON.contains(x, y):
            self.screen = "navigator"
            return
        super().tap(x, y)


class QuestDevice(Device):
    backend = "test"

    def __init__(self, game: QuestGame) -> None:
        self.game = game

    @property
    def serial(self) -> str:
        return "quest"

    def connect(self) -> None:
        pass

    def screenshot(self) -> np.ndarray:
        return self.game.render()

    def tap(self, x: int, y: int) -> None:
        self.game.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.game.swipe(y1, y2)

    def key(self, name: str) -> None:
        if name.upper() == "BACK":
            self.game.back()

    def launch_app(self, package: str) -> None:
        pass

    def stop_app(self, package: str) -> None:
        pass

    def foreground_package(self) -> str | None:
        return PACKAGE


def capture_templates(store: TemplateStore) -> None:
    """The templates a user would capture, cropped from the fake game's screens."""
    game = QuestGame({})  # every stage NEW
    shots = [
        ("list", "stage_list", TITLE, False),
        ("list", "new_mark", new_badge(ROW_TOP), True),
        ("difficulty", "difficulty_normal", difficulty_bar(0), False),
        ("support", "select_support", SUPPORT, False),
        ("team", "start_button", START, False),
        ("map", "map_screen", HUD, False),  # also in battles, as in the game
        ("battle", "battle_screen", PAUSE, False),
        ("clear", "stage_result", CLEAR, False),
        ("results", "ok_button", OK, True),
        ("defeat", "defeat_give_up", GIVE_UP, False),
    ]
    for screen, name, box, search_full in shots:
        game.screen, game.stage = screen, 0
        store.create_from_frame(
            name, game.render(), box.as_tuple(), threshold=0.9, search_full=search_full
        )
    event = EventGame({})
    store.create_from_frame(
        "event_stage_row",
        event.render(),
        Box(40, ROW_TOP + 135, 320, 30).as_tuple(),
        threshold=0.8,
        search_full=True,
    )
    nav = NavGame([{}])
    store.create_from_frame(
        "navigator_open", nav.render(), NAV_BUTTON.as_tuple(), threshold=0.9, search_full=True
    )
    nav.screen = "navigator"
    store.create_from_frame(
        "navigator_title", nav.render(), NAV_TITLE.as_tuple(), threshold=0.9, search_full=True
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def play(tmp_path: Path):
    def run(game: QuestGame, options: dict | None = None, minutes: float = 60) -> dict:
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
        end = clock.t + minutes * 60
        return farmer.run_session(
            ROUTINE, routine, routine.option_values(options), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)
    required = {name for name, doc in routine.templates.items() if doc.level == "required"}
    assert required == {
        "new_mark",
        "stage_list",
        "difficulty_normal",
        "start_button",
        "battle_screen",
        "stage_result",
    }
    assert routine.settings.run_counter == "stages_cleared"


def test_clears_every_new_stage_on_normal_and_stops(play):
    # Stages 6 and 7 are NEW, below the first screen. Stage 5 is CLEARED! on Normal: its
    # Z-Hard stays for the player.
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    cleared[5], cleared[6], cleared[7] = {"NORMAL"}, set(), set()
    game = QuestGame(cleared)
    summary = play(game)
    assert sorted(game.played) == [(6, "NORMAL"), (7, "NORMAL")]
    assert all(not game.new(s) for s in range(game.stages))
    assert game.cleared[5] == {"NORMAL"}
    assert summary["counters"]["stages_cleared"] == 2 and summary["runs"] == 2
    assert "No uncleared stage left" in summary["reason"] and not summary["error"]
    assert not game.team_tapped  # the map's move spot is never tapped in battle
    assert game.forked  # each map's fork was passed by tapping its arrow


def test_a_new_stage_above_the_screen_is_found(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    cleared[0] = set()
    game = QuestGame(cleared)
    game.offset = 4  # the list shows its end; the NEW stage is at the top
    summary = play(game)
    assert game.played == [(0, "NORMAL")] and summary["runs"] == 1


def test_stops_after_max_stages(play):
    game = QuestGame({})
    summary = play(game, {"max_stages": 2})
    # (Several NEW marks on screen: it opens whichever matches best, not the topmost.)
    assert len(game.played) == 2 and {d for _, d in game.played} == {"NORMAL"}
    assert summary["runs"] == 2


def test_a_defeat_gives_up_without_paying(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    cleared[1] = set()
    game = QuestGame(cleared, losing={(1, "NORMAL")})
    summary = play(game, {"retries": 1})
    assert game.played == [(1, "NORMAL"), (1, "NORMAL")]  # one more try, then stop
    assert "defeated" in summary["reason"] and summary["runs"] == 0
    assert not game.paid


def test_an_event_without_a_new_stage_at_the_top_stops_without_scrolling(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    game = EventGame(cleared)
    summary = play(game, minutes=10)
    assert game.swipes == 0 and not game.played
    assert "No uncleared stage left" in summary["reason"]


def test_an_event_is_scrolled_when_quick_finish_is_off(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    game = EventGame(cleared)
    play(game, {"quick_finish": "no", "max_scrolls": 2}, minutes=10)
    assert game.swipes > 0


def test_an_event_plays_the_new_stage_at_the_top(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    cleared[0] = set()
    game = EventGame(cleared)
    summary = play(game, minutes=20)
    assert game.played == [(0, "NORMAL")] and summary["runs"] == 1
    assert "No uncleared stage left" in summary["reason"]


def all_cleared(new: set[int] = frozenset()) -> dict[int, set[str]]:
    return {s: (set() if s in new else set(DIFFICULTIES)) for s in range(8)}


NEXT = {"next_event": "yes", "claim_missions": "no", "max_events": 10}


def test_goes_on_with_the_next_event_when_one_is_finished(play):
    game = NavGame([all_cleared({0}), all_cleared({0, 1})])
    summary = play(game, NEXT, minutes=60)
    assert summary["counters"]["stages_cleared"] == 3
    assert all(not game.new(s) for s in range(game.stages))
    assert game.opened  # the second event was reached through the navigator
    assert "no more events" in summary["reason"] and not summary["error"]


def test_an_event_that_stays_in_the_navigator_is_skipped(play):
    # Event 0 stays listed (sticky) with nothing to play; the farm moves on to entry 2.
    game = NavGame([all_cleared(), all_cleared({0})], sticky={0})
    summary = play(game, NEXT, minutes=60)
    assert 1 in game.opened and summary["counters"]["stages_cleared"] == 1
    assert "no more events" in summary["reason"]


def test_stops_at_max_events(play):
    game = NavGame([all_cleared({0}), all_cleared({0}), all_cleared({0})])
    summary = play(game, {**NEXT, "max_events": 1}, minutes=60)
    assert summary["counters"]["stages_cleared"] == 1
    assert "1 events done" in summary["reason"]


def test_without_next_event_the_farm_stops_on_the_finished_list(play):
    game = NavGame([all_cleared(), all_cleared({0})])
    summary = play(game, {"claim_missions": "no"}, minutes=20)
    assert not game.opened and "No uncleared stage left" in summary["reason"]


class LostGame(QuestGame):
    """A screen no rule knows (like a story chapter list) that only Back leaves."""

    def __init__(self, cleared, backs_needed: int) -> None:
        super().__init__(cleared)
        self.screen = "mystery"
        self.backs = 0
        self.backs_needed = backs_needed

    def render(self) -> np.ndarray:
        if self.screen == "mystery":
            img = _canvas((60, 30, 70))
            _text(img, "CHAPTER 6", (200, 400), 1.5)
            return img
        return super().render()

    def tap(self, x: float, y: float) -> None:
        if self.screen != "mystery":
            super().tap(x, y)

    def back(self) -> None:
        if self.screen == "mystery":
            self.backs += 1
            if self.backs >= self.backs_needed:
                self.screen = "list"
        else:
            super().back()


def test_a_lost_screen_is_left_with_back(play):
    cleared = {s: set(DIFFICULTIES) for s in range(8)}
    cleared[1] = set()
    game = LostGame(cleared, backs_needed=3)
    summary = play(game, minutes=30)
    assert game.backs == 3 and game.played == [(1, "NORMAL")]
    assert summary["runs"] == 1


def test_a_screen_that_never_clears_stops_the_farm_instead_of_looping(play):
    game = LostGame({}, backs_needed=10_000)
    summary = play(game, minutes=60)
    assert "Lost on a screen" in summary["reason"] and not game.played
    assert game.backs >= 8


def test_a_stalled_event_list_goes_on_with_the_next_entry(play):
    # Event 0 shows a NEW stage that never opens (the tap does nothing).
    class Stuck(NavGame):
        def tap(self, x, y):
            if self.screen == "list" and self.current == 0 and not NAV_BUTTON.contains(x, y):
                return
            super().tap(x, y)

    game = Stuck([all_cleared({0}), all_cleared({0})])
    summary = play(game, {**NEXT}, minutes=60)
    assert 1 in game.opened and summary["counters"]["stages_cleared"] == 1
