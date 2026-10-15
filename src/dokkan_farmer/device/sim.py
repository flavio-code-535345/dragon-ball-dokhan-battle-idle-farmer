"""A tiny fake "Dokkan-like" game for the demo mode and the test-suite.

This is *not* Dokkan Battle. It only reproduces the shape of the farm loop
(results -> ACT dialog -> team -> battle with an Auto toggle -> clear ->
friend request -> results) plus errors, defeats, crashes and the
title/news/home navigation, drawn with plain OpenCV shapes. It lets you try
the web UI without a phone and lets the tests run the bundled routine
end-to-end.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..settings import GAME_PACKAGES
from .base import Device, DeviceError, normalize_key

WIDTH, HEIGHT = 720, 1280
LAUNCHER_PACKAGE = "com.android.launcher3"
FONT = cv2.FONT_HERSHEY_DUPLEX

# BGR colours: (fill, border, text)
STYLES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "ok": ((40, 196, 250), (20, 110, 160), (25, 25, 25)),
    "cancel": ((168, 168, 168), (90, 90, 90), (20, 20, 20)),
    "start": ((20, 120, 250), (10, 60, 150), (255, 255, 255)),
    "nav": ((190, 110, 40), (110, 60, 15), (255, 255, 255)),
    "danger": ((50, 50, 210), (20, 20, 110), (255, 255, 255)),
    "auto_off": ((90, 90, 90), (40, 40, 40), (230, 230, 230)),
    "auto_on": ((60, 170, 60), (25, 90, 25), (255, 255, 255)),
    "retry": ((210, 150, 40), (120, 80, 20), (255, 255, 255)),
    "no": ((120, 70, 160), (60, 30, 90), (255, 255, 255)),
}


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


# Geometry of everything you can tap.
B = {
    "attempt_again": Box(160, 960, 400, 110),
    "results_ok": Box(240, 1110, 240, 90),
    "dialog_left": Box(100, 760, 240, 90),
    "dialog_right": Box(380, 760, 240, 90),
    "dialog_center": Box(240, 780, 240, 90),
    "act_use_item": Box(100, 540, 520, 90),
    "act_use_stone": Box(100, 650, 520, 90),
    "revive": Box(100, 600, 520, 90),
    "give_up": Box(240, 740, 240, 90),
    "start": Box(420, 1120, 260, 100),
    "auto": Box(560, 40, 140, 70),
    "title_logo": Box(110, 300, 500, 160),
    "nav_events": Box(160, 900, 400, 110),
    "nav_banner": Box(60, 300, 600, 180),
    "nav_stage": Box(60, 520, 600, 120),
    "nav_difficulty": Box(160, 700, 400, 100),
    "support_row": Box(40, 300, 640, 140),
    "dialog_title": Box(60, 400, 600, 90),
}
PANEL = Box(60, 400, 600, 520)


@dataclass
class SimConfig:
    battle_seconds: float = 8.0
    act_max: int = 40
    act_cost: int = 10
    act_regen_seconds: float = 3.0
    items: int = 2
    stones: int = 5
    friend_request_chance: float = 0.5
    error_chance: float = 0.0
    defeat_chance: float = 0.0
    start_screen: str = "results"
    auto: bool = False
    seed: int = 7


def _fit_scale(text: str, max_width: int, scale: float, thickness: int) -> float:
    while scale > 0.3:
        (tw, _), _ = cv2.getTextSize(text, FONT, scale, thickness)
        if tw <= max_width:
            break
        scale -= 0.05
    return scale


def _text(
    img: np.ndarray,
    text: str,
    center: tuple[int, int],
    scale: float = 1.0,
    color: tuple[int, int, int] = (255, 255, 255),
    thickness: int = 2,
    max_width: int | None = None,
) -> None:
    if max_width:
        scale = _fit_scale(text, max_width, scale, thickness)
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
    org = (center[0] - tw // 2, center[1] + th // 2)
    cv2.putText(img, text, org, FONT, scale, color, thickness, cv2.LINE_AA)


def _button(img: np.ndarray, box: Box, label: str, style: str) -> None:
    fill, border, fg = STYLES[style]
    p1, p2 = (box.x, box.y), (box.x + box.w - 1, box.y + box.h - 1)
    cv2.rectangle(img, p1, p2, fill, -1)
    cv2.rectangle(img, p1, p2, border, 5)
    _text(img, label, box.center, 1.1, fg, 2, max_width=box.w - 30)


def _canvas(color: tuple[int, int, int]) -> np.ndarray:
    img = np.empty((HEIGHT, WIDTH, 3), np.uint8)
    img[:] = color
    return img


def _header(img: np.ndarray, title: str, color: tuple[int, int, int] = (60, 40, 30)) -> None:
    cv2.rectangle(img, (0, 0), (WIDTH - 1, 110), color, -1)
    _text(img, title, (WIDTH // 2, 55), 1.3, (255, 255, 255), 2)


def _dialog(base: np.ndarray, title: str, title_color: tuple[int, int, int]) -> np.ndarray:
    img = (base.astype(np.float32) * 0.35).astype(np.uint8)
    cv2.rectangle(
        img, (PANEL.x, PANEL.y), (PANEL.x + PANEL.w, PANEL.y + PANEL.h), (236, 236, 240), -1
    )
    t = B["dialog_title"]
    cv2.rectangle(img, (t.x, t.y), (t.x + t.w, t.y + t.h), title_color, -1)
    _text(img, title, t.center, 1.2, (255, 255, 255), 2, max_width=t.w - 40)
    return img


class SimGame:
    """State machine + renderer for the fake game."""

    def __init__(
        self, config: SimConfig | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.cfg = config or SimConfig()
        self.clock = clock
        self.rng = random.Random(self.cfg.seed)
        self._lock = threading.RLock()
        self.screen = self.cfg.start_screen
        self.auto = self.cfg.auto
        self.act = self.cfg.act_max
        self.items = self.cfg.items
        self.stones = self.cfg.stones
        self.running = True
        self.visible = True
        self.in_quest = self.screen in {"battle", "error", "defeat"}
        self.dialog_return = "results"
        self.battle_progress = 0.0
        self.clears = 0
        self.defeats = 0
        self.revives = 0
        self.battles_started = 0
        self.history: list[str] = []
        self._error_at: float | None = None
        self._defeat_at: float | None = None
        now = clock()
        self._last_update = now
        self._act_clock = now

    # ----------------------------------------------------------------- engine
    def _goto(self, screen: str) -> None:
        self.screen = screen
        self.history.append(screen)
        del self.history[:-200]

    def _update(self) -> None:
        now = self.clock()
        dt = max(0.0, now - self._last_update)
        self._last_update = now
        if self.act >= self.cfg.act_max:
            self._act_clock = now
        else:
            gained = int((now - self._act_clock) // self.cfg.act_regen_seconds)
            if gained:
                self.act = min(self.cfg.act_max, self.act + gained)
                self._act_clock += gained * self.cfg.act_regen_seconds
        if self.running and self.screen == "battle" and self.auto:
            self.battle_progress += dt
            if self._error_at is not None and self.battle_progress >= self._error_at:
                self._error_at = None
                self._goto("error")
            elif self._defeat_at is not None and self.battle_progress >= self._defeat_at:
                self._defeat_at = None
                self._goto("defeat")
            elif self.battle_progress >= self.cfg.battle_seconds:
                self.in_quest = False
                self.clears += 1
                self._goto("clear")

    def _spend_act(self) -> bool:
        if self.act < self.cfg.act_cost:
            return False
        if self.act >= self.cfg.act_max:
            self._act_clock = self.clock()
        self.act -= self.cfg.act_cost
        return True

    def _start_battle(self) -> None:
        self.in_quest = True
        self.battles_started += 1
        self.battle_progress = 0.0
        span = max(0.5, self.cfg.battle_seconds - 1.0)
        self._error_at = (
            self.rng.uniform(0.5, span) if self.rng.random() < self.cfg.error_chance else None
        )
        self._defeat_at = (
            self.rng.uniform(0.5, span) if self.rng.random() < self.cfg.defeat_chance else None
        )
        self._goto("battle")

    # ------------------------------------------------------------------ input
    def tap(self, x: float, y: float) -> None:
        with self._lock:
            self._update()
            if not (self.running and self.visible):
                return
            handler = getattr(self, f"_tap_{self.screen}", None)
            if handler:
                handler(x, y)

    def _tap_results(self, x: float, y: float) -> None:
        if B["attempt_again"].contains(x, y):
            self.dialog_return = "results"
            enough = self.act >= self.cfg.act_cost
            self._goto("act_confirm" if enough else "act_empty")
        elif B["results_ok"].contains(x, y):
            self._goto("home")

    def _tap_act_confirm(self, x: float, y: float) -> None:
        if B["dialog_right"].contains(x, y):
            self._goto("team")
        elif B["dialog_left"].contains(x, y):
            self._goto(self.dialog_return)

    def _tap_act_empty(self, x: float, y: float) -> None:
        if B["act_use_item"].contains(x, y) and self.items > 0:
            self.items -= 1
            self.act += self.cfg.act_max
            self._goto(self.dialog_return)
        elif B["act_use_stone"].contains(x, y) and self.stones > 0:
            self.stones -= 1
            self.act += self.cfg.act_max
            self._goto(self.dialog_return)
        elif B["dialog_center"].contains(x, y):
            self._goto(self.dialog_return)

    def _tap_team(self, x: float, y: float) -> None:
        if B["start"].contains(x, y):
            if self._spend_act():
                self._start_battle()
            else:
                self.dialog_return = "team"
                self._goto("act_empty")

    def _tap_battle(self, x: float, y: float) -> None:
        if B["auto"].contains(x, y):
            self.auto = not self.auto

    def _tap_error(self, x: float, y: float) -> None:
        if B["dialog_center"].contains(x, y):
            self._goto("battle")

    def _tap_defeat(self, x: float, y: float) -> None:
        if B["revive"].contains(x, y) and self.stones > 0:
            self.stones -= 1
            self.revives += 1
            self._goto("battle")
        elif B["give_up"].contains(x, y):
            self.in_quest = False
            self.defeats += 1
            self._goto("results")

    def _tap_clear(self, x: float, y: float) -> None:
        if self.rng.random() < self.cfg.friend_request_chance:
            self._goto("friend")
        else:
            self._goto("results")

    def _tap_friend(self, x: float, y: float) -> None:
        if B["dialog_left"].contains(x, y) or B["dialog_right"].contains(x, y):
            self._goto("results")

    def _tap_title(self, x: float, y: float) -> None:
        self._goto("resume" if self.in_quest else "news")

    def _tap_resume(self, x: float, y: float) -> None:
        if B["dialog_right"].contains(x, y):
            self._goto("battle")
        elif B["dialog_left"].contains(x, y):
            self.in_quest = False
            self._goto("home")

    def _tap_news(self, x: float, y: float) -> None:
        if B["dialog_center"].contains(x, y):
            self._goto("home")

    def _tap_home(self, x: float, y: float) -> None:
        if B["nav_events"].contains(x, y):
            self._goto("events")

    def _tap_events(self, x: float, y: float) -> None:
        if B["nav_banner"].contains(x, y):
            self._goto("stages")

    def _tap_stages(self, x: float, y: float) -> None:
        if B["nav_stage"].contains(x, y):
            self._goto("difficulty")

    def _tap_difficulty(self, x: float, y: float) -> None:
        if B["nav_difficulty"].contains(x, y):
            self._goto("support")

    def _tap_support(self, x: float, y: float) -> None:
        if B["support_row"].contains(x, y):
            self._goto("team")

    def key(self, name: str) -> None:
        with self._lock:
            self._update()
            key = normalize_key(name)
            if key == "HOME":
                self.visible = False
            elif key == "BACK" and self.running and self.visible:
                back = {
                    "act_confirm": self.dialog_return,
                    "act_empty": self.dialog_return,
                    "friend": "results",
                    "news": "home",
                }
                if self.screen in back:
                    self._goto(back[self.screen])

    def launch(self) -> None:
        with self._lock:
            self._update()
            if not self.running:
                self.running = True
                self._goto("title")
            self.visible = True

    def force_stop(self) -> None:
        with self._lock:
            self._update()
            self.running = False
            self.visible = False

    def crash(self) -> None:
        """Simulate the game crashing (tests)."""
        self.force_stop()

    @property
    def in_front(self) -> bool:
        return self.running and self.visible

    # --------------------------------------------------------------- rendering
    def render(self) -> np.ndarray:
        with self._lock:
            self._update()
            if not self.in_front:
                return self._draw_launcher()
            return getattr(self, f"_draw_{self.screen}")()

    def _draw_launcher(self) -> np.ndarray:
        img = _canvas((48, 40, 36))
        for row in range(4):
            for col in range(4):
                x, y = 70 + col * 160, 260 + row * 190
                cv2.rectangle(img, (x, y), (x + 100, y + 100), (110, 95, 85), -1)
        _text(img, "ANDROID LAUNCHER", (WIDTH // 2, 140), 1.0, (210, 210, 210), 2)
        return img

    def _draw_results(self) -> np.ndarray:
        img = _canvas((70, 52, 40))
        _header(img, "QUEST RESULTS")
        _text(img, "STAGE 3 - Z-HARD", (WIDTH // 2, 210), 1.1, (240, 240, 240), 2)
        for i, line in enumerate(["EXP +12000", "ZENI +4800", "MEDALS x2"]):
            _text(img, line, (WIDTH // 2, 420 + i * 90), 1.0, (200, 230, 255), 2)
        _button(img, B["attempt_again"], "ATTEMPT AGAIN", "ok")
        _button(img, B["results_ok"], "OK", "ok")
        return img

    def _draw_act_confirm(self) -> np.ndarray:
        base = self._draw_team() if self.dialog_return == "team" else self._draw_results()
        img = _dialog(base, "USE ACT", (150, 100, 40))
        _text(
            img,
            f"Use {self.cfg.act_cost} ACT? ({self.act} left)",
            (WIDTH // 2, 600),
            0.9,
            (40, 40, 40),
            2,
        )
        _button(img, B["dialog_left"], "CANCEL", "cancel")
        _button(img, B["dialog_right"], "OK", "ok")
        return img

    def _draw_act_empty(self) -> np.ndarray:
        base = self._draw_team() if self.dialog_return == "team" else self._draw_results()
        img = _dialog(base, "NOT ENOUGH ACT", (40, 40, 180))
        if self.items > 0:
            _button(img, B["act_use_item"], "USE ACT ITEM", "nav")
        else:
            _button(img, B["act_use_item"], "NO ACT ITEMS LEFT", "auto_off")
        if self.stones > 0:
            _button(img, B["act_use_stone"], "USE DRAGON STONE", "danger")
        else:
            _button(img, B["act_use_stone"], "NO DRAGON STONES", "auto_off")
        _button(img, B["dialog_center"], "CANCEL", "cancel")
        return img

    def _draw_team(self) -> np.ndarray:
        img = _canvas((60, 45, 35))
        _header(img, "TEAM")
        for i in range(6):
            x, y = 50 + (i % 3) * 215, 260 + (i // 3) * 300
            cv2.rectangle(img, (x, y), (x + 190, y + 260), (120, 90, 70), -1)
            _text(img, f"UNIT {i + 1}", (x + 95, y + 130), 0.9, (230, 230, 230), 2)
        _text(img, f"ACT {self.act}/{self.cfg.act_max}", (190, 1170), 1.0, (180, 255, 180), 2)
        _button(img, B["start"], "START!", "start")
        return img

    def _draw_battle(self) -> np.ndarray:
        img = _canvas((50, 30, 20))
        t = self.battle_progress
        hp = max(0.0, 1.0 - t / self.cfg.battle_seconds)
        cv2.rectangle(img, (200, 160), (520, 460), (70, 70, 140), -1)
        _text(img, "ENEMY", (360, 310), 1.2, (255, 255, 255), 2)
        cv2.rectangle(img, (120, 500), (600, 530), (40, 40, 40), -1)
        cv2.rectangle(img, (120, 500), (120 + int(480 * hp), 530), (60, 60, 230), -1)
        wobble = self.clock()
        for i in range(8):
            cx = int(90 + i * 77 + 25 * math.sin(wobble * 3 + i))
            cy = int(950 + 120 * math.sin(wobble * 2 + i * 1.3))
            colour = [(60, 200, 60), (60, 60, 220), (220, 160, 40), (40, 200, 230)][i % 4]
            cv2.circle(img, (cx, cy), 38, colour, -1)
        _button(
            img,
            B["auto"],
            "AUTO ON" if self.auto else "AUTO OFF",
            "auto_on" if self.auto else "auto_off",
        )
        return img

    def _draw_error(self) -> np.ndarray:
        img = _dialog(self._draw_battle(), "CONNECTION ERROR", (40, 120, 200))
        _text(img, "Please check your connection.", (WIDTH // 2, 620), 0.8, (40, 40, 40), 2)
        _button(img, B["dialog_center"], "RETRY", "retry")
        return img

    def _draw_defeat(self) -> np.ndarray:
        img = _dialog(self._draw_battle(), "DEFEATED", (30, 30, 120))
        _button(img, B["revive"], "REVIVE (1 DRAGON STONE)", "danger")
        _button(img, B["give_up"], "GIVE UP", "cancel")
        return img

    def _draw_clear(self) -> np.ndarray:
        img = _canvas((30, 90, 140))
        _text(img, "STAGE CLEAR!", (WIDTH // 2, 520), 2.0, (255, 255, 255), 4)
        if int(self.clock() * 2) % 2 == 0:  # blinking prompt like the real thing
            _text(img, "TAP TO CONTINUE", (WIDTH // 2, 820), 1.0, (230, 240, 255), 2)
        return img

    def _draw_friend(self) -> np.ndarray:
        img = _dialog(self._draw_results(), "FRIEND REQUEST", (60, 140, 60))
        _text(img, "Send a friend request?", (WIDTH // 2, 620), 0.9, (40, 40, 40), 2)
        _button(img, B["dialog_left"], "CANCEL", "cancel")
        _button(img, B["dialog_right"], "OK", "ok")
        return img

    def _draw_title(self) -> np.ndarray:
        img = _canvas((20, 70, 140))
        b = B["title_logo"]
        cv2.rectangle(img, (b.x, b.y), (b.x + b.w, b.y + b.h), (20, 140, 250), -1)
        _text(img, "DOKKAN SIM", b.center, 1.8, (255, 255, 255), 4)
        _text(img, "TAP TO START", (WIDTH // 2, 1000), 1.0, (255, 255, 255), 2)
        return img

    def _draw_resume(self) -> np.ndarray:
        img = _dialog(self._draw_title(), "RESUME QUEST?", (150, 90, 30))
        _button(img, B["dialog_left"], "NO", "no")
        _button(img, B["dialog_right"], "YES", "nav")
        return img

    def _draw_news(self) -> np.ndarray:
        img = _dialog(self._draw_home(), "NEWS", (130, 60, 130))
        _text(img, "Welcome back, farmer!", (WIDTH // 2, 620), 0.9, (40, 40, 40), 2)
        _button(img, B["dialog_center"], "CLOSE", "cancel")
        return img

    def _draw_home(self) -> np.ndarray:
        img = _canvas((80, 60, 45))
        _header(img, "HOME")
        _text(img, f"ACT {self.act}/{self.cfg.act_max}", (WIDTH // 2, 220), 1.0, (200, 255, 200), 2)
        _button(img, B["nav_events"], "EVENTS", "nav")
        return img

    def _draw_events(self) -> np.ndarray:
        img = _canvas((75, 55, 45))
        _header(img, "EVENTS")
        _button(img, B["nav_banner"], "LIMITED EVENT: SIM SAGA", "start")
        return img

    def _draw_stages(self) -> np.ndarray:
        img = _canvas((75, 55, 45))
        _header(img, "SIM SAGA")
        for i, label in enumerate(["STAGE 1", "STAGE 2"]):
            box = Box(60, 280 + i * 120, 600, 100)
            cv2.rectangle(img, (box.x, box.y), (box.x + box.w, box.y + box.h), (90, 70, 60), -1)
            _text(img, label, box.center, 1.0, (200, 200, 200), 2)
        _button(img, B["nav_stage"], "STAGE 3", "nav")
        return img

    def _draw_difficulty(self) -> np.ndarray:
        img = _canvas((75, 55, 45))
        _header(img, "STAGE 3")
        _button(img, B["nav_difficulty"], "Z-HARD", "danger")
        return img

    def _draw_support(self) -> np.ndarray:
        img = _canvas((70, 50, 40))
        _header(img, "SELECT A SUPPORT")
        _button(img, B["support_row"], "SUPPORTER #1  LV 120", "nav")
        return img

    # ------------------------------------------------------------- templates
    @classmethod
    def template_shots(cls) -> Iterator[tuple[str, np.ndarray, Box, bool, str]]:
        """Yield ``(name, frame, box, search_full, description)`` for the demo templates."""

        def shot(screen: str, **state: Any) -> np.ndarray:
            game = cls(SimConfig(start_screen=screen), clock=lambda: 0.0)
            for key, value in state.items():
                setattr(game, key, value)
            return game.render()

        yield (
            "attempt_again",
            shot("results"),
            B["attempt_again"],
            False,
            "ATTEMPT AGAIN button on the results screen",
        )
        yield ("start_button", shot("team"), B["start"], False, "START! on the team screen")
        yield ("ok_button", shot("act_confirm"), B["dialog_right"], True, "Any OK button")
        yield ("cancel_button", shot("act_confirm"), B["dialog_left"], True, "Any CANCEL button")
        yield (
            "act_empty_dialog",
            shot("act_empty"),
            B["dialog_title"],
            False,
            "Title of the not-enough-ACT dialog",
        )
        yield ("act_use_item", shot("act_empty"), B["act_use_item"], False, "Use ACT item")
        yield ("act_use_stone", shot("act_empty"), B["act_use_stone"], False, "Use Dragon Stone")
        yield (
            "friend_request",
            shot("friend"),
            B["dialog_title"],
            False,
            "Title of the friend request popup",
        )
        yield ("auto_off", shot("battle", auto=False), B["auto"], False, "AUTO OFF toggle")
        yield ("error_retry", shot("error"), B["dialog_center"], False, "RETRY on error dialogs")
        yield ("defeat_give_up", shot("defeat"), B["give_up"], False, "GIVE UP after a defeat")
        yield ("title_screen", shot("title"), B["title_logo"], False, "Title screen logo")
        yield (
            "resume_yes",
            shot("resume", in_quest=True),
            B["dialog_right"],
            False,
            "YES on the resume-quest dialog",
        )
        yield ("close_button", shot("news"), B["dialog_center"], False, "CLOSE on news popups")
        yield ("nav_1", shot("home"), B["nav_events"], False, "EVENTS button on home")
        yield ("nav_2", shot("events"), B["nav_banner"], False, "Event banner")
        yield ("nav_3", shot("stages"), B["nav_stage"], False, "Stage 3")
        yield ("nav_4", shot("difficulty"), B["nav_difficulty"], False, "Z-HARD difficulty")
        yield ("select_support", shot("support"), B["support_row"], False, "First supporter")


def install_sim_templates(store: Any) -> int:
    """(Re)generate the simulator's templates into a :class:`TemplateStore`."""
    count = 0
    for name, frame, box, search_full, description in SimGame.template_shots():
        store.create_from_frame(
            name,
            frame,
            box.as_tuple(),
            threshold=0.9,
            search_full=search_full,
            description=f"[demo] {description}",
        )
        count += 1
    return count


