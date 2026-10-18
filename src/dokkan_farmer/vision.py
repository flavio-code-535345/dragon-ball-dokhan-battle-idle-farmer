"""Template storage and OpenCV template matching.

Templates are small crops of real screenshots (buttons, dialog titles, ...)
captured by the user in the web UI. Nothing from the game ships with this
project. Matching uses normalised cross-correlation on a downscaled frame,
optionally restricted to a search region, plus a brightness check so that
elements dimmed behind a popup are not mistaken for clickable ones.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import cv2
import numpy as np

from .util import atomic_write

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
META_FILE = "templates.json"
MODES = ("gray", "color")
#: Crops flatter than this (grey-level standard deviation) match almost anything.
MIN_STD = 4.0
#: Accepted ratio between the matched patch and the template mean brightness.
BRIGHTNESS_RANGE = (0.72, 1.4)
#: How many correlation peaks to try before giving up on the brightness check.
MAX_PEAKS = 6


#: ``version`` of the JSON from :meth:`TemplateStore.export_bundle`.
BUNDLE_VERSION = 1


def validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("Template names may only use a-z, 0-9, '_' and '-' (max 64 characters)")
    return name


def encode_png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:  # pragma: no cover - cv2 only fails on invalid input
        raise ValueError("Could not encode PNG")
    return buf.tobytes()


def encode_jpeg(image: np.ndarray, quality: int = 75, max_width: int | None = None) -> bytes:
    if max_width and image.shape[1] > max_width:
        scale = max_width / image.shape[1]
        size = (max_width, max(1, round(image.shape[0] * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:  # pragma: no cover
        raise ValueError("Could not encode JPEG")
    return buf.tobytes()


def _prepare(image: np.ndarray, mode: str, scale: float) -> np.ndarray:
    if mode == "gray" and image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if abs(scale - 1.0) > 1e-3:
        width = max(1, round(image.shape[1] * scale))
        height = max(1, round(image.shape[0] * scale))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        image = cv2.resize(image, (width, height), interpolation=interp)
    return image


def _brightness_ok(patch_mean: float, template_mean: float) -> bool:
    ratio = (patch_mean + 8.0) / (template_mean + 8.0)
    return BRIGHTNESS_RANGE[0] <= ratio <= BRIGHTNESS_RANGE[1]


def _fit_span(start: int, end: int, need: int, limit: int) -> tuple[int, int]:
    """Clamp ``[start, end)`` to ``[0, limit)`` while keeping at least ``need`` pixels."""
    start, end = max(0, start), min(limit, end)
    if end - start < need:
        center = (start + end) // 2
        start = max(0, min(limit - need, center - need // 2))
        end = min(limit, start + need)
    return start, end


#: How far a template's tap point may lie from it, in template sizes.
MAX_TAP_OFFSET = 3.0


def _tap_offset(value: Any) -> list[float] | None:
    """A valid ``[dx, dy]`` tap offset (clamped), or ``None`` for the template's centre."""
    try:
        dx, dy = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(dx) and math.isfinite(dy)):
        return None
    limit = MAX_TAP_OFFSET
    return [round(min(limit, max(-limit, dx)), 4), round(min(limit, max(-limit, dy)), 4)]


