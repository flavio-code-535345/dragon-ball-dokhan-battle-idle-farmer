from __future__ import annotations

import textwrap

import pytest

from dokkan_farmer.library import BUNDLED_DIR, RoutineLibrary
from dokkan_farmer.routine import (
    Action,
    RoutineError,
    format_text,
    loose_equal,
    parse_action,
    parse_routine,
)

DEFAULT_RULES = """
- name: Tap OK
  when: {visible: ok_button}
  do: [{tap: ok_button}]
"""


def minimal(extra: str = "", rules: str | None = None) -> str:
    """A small routine: ``extra`` top-level YAML plus a rules list."""
    rules = textwrap.dedent(rules or DEFAULT_RULES).strip("\n")
    extra = textwrap.dedent(extra).strip("\n")
    head = f"name: Test\n{extra}\n" if extra else "name: Test\n"
    return f"{head}rules:\n{textwrap.indent(rules, '  ')}\n"


def test_bundled_routines_are_valid():
    paths = sorted(BUNDLED_DIR.glob("*.yaml"))
    assert paths, "no bundled routines found"
    for path in paths:
        routine = parse_routine(path.read_text(encoding="utf-8"))
        assert routine.rules


def test_default_routine_documents_every_template():
    routine = parse_routine((BUNDLED_DIR / "dokkan_auto_farm.yaml").read_text(encoding="utf-8"))
    assert routine.referenced_templates() <= set(routine.templates)
    required = {name for name, doc in routine.templates.items() if doc.level == "required"}
    assert required == {"attempt_again", "start_button", "ok_button"}
    assert routine.counters["runs"].appears == "attempt_again"
    assert routine.counters["runs"].rearm == "start_button"
    assert routine.settings.stop_after_runs == "$max_runs"


def test_option_values_and_coercion():
    routine = parse_routine(
        minimal(
            """
        options:
          runs: {type: number, default: 5, min: 0, max: 10}
          policy: {type: choice, choices: [wait, stop]}
          flag: {type: bool, default: "yes"}
          note: {type: text}
        """
        )
    )
    values = routine.option_values()
    assert values == {"runs": 5, "policy": "wait", "flag": True, "note": ""}
    values = routine.option_values({"runs": "7", "flag": "off", "unknown": 1})
    assert values["runs"] == 7 and values["flag"] is False and "unknown" not in values
    assert routine.option_values({"runs": 2.5})["runs"] == 2.5
    with pytest.raises(RoutineError, match="at most 10"):
        routine.option_values({"runs": 11})
    with pytest.raises(RoutineError, match="one of"):
        routine.option_values({"policy": "panic"})
    with pytest.raises(RoutineError, match="not a number"):
        routine.option_values({"runs": "lots"})