class SimDevice(Device):
    backend = "sim"

    def __init__(
        self,
        game: SimGame | None = None,
        package: str = GAME_PACKAGES["global"],
    ) -> None:
        self.game = game or SimGame()
        self.package = package
        self.reachable = True
        self.taps: list[tuple[int, int]] = []
        self.keys: list[str] = []

    @property
    def serial(self) -> str:
        return "simulator"

    def _check(self) -> None:
        if not self.reachable:
            raise DeviceError("simulator is unreachable")

    def connect(self) -> None:
        self._check()

    def screenshot(self) -> np.ndarray:
        self._check()
        return self.game.render()

    def tap(self, x: int, y: int) -> None:
        self._check()
        self.taps.append((int(x), int(y)))
        del self.taps[:-500]
        self.game.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._check()

    def key(self, name: str) -> None:
        self._check()
        self.keys.append(normalize_key(name))
        self.game.key(name)

    def launch_app(self, package: str) -> None:
        self._check()
        if package != self.package:
            raise DeviceError(f"{package} is not installed on the simulator")
        self.game.launch()

    def stop_app(self, package: str) -> None:
        self._check()
        if package == self.package:
            self.game.force_stop()

    def foreground_package(self) -> str | None:
        self._check()
        return self.package if self.game.in_front else LAUNCHER_PACKAGE

    def info(self) -> dict[str, Any]:
        return {
            "manufacturer": "Dokkan Farmer",
            "model": "Simulator (demo)",
            "android": "-",
            "resolution": f"{WIDTH}x{HEIGHT}",
            "act": self.game.act,
            "clears": self.game.clears,
        }