@dataclass
class TemplateMeta:
    name: str
    threshold: float = 0.85
    mode: str = "gray"
    #: Normalised ``[x, y, w, h]`` search area; ``None`` searches the whole screen.
    region: list[float] | None = None
    #: ``[width, height]`` of the screenshot the crop was taken from (for rescaling).
    source_size: list[int] = field(default_factory=lambda: [0, 0])
    #: Crop rectangle ``[x, y, w, h]`` in source pixels.
    bbox: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    description: str = ""
    created_at: float = 0.0
    #: Where ``tap: name`` taps: ``[dx, dy]`` from the template's centre in template widths
    #: and heights (it may lie outside the template). ``None`` = the centre.
    tap: list[float] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemplateMeta:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        meta = cls(**known)
        meta.name = validate_name(meta.name)
        meta.threshold = float(min(0.99, max(0.5, meta.threshold)))
        if meta.mode not in MODES:
            meta.mode = "gray"
        meta.tap = _tap_offset(meta.tap)
        return meta

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Match:
    name: str
    score: float
    found: bool
    #: Best candidate ``(x, y, w, h)`` in screen pixels (valid even when not found).
    box: tuple[int, int, int, int]
    #: The template's tap point as ``[dx, dy]`` from the box's centre (box sizes), or None.
    tap: tuple[float, float] | None = None
    #: Set when a peak scored above the threshold but was skipped for its brightness:
    #: patch / template brightness (e.g. 0.5 = a copy behind a dimming popup).
    brightness: float | None = None

    def tap_point(self) -> tuple[float, float]:
        """Where ``tap: name`` taps, in screen pixels (before any jitter)."""
        x, y, w, h = self.box
        dx, dy = self.tap or (0.0, 0.0)
        return x + w * (0.5 + dx), y + h * (0.5 + dy)

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return x + w // 2, y + h // 2

    def to_dict(self, width: int, height: int) -> dict[str, Any]:
        x, y, w, h = self.box
        data: dict[str, Any] = {
            "name": self.name,
            "score": round(float(self.score), 3),
            "found": self.found,
            "box": [x / width, y / height, w / width, h / height],
        }
        if self.tap:  # its own tap point, in screen fractions (else: the box's centre)
            tx, ty = self.tap_point()
            data["tap"] = [round(tx / width, 4), round(ty / height, 4)]
        if self.brightness is not None:
            data["brightness"] = round(self.brightness, 2)
        return data


