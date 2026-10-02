"""Ghost estimate from screen content history (no camera, no hardware).

The estimator watches the captured screen content, keeps a low-resolution
model of what the e-ink panel is probably still showing, and reports where
old dark content is likely still visible as a dark ghost. It is a heuristic
estimate, never a measurement: only a photo of the panel can confirm it.

The model is pure Python and independent of GTK and of the serial port. The
capture backends live in ``dasungctl.screencap``; this module defines their
frame container and error type so the watcher can stay importable in the
project virtualenv, where the GTK bindings do not exist.

How one sample works, per model pixel:

- ``S`` is the current content (the captured frame);
- ``A`` is the estimate of what the panel actually shows;
- ``E = A - S`` is the visible ghost error (negative = too dark).

Pixels the capture shows as changed are rewritten by the panel with an
efficiency ``gamma`` (less than 1), so ``A`` approaches ``S`` but does not
reach it; untouched pixels keep their error, which is why a ghost stays until
the area is rewritten or a refresh clears the panel.

Only dark ghosts are counted: old dark content that the current light content
leaves visible. A dark pixel that is lighter than intended is imperfect ink,
not a distinct artifact, so positive errors do not create elements.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Callable, Sequence

from . import paths
from .windows import lookup_zone


DEFAULT_MODEL_WIDTH = 480
GRID_COLUMNS = 16
# Luminance delta (0..255) below which a pixel counts as unchanged: capture
# noise and antialiasing should not drive the panel model.
DEFAULT_NOISE = 10
# |E| below which a pixel is not a visible ghost.
DEFAULT_MIN_ERROR = 8
# Percentage of the target value an update actually reaches, by direction.
# Erasing old dark ink leaves more residue than adding ink, so the erase
# efficiency is the lower one. Both are assumptions to calibrate.
DEFAULT_GAMMA_INK = 90
DEFAULT_GAMMA_ERASE = 80
DEFAULT_MIN_CELL_PIXELS = 24
DEFAULT_MIN_CELL_FRACTION = 0.05
# Content lighter than this is a surface where a dark ghost is visible.
LIGHT_CONTENT = 128
# Arbitrary index references: level 100 is reached when the whole panel is
# off by REFERENCE_LEVEL, element severity 100 when its pixels are off by
# REFERENCE_SEVERITY.
REFERENCE_LEVEL = 32.0
REFERENCE_SEVERITY = 128.0
STATE_SAVE_SECONDS = 30.0
# One log alert per threshold crossing; see GhostWatcher.maybe_alert.
ALERT_COOLDOWN_SECONDS = 900.0
ALERT_REARM_FRACTION = 0.6
# The window list is refreshed at most this often while sampling.
ZONE_REFRESH_SECONDS = 3.0


class CaptureError(RuntimeError):
    """The screen capture backend could not deliver a frame."""


@dataclass(frozen=True)
class Frame:
    """One captured frame, already reduced to the model resolution.

    ``gray`` is one byte per pixel; ``source_width``/``source_height`` are
    the dimensions of the captured region before the reduction, so element
    coordinates can be reported in screen pixels.
    """

    gray: bytes
    width: int
    height: int
    source_width: int
    source_height: int


@dataclass(frozen=True)
class GhostElement:
    """One estimated ghost area, in screen (source) pixel coordinates.

    ``app``/``fullscreen`` describe the window that was over the area when
    the ghost formed (None when the window list is unavailable).
    """

    x: int
    y: int
    width: int
    height: int
    severity: int
    dark: bool
    age: float
    app: str | None = None
    fullscreen: bool = False


@dataclass(frozen=True)
class GhostResult:
    """The model state after one sample; level and severity are 0..100."""

    level: int
    elements: tuple[GhostElement, ...]
    dirty_fraction: float
    changed_pixels: int
    samples: int
    sampled_at: float
    source_width: int
    source_height: int
    model_width: int
    model_height: int


class GhostModel:
    """Panel estimate over a reduced grayscale frame; pure and testable."""

    def __init__(
        self,
        width: int,
        height: int,
        source_width: int,
        source_height: int,
        *,
        noise: int = DEFAULT_NOISE,
        min_error: int = DEFAULT_MIN_ERROR,
        gamma_ink: int = DEFAULT_GAMMA_INK,
        gamma_erase: int = DEFAULT_GAMMA_ERASE,
        columns: int = GRID_COLUMNS,
        min_cell_pixels: int = DEFAULT_MIN_CELL_PIXELS,
        min_cell_fraction: float = DEFAULT_MIN_CELL_FRACTION,
    ) -> None:
        """Build an empty model; the first sample creates the reference."""

        if width <= 0 or height <= 0:
            raise ValueError("model size must be positive")
        if source_width <= 0 or source_height <= 0:
            raise ValueError("source size must be positive")
        if not 0 <= noise <= 255:
            raise ValueError("noise must be in 0..255")
        if not 0 <= min_error <= 255:
            raise ValueError("min_error must be in 0..255")
        if not 1 <= gamma_ink <= 100:
            raise ValueError("gamma_ink must be in 1..100")
        if not 1 <= gamma_erase <= 100:
            raise ValueError("gamma_erase must be in 1..100")
        if not 0 <= min_cell_fraction <= 1:
            raise ValueError("min_cell_fraction must be in 0..1")
        self.width = width
        self.height = height
        self.source_width = source_width
        self.source_height = source_height
        self.noise = noise
        self.min_error = min_error
        self.gamma_ink = gamma_ink
        self.gamma_erase = gamma_erase
        self.min_cell_pixels = max(1, int(min_cell_pixels))
        self.min_cell_fraction = float(min_cell_fraction)

        size = width * height
        self._size = size
        self._shown = bytearray(size)
        self._previous = bytearray(size)
        self._error = array("h", bytes(2 * size))
        self._started = False
        self._samples = 0
        self._result: GhostResult | None = None

        self.columns = max(1, min(int(columns), width))
        self.cell_width = max(1, -(-width // self.columns))
        self.rows = max(1, -(-height // self.cell_width))
        self._cell_count = self.columns * self.rows
        self._cell_of = array("i", bytes(4 * size))
        self._cell_area = [0] * self._cell_count
        for row in range(self.rows):
            y0 = row * self.cell_width
            y1 = min(y0 + self.cell_width, height)
            for column in range(self.columns):
                x0 = column * self.cell_width
                x1 = min(x0 + self.cell_width, width)
                cell = row * self.columns + column
                self._cell_area[cell] = (y1 - y0) * (x1 - x0)
                values = array("i", [cell] * (x1 - x0))
                for y in range(y0, y1):
                    base = y * width
                    self._cell_of[base + x0 : base + x1] = values
        self._onset = [0.0] * self._cell_count
        # Per-cell annotation: application and fullscreen flag recorded when
        # the cell first became dirty, so a label survives the window closing.
        self._cell_app: list[str | None] = [None] * self._cell_count
        self._cell_full = [False] * self._cell_count
        # Last sample's per-cell dirty statistics; None before the first
        # sample and after a reset until the next one.
        self._counts: list[int] | None = None
        self._sums: list[int] | None = None
        self._darks: list[int] | None = None
        self._dirty: list[int] = []

    @property
    def result(self) -> GhostResult | None:
        """The last sample's result, or None before the first sample."""

        return self._result

    @property
    def error(self) -> array:
        """The signed ghost error per model pixel (internal, for previews)."""

        return self._error

    def sample(
        self,
        frame: bytes,
        now: float,
        wall: float | None = None,
        annotate=None,
    ) -> GhostResult:
        """Feed one frame and return the updated estimate.

        `annotate(source_x, source_y)` is called when a cell first becomes
        dirty and should return ``(app, fullscreen)`` or None; the window
        behind that area labels the ghost that formed there.
        """

        if len(frame) != self._size:
            raise ValueError(
                f"frame has {len(frame)} bytes, expected {self._size}"
            )
        wall = time.time() if wall is None else wall
        if not self._started:
            self._shown[:] = frame
            self._previous[:] = frame
            self._started = True
            self._samples = 1
            self._result = self._build_result(
                now, wall, changed=0, stats=None, annotate=annotate
            )
            return self._result
        changed, stats = self._update(frame)
        self._samples += 1
        self._result = self._build_result(
            now, wall, changed=changed, stats=stats, annotate=annotate
        )
        return self._result

    def reset(self, now: float, wall: float | None = None) -> GhostResult:
        """Model a full refresh: the panel matches the last known content."""

        wall = time.time() if wall is None else wall
        self._error = array("h", bytes(2 * self._size))
        self._onset = [0.0] * self._cell_count
        self._cell_app = [None] * self._cell_count
        self._cell_full = [False] * self._cell_count
        # Zero the stored statistics too: the dirty grid is derived from
        # them, so a reset must not leave the previous sample's cells dirty.
        self._counts = [0] * self._cell_count
        self._sums = [0] * self._cell_count
        self._darks = [0] * self._cell_count
        if self._started:
            self._shown[:] = self._previous
        self._result = self._build_result(now, wall, changed=0, stats=None)
        return self._result

    def reset_area(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        now: float,
        wall: float | None = None,
    ) -> GhostResult:
        """Model a local refresh: only this source rectangle comes clean.

        The overlay flash rewrites the pixels under the area, so the panel
        estimate there returns to the current content while every other cell
        keeps its ghost. Cells are the model's granularity: a cell touching
        the rectangle is cleared entirely.
        """

        wall = time.time() if wall is None else wall
        if width <= 0 or height <= 0:
            raise ValueError("area must be positive")
        left = max(0, min(self.width, round(x * self.width / self.source_width)))
        right = max(
            left,
            min(self.width, round((x + width) * self.width / self.source_width)),
        )
        top = max(
            0, min(self.height, round(y * self.height / self.source_height))
        )
        bottom = max(
            top,
            min(
                self.height,
                round((y + height) * self.height / self.source_height),
            ),
        )
        previous = self._previous
        shown = self._shown
        error = self._error
        for row in range(top, bottom):
            base = row * self.width
            for index in range(base + left, base + right):
                shown[index] = previous[index]
                error[index] = 0
        counts, sums, darks = self._counts, self._sums, self._darks
        first_row = top // self.cell_width
        last_row = (
            min(self.rows, (bottom - 1) // self.cell_width + 1)
            if bottom > top
            else first_row
        )
        first_column = left // self.cell_width
        last_column = (
            min(self.columns, (right - 1) // self.cell_width + 1)
            if right > left
            else first_column
        )
        for row in range(first_row, last_row):
            for column in range(first_column, last_column):
                cell = row * self.columns + column
                self._onset[cell] = 0.0
                self._cell_app[cell] = None
                self._cell_full[cell] = False
                if counts is not None and sums is not None and darks is not None:
                    counts[cell] = 0
                    sums[cell] = 0
                    darks[cell] = 0
        self._result = self._build_result(now, wall, changed=0, stats=None)
        return self._result

    def _update(self, frame: bytes):
        """Move the panel estimate where the content changed.

        Returns ``(changed_pixels, stats)``; ``stats`` is None when nothing
        changed, so the previous cell statistics stay valid.
        """

        previous = self._previous
        if frame == previous:
            return 0, None
        shown = self._shown
        error = self._error
        cell_of = self._cell_of
        noise = self.noise
        min_error = self.min_error
        gamma_ink = self.gamma_ink
        gamma_erase = self.gamma_erase
        counts = [0] * self._cell_count
        sums = [0] * self._cell_count
        darks = [0] * self._cell_count
        changed = 0

        for index in range(self._size):
            value = frame[index]
            delta = value - previous[index]
            if delta > noise or delta < -noise:
                shown_value = shown[index]
                gamma = gamma_erase if delta > 0 else gamma_ink
                shown_value += (gamma * (value - shown_value)) // 100
                shown[index] = shown_value
                error[index] = shown_value - value
                changed += 1
            level = error[index]
            # Only dark ghosts matter: old dark content left visible on the
            # current light content. Lighter-than-intended dark pixels are
            # imperfect ink, not a ghost users hunt.
            if level <= -min_error and value >= LIGHT_CONTENT:
                cell = cell_of[index]
                counts[cell] += 1
                sums[cell] += -level
                darks[cell] += 1
        previous[:] = frame
        return changed, (counts, sums, darks)

    def _build_result(
        self,
        now: float,
        wall: float,
        *,
        changed: int,
        stats,
        annotate=None,
    ) -> GhostResult:
        if stats is not None:
            self._counts, self._sums, self._darks = stats
        elif self._counts is None:
            self._counts = [0] * self._cell_count
            self._sums = [0] * self._cell_count
            self._darks = [0] * self._cell_count
        counts = self._counts
        sums = self._sums
        darks = self._darks
        assert counts is not None and sums is not None and darks is not None

        dirty: list[int] = []
        for cell in range(self._cell_count):
            area = self._cell_area[cell]
            minimum = max(
                self.min_cell_pixels, int(area * self.min_cell_fraction)
            )
            if counts[cell] >= minimum:
                if self._onset[cell] == 0.0:
                    self._onset[cell] = now
                    self._annotate_cell(cell, annotate)
                dirty.append(cell)
            else:
                self._onset[cell] = 0.0
                self._cell_app[cell] = None
                self._cell_full[cell] = False
        self._dirty = dirty

        total_abs = sum(sums[cell] for cell in dirty)
        dirty_pixels = sum(counts[cell] for cell in dirty)
        level = min(
            100, round(100.0 * total_abs / (self._size * REFERENCE_LEVEL))
        )
        elements = self._elements(now)
        return GhostResult(
            level=level,
            elements=elements,
            dirty_fraction=dirty_pixels / self._size,
            changed_pixels=changed,
            samples=self._samples,
            sampled_at=wall,
            source_width=self.source_width,
            source_height=self.source_height,
            model_width=self.width,
            model_height=self.height,
        )

    def _annotate_cell(self, cell: int, annotate) -> None:
        """Record the window behind a cell that just became dirty."""

        if annotate is None:
            self._cell_app[cell] = None
            self._cell_full[cell] = False
            return
        row, column = divmod(cell, self.columns)
        left = column * self.cell_width
        top = row * self.cell_width
        right = min(left + self.cell_width, self.width)
        bottom = min(top + self.cell_width, self.height)
        source_x = ((left + right) // 2) * self.source_width // self.width
        source_y = ((top + bottom) // 2) * self.source_height // self.height
        try:
            annotation = annotate(source_x, source_y)
        except Exception:  # pragma: no cover - labels must never break sampling
            annotation = None
        if annotation:
            self._cell_app[cell], self._cell_full[cell] = annotation
        else:
            self._cell_app[cell] = None
            self._cell_full[cell] = False

    def _components(self) -> list[list[int]]:
        """Dirty cells grouped by 8-connectivity: one list per area."""

        if not self._dirty:
            return []
        dirty = set(self._dirty)
        components: list[list[int]] = []
        seen: set[int] = set()
        for start in sorted(dirty):
            if start in seen:
                continue
            stack = [start]
            seen.add(start)
            cells: list[int] = []
            while stack:
                cell = stack.pop()
                cells.append(cell)
                row, column = divmod(cell, self.columns)
                for row_step in (-1, 0, 1):
                    for column_step in (-1, 0, 1):
                        other_row = row + row_step
                        other_column = column + column_step
                        if not 0 <= other_row < self.rows:
                            continue
                        if not 0 <= other_column < self.columns:
                            continue
                        other = other_row * self.columns + other_column
                        if other in dirty and other not in seen:
                            seen.add(other)
                            stack.append(other)
            components.append(cells)
        return components

    def _element_from_cells(
        self, cells: Sequence[int], now: float
    ) -> GhostElement:
        """One ghost area from a group of dirty cells."""

        counts = self._counts
        sums = self._sums
        darks = self._darks
        assert counts is not None and sums is not None and darks is not None
        pixel_count = sum(counts[cell] for cell in cells)
        abs_sum = sum(sums[cell] for cell in cells)
        dark_count = sum(darks[cell] for cell in cells)
        x0, y0 = self.width, self.height
        x1 = y1 = 0
        onset = now
        for cell in cells:
            row, column = divmod(cell, self.columns)
            left = column * self.cell_width
            top = row * self.cell_width
            right = min(left + self.cell_width, self.width)
            bottom = min(top + self.cell_width, self.height)
            x0 = min(x0, left)
            y0 = min(y0, top)
            x1 = max(x1, right)
            y1 = max(y1, bottom)
            if self._onset[cell] and self._onset[cell] < onset:
                onset = self._onset[cell]
        severity = min(
            100, round(100.0 * (abs_sum / pixel_count) / REFERENCE_SEVERITY)
        )
        # The window label comes from the cell with the most dirty pixels:
        # that is where the ghost mostly is.
        best = max(cells, key=lambda cell: counts[cell])
        return GhostElement(
            x=round(x0 * self.source_width / self.width),
            y=round(y0 * self.source_height / self.height),
            width=max(1, round((x1 - x0) * self.source_width / self.width)),
            height=max(
                1, round((y1 - y0) * self.source_height / self.height)
            ),
            severity=severity,
            dark=dark_count * 2 >= pixel_count,
            age=max(0.0, now - onset),
            app=self._cell_app[best],
            fullscreen=bool(self._cell_full[best]),
        )

    def _elements(self, now: float) -> tuple[GhostElement, ...]:
        """Group dirty cells into connected ghost areas."""

        elements = [
            self._element_from_cells(cells, now) for cells in self._components()
        ]
        elements.sort(key=lambda item: (item.y, item.x))
        return tuple(elements)

    def preview_rgb(self, width: int, height: int) -> bytes:
        """Ghost-only preview at the requested size, as RGB bytes.

        The preview shows the estimated dark residue on a neutral background,
        with the cell grid faint and the element boxes outlined.
        """

        if width <= 0 or height <= 0:
            raise ValueError("preview size must be positive")
        rows = [
            min(self.height - 1, y * self.height // height) for y in range(height)
        ]
        columns = [
            min(self.width - 1, x * self.width // width) for x in range(width)
        ]
        error = self._error
        min_error = self.min_error
        content = self._shown
        buffer = bytearray(width * height * 3)
        base = 235
        index = 0
        for model_y in rows:
            line = model_y * self.width
            for model_x in columns:
                darkness = 0
                value = error[line + model_x]
                if value <= -min_error and content[line + model_x] >= LIGHT_CONTENT:
                    darkness = min(200, -value)
                gray = max(0, base - darkness)
                if model_x % self.cell_width == 0 or model_y % self.cell_width == 0:
                    gray = min(gray, 215)
                buffer[index] = buffer[index + 1] = buffer[index + 2] = gray
                index += 3
        self._draw_boxes(buffer, width, height)
        return bytes(buffer)

    def _draw_boxes(self, buffer: bytearray, width: int, height: int) -> None:
        """Outline the estimated elements in the preview buffer."""

        if self._result is None:
            return
        red, green, blue = 30, 90, 220
        for element in self._result.elements:
            x0 = element.x * width // self.source_width
            y0 = element.y * height // self.source_height
            x1 = min(
                width - 1,
                (element.x + element.width) * width // self.source_width,
            )
            y1 = min(
                height - 1,
                (element.y + element.height) * height // self.source_height,
            )
            x0, y0 = max(0, x0), max(0, y0)
            for x in range(x0, max(x0 + 1, x1 + 1)):
                for y in (y0, y1):
                    index = (y * width + x) * 3
                    buffer[index] = red
                    buffer[index + 1] = green
                    buffer[index + 2] = blue
            for y in range(y0, max(y0 + 1, y1 + 1)):
                for x in (x0, x1):
                    index = (y * width + x) * 3
                    buffer[index] = red
                    buffer[index + 1] = green
                    buffer[index + 2] = blue


@dataclass(frozen=True)
class GhostState:
    """Persisted estimate and alert bookkeeping.

    The JSON keys `notified_at` and `armed` keep their historical names even
    though the crossing now writes a log line and no desktop notification.
    """

    saved_at: str
    level: int
    elements: tuple[GhostElement, ...]
    notified_at: float
    armed: bool


def save_ghost_state(
    result: GhostResult | None,
    path: Path | None = None,
    *,
    notified_at: float = 0.0,
    armed: bool = True,
) -> Path:
    """Write the compact estimate summary and return the file path."""

    target = path or paths.ghost_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "level": result.level if result else 0,
        "notified_at": notified_at or 0.0,
        "armed": bool(armed),
        "elements": [
            {
                "x": element.x,
                "y": element.y,
                "width": element.width,
                "height": element.height,
                "severity": element.severity,
                "dark": bool(element.dark),
                "age": round(float(element.age), 1),
                "app": element.app,
                "fullscreen": bool(element.fullscreen),
            }
            for element in (result.elements if result else ())
        ],
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def load_ghost_state(path: Path | None = None) -> GhostState | None:
    """Read the saved summary; a corrupt file raises ValueError."""

    target = path or paths.ghost_state_path()
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{target}: the saved estimate must be an object")
    unknown = set(data) - {
        "version",
        "saved_at",
        "level",
        "notified_at",
        "armed",
        "elements",
    }
    if unknown:
        raise ValueError(f"{target}: unknown fields: {', '.join(sorted(unknown))}")
    try:
        level = int(data.get("level", 0))
        notified_at = float(data.get("notified_at", 0.0) or 0.0)
        armed = bool(data.get("armed", True))
        saved_at = str(data.get("saved_at", ""))
        elements = tuple(
            GhostElement(
                x=int(item["x"]),
                y=int(item["y"]),
                width=int(item["width"]),
                height=int(item["height"]),
                severity=int(item["severity"]),
                dark=bool(item.get("dark", True)),
                age=float(item.get("age", 0.0)),
                app=str(item["app"]) if item.get("app") else None,
                fullscreen=bool(item.get("fullscreen", False)),
            )
            for item in data.get("elements", [])
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{target}: invalid saved estimate: {exc}") from exc
    if not 0 <= level <= 100:
        raise ValueError(f"{target}: level must be in 0..100")
    return GhostState(
        saved_at=saved_at,
        level=level,
        elements=elements,
        notified_at=notified_at,
        armed=armed,
    )


class GhostWatcher:
    """Sampling cadence, capture failures and threshold alert policy.

    The capture backend is injected; tests use a fake. The watcher never
    touches GTK or the serial port.
    """

    def __init__(
        self,
        capturer=None,
        *,
        enabled: bool = True,
        interval: float = 2.0,
        max_interval: float = 30.0,
        threshold: float = 20.0,
        width: int = DEFAULT_MODEL_WIDTH,
        noise: int = DEFAULT_NOISE,
        min_error: int = DEFAULT_MIN_ERROR,
        gamma_ink: int = DEFAULT_GAMMA_INK,
        gamma_erase: int = DEFAULT_GAMMA_ERASE,
        cooldown: float = ALERT_COOLDOWN_SECONDS,
        zones=None,
        zone_interval: float = ZONE_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Store the sampling policy; no capture happens here."""

        if interval <= 0:
            raise ValueError("interval must be greater than zero")
        if not 0 <= threshold <= 100:
            raise ValueError("threshold must be in 0..100")
        self.capturer = capturer
        self.enabled = bool(enabled)
        self.interval = float(interval)
        self.max_interval = max(self.interval, float(max_interval))
        self.threshold = float(threshold)
        self.width = int(width)
        self.model_options = {
            "noise": noise,
            "min_error": min_error,
            "gamma_ink": gamma_ink,
            "gamma_erase": gamma_erase,
        }
        self.cooldown = float(cooldown)
        self.zones = zones
        self.zone_interval = max(0.0, float(zone_interval))
        self.model: GhostModel | None = None
        self.result: GhostResult | None = None
        self.error: str | None = None
        self.zone_error: str | None = None
        self.capture_failed = False
        self.saved: GhostState | None = None
        self._clock = clock
        self._wall = wall_clock
        self._last_sample: float | None = None
        self._last_save = 0.0
        self._alerted_at = 0.0
        self._armed = True
        self._busy = False
        # Adaptive sampling: each sample without changes doubles the wait up
        # to `max_interval`; the base interval returns as soon as something
        # changes (and while the ghost window is open).
        self._idle_steps = 0
        self._force_base = False
        self._zone_list = ()
        self._zones_at = 0.0

    # -- persistence -------------------------------------------------------

    def load_state(self, path: Path | None = None) -> GhostState | None:
        """Load the saved summary; a corrupt file is reported, not fatal."""

        try:
            self.saved = load_ghost_state(path)
        except ValueError as exc:
            self.saved = None
            self.error = str(exc)
            return None
        if self.saved is not None:
            self._alerted_at = self.saved.notified_at
            self._armed = self.saved.armed
        return self.saved

    def save_state(self, path: Path | None = None, *, force: bool = False) -> None:
        """Persist the summary, rate-limited unless `force` is set."""

        now = self._clock()
        if not force and now - self._last_save < STATE_SAVE_SECONDS:
            return
        try:
            save_ghost_state(
                self.result,
                path,
                notified_at=self._alerted_at,
                armed=self._armed,
            )
        except OSError as exc:
            self.error = f"cannot save the estimate: {exc}"
            return
        self._last_save = now

    # -- sampling ----------------------------------------------------------

    @property
    def current_interval(self) -> float:
        """Seconds between the next samples, after the idle backoff."""

        if self._force_base or self._idle_steps == 0:
            return self.interval
        return min(self.max_interval, self.interval * (2 ** self._idle_steps))

    def set_force_base(self, active: bool) -> None:
        """Keep the base cadence (the ghost window is visible)."""

        self._force_base = bool(active)

    def due(self, now: float | None = None) -> bool:
        """True when a new sample should be taken."""

        if not self.enabled or self.capture_failed:
            return False
        moment = self._clock() if now is None else now
        if self._last_sample is None:
            return True
        return moment - self._last_sample >= self.current_interval

    def sample(self) -> GhostResult | None:
        """Capture one frame and update the estimate; errors are kept here.

        The guard matters while the Wayland portal dialog is open: the nested
        GTK main loop can run the tray timer again, and a second sample must
        not open a second share request.
        """

        if self._busy:
            return None
        self._busy = True
        try:
            return self._sample()
        finally:
            self._busy = False

    def _sample(self) -> GhostResult | None:
        self._last_sample = self._clock()
        if not self.enabled or self.capture_failed:
            return None
        if self.capturer is None:
            self.error = "no screen capture backend for this session"
            self.capture_failed = True
            return None
        try:
            frame = self.capturer.grab(self.width)
        except CaptureError as exc:
            self.error = str(exc)
            self.capture_failed = True
            return None
        except Exception as exc:  # pragma: no cover - defensive
            self.error = f"screen capture failed: {exc}"
            self.capture_failed = True
            return None
        self.error = None
        if (
            self.model is None
            or self.model.width != frame.width
            or self.model.height != frame.height
        ):
            self.model = GhostModel(
                frame.width,
                frame.height,
                frame.source_width,
                frame.source_height,
                **self.model_options,
            )
        now = self._clock()
        self._refresh_zones(frame, now)
        annotate = self._annotate if self._zone_list else None
        result = self.model.sample(
            frame.gray, now=now, wall=self._wall(), annotate=annotate
        )
        self.result = result
        if result.changed_pixels:
            self._idle_steps = 0
        else:
            self._idle_steps += 1
        self.save_state()
        return result

    def _refresh_zones(self, frame: Frame, now: float) -> None:
        """Refresh the cached window list for the captured monitor."""

        if self.zones is None:
            return
        if self._zones_at and now - self._zones_at < self.zone_interval:
            return
        origin = getattr(self.capturer, "source_origin", None)
        if origin is None:
            return
        self._zones_at = now
        try:
            self._zone_list = self.zones.zones(
                origin[0], origin[1], frame.source_width, frame.source_height
            )
            self.zone_error = None
        except Exception as exc:  # pragma: no cover - labels are best-effort
            self._zone_list = ()
            self.zone_error = f"window list unavailable: {exc}"

    def _annotate(self, x: int, y: int):
        """Label for one point: (application, fullscreen) or None."""

        zone = lookup_zone(self._zone_list, x, y)
        if zone is None:
            return None
        return zone.app, zone.fullscreen

    def reset(self) -> GhostResult | None:
        """Model a panel refresh and re-arm the alert."""

        self._armed = True
        self._alerted_at = 0.0
        if self.model is not None:
            self.result = self.model.reset(now=self._clock(), wall=self._wall())
        self.save_state(force=True)
        return self.result

    def reset_area(
        self, x: int, y: int, width: int, height: int
    ) -> GhostResult | None:
        """Clear the estimate over one source rectangle (local refresh).

        Called after a zone flash: the flashed area goes back to the current
        content while every other area keeps its ghost, so the estimator does
        not propose the same rectangle again straight away.
        """

        if self.model is None:
            return None
        self.result = self.model.reset_area(
            x, y, width, height, now=self._clock(), wall=self._wall()
        )
        self.save_state(force=True)
        return self.result

    def retry(self) -> None:
        """Allow capture again after a failure (for example a denied share)."""

        self.error = None
        self.capture_failed = False
        self._last_sample = None
        self._idle_steps = 0
        self._zones_at = 0.0
        reset = getattr(self.capturer, "reset", None)
        if callable(reset):
            reset()

    # -- threshold alert ---------------------------------------------------

    def maybe_alert(self) -> str | None:
        """Text for the log when the level crosses the threshold, else None.

        One alert per crossing: the estimate must fall below 60% of the
        threshold to re-arm, and the cooldown limits how often the same
        crossing can be reported. The text is only ever written to the log
        file; there is no desktop notification.
        """

        if self.result is None:
            return None
        level = self.result.level
        if self._armed and level >= self.threshold:
            moment = self._wall()
            if (
                self._alerted_at
                and moment - self._alerted_at < self.cooldown
            ):
                return None
            self._armed = False
            self._alerted_at = moment
            self.save_state(force=True)
            count = len(self.result.elements)
            areas = "area" if count == 1 else "areas"
            return (
                f"Ghost estimate: {count} {areas}{self._labels_text()}, "
                f"level {level}/100"
            )
        if not self._armed and level < self.threshold * ALERT_REARM_FRACTION:
            self._armed = True
            self.save_state(force=True)
        return None

    def _labels_text(self) -> str:
        """` (konsole, firefox)` for the alert, unique applications."""

        apps: list[str] = []
        for element in self.result.elements if self.result else ():
            if element.app and element.app not in apps:
                apps.append(element.app)
        if not apps:
            return ""
        shown = ", ".join(apps[:2])
        if len(apps) > 2:
            shown += f" (+{len(apps) - 2})"
        return f" ({shown})"


def format_age(seconds: float) -> str:
    """Short age label for the element list (`12 s`, `3 min`, `1 h`)."""

    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.0f} h"
