"""Routine files: YAML rule lists that tell the farmer what to tap and when.

Every tick the engine takes a screenshot and fires the *first* rule whose
``when`` conditions hold (respecting cooldowns). See ``docs/ROUTINES.md``.
"""

from __future__ import annotations

import math
import re
import string
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .device.base import normalize_key
from .vision import validate_name

REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")
COUNTER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


class RoutineError(ValueError):
    """A routine could not be parsed, validated or configured."""


# --------------------------------------------------------------------- helpers
def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(REF_RE.match(value))


def resolve(value: Any, options: dict[str, Any]) -> Any:
    """Replace a ``$option`` reference by the option's current value."""
    if is_ref(value):
        return options[value[1:]]
    return value


def resolve_number(value: Any, options: dict[str, Any]) -> float:
    resolved = resolve(value, options)
    if isinstance(resolved, bool):
        return float(resolved)
    try:
        return float(resolved)
    except (TypeError, ValueError):
        raise RoutineError(f"Expected a number, got {resolved!r}") from None


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_text(template: str, values: dict[str, Any]) -> str:
    """``str.format`` that leaves unknown ``{placeholders}`` untouched."""
    try:
        return string.Formatter().vformat(template, (), _SafeDict(values))
    except (ValueError, IndexError, AttributeError, KeyError):
        return template


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _num_or_ref(value: Any) -> float | str:
    if isinstance(value, bool):
        raise ValueError("expected a number")
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        return float(value)
    if isinstance(value, str):
        if is_ref(value):
            return value
        try:
            return float(value)
        except ValueError:
            pass
    raise ValueError(f"expected a number or a $option reference, got {value!r}")


def _counter_name(value: Any) -> str:
    if not isinstance(value, str) or not COUNTER_RE.match(value):
        raise ValueError(f"invalid counter name {value!r} (letters, digits and '_')")
    return value


NameList = Annotated[list[str], BeforeValidator(_as_list)]
NumRef = Annotated[float | str, BeforeValidator(_num_or_ref)]