class TemplateStore:
    """PNG crops plus a ``templates.json`` with their settings, in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._meta: dict[str, TemplateMeta] = {}
        self._images: dict[str, np.ndarray] = {}
        self.version = 0
        self.reload()

    # ------------------------------------------------------------------ loading
    @property
    def meta_path(self) -> Path:
        return self.directory / META_FILE

    def image_path(self, name: str) -> Path:
        return self.directory / f"{validate_name(name)}.png"

    def reload(self) -> None:
        with self._lock:
            raw: dict[str, Any] = {}
            if self.meta_path.exists():
                try:
                    raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    log.error("Could not read %s: %s", self.meta_path, exc)
            metas: dict[str, TemplateMeta] = {}
            images: dict[str, np.ndarray] = {}
            for name, data in raw.items():
                try:
                    meta = TemplateMeta.from_dict({**data, "name": name})
                except (TypeError, ValueError) as exc:
                    log.warning("Ignoring template %r: %s", name, exc)
                    continue
                image = cv2.imread(str(self.image_path(meta.name)), cv2.IMREAD_COLOR)
                if image is None:
                    log.warning("Template %r has no image file, ignoring it", name)
                    continue
                metas[meta.name], images[meta.name] = meta, image
            # PNGs dropped into the folder by hand get default settings.
            for png in sorted(self.directory.glob("*.png")):
                name = png.stem
                if name in metas or not NAME_RE.match(name):
                    continue
                image = cv2.imread(str(png), cv2.IMREAD_COLOR)
                if image is None:
                    continue
                h, w = image.shape[:2]
                metas[name] = TemplateMeta(name=name, bbox=[0, 0, w, h])
                images[name] = image
            self._meta, self._images = metas, images
            self.version += 1

    def _write_meta(self) -> None:
        data = {name: meta.to_dict() for name, meta in sorted(self._meta.items())}
        atomic_write(self.meta_path, json.dumps(data, indent=2).encode("utf-8"))

    # ------------------------------------------------------------------ queries
    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._meta)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._meta

    def get(self, name: str) -> tuple[TemplateMeta, np.ndarray] | None:
        with self._lock:
            meta = self._meta.get(name)
            return None if meta is None else (meta, self._images[name])

    def metas(self) -> list[TemplateMeta]:
        with self._lock:
            return [self._meta[name] for name in sorted(self._meta)]

    def png_bytes(self, name: str) -> bytes:
        entry = self.get(name)
        if entry is None:
            raise KeyError(name)
        return encode_png(entry[1])

    # ---------------------------------------------------------------- mutations
    def save(self, meta: TemplateMeta, image: np.ndarray) -> TemplateMeta:
        meta.name = validate_name(meta.name)
        with self._lock:
            atomic_write(self.image_path(meta.name), encode_png(image))
            self._meta[meta.name], self._images[meta.name] = meta, image.copy()
            self._write_meta()
            self.version += 1
        return meta

    def create_from_frame(
        self,
        name: str,
        frame: np.ndarray,
        box: tuple[int, int, int, int],
        *,
        threshold: float = 0.85,
        mode: str = "gray",
        search_full: bool = False,
        margin: float = 0.06,
        description: str = "",
        tap: Any = None,
    ) -> TemplateMeta:
        """``tap``: ``[dx, dy]`` from the crop's centre in crop sizes (None = the centre)."""
        name = validate_name(name)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        fh, fw = frame.shape[:2]
        x, y, w, h = (round(v) for v in box)
        x, y = max(0, min(fw - 1, x)), max(0, min(fh - 1, y))
        w, h = min(w, fw - x), min(h, fh - y)
        if w < 8 or h < 8:
            raise ValueError("Selection is too small: drag a box of at least 8x8 pixels")
        crop = frame[y : y + h, x : x + w].copy()
        std = float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).std())
        if std < MIN_STD:
            raise ValueError(
                "Selection is almost a flat colour, which would match everywhere. "
                "Include some text or edges."
            )
        meta = TemplateMeta(
            name=name,
            threshold=float(min(0.99, max(0.5, threshold))),
            mode=mode,
            region=None if search_full else _near_region((x, y, w, h), (fw, fh), margin),
            source_size=[fw, fh],
            bbox=[x, y, w, h],
            description=description.strip(),
            created_at=time.time(),
            tap=_tap_offset(tap),
        )
        return self.save(meta, crop)

    def update(
        self,
        name: str,
        *,
        threshold: float | None = None,
        mode: str | None = None,
        search_full: bool | None = None,
        description: str | None = None,
        reset_tap: bool = False,
    ) -> TemplateMeta:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None:
                raise KeyError(name)
            if threshold is not None:
                meta.threshold = float(min(0.99, max(0.5, threshold)))
            if mode is not None:
                if mode not in MODES:
                    raise ValueError(f"mode must be one of {MODES}")
                meta.mode = mode
            if search_full is True:
                meta.region = None
            elif search_full is False and meta.region is None:
                fw, fh = meta.source_size
                if fw and fh:
                    meta.region = _near_region(tuple(meta.bbox), (fw, fh), 0.06)
            if description is not None:
                meta.description = description.strip()
            if reset_tap:
                meta.tap = None
            self._write_meta()
            self.version += 1
            return meta

    # ---------------------------------------------------------- import/export
    def export_bundle(self) -> dict[str, Any]:
        """Every template with its PNG (base64) in one JSON-able dict."""
        with self._lock:
            entries = [
                {**self._meta[name].to_dict(), "png": base64.b64encode(encode_png(image)).decode()}
                for name, image in sorted(self._images.items())
            ]
        return {"version": BUNDLE_VERSION, "templates": entries}

    def import_bundle(self, bundle: dict[str, Any]) -> list[str]:
        """Adds or replaces the templates of an :meth:`export_bundle` dict; all or none.

        An entry without ``region`` gets the usual search area around its ``bbox``
        (``"region": null`` searches the whole screen)."""
        entries = bundle.get("templates") if isinstance(bundle, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("The file has no templates")
        parsed: list[tuple[TemplateMeta, np.ndarray]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Each template must be an object")
            label = entry.get("name", "?")
            try:
                data = base64.b64decode(entry.get("png") or "", validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"Template {label}: png is not valid base64") from None
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"Template {label}: png is not an image")
            h, w = image.shape[:2]
            if w < 8 or h < 8:
                raise ValueError(f"Template {label}: the image is smaller than 8x8")
            try:
                meta = TemplateMeta.from_dict({k: v for k, v in entry.items() if k != "png"})
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Template {label}: {exc}") from None
            if len(meta.bbox) != 4 or not meta.bbox[2]:
                meta.bbox = [0, 0, w, h]
            fw, fh = [*meta.source_size, 0, 0][:2]
            meta.source_size = [int(fw), int(fh)]
            if "region" not in entry:
                meta.region = _near_region(tuple(meta.bbox), (fw, fh), 0.06) if fw and fh else None
            meta.created_at = meta.created_at or time.time()
            parsed.append((meta, image))
        with self._lock:
            for meta, image in parsed:
                self.save(meta, image)
        return [meta.name for meta, _ in parsed]

    def delete(self, name: str) -> None:
        with self._lock:
            if name not in self._meta:
                raise KeyError(name)
            del self._meta[name], self._images[name]
            self.image_path(name).unlink(missing_ok=True)
            self._write_meta()
            self.version += 1


def _near_region(box: tuple[int, ...], size: tuple[int, int], margin: float) -> list[float]:
    x, y, w, h = box
    fw, fh = size
    x0 = max(0.0, x / fw - margin)
    y0 = max(0.0, y / fh - margin)
    x1 = min(1.0, (x + w) / fw + margin)
    y1 = min(1.0, (y + h) / fh + margin)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


class _Search(NamedTuple):
    result: np.ndarray
    window: np.ndarray
    template: np.ndarray
    offset: tuple[int, int]
    scale: tuple[float, float]


class Matcher:
    """Matches stored templates against frames, caching resized templates."""

    def __init__(self, store: TemplateStore, work_scale: float = 0.5) -> None:
        self.store = store
        self.work_scale = float(min(1.0, max(0.2, work_scale)))
        self._cache: dict[tuple[str, str, float], np.ndarray] = {}
        self._cache_version = -1
        self._lock = threading.Lock()

    def analyze(self, frame: np.ndarray) -> FrameAnalysis:
        return FrameAnalysis(self, frame)

    def prepared_template(
        self, name: str, image: np.ndarray, mode: str, scale: float
    ) -> np.ndarray:
        key = (name, mode, round(scale, 4))
        with self._lock:
            if self._cache_version != self.store.version:
                self._cache.clear()
                self._cache_version = self.store.version
            cached = self._cache.get(key)
            if cached is None:
                cached = _prepare(image, mode, scale)
                self._cache[key] = cached
            return cached


class ColorBlob(NamedTuple):
    #: Fraction of the screen the blob covers.
    area: float
    #: Its centre of mass, in screen pixels.
    x: float
    y: float


#: Colour blobs are looked for on a frame this wide (faster; areas are fractions anyway).
COLOR_WIDTH = 360


def find_color_blobs(
    frame: np.ndarray,
    hsv_min: list[int],
    hsv_max: list[int],
    min_area: float,
    max_area: float,
    region: list[float] | None = None,
    close: int = 0,
) -> list[ColorBlob]:
    """Connected areas of pixels within an HSV range, sized ``min_area``..``max_area`` of the
    screen, largest first (e.g. the blue path arrows of the map, whichever way they point)."""
    width = frame.shape[1]
    scale = min(1.0, COLOR_WIDTH / width)
    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    x0 = y0 = 0
    if region:
        rx, ry, rw, rh = region
        x0, y0 = int(rx * sw), int(ry * sh)
        small = small[y0 : max(y0 + 1, int((ry + rh) * sh)), x0 : max(x0 + 1, int((rx + rw) * sw))]
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    if hsv_min[0] > hsv_max[0]:  # the hue wraps around (red)
        low = cv2.inRange(hsv, np.array(hsv_min, np.uint8), np.array([179, *hsv_max[1:]], np.uint8))
        high = cv2.inRange(hsv, np.array([0, *hsv_min[1:]], np.uint8), np.array(hsv_max, np.uint8))
        mask = cv2.bitwise_or(low, high)
    else:
        mask = cv2.inRange(hsv, np.array(hsv_min, np.uint8), np.array(hsv_max, np.uint8))
    if close > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * close + 1, 2 * close + 1))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
    total = float(sw * sh)
    blobs = []
    for i in range(1, count):
        area = stats[i, cv2.CC_STAT_AREA] / total
        if min_area <= area <= max_area:
            cx, cy = centroids[i]
            blobs.append(ColorBlob(area, (cx + x0) / scale, (cy + y0) / scale))
    return sorted(blobs, key=lambda b: -b.area)


