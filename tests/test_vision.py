from __future__ import annotations

import cv2
import numpy as np
import pytest

from dokkan_farmer.vision import Matcher, TemplateStore, validate_name


def background(width: int = 720, height: int = 1280, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(40, 90, size=(height // 8, width // 8, 3), dtype=np.uint8)
    return cv2.resize(noise, (width, height), interpolation=cv2.INTER_NEAREST)


def draw_button(img: np.ndarray, x: int, y: int, text: str, color=(40, 190, 250)) -> None:
    cv2.rectangle(img, (x, y), (x + 220, y + 80), color, -1)
    cv2.rectangle(img, (x, y), (x + 220, y + 80), (20, 20, 20), 4)
    cv2.putText(img, text, (x + 30, y + 55), cv2.FONT_HERSHEY_DUPLEX, 1.3, (20, 20, 20), 3)


@pytest.fixture
def store(tmp_path) -> TemplateStore:
    return TemplateStore(tmp_path / "templates")


def test_finds_template_where_it_was_captured(store):
    frame = background()
    draw_button(frame, 250, 900, "AGAIN")
    store.create_from_frame("again", frame, (250, 900, 221, 81))
    match = Matcher(store).analyze(frame).find("again")
    assert match is not None and match.found
    assert match.score > 0.93  # downscaled matching is not pixel-exact
    x, y, w, h = match.box
    assert abs(x - 250) <= 4 and abs(y - 900) <= 4
    assert abs(w - 221) <= 4 and abs(h - 81) <= 4


def test_not_found_on_other_screen(store):
    frame = background()
    draw_button(frame, 250, 900, "AGAIN")
    store.create_from_frame("again", frame, (250, 900, 221, 81))
    other = background(seed=2)
    draw_button(other, 250, 900, "QUIT")
    analysis = Matcher(store).analyze(other)
    assert not analysis.visible("again")
    assert analysis.find("missing") is None


def test_rescales_to_other_resolutions(store):
    frame = background()
    draw_button(frame, 250, 900, "AGAIN")
    store.create_from_frame("again", frame, (250, 900, 221, 81), search_full=True)
    bigger = cv2.resize(frame, (1080, 1920), interpolation=cv2.INTER_LINEAR)
    match = Matcher(store).analyze(bigger).find("again")
    assert match.found
    assert abs(match.box[0] - 375) <= 8 and abs(match.box[1] - 1350) <= 8


def test_region_restricts_search(store):
    frame = background()
    draw_button(frame, 250, 900, "AGAIN")
    store.create_from_frame("again", frame, (250, 900, 221, 81))  # "near" search
    moved = background()
    draw_button(moved, 250, 200, "AGAIN")
    assert not Matcher(store).analyze(moved).visible("again")
    store.update("again", search_full=True)
    assert Matcher(store).analyze(moved).visible("again")


def test_dimmed_copy_behind_popup_is_ignored(store):
    frame = background()
    draw_button(frame, 250, 900, "OK")
    store.create_from_frame("ok", frame, (250, 900, 221, 81), search_full=True)
    dimmed = (frame.astype(np.float32) * 0.35).astype(np.uint8)
    assert not Matcher(store).analyze(dimmed).visible("ok")
    draw_button(dimmed, 250, 300, "OK")  # a real, bright OK in a dialog
    match = Matcher(store).analyze(dimmed).find("ok")
    assert match.found
    assert abs(match.box[1] - 300) <= 4


def test_colour_mode_tells_equal_luminance_colours_apart(store):
    frame = background()
    draw_button(frame, 250, 900, "ON", color=(0, 128, 0))  # green
    other = background()
    draw_button(other, 250, 900, "ON", color=(0, 0, 255))  # red, same grey level
    store.create_from_frame("auto_on", frame, (250, 900, 221, 81))
    assert Matcher(store).analyze(other).visible("auto_on")  # grey can't tell
    store.update("auto_on", mode="color")
    assert Matcher(store).analyze(frame).visible("auto_on")
    assert not Matcher(store).analyze(other).visible("auto_on")


def test_rejects_flat_and_tiny_selections(store):
    frame = np.full((1280, 720, 3), 120, np.uint8)
    with pytest.raises(ValueError, match="flat"):
        store.create_from_frame("flat", frame, (10, 10, 100, 100))
    with pytest.raises(ValueError, match="small"):
        store.create_from_frame("tiny", background(), (10, 10, 4, 4))


def test_persistence_update_delete(tmp_path):
    frame = background()
    draw_button(frame, 100, 100, "GO")
    store = TemplateStore(tmp_path / "t")
    store.create_from_frame("go", frame, (100, 100, 221, 81), threshold=0.9, description="Go!")
    reloaded = TemplateStore(tmp_path / "t")
    meta, image = reloaded.get("go")
    assert meta.threshold == 0.9 and meta.description == "Go!"
    assert meta.source_size == [720, 1280]
    assert image.shape[:2] == (81, 221)
    reloaded.update("go", threshold=0.7, mode="color")
    assert TemplateStore(tmp_path / "t").get("go")[0].mode == "color"
    reloaded.delete("go")
    assert not TemplateStore(tmp_path / "t").has("go")
    with pytest.raises(KeyError):
        reloaded.delete("go")


def test_png_dropped_in_folder_is_picked_up(tmp_path):
    frame = background()
    draw_button(frame, 100, 100, "GO")
    directory = tmp_path / "t"
    directory.mkdir()
    cv2.imwrite(str(directory / "manual.png"), frame[100:181, 100:321])
    store = TemplateStore(directory)
    assert store.has("manual")
    assert Matcher(store).analyze(frame).visible("manual")


def test_ambiguous_template_has_two_candidates(store):
    frame = background()
    draw_button(frame, 100, 300, "OK")
    draw_button(frame, 400, 900, "OK")
    store.create_from_frame("ok", frame, (100, 300, 221, 81), search_full=True)
    candidates = Matcher(store).analyze(frame).candidates("ok", 2)
    assert len(candidates) == 2
    assert candidates[1][0] > 0.9


@pytest.mark.parametrize("name", ["", "../etc", "Has Space", "x" * 65, "-dash"])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_valid_name_is_lowercased():
    assert validate_name("Attempt_Again") == "attempt_again"
