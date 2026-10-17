"""The bundled "Dokkan Story" routine: saga by saga through a fake saga wheel."""

from __future__ import annotations

from pathlib import Path

import pytest

from dokkan_farmer.device.sim import Box, _button, _text
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import parse_routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.vision import Matcher, TemplateStore
from test_clear_new_stages import (
    CLEAR,
    DIFFICULTIES,
    HUD,
    OK,
    PAUSE,
    START,
    TITLE,
    Clock,
    QuestDevice,
    QuestGame,
    difficulty_bar,
    new_badge,
)
from test_clear_new_stages import ROW_TOP as LIST_TOP

ROUTINE = "dokkan_story"
TAP_MARK = Box(260, 760, 200, 45)
SAGA = Box(40, 860, 640, 110)
BACK = Box(20, 1160, 160, 90)
STAGES = 4  # per saga: the fake's stage list shows four rows


class StoryGame(QuestGame):
    """A wheel of sagas, each a QuestGame stage list; Back returns to the wheel."""

    def __init__(self, sagas: list[set[int]]) -> None:
        # sagas[i] = the stages of saga i that are already cleared
        super().__init__({}, stages=STAGES)
        self.sagas = [
            {s: ({"NORMAL"} if s in done else set()) for s in range(STAGES)} for done in sagas
        ]
        self.saga = self.wheel = 0
        self.screen = "wheel"
        self.story: list[tuple[int, int]] = []  # (saga, stage) played
        self.loading = 0  # renders left of the moment between a stage tap and its difficulties

    def render(self):
        if self.screen == "wheel":
            img = super().render()  # a blank canvas for unknown screens
            _button(img, TITLE, "SAGAS", "auto_off")
            _text(img, "TAP", TAP_MARK.center, 1.2)
            _button(img, SAGA, f"SAGA {self.wheel + 1}", "nav")
            return img
        if self.loading > 0 and self.screen == "difficulty":
            self.loading -= 1  # still loading: only the back button is drawn
            img = QuestGame({}).render()
            img[:] = (40, 30, 30)
            _button(img, BACK, "BACK", "no")
            return img
        img = super().render()
        if self.screen in ("list", "difficulty"):
            _button(img, BACK, "BACK", "no")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "wheel":
            if SAGA.contains(x, y):
                self.saga, self.screen, self.offset = self.wheel, "list", 0
                self.cleared = self.sagas[self.saga]
            return
        if self.screen == "list" and BACK.contains(x, y):
            self.screen = "wheel"
            return
        if self.screen == "difficulty" and BACK.contains(x, y):
            self.screen = "list"  # Back from the difficulties: the stage list again
            return
        before = len(self.played)
        was = self.screen
        super().tap(x, y)
        if was == "list" and self.screen == "difficulty":
            self.loading = 2
        if self.screen == "support":  # like Dragon Ball Story: straight to the team
            self.screen = "team"
        if len(self.played) > before:
            self.story.append((self.saga, self.stage))

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen == "wheel":
            step = 1 if y2 > y1 else -1  # a swipe down turns to the later sagas
            self.wheel = min(max(self.wheel + step, 0), len(self.sagas) - 1)
        else:
            super().swipe(y1, y2)