# --------------------------------------------------------------------- options
class OptionSpec(BaseModel):
    """A setting the user can change in the web UI before starting a routine."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["number", "choice", "bool", "text"] = "number"
    default: Any = None
    label: str = ""
    help: str = ""
    choices: list[str] | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self) -> OptionSpec:
        if self.type == "choice":
            if not self.choices:
                raise ValueError("choice options need a non-empty 'choices' list")
            self.choices = [str(c) for c in self.choices]
            if self.default is None:
                self.default = self.choices[0]
        elif self.default is None:
            self.default = {"number": 0, "bool": False, "text": ""}[self.type]
        self.default = self.coerce(self.default)
        return self

    def coerce(self, value: Any) -> Any:
        if self.type == "number":
            if isinstance(value, bool):
                raise ValueError(f"{value!r} is not a number")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{value!r} is not a number") from None
            if not math.isfinite(number):
                raise ValueError("must be a finite number")
            if self.min is not None and number < self.min:
                raise ValueError(f"must be at least {self.min:g}")
            if self.max is not None and number > self.max:
                raise ValueError(f"must be at most {self.max:g}")
            return int(number) if number.is_integer() else number
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, int | float):
                return bool(value)
            text = str(value).strip().lower()
            if text in TRUE_WORDS:
                return True
            if text in FALSE_WORDS:
                return False
            raise ValueError(f"{value!r} is not true/false")
        if self.type == "choice":
            text = str(value)
            if self.choices is not None and text not in self.choices:
                raise ValueError(f"must be one of: {', '.join(self.choices)}")
            return text
        return str(value)


def loose_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):

        def as_bool(v: Any) -> bool:
            return str(v).strip().lower() in TRUE_WORDS if isinstance(v, str) else bool(v)

        return as_bool(actual) == as_bool(expected)
    if isinstance(actual, int | float) and isinstance(expected, int | float | str):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return str(actual) == str(expected)


# ---------------------------------------------------------------------- colours
class ColorSpec(BaseModel):
    """A colour to look for: blobs whose pixels are within an HSV range (OpenCV scales:
    hue 0-179, saturation and value 0-255), sized ``min_area``..``max_area`` of the screen."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    hsv_min: list[int]
    hsv_max: list[int]
    min_area: float = Field(0.001, gt=0, le=1)
    max_area: float = Field(0.05, gt=0, le=1)
    #: ``[x, y, w, h]`` fractions of the screen to look in; ``None`` = all of it.
    region: list[float] | None = None

    @field_validator("hsv_min", "hsv_max")
    @classmethod
    def _hsv(cls, value: list[int]) -> list[int]:
        if len(value) != 3 or not (0 <= value[0] <= 179) or not all(0 <= v <= 255 for v in value):
            raise ValueError("an HSV colour is [hue 0-179, saturation 0-255, value 0-255]")
        return value

    @field_validator("region")
    @classmethod
    def _region(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and (len(value) != 4 or not all(0 <= v <= 1 for v in value)):
            raise ValueError("region is [x, y, w, h], fractions of the screen")
        return value

    @model_validator(mode="after")
    def _ranges(self) -> ColorSpec:
        if any(lo > hi for lo, hi in zip(self.hsv_min, self.hsv_max, strict=True)):
            raise ValueError("hsv_min must not be above hsv_max")
        if self.min_area > self.max_area:
            raise ValueError("min_area must not be above max_area")
        return self


# ------------------------------------------------------------------ conditions
class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: NameList = []
    any_visible: NameList = []
    not_visible: NameList = []
    static_for: NumRef | None = None
    idle_for: NumRef | None = None
    option: dict[str, Any] = {}
    counter_below: dict[str, NumRef] = {}
    counter_at_least: dict[str, NumRef] = {}
    #: Colours (from the routine's ``colors``) that must all be on screen.
    color_visible: NameList = []
    always: bool = False

    @field_validator("visible", "any_visible", "not_visible", "color_visible")
    @classmethod
    def _template_names(cls, names: list[str]) -> list[str]:
        return [validate_name(n) for n in names]

    @field_validator("counter_below", "counter_at_least")
    @classmethod
    def _counter_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            _counter_name(name)
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> Condition:
        if not (
            self.visible
            or self.any_visible
            or self.not_visible
            or self.static_for is not None
            or self.idle_for is not None
            or self.option
            or self.counter_below
            or self.counter_at_least
            or self.color_visible
            or self.always
        ):
            raise ValueError("'when' needs at least one condition (or 'always: true')")
        return self

    def templates(self) -> set[str]:
        return set(self.visible) | set(self.any_visible) | set(self.not_visible)


# --------------------------------------------------------------------- actions
SIMPLE_ACTIONS = {"launch_app", "stop_app", "restart_app", "screenshot"}
ACTION_KINDS = {
    "tap",
    "tap_color",
    "tap_xy",
    "swipe",
    "key",
    "wait",
    "wait_minutes",
    "count",
    "reset",
    "notify",
    "log",
    "stop",
    *SIMPLE_ACTIONS,
}


def _point(value: Any, what: str) -> list[float | str]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"{what} must be [x, y]")
    return [_num_or_ref(v) for v in value]


