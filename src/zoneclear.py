"""Zone clearing: flash estimated ghost areas so the panel rewrites them.

The confirmed serial protocol has no regional refresh: the monitor's soft and
hard refresh frames are global. To rewrite only one area, this module briefly
covers it with a borderless overlay window that alternates white and black,
which makes the e-ink controller update those pixels. It is the same software
route the removed 2026-09-26 zone mode used; its physical efficacy was never
established, so the feature stays opt-in and every result must be judged on
the panel.

X11 only: on Wayland a window cannot be placed at global screen coordinates,
so the tray disables the feature there.

The selection policy and the flash sequence are pure and testable; only
``ZoneFlasher`` needs GTK.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Sequence

from .ghostwatch import GhostElement


CLEAR_STYLES = ("white-black", "single")
# The shipped defaults, also read by config.DEFAULT_GHOST["clear"]. The three
# levels are the phase colors (the panel is monochrome) and the ms values are
# the phase durations. `delay` is the only selection parameter left: the
# seconds between the estimator recognizing an area and its flash. These are
# the maintainer's tuned values (single grey 168/30 ms, white-black 15 ms per
# phase, 15 s delay, automatic clearing on); the `Defaults` button in the
# clearing editor restores them.
DEFAULT_CLEAR = {
    "enabled": True,
    "style": "single",
    "white_level": 255,
    "white_ms": 15,
    "black_level": 0,
    "black_ms": 15,
    "grey_level": 168,
    "grey_ms": 30,
    "delay": 15.0,
}


class ClearError(ValueError):
    """A clearing setting is missing or invalid."""


def validate_clear(data, prefix: str = "ghost.clear") -> dict:
    """Validate a clearing-settings mapping and fill its defaults.

    Used by the config file and by the tray's saved preferences, so the two
    cannot drift; the `prefix` only shapes the error messages.
    """

    if not isinstance(data, dict):
        raise ClearError(f"'{prefix}' must be a JSON object")
    unknown = set(data) - set(DEFAULT_CLEAR)
    if unknown:
        raise ClearError(
            f"'{prefix}' has unknown fields: " + ", ".join(sorted(unknown))
        )
    result: dict = {}
    enabled = data.get("enabled", DEFAULT_CLEAR["enabled"])
    if not isinstance(enabled, bool):
        raise ClearError(f"'{prefix}.enabled' must be true or false")
    result["enabled"] = enabled
    style = data.get("style", DEFAULT_CLEAR["style"])
    if style not in CLEAR_STYLES:
        raise ClearError(
            f"'{prefix}.style' must be one of " + ", ".join(CLEAR_STYLES)
        )
    result["style"] = style
    for name in ("white_level", "black_level", "grey_level"):
        value = data.get(name, DEFAULT_CLEAR[name])
        if isinstance(value, bool) or not isinstance(value, int):
            raise ClearError(f"'{prefix}.{name}' must be an integer")
        if not 0 <= value <= 255:
            raise ClearError(f"'{prefix}.{name}' must be in 0..255")
        result[name] = value
    for name in ("white_ms", "black_ms", "grey_ms"):
        value = data.get(name, DEFAULT_CLEAR[name])
        if isinstance(value, bool) or not isinstance(value, int):
            raise ClearError(f"'{prefix}.{name}' must be an integer")
        if not 1 <= value <= 2000:
            raise ClearError(f"'{prefix}.{name}' must be in 1..2000")
        result[name] = value
    delay = data.get("delay", DEFAULT_CLEAR["delay"])
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        raise ClearError(f"'{prefix}.delay' must be a number of seconds")
    if not 0 <= delay <= 86_400:
        raise ClearError(f"'{prefix}.delay' must be in 0..86400")
    result["delay"] = float(delay)
    return result


@dataclass(frozen=True)
class ClearSettings:
    """Clearing policy, filled from the validated `ghost.clear` config."""

    enabled: bool = DEFAULT_CLEAR["enabled"]
    style: str = DEFAULT_CLEAR["style"]
    white_level: int = DEFAULT_CLEAR["white_level"]
    white_ms: int = DEFAULT_CLEAR["white_ms"]
    black_level: int = DEFAULT_CLEAR["black_level"]
    black_ms: int = DEFAULT_CLEAR["black_ms"]
    grey_level: int = DEFAULT_CLEAR["grey_level"]
    grey_ms: int = DEFAULT_CLEAR["grey_ms"]
    delay: float = DEFAULT_CLEAR["delay"]

    @classmethod
    def from_mapping(cls, data) -> "ClearSettings":
        """Build the settings from a mapping, ignoring unknown keys."""

        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in (data or {}).items() if key in known}
        return cls(**values)


def flash_phases(settings: ClearSettings) -> tuple[tuple[int | None, int], ...]:
    """One flash as (grey level or None, milliseconds) phases.

    ``None`` is the restore step: hiding the overlay reveals the content
    again, so the controller finishes the rewrite.
    """

    if settings.style == "single":
        return (
            (settings.grey_level, max(1, int(settings.grey_ms))),
            (None, 0),
        )
    return (
        (settings.white_level, max(1, int(settings.white_ms))),
        (settings.black_level, max(1, int(settings.black_ms))),
        (None, 0),
    )


def _bounds(rect) -> tuple[int, int, int, int]:
    if isinstance(rect, (tuple, list)):
        return int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
    return int(rect.x), int(rect.y), int(rect.width), int(rect.height)


def due_clear_elements(
    elements: Sequence[GhostElement],
    settings: ClearSettings,
) -> tuple[GhostElement, ...]:
    """Every area due for a flash, oldest first.

    The whole automatic policy is the delay: an area the estimator reports
    (a rectangle in the preview) is flashed once it has been dirty for
    `settings.delay` seconds. All due areas are returned together, so the
    tray flashes them in one synchronized wave.
    """

    due = [element for element in elements if element.age >= settings.delay]
    due.sort(key=lambda element: element.age, reverse=True)
    return tuple(due)


class ZoneFlasher:
    """Borderless overlays that flash global screen rectangles together.

    One undecorated, click-through window per rectangle, all sharing the
    same phase timer, so a wave of areas lights up and goes dark at the
    same moment. The tray creates one instance and calls `flash` with a
    callback run on the GTK main loop when the overlays are gone again.

    The phase timer starts on the first window's map event: mapping a
    managed window takes the compositor a couple of frames, and the first
    phase would otherwise be half over before anything is visible.
    """

    MAP_TIMEOUT_MS = 1000
    MAP_SETTLE_MS = 40

    def __init__(self, gtk: dict, *, available: bool) -> None:
        """Store the GTK modules; `available` is False on Wayland."""

        self.Gtk = gtk["Gtk"]
        self.Gdk = gtk["Gdk"]
        self.GLib = gtk["GLib"]
        self._available = bool(available)
        self._windows: list = []
        self._phases: tuple[tuple[int | None, int], ...] = ()
        self._index = 0
        self._pending_phase: tuple[int, int] | None = None
        self._map_timeout = None
        self._on_done = None
        self.busy = False

    @property
    def available(self) -> bool:
        """True when an overlay can be placed (X11 only)."""

        return self._available

    def flash(self, rects, phases, on_done) -> bool:
        """Start one flash over a sequence of global rectangles.

        Returns False when the overlay is unavailable, already running, or
        every rectangle is empty; `on_done` runs on the main loop after all
        overlays are hidden.
        """

        phases = tuple(phases)
        if not self._available or self.busy or not phases:
            return False
        boxes = [
            box
            for box in (_bounds(rect) for rect in rects)
            if box[2] > 0 and box[3] > 0
        ]
        if not boxes:
            return False
        color, ms = phases[0]
        if color is None:
            return False
        Gtk = self.Gtk
        windows = []
        for x, y, width, height in boxes:
            window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            window.set_decorated(False)
            window.set_skip_taskbar_hint(True)
            window.set_skip_pager_hint(True)
            window.set_keep_above(True)
            window.set_accept_focus(False)
            window.set_focus_on_map(False)
            window.set_default_size(width, height)
            window.move(x, y)
            window.set_wmclass("dasungctl-clear", "Dasungctl-clear")
            window.connect("delete-event", lambda *_args: True)
            if not windows:
                # One map event is enough to start the shared phase timer.
                window.connect("map-event", self._on_map_event)
            # An override-redirect window skips the window manager: it maps
            # without the desktop's map animation, which would otherwise
            # hide most of the first phase behind a fade-in.
            try:
                window.realize()
                gdk_window = window.get_window()
                if gdk_window is not None:
                    gdk_window.set_override_redirect(True)
            except Exception:  # pragma: no cover - not available everywhere
                pass
            windows.append(window)
        self._windows = windows
        self._phases = phases
        self._index = 1
        self._pending_phase = (color, ms)
        self._on_done = on_done
        self.busy = True
        self._apply_color(color)
        for window in windows:
            window.show_all()
            self._pass_through(window)
        # If the compositor never maps the windows, start anyway (and let
        # the phase timers end the flash) instead of staying busy forever.
        self._map_timeout = self.GLib.timeout_add(
            self.MAP_TIMEOUT_MS, self._start_first_phase
        )
        return True

    def _on_map_event(self, *_args):
        if self._pending_phase is None:
            return False
        self.GLib.timeout_add(self.MAP_SETTLE_MS, self._start_first_phase)
        return False

    def _start_first_phase(self):
        """Begin the first phase's timer once the overlay is on screen."""

        pending, self._pending_phase = self._pending_phase, None
        if self._map_timeout is not None:
            self.GLib.source_remove(self._map_timeout)
            self._map_timeout = None
        if pending is None or not self._windows:
            return False
        _color, ms = pending
        self.GLib.timeout_add(max(1, int(ms)), self._next_phase)
        return False

    def _apply_color(self, color: int) -> None:
        rgba = self.Gdk.RGBA()
        rgba.parse(f"#{color:02x}{color:02x}{color:02x}")
        for window in self._windows:
            window.override_background_color(
                self.Gtk.StateFlags.NORMAL, rgba
            )

    def _pass_through(self, window) -> None:
        """Empty the overlay's input shape so clicks reach the desktop.

        PyGObject binds `Gdk.Window.input_shape_combine_region` with the
        offset arguments (`region, 0, 0`), unlike the C API's separate
        parameters: calling it with the region only raises TypeError. Only a
        missing pycairo is tolerated, so a signature change cannot hide again.
        """

        try:
            import cairo
        except ImportError:  # pragma: no cover - GTK ships pycairo
            return
        gdk_window = window.get_window()
        if gdk_window is not None:
            gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)

    def _next_phase(self):
        if not self._windows:
            self.busy = False
            return False
        if self._index >= len(self._phases):
            self._finish()
            return False
        color, ms = self._phases[self._index]
        self._index += 1
        if color is None:
            self._finish()
            return False
        self._apply_color(color)
        self.GLib.timeout_add(max(1, int(ms)), self._next_phase)
        return False

    def _finish(self) -> None:
        windows, self._windows = self._windows, []
        self._phases = ()
        self._index = 0
        self._pending_phase = None
        if self._map_timeout is not None:
            self.GLib.source_remove(self._map_timeout)
            self._map_timeout = None
        for window in windows:
            window.destroy()
        self.busy = False
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback()

    def close(self) -> None:
        """Abandon a running flash and destroy the overlays (shutdown).

        The phase timer is not cancelled; its callback finds no windows and
        finishes itself. The completion callback is not run: the tray is
        quitting, and the estimate is saved separately.
        """

        if self._map_timeout is not None:
            self.GLib.source_remove(self._map_timeout)
            self._map_timeout = None
        windows, self._windows = self._windows, []
        self._phases = ()
        self._index = 0
        self._pending_phase = None
        self._on_done = None
        for window in windows:
            window.destroy()
        self.busy = False