class AreaGame(StoryGame):
    """Quest: Dokkan Story: a list of areas (NEW on those with uncleared stages) instead
    of a saga wheel; an area opens with a tap on its row, Back returns to the list."""

    def __init__(self, areas: list[set[int]]) -> None:
        super().__init__(areas)
        self.screen = "areas"

    def render(self):
        if self.screen != "areas":
            img = super().render()
            return img
        self.screen = "wheel"
        img = QuestGame.render(self)  # a blank canvas
        self.screen = "areas"
        _button(img, TITLE, "CHAPTER 5", "retry")
        _button(img, BACK, "BACK", "no")  # Back on the area list leads out (not played here)
        for row, area in enumerate(self.sagas[:4]):
            top = LIST_TOP + row * 200
            _text(img, f"AREA {row + 31}", (220, top + 90), 1.2)
            if any(not done for done in area.values()):
                _button(img, new_badge(top), "NEW", "danger")
        return img

    def tap(self, x: float, y: float) -> None:
        if self.screen == "areas":
            row = int((y - LIST_TOP) // 200) if y >= LIST_TOP else -1
            if 0 <= row < min(4, len(self.sagas)):
                self.saga, self.screen, self.offset = row, "list", 0
                self.cleared = self.sagas[row]
            return
        if self.screen == "list" and BACK.contains(x, y):
            self.screen = "areas"
            return
        super().tap(x, y)


class HardAreaGame(AreaGame):
    """Quest stages: HARD / Z-HARD / SUPER, no NORMAL."""

    def __init__(self, areas: list[set[int]]) -> None:
        super().__init__(areas)
        self.difficulties = ("HARD", "Z-HARD")


class SuperAreaGame(AreaGame):
    """Quest stages: HARD / Z-HARD / SUPER; SUPER pays the stones of the lower ones too."""

    def __init__(self, areas: list[set[int]]) -> None:
        super().__init__(areas)
        self.difficulties = ("HARD", "Z-HARD", "SUPER")


HOME_START = Box(220, 700, 280, 90)
MENU_QUEST = Box(80, 500, 320, 100)
CHAPTER_TAP = Box(280, 120, 160, 45)  # the "TAP" marker above the selected chapter
CHAPTER_CARD = Box(40, 200, 640, 500)


class ChapterGame(AreaGame):
    """Home → START → menu (Quest) → chapter wheel → a chapter's area list; a swipe down
    turns the wheel to the later chapters, Back on the area list returns to the wheel."""

    def __init__(self, chapters: list[list[set[int]]], turns_to_first: int = 0) -> None:
        super().__init__(chapters[0])
        # chapters[c][a] = the stages of area a already cleared; kept like StoryGame's sagas
        self.chapters = [
            [{s: ({"NORMAL"} if s in done else set()) for s in range(STAGES)} for done in areas]
            for areas in chapters
        ]
        self.chapter = 0
        self.wheel = 0
        self.opened: list[int] = []
        self.screen = "home"

    def render(self):
        if self.screen in ("home", "menu", "chapters"):
            img = QuestGame({}).render()  # a blank canvas
            img[:] = (40, 30, 30)
            if self.screen == "home":
                _button(img, TITLE, "HOME", "retry")
                _button(img, HOME_START, "START", "start")
            elif self.screen == "menu":
                _button(img, MENU_QUEST, "QUEST DOKKAN STORY", "nav")
            else:
                _text(img, "TAP", CHAPTER_TAP.center, 1.2)
                _button(img, CHAPTER_CARD, f"CHAPTER {self.wheel + 5}", "auto_on")
                _button(img, BACK, "BACK", "no")
            return img
        return super().render()

    def tap(self, x: float, y: float) -> None:
        if self.screen == "home":
            if HOME_START.contains(x, y):
                self.screen = "menu"
        elif self.screen == "menu":
            if MENU_QUEST.contains(x, y):
                self.screen = "chapters"
        elif self.screen == "chapters":
            if CHAPTER_CARD.contains(x, y):
                self.chapter = self.wheel
                self.opened.append(self.chapter)
                self.sagas = self.chapters[self.chapter]
                self.screen, self.offset = "areas", 0
        elif self.screen == "areas" and BACK.contains(x, y):
            self.screen = "chapters"
        else:
            super().tap(x, y)

    def swipe(self, y1: float, y2: float) -> None:
        if self.screen == "chapters":
            step = 1 if y2 > y1 else -1
            self.wheel = min(max(self.wheel + step, 0), len(self.chapters) - 1)
        else:
            super().swipe(y1, y2)


def capture_navigation(store: TemplateStore) -> None:
    game = ChapterGame([[set()]])
    for screen, name, box in (
        ("home", "home_screen", HOME_START),
        ("menu", "menu_quest", MENU_QUEST),
        ("chapters", "chapter_wheel", CHAPTER_TAP),
    ):
        game.screen = screen
        store.create_from_frame(name, game.render(), box.as_tuple(), threshold=0.9)


def capture_difficulties(store: TemplateStore) -> None:
    """The HARD / Z-HARD / SUPER labels of a Quest stage's difficulty list."""
    game = SuperAreaGame([set()])
    game.screen, game.stage = "difficulty", 0
    for row, name in enumerate(("difficulty_hard", "difficulty_zhard", "difficulty_super")):
        store.create_from_frame(
            name,
            game.render(),
            difficulty_bar(row).as_tuple(),
            threshold=0.9,
            search_full=True,
        )


def capture_templates(store: TemplateStore) -> None:
    game = StoryGame([set()])
    game.cleared = game.sagas[0]
    shots = [
        ("wheel", "saga_wheel", TAP_MARK, False),
        ("list", "stage_list", TITLE, False),
        ("list", "new_mark", new_badge(LIST_TOP), True),
        ("list", "back_button", BACK, False),
        ("difficulty", "difficulty_normal", difficulty_bar(0), True),
        ("team", "start_button", START, False),
        ("map", "map_screen", HUD, False),  # also in battles, as in the game
        ("battle", "battle_screen", PAUSE, False),
        ("clear", "stage_result", CLEAR, False),
        ("results", "ok_button", OK, True),
    ]
    for screen, name, box, search_full in shots:
        game.screen, game.stage = screen, 0
        store.create_from_frame(
            name, game.render(), box.as_tuple(), threshold=0.9, search_full=search_full
        )
    hard = HardAreaGame([set()])  # HARD is where NORMAL was
    hard.screen, hard.stage = "difficulty", 0
    store.create_from_frame(
        "difficulty_hard",
        hard.render(),
        difficulty_bar(0).as_tuple(),
        threshold=0.9,
        search_full=True,
    )


@pytest.fixture
def play(tmp_path: Path):
    def run(
        game: StoryGame,
        options: dict | None = None,
        difficulties: bool = False,
        navigation: bool = False,
    ) -> dict:
        settings = Settings(data_dir=tmp_path / "data", device_backend="sim")
        store = TemplateStore(settings.templates_dir)
        capture_templates(store)
        if difficulties:
            capture_difficulties(store)
        if navigation:
            capture_navigation(store)
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
        end = clock.t + 120 * 60
        return farmer.run_session(
            ROUTINE, routine, routine.option_values(options), until=lambda: clock.t >= end
        )

    return run


def test_the_routine_documents_its_templates():
    routine = parse_routine((BUNDLED_DIR / f"{ROUTINE}.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)
    assert routine.templates["saga_wheel"].level == "recommended"  # Quest areas need no wheel
    assert {"back_button", "stage_result"} <= {
        name for name, doc in routine.templates.items() if doc.level == "required"
    }


def test_plays_saga_after_saga_to_the_end_of_the_series(play):
    # Saga 1 is done, saga 2 has two stages left, saga 3 one.
    game = StoryGame([set(range(STAGES)), {0, 1}, {0, 1, 2}])
    summary = play(game, {"empty_sagas": 2})
    assert sorted(game.story) == [(1, 2), (1, 3), (2, 3)]
    assert {d for _, d in game.played} == {DIFFICULTIES[0]}
    assert summary["counters"]["stages_cleared"] == 3
    assert "series is done" in summary["reason"] and not summary["error"]


def test_quest_areas_are_opened_by_their_new_mark(play):
    # Area 31 is done; 32 has one stage left, 33 two.
    game = AreaGame([set(range(STAGES)), {0, 1, 2}, {0, 1}])
    play(game)
    assert sorted(game.story) == [(1, 3), (2, 2), (2, 3)]
    assert all(all(done for done in area.values()) for area in game.sagas)


def test_quest_stages_without_normal_are_played_on_hard(play):
    game = HardAreaGame([{0, 1}, {0, 1, 2, 3}])
    summary = play(game)
    assert sorted(game.story) == [(0, 2), (0, 3)]
    assert {d for _, d in game.played} == {"HARD"}
    # Nothing left: the routine goes back out of the lists and says so.
    assert "No NEW stage or area left" in summary["reason"] and not summary["error"]


def test_the_highest_difficulty_is_played_and_pays_the_lower_ones(play):
    game = SuperAreaGame([{0}, set(range(STAGES))])  # stages 1-3 of area 0 are NEW
    play(game, difficulties=True)
    assert {d for _, d in game.played} == {"SUPER"}
    assert len(game.played) == 3  # one clear per stage, none for HARD / Z-HARD
    assert all(game.cleared[s] == {"HARD", "Z-HARD", "SUPER"} for s in range(1, STAGES))


def test_the_easiest_difficulty_can_be_chosen(play):
    game = SuperAreaGame([{0, 1, 2}, set(range(STAGES))])  # one NEW stage left
    play(game, {"difficulty": "easiest"}, difficulties=True)
    assert {d for _, d in game.played} == {"HARD"}


def test_quest_chapters_are_reached_from_the_home_screen(play):
    # Chapter 5 is done; chapter 6 has two areas with one NEW stage each.
    done = [set(range(STAGES))]
    chapters = [done, [{0, 1, 2}, {0, 1, 2}]]
    game = ChapterGame(chapters)
    summary = play(game, {"start_in": "quest", "chapter_turns": 1}, navigation=True)
    assert game.opened[0] == 1  # one wheel turn: the second chapter
    assert sorted(game.story) == [(0, 3), (1, 3)]
    assert "No NEW stage or area left" in summary["reason"] and not summary["error"]
    assert all(all(done for done in area.values()) for area in game.chapters[1])


def test_the_home_screen_is_left_alone_when_starting_here(play):
    game = ChapterGame([[{0, 1, 2}]])
    play(game, navigation=True)  # start_in = here (the default): nothing taps START
    assert game.screen == "home" and game.opened == []