def parse_action(raw: Any) -> dict[str, Any]:
    """Normalise the YAML shorthands into ``{"kind": ..., "args": {...}}``."""
    if isinstance(raw, str):
        name = raw.strip()
        if name in SIMPLE_ACTIONS:
            return {"kind": name, "args": {}}
        raise ValueError(f"unknown action {raw!r}")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("an action must be a mapping such as {tap: ok_button}")
    kinds = [k for k in raw if k in ACTION_KINDS]
    if len(kinds) != 1:
        raise ValueError(
            f"an action needs exactly one of {', '.join(sorted(ACTION_KINDS))}; got {sorted(raw)}"
        )
    kind = kinds[0]
    value = raw[kind]
    extra = {k: v for k, v in raw.items() if k != kind}
    args: dict[str, Any]

    if kind == "tap":
        if isinstance(value, dict):
            template = value.get("template", value.get("name"))
            offset = value.get("offset", extra.pop("offset", [0, 0]))
        else:
            template = value
            offset = extra.pop("offset", [0, 0])
        if not isinstance(template, str):
            raise ValueError("tap needs a template name")
        if not isinstance(offset, list | tuple) or len(offset) != 2:
            raise ValueError("tap offset must be [dx, dy] (fractions of the template size)")
        dx, dy = (float(v) for v in offset)
        if abs(dx) > 3 or abs(dy) > 3:
            raise ValueError("tap offset is a fraction of the template size, keep it within ±3")
        args = {"template": validate_name(template), "offset": [dx, dy]}
    elif kind == "tap_color":
        if not isinstance(value, str):
            raise ValueError("tap_color needs the name of a colour from 'colors'")
        args = {"color": validate_name(value)}
    elif kind == "tap_xy":
        x, y = _point(value, "tap_xy")
        args = {"x": x, "y": y}
    elif kind == "swipe":
        if isinstance(value, dict):
            (x1, y1), (x2, y2) = _point(value.get("from"), "from"), _point(value.get("to"), "to")
            ms = _num_or_ref(value.get("ms", 300))
        elif isinstance(value, list | tuple) and len(value) in (4, 5):
            x1, y1, x2, y2 = (_num_or_ref(v) for v in value[:4])
            ms = _num_or_ref(value[4]) if len(value) == 5 else 300.0
        else:
            raise ValueError("swipe needs {from: [x, y], to: [x, y], ms: 300}")
        args = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}
    elif kind == "key":
        args = {"key": normalize_key(str(value))}
    elif kind == "wait":
        args = {"seconds": _num_or_ref(value)}
    elif kind == "wait_minutes":
        args = {"minutes": _num_or_ref(value)}
    elif kind == "count":
        if isinstance(value, dict):
            args = {"counter": _counter_name(value.get("counter")), "by": int(value.get("by", 1))}
        else:
            args = {"counter": _counter_name(value), "by": int(extra.pop("by", 1))}
    elif kind == "reset":
        args = {"counter": _counter_name(value)}
    elif kind in {"notify", "log", "stop"}:
        args = {"text": "" if value is None else str(value)}
    elif kind == "screenshot":
        args = {"label": "" if value in (None, True) else str(value)}
    else:  # launch_app / stop_app / restart_app
        args = {}
    if extra:
        raise ValueError(f"unexpected key(s) {sorted(extra)} in {kind} action")
    return {"kind": kind, "args": args}


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    args: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, raw: Any) -> Any:
        normalized = isinstance(raw, dict) and set(raw) <= {"kind", "args"}
        if normalized and raw.get("kind") in ACTION_KINDS:
            return raw
        return parse_action(raw)

    def describe(self) -> str:
        if not self.args:
            return self.kind
        if self.kind == "tap":
            return f"tap {self.args['template']}"
        values = ", ".join(str(v) for v in self.args.values())
        return f"{self.kind} {values}"


# ----------------------------------------------------------------------- rules
class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    when: Condition
    do: list[Action] = Field(min_length=1)
    cooldown: NumRef = 0.0
    delay: NumRef | None = None
    enabled: bool = True

    def tap_templates(self) -> set[str]:
        return {a.args["template"] for a in self.do if a.kind == "tap"}

    def templates(self) -> set[str]:
        return self.when.templates() | self.tap_templates()


class CounterSpec(BaseModel):
    """A counter that increments when a template *appears* (e.g. the results screen)."""

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    #: Count when this template appears (e.g. the "Attempt Again" button of the results screen).
    appears: str | None = None
    #: Only count again after this template was seen (e.g. the Start button of the next run).
    rearm: str | None = None

    @field_validator("appears", "rearm")
    @classmethod
    def _names(cls, value: str | None) -> str | None:
        return None if value is None else validate_name(value)


def _default_recovery() -> list[Action]:
    return [Action(kind="key", args={"key": "BACK"}), Action(kind="wait", args={"seconds": 3.0})]


class StuckSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Screen unchanged for this long (and no rule helped) -> recovery.
    static_seconds: NumRef = 180.0
    #: No rule fired for this long -> recovery. Must be longer than your longest battle.
    idle_seconds: NumRef = 1500.0
    recovery: list[Action] = Field(default_factory=_default_recovery)
    #: Restart the game after this many recoveries in a row did not help.
    restart_after: int = Field(2, ge=0)
    #: Stop farming after this many restarts without finishing a run (0 = never).
    max_restarts: int = Field(3, ge=0)


class RoutineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tick_interval: NumRef = 1.0
    action_delay: NumRef = 1.5
    stop_after_runs: NumRef = 0.0
    stop_after_minutes: NumRef = 0.0
    run_counter: str = "runs"
    ensure_foreground: bool = True
    foreground_check_seconds: float = Field(30.0, gt=0)
    stuck: StuckSettings = Field(default_factory=StuckSettings)

    @field_validator("run_counter")
    @classmethod
    def _run_counter(cls, value: str) -> str:
        return _counter_name(value)


def _template_doc(value: Any) -> Any:
    return {"description": value} if isinstance(value, str) else value


class TemplateDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    level: Literal["required", "recommended", "optional"] = "optional"


