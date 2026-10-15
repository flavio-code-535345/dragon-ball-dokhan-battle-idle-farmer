from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dokkan_farmer.device.sim import SimConfig, SimDevice, SimGame, install_sim_templates
from dokkan_farmer.engine import Farmer
from dokkan_farmer.library import RoutineLibrary
from dokkan_farmer.routine import Routine
from dokkan_farmer.settings import Settings
from dokkan_farmer.state import StateStore
from dokkan_farmer.vision import Matcher, TemplateStore


class VirtualClock:
    """Deterministic time: sleeping just advances the clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture(scope="session")
def sim_templates_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("sim-templates")
    install_sim_templates(TemplateStore(directory))
    return directory


@dataclass
class SimEnv:
    settings: Settings
    clock: VirtualClock
    game: SimGame
    device: SimDevice
    store: TemplateStore
    library: RoutineLibrary
    state: StateStore
    make_farmer: Callable[..., Farmer]

    @property
    def routine(self) -> Routine:
        return self.library.load("dokkan_auto_farm")

    def run(
        self,
        minutes: float,
        options: dict[str, Any] | None = None,
        farmer: Farmer | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        farmer = farmer or self.make_farmer()
        routine = self.routine
        end = self.clock.t + minutes * 60

        def until() -> bool:
            if on_tick is not None:
                on_tick()
            return self.clock.t >= end

        return farmer.run_session(
            "dokkan_auto_farm", routine, routine.option_values(options), until=until
        )


@pytest.fixture
def make_sim(tmp_path: Path, clock: VirtualClock, sim_templates_dir: Path):
    def factory(config: SimConfig | None = None, **settings_overrides: Any) -> SimEnv:
        settings = Settings(data_dir=tmp_path / "data", device_backend="sim", **settings_overrides)
        shutil.copytree(sim_templates_dir, settings.templates_dir)
        store = TemplateStore(settings.templates_dir)
        game = SimGame(config or SimConfig(), clock=clock)
        device = SimDevice(game, package=settings.game_package)
        library = RoutineLibrary(settings.routines_dir)
        library.install_defaults()
        state = StateStore(settings.state_file)

        def make_farmer(**kwargs: Any) -> Farmer:
            return Farmer(
                device,
                Matcher(store, settings.match_scale),
                settings,
                state=state,
                clock=clock,
                wall=clock,
                sleeper=clock.sleep,
                **kwargs,
            )

        return SimEnv(settings, clock, game, device, store, library, state, make_farmer)

    return factory