class FrameAnalysis:
    """Lazy, cached template lookups for one screenshot."""

    def __init__(self, matcher: Matcher, frame: np.ndarray) -> None:
        self.matcher = matcher
        self.frame = frame
        self.height, self.width = frame.shape[:2]
        self._prepared: dict[tuple[str, float], np.ndarray] = {}
        self._results: dict[str, Match | None] = {}
        self._colors: dict[str, list[ColorBlob]] = {}

    def color_blobs(self, name: str, spec: Any) -> list[ColorBlob]:
        """Blobs of the routine colour ``name`` (a ``routine.ColorSpec``), largest first."""
        if name not in self._colors:
            self._colors[name] = find_color_blobs(
                self.frame,
                spec.hsv_min,
                spec.hsv_max,
                spec.min_area,
                spec.max_area,
                spec.region,
                spec.close,
            )
        return self._colors[name]

    def _frame(self, mode: str, scale: float) -> np.ndarray:
        key = (mode, scale)
        if key not in self._prepared:
            self._prepared[key] = _prepare(self.frame, mode, scale)
        return self._prepared[key]

    def find(self, name: str) -> Match | None:
        """Best match for ``name`` (``None`` if no such template exists)."""
        if name not in self._results:
            entry = self.matcher.store.get(name)
            self._results[name] = None if entry is None else self._match(*entry)
        return self._results[name]

    def visible(self, name: str) -> bool:
        match = self.find(name)
        return bool(match and match.found)

    def matches(self) -> list[Match]:
        return [m for m in self._results.values() if m is not None]

    def _search(self, meta: TemplateMeta, image: np.ndarray) -> _Search | None:
        source_width = meta.source_size[0] or self.width
        device_scale = self.width / source_width
        scale = self.matcher.work_scale
        th, tw = image.shape[:2]
        if min(tw, th) * device_scale * scale < 12:
            scale = 1.0  # tiny templates need full resolution
        frame = self._frame(meta.mode, scale)
        fh, fw = frame.shape[:2]
        kx, ky = fw / self.width, fh / self.height
        template = self.matcher.prepared_template(meta.name, image, meta.mode, device_scale * scale)
        ph, pw = template.shape[:2]
        if ph > fh or pw > fw:
            return None
        if meta.region:
            rx, ry, rw, rh = meta.region
            x0, x1 = _fit_span(int(rx * fw), math.ceil((rx + rw) * fw), pw, fw)
            y0, y1 = _fit_span(int(ry * fh), math.ceil((ry + rh) * fh), ph, fh)
        else:
            x0, y0, x1, y1 = 0, 0, fw, fh
        window = frame[y0:y1, x0:x1]
        result = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
        np.nan_to_num(result, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        return _Search(result, window, template, (x0, y0), (kx, ky))

    def _to_box(self, search: _Search, loc: tuple[int, int]) -> tuple[int, int, int, int]:
        (x0, y0), (kx, ky) = search.offset, search.scale
        ph, pw = search.template.shape[:2]
        return (
            round((loc[0] + x0) / kx),
            round((loc[1] + y0) / ky),
            round(pw / kx),
            round(ph / ky),
        )

    def _match(self, meta: TemplateMeta, image: np.ndarray) -> Match:
        search = self._search(meta, image)
        tap = tuple(meta.tap) if meta.tap else None
        if search is None:
            return Match(meta.name, 0.0, False, (0, 0, 0, 0), tap)
        result = search.result
        ph, pw = search.template.shape[:2]
        template_mean = float(search.template.mean())
        first: tuple[float, tuple[int, int]] | None = None
        skipped: float | None = None  # brightness ratio of the best peak skipped for it
        for _ in range(MAX_PEAKS):
            _, score, _, loc = cv2.minMaxLoc(result)
            if first is None:
                first = (float(score), loc)
            if score < meta.threshold:
                break
            x, y = loc
            patch = search.window[y : y + ph, x : x + pw]
            patch_mean = float(patch.mean())
            if _brightness_ok(patch_mean, template_mean):
                return Match(meta.name, float(score), True, self._to_box(search, loc), tap)
            if skipped is None:
                skipped = (patch_mean + 8.0) / (template_mean + 8.0)
            # Dimmed copy (e.g. behind a popup): suppress this peak and look further.
            y0, x0 = max(0, y - ph // 2), max(0, x - pw // 2)
            result[y0 : y + ph // 2 + 1, x0 : x + pw // 2 + 1] = -1
        assert first is not None
        box = self._to_box(search, first[1])
        return Match(meta.name, first[0], False, box, tap, brightness=skipped)

    def candidates(
        self, name: str, limit: int = 3
    ) -> list[tuple[float, tuple[int, int, int, int]]]:
        """Top correlation peaks for ``name`` (diagnostics: is a template ambiguous?)."""
        entry = self.matcher.store.get(name)
        if entry is None:
            return []
        search = self._search(*entry)
        if search is None:
            return []
        result = search.result.copy()
        ph, pw = search.template.shape[:2]
        found = []
        for _ in range(limit):
            _, score, _, loc = cv2.minMaxLoc(result)
            if score <= 0:
                break
            found.append((float(score), self._to_box(search, loc)))
            x, y = loc
            result[max(0, y - ph) : y + ph + 1, max(0, x - pw) : x + pw + 1] = -1
        return found