def _collect_refs(obj: Any) -> set[str]:
    if is_ref(obj):
        return {obj[1:]}
    if isinstance(obj, dict):
        return set().union(*(_collect_refs(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list | tuple):
        return set().union(*(_collect_refs(v) for v in obj)) if obj else set()
    return set()


class Routine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    version: int = 1
    options: dict[str, OptionSpec] = {}
    settings: RoutineSettings = Field(default_factory=RoutineSettings)
    templates: dict[str, Annotated[TemplateDoc, BeforeValidator(_template_doc)]] = {}
    counters: dict[str, CounterSpec] = {}
    #: Named colours for ``color_visible`` and ``tap_color`` (e.g. the map's path arrows).
    colors: dict[str, ColorSpec] = {}
    rules: list[Rule] = Field(min_length=1)

    @field_validator("templates")
    @classmethod
    def _template_keys(cls, value: dict[str, TemplateDoc]) -> dict[str, TemplateDoc]:
        for name in value:
            validate_name(name)
        return value

    @field_validator("counters")
    @classmethod
    def _counter_keys(cls, value: dict[str, CounterSpec]) -> dict[str, CounterSpec]:
        for name in value:
            _counter_name(name)
        return value

    @field_validator("colors")
    @classmethod
    def _color_keys(cls, value: dict[str, ColorSpec]) -> dict[str, ColorSpec]:
        for name in value:
            validate_name(name)
        return value

    @model_validator(mode="after")
    def _check_references(self) -> Routine:
        missing = sorted(r for r in _collect_refs(self.model_dump()) if r not in self.options)
        if missing:
            refs = ", ".join("$" + m for m in missing)
            raise ValueError(f"unknown option reference(s) {refs}: declare them under 'options'")
        for rule in self.rules:
            colors = set(rule.when.color_visible) | {
                a.args["color"] for a in rule.do if a.kind == "tap_color"
            }
            unknown = sorted(colors - set(self.colors))
            if unknown:
                raise ValueError(
                    f"rule {rule.name!r} uses unknown colour(s) {', '.join(unknown)}: "
                    "declare them under 'colors'"
                )
            for key, expected in rule.when.option.items():
                spec = self.options.get(key)
                if spec is None:
                    raise ValueError(f"rule {rule.name!r} checks unknown option {key!r}")
                try:
                    spec.coerce(expected)
                except ValueError as exc:
                    raise ValueError(f"rule {rule.name!r}, option {key}: {exc}") from None
        return self

    # ------------------------------------------------------------- helpers
    def referenced_templates(self) -> set[str]:
        names: set[str] = set()
        for rule in self.rules:
            names |= rule.templates()
        for counter in self.counters.values():
            names |= {n for n in (counter.appears, counter.rearm) if n}
        for action in self.settings.stuck.recovery:
            if action.kind == "tap":
                names.add(action.args["template"])
        return names

    def template_checklist(self) -> dict[str, TemplateDoc]:
        """Documented templates first, then any other referenced ones."""
        docs = dict(self.templates)
        for name in sorted(self.referenced_templates() - set(docs)):
            docs[name] = TemplateDoc()
        return docs

    def sanitize_options(self, values: dict[str, Any]) -> dict[str, Any]:
        """Keep only the saved values that are still valid for the current options."""
        clean = {}
        for key, value in values.items():
            spec = self.options.get(key)
            if spec is None:
                continue
            try:
                clean[key] = spec.coerce(value)
            except ValueError:
                continue
        return clean

    def option_values(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        values = {key: spec.default for key, spec in self.options.items()}
        for key, value in (overrides or {}).items():
            spec = self.options.get(key)
            if spec is None:
                continue
            try:
                values[key] = spec.coerce(value)
            except ValueError as exc:
                label = spec.label or key
                raise RoutineError(f"{label}: {exc}") from None
        return values


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors()[:8]:
        loc = ".".join(str(p) for p in error["loc"])
        msg = error["msg"]
        for prefix in ("Value error, ", "Assertion failed, "):
            if msg.startswith(prefix):
                msg = msg[len(prefix) :]
        lines.append(f"{loc}: {msg}" if loc else msg)
    return "\n".join(lines)


def parse_routine(text: str) -> Routine:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutineError(f"YAML syntax error: {exc}") from None
    if not isinstance(data, dict):
        raise RoutineError("A routine file must be a YAML mapping (name:, rules:, ...)")
    try:
        return Routine.model_validate(data)
    except ValidationError as exc:
        raise RoutineError(_format_errors(exc)) from None