@pytest.mark.parametrize(
    ("raw", "kind", "args"),
    [
        ({"tap": "ok_button"}, "tap", {"template": "ok_button", "offset": [0.0, 0.0]}),
        (
            {"tap": {"template": "ok", "offset": [0.2, -0.1]}},
            "tap",
            {"template": "ok", "offset": [0.2, -0.1]},
        ),
        ({"tap_xy": [0.5, 0.25]}, "tap_xy", {"x": 0.5, "y": 0.25}),
        ({"tap_xy": ["$x", 100]}, "tap_xy", {"x": "$x", "y": 100.0}),
        (
            {"swipe": [0.1, 0.2, 0.3, 0.4]},
            "swipe",
            {"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4, "ms": 300.0},
        ),
        (
            {"swipe": {"from": [1, 2], "to": [3, 4], "ms": 50}},
            "swipe",
            {"x1": 1.0, "y1": 2.0, "x2": 3.0, "y2": 4.0, "ms": 50.0},
        ),
        ({"key": "back"}, "key", {"key": "BACK"}),
        ({"wait": 2}, "wait", {"seconds": 2.0}),
        ({"wait_minutes": "$m"}, "wait_minutes", {"minutes": "$m"}),
        ({"count": "runs"}, "count", {"counter": "runs", "by": 1}),
        ({"count": {"counter": "x", "by": 3}}, "count", {"counter": "x", "by": 3}),
        ({"reset": "x"}, "reset", {"counter": "x"}),
        ({"notify": "hi {runs}"}, "notify", {"text": "hi {runs}"}),
        ({"stop": None}, "stop", {"text": ""}),
        ("restart_app", "restart_app", {}),
        ({"screenshot": "label"}, "screenshot", {"label": "label"}),
    ],
)
def test_action_shorthands(raw, kind, args):
    parsed = parse_action(raw)
    assert parsed == {"kind": kind, "args": args}
    assert Action.model_validate(raw).kind == kind


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("dance", "unknown action"),
        ({"tapp": "x"}, "exactly one of"),
        ({"tap": "x", "wait": 1}, "exactly one of"),
        ({"tap": "Bad Name"}, "Template names"),
        ({"tap": "ok", "offset": [9, 0]}, "offset"),
        ({"key": "VOLUME_UP"}, "Unknown key"),
        ({"wait": "soon"}, "number"),
        ({"tap_xy": [1]}, r"\[x, y\]"),
        ({"count": "bad name"}, "counter"),
        ({"wait": 1, "extra": True}, "unexpected"),
    ],
)
def test_action_errors(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_action(raw)


def test_unknown_option_reference_is_rejected():
    with pytest.raises(RoutineError, match=r"\$nope"):
        parse_routine(minimal("settings: {stop_after_runs: $nope}"))


def test_option_condition_must_match_declared_option():
    rules = """
      - name: Wait
        when: {option: {policy: sleep}}
        do: [{wait: 1}]
    """
    with pytest.raises(RoutineError, match="unknown option"):
        parse_routine(minimal(rules=rules))
    extra = "options: {policy: {type: choice, choices: [wait, stop]}}"
    with pytest.raises(RoutineError, match="one of"):
        parse_routine(minimal(extra, rules))


def test_rule_needs_a_condition_and_an_action():
    with pytest.raises(RoutineError, match="at least one condition"):
        parse_routine(minimal(rules="- {name: X, when: {}, do: [{wait: 1}]}"))
    with pytest.raises(RoutineError, match="do"):
        parse_routine(minimal(rules="- {name: X, when: {always: true}, do: []}"))


def test_yaml_and_structure_errors_are_readable():
    with pytest.raises(RoutineError, match="YAML syntax"):
        parse_routine("name: [unclosed")
    with pytest.raises(RoutineError, match="mapping"):
        parse_routine("- just a list")
    with pytest.raises(RoutineError, match="rules"):
        parse_routine("name: No rules")
    with pytest.raises(RoutineError, match="Extra inputs"):
        parse_routine(minimal("colour: blue"))


def test_template_shorthand_and_checklist():
    routine = parse_routine(minimal("templates: {ok_button: 'The OK button'}"))
    assert routine.templates["ok_button"].description == "The OK button"
    rules = """
      - name: Close
        when: {visible: dialog}
        do: [{tap: close}]
    """
    routine = parse_routine(minimal(rules=rules))
    assert set(routine.template_checklist()) == {"dialog", "close"}


def test_format_text_keeps_unknown_placeholders():
    assert format_text("{runs} runs, {missing}", {"runs": 3}) == "3 runs, {missing}"
    assert format_text("broken {", {}) == "broken {"


@pytest.mark.parametrize(
    ("actual", "expected", "result"),
    [
        (True, "true", True),
        (False, "yes", False),
        (5, "5", True),
        (5.0, 5, True),
        ("wait", "wait", True),
        ("wait", "stop", False),
    ],
)
def test_loose_equal(actual, expected, result):
    assert loose_equal(actual, expected) is result


def test_library_install_save_restore(tmp_path):
    library = RoutineLibrary(tmp_path / "routines")
    assert "dokkan_auto_farm" in library.install_defaults()
    assert library.install_defaults() == []  # never overwrites
    original = library.read_text("dokkan_auto_farm")
    library.save_text("dokkan_auto_farm", original.replace("name: Dokkan Auto Farm", "name: Mine"))
    info = {i.id: i for i in library.list()}["dokkan_auto_farm"]
    assert info.name == "Mine" and info.modified and info.bundled
    with pytest.raises(RoutineError):
        library.save_text("dokkan_auto_farm", "name: broken")
    assert library.load("dokkan_auto_farm").name == "Mine"  # bad save did not clobber it
    library.restore("dokkan_auto_farm")
    assert library.read_text("dokkan_auto_farm") == original
    library.save_text("copy", original)
    library.delete("copy")
    assert not library.exists("copy")
    with pytest.raises(RoutineError):
        library.path("../evil")


def test_sanitize_options_drops_stale_values():
    routine = parse_routine(
        minimal("options: {policy: {type: choice, choices: [wait, stop]}, n: {type: number}}")
    )
    saved = {"policy": "removed-choice", "n": "4", "gone": 1}
    assert routine.sanitize_options(saved) == {"n": 4}
    assert routine.option_values(routine.sanitize_options(saved)) == {"policy": "wait", "n": 4}


def test_colours_are_declared_and_checked():
    base = (
        "name: C\nrules:\n  - name: Arrow\n"
        "    when: {color_visible: arrow}\n    do: [{tap_color: arrow}]\n"
    )
    with pytest.raises(RoutineError, match="unknown colour"):
        parse_routine(base)
    colors = "colors:\n  arrow: {hsv_min: [95, 200, 150], hsv_max: [115, 255, 255]}\n"
    routine = parse_routine(colors + base)
    assert routine.colors["arrow"].min_area == 0.001
    assert routine.rules[0].do[0].args == {"color": "arrow"}
    bad = "colors:\n  arrow: {hsv_min: [200, 0, 0], hsv_max: [115, 255, 255]}\n"
    with pytest.raises(RoutineError, match="hue 0-179"):
        parse_routine(bad + base)
    upside = "colors:\n  arrow: {hsv_min: [95, 255, 0], hsv_max: [115, 200, 255]}\n"
    with pytest.raises(RoutineError, match="hsv_min"):
        parse_routine(upside + base)
    wrap = "colors:\n  arrow: {hsv_min: [172, 200, 150], hsv_max: [6, 255, 255]}\n"  # red
    assert parse_routine(wrap + base).colors["arrow"].hsv_min[0] == 172
    absent = base.replace("color_visible", "color_absent")
    with pytest.raises(RoutineError, match="unknown colour"):
        parse_routine(absent)
    assert parse_routine(colors + absent).rules[0].when.color_absent == ["arrow"]


def test_colour_near_must_be_a_point_on_the_screen():
    base = (
        "name: C\nrules:\n  - name: Arrow\n"
        "    when: {color_visible: arrow}\n    do: [{tap_color: arrow}]\n"
    )
    ok = (
        "colors:\n  arrow: {hsv_min: [95, 200, 150], hsv_max: [115, 255, 255], near: [0.5, 0.46]}\n"
    )
    assert parse_routine(ok + base).colors["arrow"].near == [0.5, 0.46]
    bad = ok.replace("[0.5, 0.46]", "[0.5, 1.4]")
    with pytest.raises(RoutineError, match="near"):
        parse_routine(bad + base)
