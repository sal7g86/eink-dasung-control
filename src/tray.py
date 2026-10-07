"""Universal tray icon (StatusNotifierItem) for GNOME, KDE and Cinnamon.

The tray talks to the monitor in-process through the project's client, so
serial behaviour is the same everywhere. GTK 3 and Ayatana AppIndicator bindings
live in the system Python, not in the project virtualenv: `dasungctl tray`
re-executes itself with the system interpreter when needed. The source runner
``python3 src/_source_run.py`` starts the tray from a checkout, and
``python3 -m dasungctl.tray`` does the same once the package is installed.

Every successful change is stored as the last configuration (see
``dasungctl.state``) and applied again the next time the tray starts, so the
monitor keeps the user's settings without named profiles or manual snapshots.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from functools import partial
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import threading
import time
import warnings

from . import controls
from . import logfile
from .client import DasungClient, MonitorInfo
from .config import (
    Config,
    ConfigError,
    autorefresh_settings,
    ghost_settings,
    load_config,
)
from .errors import (
    KIND_CONNECTION,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    SEVERITY_WORKING,
    error_kind,
    short_error,
)
from .ghostwatch import GhostWatcher
from .locking import serial_lock
from .panels import DEFAULT_PANEL, get_panel
from .protocol import (
    DisplayMode,
    FrontlightMode,
    ProtocolError,
    display_mode_name,
)
from .screencap import monitor_output_present, open_capture
from .state import StateError, apply_fields, load_last, save_last
from .transport import SerialTransport, TransportError
from .windows import open_zones
from .zoneclear import (
    ClearSettings,
    due_clear_elements,
    flash_phases,
    open_flasher,
)


# Gtk.ImageMenuItem is the only way to attach an icon to a menu item (the
# panel needs the dbusmenu "icon-name" property), but GTK 3 deprecated it.
warnings.filterwarnings(
    "ignore", message=r".*ImageMenuItem.*", category=DeprecationWarning
)


TRAY_ERRORS = (TransportError, ProtocolError, ValueError, OSError)
AUTOSTART_FILENAME = "dasungctl-tray.desktop"
LAUNCHER_FILENAME = "dasungctl"
ICON_NAME = "dasung-ink"
ICON_FILENAME = f"{ICON_NAME}.png"
# Theme icons adapt to light and dark panels; the bundled PNG is only a
# fallback because a light-detailed bitmap reads as a white blob at 22 px.
ICON_FALLBACKS = ("video-display-symbolic", "video-display", "computer-symbolic")
# Panel-dependent UI tables. These aliases describe the default profile so
# module-level importers (tests, helpers) keep working; the tray itself reads
# `controller.panel` for the active one.
FRONTLIGHT_STEP = DEFAULT_PANEL.frontlight_step
# The panel's brightness scale, measured on 2026-09-28 (docs/protocol.md): off
# plus ten levels of 10. The scale wraps around at both ends.
FRONTLIGHT_PRESETS: tuple[tuple[int, str], ...] = DEFAULT_PANEL.frontlight_levels
# Project scale for the manual temperature, coldest first, measured on
# 2026-09-28 (docs/protocol.md): the firmware accepts 0..100 (0 warmest, 100
# coldest) and clamps higher bytes to 100. Levels are labelled 1..10.
TEMPERATURE_LEVELS: tuple[int, ...] = DEFAULT_PANEL.temperature_levels
TEMPERATURE_LEVEL_LABELS: tuple[str, ...] = (
    "1 (cold)",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10 (warm)",
)
TEMPERATURE_LEVEL_PRESETS: tuple[tuple[int, str], ...] = tuple(
    zip(TEMPERATURE_LEVELS, TEMPERATURE_LEVEL_LABELS)
)
INTERVAL_CHOICES = (5, 10, 30, 60, 120, 180, 300, 600)
SCALE_APPLY_DELAY_MS = 400
# The ghost window preview is rendered at this width by the model and scaled
# up by GTK; the model's own resolution stays independent of it.
GHOST_PREVIEW_WIDTH = 300
# The "Test flash" button previews the current settings on a square this
# large at the center of the captured monitor: half of its smaller side, so
# it scales with the panel and is big enough to judge by eye.
TEST_FLASH_FRACTION = 0.5
# Tooltips of the `Clearing settings` editor. One table so the wording stays
# together with the policy semantics: levels are the 0-255 grey scale of the
# overlay phases and `delay` is the only selection parameter.
CLEAR_TOOLTIPS = {
    "style": (
        "White\u2013black alternates a bright and a dark phase; single grey "
        "runs one pulse. A flash only rewrites the area: it is a software "
        "overlay, not a monitor command."
    ),
    "white_level": "Brightness of the first phase, 0 (black) to 255 (white).",
    "white_ms": (
        "How long the first phase stays on screen, in milliseconds "
        "(1-2000). Below about 10-20 ms the compositor may not redraw the "
        "phase in time, so the panel can miss it."
    ),
    "black_level": "Brightness of the second phase, 0 (black) to 255 (white).",
    "black_ms": (
        "How long the second phase stays on screen, in milliseconds "
        "(1-2000)."
    ),
    "grey_level": "Brightness of the single grey pulse, 0 (black) to 255 (white).",
    "grey_ms": (
        "How long the single grey pulse stays on screen, in milliseconds "
        "(1-2000)."
    ),
    "delay": (
        "Seconds between the estimator recognizing an area (a rectangle in "
        "the preview) and its flash. That is the whole automatic policy."
    ),
}
# The tray shows and persists exactly the read fields of the active profile
# and never uses VERSION, SELECTOR_03, MUX or SELECTOR_11, so reloads read
# only those selectors: four fewer serial round trips per read.
READ_FIELDS = DEFAULT_PANEL.read_fields
# While the tray runs, a timer mirrors changes made with the monitor's
# physical buttons: the display fields are read back this often, and a
# confirmed difference updates the UI and the saved last configuration.
POLL_SECONDS = 5.0
# Failed exchanges in a row before the monitor is declared unavailable: one
# stall can happen while the panel processes a refresh, and it must not stop
# the automatic features or erase the saved preferences.
MONITOR_FAILURES = 2

SCALE_STEPS = {
    "contrast": 1,
    "speed": 1,
    "frontlight": FRONTLIGHT_STEP,
}
# Icon candidate chains, resolved against the icon theme at startup. The first
# name that exists in the theme wins; the last entry is the fail-safe. Names
# not in the theme are skipped silently.
ICON_INFO = ("dialog-information-symbolic",)
ICON_OK = ("emblem-ok-symbolic", "object-select-symbolic")
ICON_ERROR = ("dialog-error-symbolic", "dialog-warning-symbolic")
ICON_WARNING = ("dialog-warning-symbolic", "dialog-error-symbolic")
ICON_WORKING = ("emblem-synchronizing-symbolic", "view-refresh-symbolic")
ICON_CONTROLS = ("preferences-system-symbolic", "preferences-desktop-display-symbolic")
ICON_MODE = ("video-display-symbolic", "computer-symbolic")
ICON_CONTRAST = ("applications-graphics-symbolic", "image-x-generic-symbolic")
ICON_SPEED = ("media-seek-forward-symbolic", "media-playlist-repeat-symbolic")
ICON_FRONTLIGHT = ("display-brightness-symbolic", "weather-clear-symbolic")
ICON_FRONTLIGHT_OFF = ("weather-clear-night-symbolic", "display-brightness-symbolic")
ICON_FRONTLIGHT_MODE = ("power-symbolic", "weather-clear-night-symbolic")
ICON_REFRESH = ("view-refresh-symbolic", "emblem-synchronizing-symbolic")
ICON_SOFT_REFRESH = ("view-refresh-symbolic",)
ICON_HARD_REFRESH = ("edit-clear-symbolic", "window-close-symbolic")
ICON_AUTO_REFRESH = ("media-playlist-repeat-symbolic", "view-refresh-symbolic")
ICON_INTERVAL = ("alarm-symbolic", "appointment-soon-symbolic")
ICON_RELOAD = ("folder-download-symbolic", "document-revert-symbolic")
ICON_GHOST = ("system-search-symbolic", "edit-find-symbolic")
ICON_QUIT = ("application-exit-symbolic", "system-log-out-symbolic")
ICON_CUSTOM = ("document-edit-symbolic", "emblem-system-symbolic")
MODE_ICONS = {
    int(DisplayMode.AUTO): ("system-run-symbolic", "view-refresh-symbolic"),
    int(DisplayMode.TEXT): ("text-x-generic-symbolic",),
    int(DisplayMode.GRAPHIC): ("image-x-generic-symbolic",),
    int(DisplayMode.VIDEO): ("video-x-generic-symbolic", "video-display-symbolic"),
}
FRONTLIGHT_MODE_ICONS = {
    int(FrontlightMode.OFF): ICON_FRONTLIGHT_OFF,
    int(FrontlightMode.COLD): ("weather-snow-symbolic", "weather-overcast-symbolic"),
    int(FrontlightMode.WARM): ("weather-clear-symbolic",),
    int(FrontlightMode.MIXED): ("weather-few-clouds-symbolic",),
    DEFAULT_PANEL.custom_frontlight_mode: ICON_CUSTOM,
}

_ICON_CACHE: dict[tuple[str, ...], str | None] = {}


def icon_name(Gtk, candidates) -> str | None:
    """First candidate present in the icon theme, resolved once per chain."""

    if candidates not in _ICON_CACHE:
        theme = Gtk.IconTheme.get_default()
        _ICON_CACHE[candidates] = next(
            (name for name in candidates if theme.lookup_icon(name, 16, 0)),
            None,
        )
    return _ICON_CACHE[candidates]


def status_symbol(message: str, severity: str = "") -> tuple[tuple[str, ...], str]:
    """Icon chain and CSS class for a non-empty status message.

    The severity is structured (`errors.SEVERITY_*`), never parsed from the
    message text.
    """

    if severity == SEVERITY_ERROR:
        return ICON_ERROR, "dasung-error"
    if severity == SEVERITY_WARN:
        return ICON_WARNING, ""
    if severity == SEVERITY_WORKING:
        return ICON_WORKING, ""
    return ICON_OK, "dasung-ok"


def nearest_preset(
    presets: tuple[tuple[int, str], ...], value: int | None
) -> tuple[int, str] | None:
    """Preset whose value is closest to `value`; None when unknown."""

    if value is None or not presets:
        return None
    return min(presets, key=lambda preset: abs(preset[0] - int(value)))


def frontlight_label(value: int | None, panel=None) -> str | None:
    """Label a raw frontlight byte with its calibrated level (`Off`, `1`..`10`)."""

    return get_panel(panel).frontlight_label(value)


def interval_label(seconds: float) -> str:
    """Short label for an auto-refresh interval (`30 s`, `5 min`)."""

    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min"


def temperature_level(value: int | None, panel=None) -> int | None:
    """Byte of the project temperature level closest to `value`."""

    return get_panel(panel).temperature_level(value)


def temperature_level_number(value: int | None, panel=None) -> int | None:
    """Level 1..10 closest to a raw temperature byte; None when unknown."""

    return get_panel(panel).temperature_level_number(value)


def temperature_byte(level: int, panel=None) -> int:
    """Byte for a 1..10 temperature level (1 coldest, 10 warmest)."""

    return get_panel(panel).temperature_byte(level)


def temperature_presets(panel=None) -> tuple[tuple[int, str], ...]:
    """`(byte, label)` entries of the custom-temperature submenu."""

    levels = get_panel(panel).temperature_levels
    entries = []
    for index, byte in enumerate(levels):
        if index == 0:
            entries.append((byte, "1 (cold)"))
        elif index == len(levels) - 1:
            entries.append((byte, f"{index + 1} (warm)"))
        else:
            entries.append((byte, str(index + 1)))
    return tuple(entries)


class TrayDependencyError(RuntimeError):
    """GTK 3 or the Ayatana AppIndicator bindings are unavailable."""


def icon_path() -> Path:
    """Absolute path of the bundled fallback tray icon."""

    return Path(__file__).resolve().parent / "icons" / ICON_FILENAME


# MonitorInfo is frozen, so one all-unknown instance can be shared.
EMPTY_INFO = MonitorInfo(**{spec.name: None for spec in fields(MonitorInfo)})


@dataclass
class TrayState:
    """Everything the tray shows; mutable and independent of GTK."""

    info: MonitorInfo
    message: str = ""
    # Structured feedback for the UI: `severity` drives the status icon and
    # colour (errors.SEVERITY_*), `error_detail` the Controls window tooltip
    # and `error_kind` whether a failure means the monitor is unreachable.
    severity: str = ""
    error_detail: str = ""
    error_kind: str | None = None
    custom_temperature: int | None = None
    autorefresh: bool = False
    interval: float = 300.0
    last_refresh: float = 0.0
    # Automatic zone clearing (the ghost window's switch) and the clearing
    # settings edited in that window; off by default and remembered in the
    # state file, like the auto-refresh timer.
    ghost_clear: bool = False
    ghost_clear_settings: dict = field(default_factory=dict)
    # The ghost estimate's runtime switch, flipped by the `Ghost estimate…`
    # window: on by default and remembered in the state file. The config's
    # `ghost.enabled` remains the master switch that can disable the feature
    # entirely.
    ghost_estimate: bool = True

    @property
    def mode_name(self) -> str:
        """The mode name for the UI, or `?` before the first read."""

        mode = self.info.mode
        return display_mode_name(mode) if mode is not None else "?"


def _change_text(name: str, value: int, panel=None) -> str:
    """Describe one externally changed field for the status message."""

    profile = get_panel(panel)
    if name == "mode":
        return f"mode {profile.display_mode_name(value)}"
    if name == "speed":
        return f"speed {profile.speed_name(value)}"
    if name == "frontlight":
        return f"frontlight {profile.frontlight_label(value)}"
    if name == "frontlight_mode":
        return f"frontlight mode {profile.frontlight_mode_name(value)}"
    return f"{name} {value}"


class TrayController:
    """Serial operations and auto-refresh timing, without any GUI imports."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        device: str | None = None,
        timeout: float | None = None,
        interval: float | None = None,
        opener=None,
        locker=None,
        clock=time.monotonic,
        log=None,
    ) -> None:
        """Resolve the settings and prepare the state; no serial access."""

        self.config = config or Config()
        self.log = log if log is not None else logfile.NullLog()
        self.panel = get_panel(self.config.panel)
        self.device = device or self.config.device
        self.timeout = timeout if timeout is not None else self.config.timeout
        default_interval, timer_hard = autorefresh_settings(self.config)
        clear_defaults = dict(ghost_settings(self.config)["clear"])
        self.state = TrayState(
            info=EMPTY_INFO,
            interval=default_interval if interval is None else float(interval),
            ghost_clear=bool(clear_defaults["enabled"]),
            ghost_clear_settings=clear_defaults,
        )
        # The config picks the waveform the periodic timer sends; the tray's
        # own auto-refresh switch and interval start from the config too and
        # are restored from the state file when one exists.
        self.timer_hard = timer_hard
        self._opener = opener or self._open_transport
        self._locker = locker or serial_lock
        self._clock = clock
        # Monitor reachability, split by signal: `serial_available` comes
        # from the serial exchanges, `display_present` from the e-ink output
        # probe (Gdk monitors on X11, DRM sysfs on Wayland) and
        # `monitor_available` is their combination. The CH340
        # stays powered when the panel is switched off and its selectors keep
        # answering, so only the output tells "off" from "on".
        self.monitor_available: bool | None = None
        self.serial_available: bool | None = None
        self.display_present: bool | None = None
        self._failures = 0
        self._display_misses = 0
        # True once restore() has loaded the saved preferences: the startup
        # output probe can confirm the panel is off before that, and there is
        # nothing meaningful to persist until the saved values are in.
        self.prefs_loaded = False
        # True while the saved configuration has not been applied yet (the
        # panel was off at startup, or the session failed): the tray retries
        # it when the monitor becomes reachable.
        self.restore_pending = False
        # External-change polling starts counting at construction, so the
        # first poll happens one POLL_SECONDS after the tray starts.
        self._last_poll = self._clock()
        # Fields of the last successfully saved configuration, so that
        # applying the same value twice does not rewrite the state file.
        self._stored_key: tuple | None = None

    def _open_transport(self):
        return SerialTransport(device=self.device, timeout=self.timeout)

    @contextmanager
    def _session(self):
        """One locked serial session with a fresh client."""

        with self._locker():
            with self._opener() as transport:
                yield DasungClient(transport)

    def _set_status(self, message: str, severity: str = "", detail: str = "") -> None:
        """Publish a status line and clear the previous error context."""

        self.state.message = message
        self.state.severity = severity
        self.state.error_detail = detail
        self.state.error_kind = None

    def _set_error(
        self, message: str, detail: str = "", kind: str = KIND_CONNECTION
    ) -> None:
        """Publish a failure; `detail` feeds the window tooltip and the log."""

        self.state.message = message
        self.state.severity = SEVERITY_ERROR
        self.state.error_detail = detail
        self.state.error_kind = kind

    def _strike(self) -> bool:
        """Count one connection failure; True when the monitor is confirmed lost.

        One failed exchange can be a transient stall (the firmware ignores
        reads right after a command), so the monitor is declared unavailable
        only after `MONITOR_FAILURES` failures in a row; a success resets the
        counter.
        """

        self._failures += 1
        if self._failures < MONITOR_FAILURES:
            return False
        self.serial_available = False
        self.monitor_available = False
        self._stop_automatic()
        if self._failures == MONITOR_FAILURES and self.display_present is True:
            self.log.warn(
                "serial silent while the display output is present: if it "
                "stays mute, replug the monitor's USB cable"
            )
        return True

    def _succeed(self) -> None:
        """Reset the failure count and announce a recovery once."""

        was = self.monitor_available
        self._failures = 0
        self.serial_available = True
        if self.display_present is False:
            # The serial interface works but the panel is off: it stays
            # unavailable until the display output comes back.
            self.monitor_available = False
            return
        self.monitor_available = True
        if was is False and self.state.severity == SEVERITY_ERROR:
            self._set_status("monitor connected")
            self.log.info(self.state.message)

    def set_display_present(self, present: bool | None) -> None:
        """Feed the e-ink output probe into the availability state.

        `False` means the output is missing (the panel's HDMI receiver is
        off), `True` that it is there, `None` that the probe cannot tell
        (no display, no readable output data, no way to identify the panel):
        `None` leaves the serial-only behaviour. The first verdict is
        conclusive; after the output was seen, two misses in a row confirm
        the panel is off so a display reconfiguration does not switch the
        automatic features off.
        """

        if present is None:
            return
        if present:
            self._display_misses = 0
            was_off = self.display_present is False
            self.display_present = True
            if was_off and self.serial_available is True:
                self.monitor_available = True
                if self.state.severity == SEVERITY_ERROR:
                    self._set_status("monitor connected")
                    self.log.info(self.state.message)
            return
        self._display_misses += 1
        # The first verdict is conclusive: the output was never seen, so a
        # tray started with the panel off must not wait (and must not write
        # to the serial). Later misses need confirmation, so a display
        # reconfiguration does not switch the automatic features off.
        if (
            self.display_present is not None
            and self._display_misses < MONITOR_FAILURES
        ):
            return
        if self.display_present is False:
            # A later success message may have replaced the reason.
            if (
                self.serial_available is not False
                and self.state.severity != SEVERITY_ERROR
            ):
                self._set_error("monitor off (no display output)")
            return
        self.display_present = False
        self.monitor_available = False
        if self.serial_available is False:
            # The serial side already reported the monitor missing.
            return
        self._stop_automatic()
        self._set_error("monitor off (no display output)")
        self.log.warn("monitor off: no display output")

    def _panel_off(self) -> bool:
        """True when the e-ink output is known to be missing (panel off).

        The serial interface stays powered in that state, but a command sent
        to a switched-off panel can wedge the monitor's firmware (observed
        2026-10-04): while the output is missing the tray never opens the
        serial port.
        """

        return self.display_present is False

    def _stop_automatic(self) -> None:
        """Switch the automatic features off when the monitor is gone.

        The choice is persisted like any manual switch, so the features stay
        off when the monitor comes back. Before restore() has loaded the
        saved preferences there is nothing to persist: the availability gate
        already keeps the features from running, and restore() stops them
        once it has the saved values.
        """

        if not self.prefs_loaded:
            return
        if self.state.autorefresh:
            self.log.warn("auto-refresh off: monitor unavailable")
            self.set_autorefresh(False, announce=False)
        if self.state.ghost_estimate:
            self.log.warn("ghost estimate off: monitor unavailable")
            self.set_ghost_estimate(False, announce=False)

    def _fail(self, exc: Exception) -> None:
        """Publish a failed operation and count connection failures."""

        kind = error_kind(exc)
        if kind == KIND_CONNECTION:
            self._strike()
        self._set_error(short_error(exc), str(exc), kind)

    def _blank_read(self, info: MonitorInfo) -> bool:
        """True when a lenient read obtained no field at all.

        The tray reads leniently, so a missing monitor does not raise: every
        selector simply fails and the answer is all None. Losing every field
        that way is a missing monitor, not a successful reload.
        """

        return all(
            getattr(info, name) is None for name in self.panel.read_fields
        )

    def _saved_info(self, saved) -> MonitorInfo:
        """The saved monitor fields as a MonitorInfo (mode already resolved)."""

        return replace(
            EMPTY_INFO,
            **{
                name: saved[name]
                for name in self.panel.read_fields
                if name in saved
            },
        )

    def _normalize(self, info: MonitorInfo) -> MonitorInfo:
        """Map a mixed read-back to the project's custom mode.

        The firmware answers 3 (mixed) after a custom write and has no mode 4:
        when the read temperature matches the last manually set one, the UI
        keeps showing `custom` instead of `mixed`. A mixed preset or a
        physical lamp press keeps its own temperature (70), so it still reads
        as mixed.
        """

        if (
            info.frontlight_mode == self.panel.mixed_frontlight_mode
            and info.temperature is not None
            and info.temperature == self.state.custom_temperature
        ):
            return replace(
                info, frontlight_mode=self.panel.custom_frontlight_mode
            )
        return info

    def _remember(self, info: MonitorInfo) -> None:
        """Mirror a fresh read into the state, keeping the custom temperature."""

        self.state.info = info
        if (
            info.frontlight_mode == self.panel.custom_frontlight_mode
            and info.temperature is not None
        ):
            self.state.custom_temperature = info.temperature

    def read(self) -> bool:
        """Reload the display fields from the monitor (no writes)."""

        if self._panel_off():
            self.log.warn("reload skipped: monitor off (no display output)")
            self._set_error("monitor off (no display output)")
            return False
        try:
            with self._session() as client:
                info = client.read_info(
                    lenient=True, fields=self.panel.read_fields
                )
        except TRAY_ERRORS as exc:
            self.log.error(f"reload failed: {exc}")
            self._fail(exc)
            return False
        if self._blank_read(info):
            self.log.error("reload failed: no selector answered")
            self._strike()
            self._set_error("monitor not responding", "no selector answered")
            return False
        self._succeed()
        self._remember(self._normalize(info))
        self._set_status("reloaded")
        self.log.info("reloaded from the monitor")
        return True

    def sync(self) -> bool:
        """Mirror changes made on the monitor's physical controls.

        The display fields are read leniently: a selector that does not answer
        (the monitor stalls right after a write) keeps its previous value, so
        only confirmed differences update the UI and the saved configuration.
        The poll is also the availability probe: one failed read stays silent,
        while the second in a row declares the monitor missing (the status
        line says so and the automatic features are switched off). A panel
        known to be off is never polled at all.
        """

        self._last_poll = self._clock()
        if self._panel_off():
            return False
        try:
            with self._session() as client:
                info = client.read_info(
                    lenient=True, fields=self.panel.read_fields
                )
        except TRAY_ERRORS as exc:
            if self._strike():
                self._set_error(short_error(exc), str(exc))
            return False
        if self._blank_read(info):
            if self._strike():
                self._set_error(
                    "monitor not responding", "no selector answered"
                )
            return False
        self._succeed()
        info = self._normalize(info)
        changed = {
            name: getattr(info, name)
            for name in self.panel.read_fields
            if getattr(info, name) is not None
            and getattr(info, name) != getattr(self.state.info, name)
        }
        if not changed:
            return False
        self._remember(replace(self.state.info, **changed))
        self._set_status(
            "monitor changed: "
            + ", ".join(
                _change_text(name, value, self.panel)
                for name, value in changed.items()
            )
        )
        self.log.info(self.state.message)
        self._store_last()
        return True

    def restore(self) -> bool:
        """Apply the saved configuration, then mirror the monitor state.

        A missing file simply means a plain read; a corrupt or stale one is
        reported without preventing the tray from starting.
        """

        saved = None
        note = ""
        detail = ""
        try:
            saved = load_last(panel=self.panel)
        except StateError as exc:
            note = "saved settings ignored (invalid file)"
            detail = str(exc)
        restored = saved or {}
        # The preferences are loaded before the serial session: when the
        # monitor is missing the session fails, and switching the automatic
        # features off must persist them without losing the saved values.
        if "autorefresh" in restored:
            self.state.autorefresh = restored["autorefresh"]
            if self.state.autorefresh:
                # Count the first interval from startup, not from tray start.
                self.state.last_refresh = self._clock()
        if "autorefresh_interval" in restored:
            self.state.interval = restored["autorefresh_interval"]
        if "ghost_clear" in restored:
            merged = dict(self.state.ghost_clear_settings)
            merged.update(restored["ghost_clear"])
            merged["enabled"] = bool(merged["enabled"])
            self.state.ghost_clear_settings = merged
            self.state.ghost_clear = merged["enabled"]
        if "ghost_estimate" in restored:
            self.state.ghost_estimate = restored["ghost_estimate"]
        self.prefs_loaded = True
        if self._panel_off():
            # The serial is never opened with the panel off: the saved
            # configuration is applied again when the output comes back.
            if saved:
                self.state.info = self._saved_info(saved)
            if note:
                self.log.warn(f"last configuration ignored: {detail}")
            self._stop_automatic()
            self.log.warn("restore skipped: monitor off (no display output)")
            self._set_error("monitor off (no display output)")
            self.restore_pending = True
            return False
        try:
            with self._session() as client:
                if saved:
                    temperature = saved.get("temperature")
                    if (
                        saved.get("frontlight_mode")
                        == self.panel.custom_frontlight_mode
                        and temperature is not None
                    ):
                        # The firmware has no custom mode: seed the manual
                        # temperature so the save/restore round trip keeps
                        # showing `custom` instead of `mixed`.
                        self.state.custom_temperature = temperature
                    apply_fields(client, saved, wait=True, panel=self.panel)
                info = client.read_info(
                    lenient=True, fields=self.panel.read_fields
                )
        except TRAY_ERRORS as exc:
            if saved:
                # Keep the saved settings in the state even though the session
                # failed: switching the automatic features off rewrites the
                # file, and it must not lose the monitor fields with it.
                self.state.info = self._saved_info(saved)
            self.log.error(f"restore failed: {exc}")
            self._fail(exc)
            self.restore_pending = True
            return False
        self._succeed()
        self._remember(self._normalize(info))
        self.restore_pending = False
        if note:
            self._set_status(note, SEVERITY_WARN, detail)
            self.log.warn(f"last configuration ignored: {detail}")
        elif saved:
            self._set_status("last configuration restored")
            self.log.info(self.state.message)
        else:
            self._set_status("reloaded")
            self.log.info("reloaded from the monitor")
        return True

    def apply(self, name: str, value: int) -> bool:
        """Apply one field through the shared control logic and persist it."""

        if self._panel_off():
            self.log.warn("apply skipped: monitor off (no display output)")
            self._set_error("monitor off (no display output)")
            return False
        try:
            with self._session() as client:
                applied = controls.apply_field(
                    client, self.state, name, value, self.panel
                )
        except TRAY_ERRORS as exc:
            self.log.error(
                f"apply {_change_text(name, value, self.panel)} failed: {exc}"
            )
            self._fail(exc)
            return False
        if not applied:
            # controls already published the short message and its detail; a
            # connection failure also counts toward the availability check.
            if self.state.error_kind == KIND_CONNECTION:
                self._strike()
            return False
        self._succeed()
        self.log.info(f"applied {_change_text(name, value, self.panel)}")
        self._store_last()
        return applied

    def _store_last(self) -> None:
        """Persist the last configuration, skipping identical rewrites.

        Covers both the monitor fields and the tray preferences (auto-refresh
        on/off and interval). Called after every successful change; the JSON
        file is only rewritten when something actually changed, so
        re-selecting the current value costs no disk write.
        """

        key = tuple(
            getattr(self.state.info, field) for field in self.panel.read_fields
        ) + (
            self.state.autorefresh,
            self.state.interval,
            self.state.ghost_clear,
            self.state.ghost_estimate,
            tuple(sorted(self.state.ghost_clear_settings.items())),
        )
        if key == self._stored_key:
            return
        try:
            save_last(
                self.state.info,
                prefs={
                    "autorefresh": self.state.autorefresh,
                    "autorefresh_interval": self.state.interval,
                    "ghost_clear": dict(self.state.ghost_clear_settings),
                    "ghost_estimate": self.state.ghost_estimate,
                },
                panel=self.panel,
            )
        except OSError as exc:
            self.state.message += " (not saved)"
            if self.state.severity != SEVERITY_ERROR:
                self.state.severity = SEVERITY_WARN
            self.state.error_detail = str(exc)
            self.log.error(f"cannot save the last configuration: {exc}")
            return
        self._stored_key = key

    def refresh(self, hard: bool = False) -> bool:
        """Send a global soft or hard refresh and timestamp it."""

        if self._panel_off():
            self.log.warn("refresh skipped: monitor off (no display output)")
            self._set_error("monitor off (no display output)")
            return False
        try:
            with self._session() as client:
                client.refresh(hard=hard, wait=True)
        except TRAY_ERRORS as exc:
            self.log.error(f"refresh failed: {exc}")
            self._fail(exc)
            return False
        self._succeed()
        self.state.last_refresh = self._clock()
        self._set_status("hard refresh sent" if hard else "soft refresh sent")
        self.log.info(self.state.message)
        return True

    def set_autorefresh(self, enabled: bool, *, announce: bool = True) -> bool:
        """Turn the periodic refresh on or off (timing restarts from now).

        The switch is refused while the monitor is missing: the timer would
        have nothing to refresh. Returns whether the choice was applied.
        """

        if enabled and self.monitor_available is not True:
            self.log.warn("auto-refresh not started: monitor unavailable")
            return False
        self.state.autorefresh = bool(enabled)
        self.state.last_refresh = self._clock()
        if announce:
            self._set_status("auto-refresh on" if enabled else "auto-refresh off")
        self.log.info("auto-refresh on" if enabled else "auto-refresh off")
        self._store_last()
        return True

    def set_interval(self, seconds: float) -> None:
        """Set the auto-refresh period in seconds."""

        self.state.interval = float(seconds)
        self._set_status(f"auto-refresh interval: {interval_label(seconds)}")
        self.log.info(self.state.message)
        self._store_last()

    def set_ghost_clear(self, enabled: bool) -> None:
        """Turn automatic zone clearing on or off and persist the choice."""

        self.state.ghost_clear = bool(enabled)
        self.state.ghost_clear_settings["enabled"] = bool(enabled)
        self._set_status(
            "ghost auto-clear on" if enabled else "ghost auto-clear off"
        )
        self.log.info(self.state.message)
        self._store_last()

    def set_ghost_clear_settings(self, values) -> None:
        """Merge clearing settings from the ghost window and persist them."""

        merged = dict(self.state.ghost_clear_settings)
        merged.update(values)
        merged["enabled"] = self.state.ghost_clear
        self.state.ghost_clear_settings = merged
        self._set_status("ghost clearing settings updated")
        self.log.info(self.state.message)
        self._store_last()

    def set_ghost_estimate(self, running: bool, *, announce: bool = True) -> bool:
        """Stop or restart the ghost estimate and persist the choice.

        Refused while the monitor is missing: sampling would be suspended
        anyway. Returns whether the choice was applied.
        """

        if running and self.monitor_available is not True:
            self.log.warn("ghost estimate not started: monitor unavailable")
            return False
        self.state.ghost_estimate = bool(running)
        if announce:
            self._set_status(
                "ghost estimate started" if running else "ghost estimate stopped"
            )
        self.log.info(
            "ghost estimate started" if running else "ghost estimate stopped"
        )
        self._store_last()
        return True

    def due(self, now: float | None = None) -> bool:
        """True when auto-refresh is on and its interval has elapsed.

        Automatic refreshes need the monitor: while it is missing or not
        confirmed yet the timer waits.
        """

        if not self.state.autorefresh or self.monitor_available is not True:
            return False
        moment = self._clock() if now is None else now
        return moment - self.state.last_refresh >= self.state.interval

    def poll_due(self, now: float | None = None) -> bool:
        """True when the external-change poll is due."""

        moment = self._clock() if now is None else now
        return moment - self._last_poll >= POLL_SECONDS

    def tick(self, now: float | None = None) -> bool:
        """Send the periodic refresh when it is due (used by tests)."""

        if not self.due(now):
            return False
        return self.refresh(hard=self.timer_hard)


def autostart_path() -> Path:
    """The .desktop file the tray installs for login startup."""

    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / AUTOSTART_FILENAME


def _exec_argument(value: str) -> str:
    """Quote one `Exec` argument the way the desktop entry spec requires."""

    if not any(character in value for character in ' \t"\\$`'):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def _autostart_command() -> str:
    """The command the autostart entry runs.

    A `dasungctl` launcher in `~/.local/bin` (written by
    `tray --install-launcher`, or a machine-specific script) wins: it is the
    entry point the user knows, and a script can report a missing disk where
    the direct command below would just fail silently at login.
    """

    launcher = Path.home() / ".local" / "bin" / "dasungctl"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return f"{_exec_argument(str(launcher))} tray"
    runner = Path(__file__).resolve().parent / "_source_run.py"
    python = shutil.which("python3") or sys.executable
    return (
        f"{_exec_argument(python)} {_exec_argument(str(runner))} "
        f"--module dasungctl.tray"
    )


def autostart_entry() -> str:
    """The .desktop file that starts the tray at login."""

    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DASUNG Control Tray\n"
        "Comment=Dasung Paperlike monitor control in the system tray\n"
        f"Exec={_autostart_command()}\n"
        f"Icon={icon_path()}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def write_autostart() -> Path:
    """Write the autostart .desktop file and return its path."""

    target = autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(autostart_entry(), encoding="utf-8")
    return target


def launcher_path() -> Path:
    """The `dasungctl` command the tray installs in `~/.local/bin`."""

    return Path.home() / ".local" / "bin" / LAUNCHER_FILENAME


def launcher_script() -> str:
    """The launcher that runs this checkout with the system Python.

    The GTK bindings live in the system Python, and `_source_run.py` maps the
    package name onto the sources, so the command works without installing
    anything. When the checkout is on a removable disk the script reports the
    missing path (with a desktop notification) instead of failing silently.
    """

    runner = Path(__file__).resolve().parent / "_source_run.py"
    return (
        "#!/bin/sh\n"
        "# dasungctl launcher: runs the program from its checkout with the\n"
        "# system Python, where the GTK bindings live. Generated by\n"
        "# `dasungctl tray --install-launcher`.\n"
        "set -u\n"
        "\n"
        f"runner={shlex.quote(str(runner))}\n"
        'if [ ! -f "$runner" ]; then\n'
        '    warning="dasungctl: the program is not available."\n'
        '    detail="Expected $runner. If it is on a removable disk, '
        'attach it and retry."\n'
        "    printf '%s\\n%s\\n' \"$warning\" \"$detail\" >&2\n"
        "    if command -v notify-send >/dev/null 2>&1 &&\n"
        '        [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then\n'
        "        notify-send --urgency=critical --icon=dialog-error "
        "dasungctl \\\n"
        '            "$warning $detail" >/dev/null 2>&1 || true\n'
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        "\n"
        'python="${DASUNGCTL_PYTHON:-/usr/bin/python3}"\n'
        '[ -x "$python" ] || python=python3\n'
        'exec "$python" "$runner" "$@"\n'
    )


def write_launcher() -> Path:
    """Write the `dasungctl` launcher and return its path."""

    target = launcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(launcher_script(), encoding="utf-8")
    target.chmod(0o755)
    return target


def _load_gtk() -> dict:
    """Import GTK 3 and the Ayatana indicator, or raise TrayDependencyError.

    The bindings only exist in the system Python (see the module docstring),
    so the failure message names the packages a user usually has to install.
    """

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import (
            AyatanaAppIndicator3,
            Gdk,
            GdkPixbuf,
            GLib,
            Gtk,
        )
    except (ImportError, ValueError) as exc:
        raise TrayDependencyError(
            "GTK 3 and Ayatana AppIndicator are required for the tray. Install "
            "python3-gi, gir1.2-gtk-3.0 and gir1.2-ayatanaappindicator3-0.1 "
            "(on GNOME also gnome-shell-extension-appindicator), then rerun "
            "`dasungctl tray`."
        ) from exc
    return {
        "Gtk": Gtk,
        "GLib": GLib,
        "Gdk": Gdk,
        "GdkPixbuf": GdkPixbuf,
        "Indicator": AyatanaAppIndicator3,
    }


# GTK 3.24 emits this Gdk critical while it processes the toplevel's map and
# unmap events. It was reproduced with the Controls window (open, close,
# reopen) and traced with gdb: the assertion fires inside GDK's own event
# dispatch (gdk_display_get_event -> internal GDK frames ->
# gdk_window_thaw_toplevel_updates) and no project frame is involved. It
# survives every app-level restructuring tried (show_all vs present, deferred
# or skipped updates, destroying and recreating the window) and costs nothing
# but log noise; the installed GTK build (3.24.41-4ubuntu1.3) has no debug
# symbols on debuginfod or ddebs, so the remaining frames cannot be named.
# Filter only this exact message and forward every other Gdk log.
GDK_KNOWN_NOISE = "gdk_window_thaw_toplevel_updates: assertion"
# libayatana-appindicator warns about its own deprecation on every indicator
# creation; the replacement bindings are not shipped on the tested systems.
APPINDICATOR_KNOWN_NOISE = "libayatana-appindicator is deprecated"


def gdk_noise(message: str) -> bool:
    """True for the known harmless Gdk critical documented above."""

    return GDK_KNOWN_NOISE in message


def appindicator_noise(message: str) -> bool:
    """True for the known harmless libayatana-appindicator warning."""

    return APPINDICATOR_KNOWN_NOISE in message


def install_gdk_log_filter(GLib) -> None:
    """Drop only the known Gdk critical; forward all other Gdk logs."""

    def handler(domain, level, message, user_data):
        if gdk_noise(message):
            return
        GLib.log_default_handler(domain, level, message, user_data)

    GLib.log_set_handler(
        "Gdk", GLib.LogLevelFlags.LEVEL_CRITICAL, handler, None
    )


def install_appindicator_log_filter(GLib) -> None:
    """Drop the indicator deprecation warning; forward other indicator logs."""

    def handler(domain, level, message, user_data):
        if appindicator_noise(message):
            return
        GLib.log_default_handler(domain, level, message, user_data)

    GLib.log_set_handler(
        "libayatana-appindicator",
        GLib.LogLevelFlags.LEVEL_WARNING,
        handler,
        None,
    )


class TrayApp:
    """Thin GTK layer around TrayController."""

    def __init__(
        self,
        controller: TrayController,
        gtk: dict,
        watcher: GhostWatcher | None = None,
        log=None,
        *,
        ghost_output: str = "auto",
    ) -> None:
        """Build the indicator and menu; GTK must already be initialised."""

        self.controller = controller
        self.log = log if log is not None else logfile.NullLog()
        self.Gtk = gtk["Gtk"]
        self.GLib = gtk["GLib"]
        self.Gdk = gtk["Gdk"]
        self.GdkPixbuf = gtk["GdkPixbuf"]
        self.watcher = watcher
        # The e-ink output probe looks for the same monitor the capture uses
        # (`ghost.output`, `auto` by default) and runs with the serial poll.
        self._ghost_output = ghost_output
        self._last_output_poll: float | None = None
        self._busy = False
        self._pending = None
        self._updating = False
        self._window = None
        self._ghost_window = None
        # One quit path for the menu entry and SIGINT/SIGTERM, so cleanup
        # (estimate saved, capture closed, overlays destroyed) runs once.
        self._quitting = False
        # Last ghost sampling error written to the log, so a persistent
        # failure is reported once and not on every tick.
        self._logged_ghost_error: str | None = None
        # Last known monitor reachability: on False -> True the estimate is
        # reset, because the panel state is unknown across the gap.
        self._last_available: bool | None = None
        # Zone clearing: the overlay flasher and the last note shown in the
        # ghost window. The settings themselves come from the controller's
        # saved state (the ghost window edits them), so editor and
        # persistence share one source. The flasher picks the session's
        # placement route: X11 override-redirect windows or Wayland
        # layer-shell surfaces; unsupported sessions report the reason.
        self._clearer = open_flasher(gtk)
        self._clear_note: str | None = None
        # Last text/icon exported to the panel: refresh_menu() compares before
        # writing, so unchanged state produces no D-Bus property updates and
        # no panel redraws.
        self._indicator_label: str | None = None
        self._indicator_title: str | None = None
        self._status_label: str | None = None
        self._status_icon_name: str | None = None

        indicator = gtk["Indicator"].Indicator.new(
            "dasungctl",
            ICON_NAME,
            gtk["Indicator"].IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_status(gtk["Indicator"].IndicatorStatus.ACTIVE)
        self._set_icon(indicator)
        indicator.set_label("…", "")
        self.indicator = indicator
        self.menu = self.Gtk.Menu()
        self._build_menu()
        # The menu is exported over D-Bus (StatusNotifierItem/dbusmenu) and
        # rendered by the panel; hidden widgets are exported as visible=false,
        # which leaves the popup as an empty white rectangle. show_all() is
        # recursive, so the submenus are covered too.
        self.menu.show_all()
        indicator.set_menu(self.menu)

    def _set_icon(self, indicator) -> None:
        """Prefer a symbolic theme icon, fall back to the bundled bitmap."""

        theme = self.Gtk.IconTheme.get_default()
        for name in ICON_FALLBACKS:
            if theme.lookup_icon(name, 24, 0) is not None:
                indicator.set_icon_full(name, "DASUNG monitor")
                return
        indicator.set_icon_full(str(icon_path()), "DASUNG monitor")

    # -- menu construction -------------------------------------------------

    def _image_item(self, label, candidates, handler=None):
        """Menu item whose theme icon the panel exports as dbusmenu icon-name.

        Only Gtk.ImageMenuItem can carry an icon that survives the D-Bus
        export, so the entries that need a radio/check indicator stay plain
        text (see _radio_items) and show their state in the parent label.
        """

        item = self.Gtk.ImageMenuItem.new_with_mnemonic(label)
        name = icon_name(self.Gtk, tuple(candidates))
        if name is not None:
            item.set_image(
                self.Gtk.Image.new_from_icon_name(name, self.Gtk.IconSize.MENU)
            )
            item.set_always_show_image(True)
        if handler is not None:
            item.connect("activate", lambda _item: handler())
        return item

    def _submenu(self, label, candidates):
        """Append an icon parent with an empty submenu and return both."""

        parent = self._image_item(label, candidates)
        submenu = self.Gtk.Menu()
        parent.set_submenu(submenu)
        self.menu.append(parent)
        return parent, submenu

    def _radio_items(self, submenu, values, on_toggle):
        """Radio choices inside a submenu; returns each item keyed by value."""

        Gtk = self.Gtk
        items: dict[int, object] = {}
        group = None
        for value, text in values:
            item = Gtk.RadioMenuItem(label=text)
            if group is None:
                group = item
            else:
                item.join_group(group)
            item.connect("toggled", lambda widget, v=value: on_toggle(widget, v))
            submenu.append(item)
            items[value] = item
        return items

    def _build_menu(self) -> None:
        """Create the whole menu once; refresh_menu() only rewrites labels."""

        Gtk = self.Gtk
        self.status_item = self._image_item("connecting…", ICON_INFO)
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        self.controls_item = self._image_item(
            "Controls…", ICON_CONTROLS, lambda: self._open_window(None)
        )
        self.menu.append(self.controls_item)
        self.ghost_item = self._image_item(
            "Ghost estimate…", ICON_GHOST, lambda: self._open_ghost_window(None)
        )
        self.menu.append(self.ghost_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        panel = self.controller.panel

        self.mode_item, mode_menu = self._submenu("Mode", ICON_MODE)
        for value, name in panel.modes.items():
            mode_menu.append(
                self._image_item(
                    name,
                    MODE_ICONS.get(value, ICON_MODE),
                    lambda m=int(value): self._run(
                        self.controller.apply, "mode", m
                    ),
                )
            )

        self.contrast_item, contrast_menu = self._submenu("Contrast", ICON_CONTRAST)
        low, high = panel.limits["contrast"]
        self._contrast_items = self._radio_items(
            contrast_menu,
            [(value, str(value)) for value in range(low, high + 1)],
            lambda item, value: self._radio_toggled(item, "contrast", value),
        )

        self.speed_item, speed_menu = self._submenu("Speed", ICON_SPEED)
        self._speed_items = self._radio_items(
            speed_menu,
            [
                (value, panel.speed_name(value))
                for value in range(1, len(panel.speed_labels) + 1)
            ],
            lambda item, value: self._radio_toggled(item, "speed", value),
        )

        self.frontlight_item, frontlight_menu = self._submenu(
            "Frontlight", ICON_FRONTLIGHT
        )
        self._frontlight_items = self._radio_items(
            frontlight_menu,
            panel.frontlight_levels,
            lambda item, value: self._radio_toggled(item, "frontlight", value),
        )

        self.frontlight_mode_item, frontlight_mode_menu = self._submenu(
            "Frontlight mode", ICON_FRONTLIGHT_MODE
        )
        for value, name in panel.frontlight_modes.items():
            frontlight_mode_menu.append(
                self._image_item(
                    name,
                    FRONTLIGHT_MODE_ICONS.get(value, ICON_FRONTLIGHT_MODE),
                    lambda m=int(value): self._run(
                        self.controller.apply, "frontlight_mode", m
                    ),
                )
            )
        custom_item = self._image_item(
            "Custom",
            FRONTLIGHT_MODE_ICONS.get(
                panel.custom_frontlight_mode, ICON_CUSTOM
            ),
        )
        custom_menu = Gtk.Menu()
        custom_item.set_submenu(custom_menu)
        self._temperature_items = self._radio_items(
            custom_menu,
            temperature_presets(panel),
            lambda item, value: self._radio_toggled(item, "temperature", value),
        )
        frontlight_mode_menu.append(custom_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        _, refresh_menu = self._submenu("Refresh", ICON_REFRESH)
        refresh_menu.append(
            self._image_item(
                "Soft refresh",
                ICON_SOFT_REFRESH,
                lambda: self._run(self._refresh_action, False),
            )
        )
        refresh_menu.append(
            self._image_item(
                "Hard refresh",
                ICON_HARD_REFRESH,
                lambda: self._run(self._refresh_action, True),
            )
        )

        self.autorefresh_item = self._image_item(
            "Auto-refresh", ICON_AUTO_REFRESH, self._toggle_autorefresh
        )
        self.menu.append(self.autorefresh_item)
        self.interval_item, interval_menu = self._submenu("Interval", ICON_INTERVAL)
        self._interval_items = self._radio_items(
            interval_menu,
            [(seconds, interval_label(seconds)) for seconds in INTERVAL_CHOICES],
            self._on_interval,
        )

        self.menu.append(Gtk.SeparatorMenuItem())
        self.reload_item = self._image_item(
            "Reload from monitor",
            ICON_RELOAD,
            lambda: self._run(self.controller.read),
        )
        self.menu.append(self.reload_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.quit_item = self._image_item("Quit", ICON_QUIT, self.quit)
        self.menu.append(self.quit_item)

    def _radio_toggled(self, item, field: str, value: int) -> None:
        """Apply the radio choice the user just selected.

        Listening to "toggled" (and not "activate") means a click applies one
        value only: activating the new item deactivates the old one, and only
        the newly active item is applied. Programmatic synchronization in
        refresh_menu() also emits "toggled", hence the `_updating` guard.
        """

        if self._updating or not item.get_active():
            return
        self._run(self.controller.apply, field, value)

    # -- actions -----------------------------------------------------------

    def _toggle_autorefresh(self) -> None:
        self.controller.set_autorefresh(not self.controller.state.autorefresh)
        self.refresh_menu()

    def _on_interval(self, item, seconds: int) -> None:
        if self._updating or not item.get_active():
            return
        self.controller.set_interval(seconds)
        self.refresh_menu()

    def _run(self, work, *args, quiet: bool = False) -> None:
        """Run one serial operation off the GTK main loop.

        While an operation is running the latest request is kept instead of
        dropping it: rapid menu clicks used to disappear, and a user change
        could be silently lost behind a running operation. `quiet` skips the
        "working…" feedback for the periodic external-change poll.
        """

        if self._busy:
            self._pending = (work, args, quiet)
            self.refresh_menu()
            return
        self._busy = True
        if not quiet:
            self.controller.state.message = "working…"
            self.controller.state.severity = SEVERITY_WORKING
            self.controller.state.error_detail = ""
            self.controller.state.error_kind = None
            self.refresh_menu()

        def target():
            try:
                work(*args)
            finally:
                self.GLib.idle_add(self._finished)

        threading.Thread(target=target, daemon=True).start()

    def _finished(self):
        """Back on the main loop: run the queued request or settle the view."""

        self._busy = False
        pending, self._pending = self._pending, None
        if pending is not None:
            self._run(pending[0], *pending[1], quiet=pending[2])
            return False
        self.refresh_menu()
        return False

    def start(self) -> None:
        """Start the auto-refresh timer and restore the saved configuration."""

        self.GLib.timeout_add_seconds(1, self._on_timer)
        # The output probe runs before restore: a tray started with the panel
        # off must not write to the serial, because a command sent to a
        # switched-off panel can wedge the monitor's firmware.
        self._check_display_output()
        self._run(self.controller.restore)

    def _on_timer(self):
        self._check_display_output()
        if not self._busy and self.controller.display_present is not False:
            if self.controller.due():
                self._run(self._refresh_action, False)
            elif self.controller.poll_due():
                self._run(self.controller.sync, quiet=True)
        self._watch_availability()
        self._ghost_tick()
        # No periodic window refresh here: every state change reaches the
        # window through refresh_menu(), and a timer that rewrote the widgets
        # would undo a slider drag or a value being typed.
        return True

    def _check_display_output(self) -> None:
        """Tell the controller whether the e-ink display output is present.

        Runs with the serial poll; the probe returns None when it cannot
        tell (no display, no readable DRM data, no EDID names), which leaves
        the serial-only behaviour.
        """

        now = time.monotonic()
        if (
            self._last_output_poll is not None
            and now - self._last_output_poll < POLL_SECONDS
        ):
            return
        self._last_output_poll = now
        try:
            present = monitor_output_present(
                self.Gdk, self._ghost_output, self.controller.panel.edid_names
            )
        except Exception as exc:  # pragma: no cover - depends on the X server
            self.log.warn(f"display check failed: {exc}")
            return
        self.controller.set_display_present(present)

    def _watch_availability(self) -> None:
        """Reset the estimate when the monitor comes back.

        The panel did a startup refresh while the monitor was off, so the
        areas estimated before the gap say nothing about its current ink.
        The capture re-resolves its monitor too: the X11 backend caches the
        Gdk monitor it resolved on the first grab. When the saved
        configuration was never applied (the panel was off, or the session
        failed), it is restored now.
        """

        available = self.controller.monitor_available
        if self._last_available is False and available is True:
            self.log.info("monitor returned: ghost estimate reset")
            capturer = getattr(self.watcher, "capturer", None)
            reset = getattr(capturer, "reset", None)
            if callable(reset):
                reset()
            self._ghost_reset()
            if getattr(self.controller, "restore_pending", False):
                self._run(self.controller.restore)
        self._last_available = available

    # -- view updates ------------------------------------------------------

    @staticmethod
    def _set_label(widget, text: str) -> None:
        """Write a menu label only when its text changed (dbusmenu churn)."""

        if widget.get_label() != text:
            widget.set_label(text)

    def refresh_menu(self) -> None:
        """Mirror the controller state into the indicator and the menu.

        Values are compared before writing: the menu is exported over D-Bus,
        and redundant set_label()/set_title() calls would make the panel
        relayout for nothing.
        """

        state = self.controller.state
        info = state.info
        panel = self.controller.panel
        mode = (
            panel.display_mode_name(info.mode) if info.mode is not None else "?"
        )
        label = mode if mode != "?" else "e-ink"
        if label != self._indicator_label:
            self.indicator.set_label(label, "")
            self._indicator_label = label
        speed = panel.speed_name(info.speed) if info.speed is not None else None
        title = (
            f"DASUNG {mode} contrast={info.contrast} speed={speed} "
            f"frontlight={panel.frontlight_label(info.frontlight)}"
        )
        if state.message:
            # Surfaces the last result outside the menu: on panels that show
            # indicators only as a tooltip this is the only feedback.
            title += f" — {state.message}"
        if title != self._indicator_title:
            self.indicator.set_title(title)
            self._indicator_title = title
        busy = " [working…]" if self._busy else ""
        status = f"{state.message or mode}{busy}"
        if status != self._status_label:
            self.status_item.set_label(status)
            self._status_label = status
        self._update_menu_status_icon(state.message, state.severity)
        self._update_group_labels(info, state)
        self._updating = True
        try:
            for seconds, item in self._interval_items.items():
                item.set_active(abs(state.interval - seconds) < 0.5)
            self._select(self._contrast_items, info.contrast)
            self._select(self._speed_items, info.speed)
            frontlight = panel.frontlight_level(info.frontlight)
            self._select(
                self._frontlight_items, frontlight[0] if frontlight else None
            )
            self._select(
                self._temperature_items, panel.temperature_level(info.temperature)
            )
        finally:
            self._updating = False
        if self._window is not None:
            self._window.update()

    def _update_menu_status_icon(self, message: str, severity: str = "") -> None:
        """Swap the status icon only when the resolved theme icon changed."""

        candidates = (
            ICON_INFO if not message else status_symbol(message, severity)[0]
        )
        name = icon_name(self.Gtk, candidates)
        if name == self._status_icon_name:
            return
        self._status_icon_name = name
        if name is not None:
            self.status_item.set_image(
                self.Gtk.Image.new_from_icon_name(name, self.Gtk.IconSize.MENU)
            )

    def _update_group_labels(self, info: MonitorInfo, state: TrayState) -> None:
        """Show the current value next to each submenu that has no radio dot."""

        panel = self.controller.panel

        def with_value(label: str, value) -> str:
            return f"{label}: {value}" if value is not None else label

        mode = (
            panel.display_mode_name(info.mode) if info.mode is not None else None
        )
        speed = panel.speed_name(info.speed) if info.speed is not None else None
        frontlight = panel.frontlight_level(info.frontlight)
        frontlight_mode = (
            panel.frontlight_mode_name(info.frontlight_mode)
            if info.frontlight_mode is not None
            else None
        )
        self._set_label(self.mode_item, with_value("Mode", mode))
        self._set_label(self.contrast_item, with_value("Contrast", info.contrast))
        self._set_label(self.speed_item, with_value("Speed", speed))
        self._set_label(
            self.frontlight_item,
            with_value("Frontlight", frontlight[1] if frontlight else None),
        )
        self._set_label(
            self.frontlight_mode_item,
            with_value("Frontlight mode", frontlight_mode),
        )
        self._set_label(
            self.autorefresh_item,
            f"Auto-refresh: {'on' if state.autorefresh else 'off'}",
        )
        self._set_label(
            self.interval_item, f"Interval: {interval_label(state.interval)}"
        )

    @staticmethod
    def _select(items: dict[int, object], value: int | None) -> None:
        """Check the radio item matching `value`, if it is not checked yet."""

        if value is None:
            return
        item = items.get(int(value))
        if item is not None and not item.get_active():
            item.set_active(True)

    # -- window placement ---------------------------------------------------

    def move_to_current_desktop(self) -> bool:
        """Move the tray's windows to the active desktop (Wayland route).

        On X11 `tray_windows` uses the EWMH client message directly; on
        Wayland the compositor decides, so on KDE the shared KWin scripting
        provider performs the move. Best-effort: False when the provider is
        absent or the action could not start.
        """

        provider = getattr(self.watcher, "zones", None)
        move = getattr(provider, "move_to_current_desktop", None)
        if not callable(move):
            return False
        return bool(move())

    # -- controls window ---------------------------------------------------

    def _open_window(self, _item) -> None:
        """Create or raise the Controls window, reading if it is still empty."""

        from .tray_windows import ControlsWindow

        if self._window is None:
            self._window = ControlsWindow(self)
            if self.controller.state.info.mode is None:
                # Nothing read yet: fill the window with real values.
                self._run(self.controller.read)
        self._window.present()
        self._window.update()

    # -- ghost estimate ----------------------------------------------------

    def _open_ghost_window(self, _item) -> None:
        """Create or raise the ghost diagnostic window."""

        from .tray_windows import GhostWindow

        if self._ghost_window is None:
            self._ghost_window = GhostWindow(self)
        self._ghost_window.present()
        self._ghost_window.follow(self.watcher)

    def set_ghost_estimate(self, running: bool) -> None:
        """Stop or restart sampling from the ghost window and remember it.

        Starting is refused while the monitor is missing: the state keeps the
        switch off and the ghost window says why.
        """

        if self.controller.set_ghost_estimate(running):
            if self.watcher is not None:
                self.watcher.set_paused(not running)
        self._refresh_ghost_view()

    def _refresh_action(self, hard: bool) -> bool:
        """Send a refresh and tell the ghost model the panel is clean.

        The reset runs on the main loop: this method runs in a worker thread,
        and the estimator is only touched by the GTK timer.
        """

        ok = self.controller.refresh(hard)
        if ok and self.watcher is not None:
            self.GLib.idle_add(self._ghost_reset)
        return ok

    def _ghost_reset(self):
        if self.watcher is not None:
            self.watcher.reset()
            self.log.info("ghost estimate reset")
            self._clear_note = None
            if (
                self._ghost_window is not None
                and self._ghost_window.window.get_visible()
            ):
                self._ghost_window.follow(self.watcher)
        return False

    def _ghost_tick(self) -> None:
        """Sample the screen when due, clear areas, log the alerts."""

        watcher = self.watcher
        if watcher is None or not watcher.enabled:
            return
        visible = (
            self._ghost_window is not None
            and self._ghost_window.window.get_visible()
        )
        # The diagnostic window is live: never back off while it is open.
        watcher.set_force_base(visible)
        # The window's Stop/Start button: a stopped estimate never samples,
        # so the automatic clearing (which runs after a fresh sample) pauses
        # too while the last result stays on screen. The controller's state
        # is the persisted source, also restored at startup. Sampling also
        # waits for a reachable monitor: with it missing there is nothing to
        # estimate, and the switch was already saved off.
        watcher.set_paused(
            not self.controller.state.ghost_estimate
            or self.controller.monitor_available is not True
        )
        if self._clearer.busy:
            # The overlay is not content: never let a sample see the flash.
            if visible:
                self._ghost_window.follow(watcher)
            return
        if watcher.due():
            watcher.sample()
            self._log_ghost_error(watcher)
            alert = watcher.maybe_alert()
            if alert:
                self.log.info(alert)
            self._maybe_ghost_clear_auto()
        if visible:
            self._ghost_window.follow(watcher)

    def _log_ghost_error(self, watcher) -> None:
        """Write a new ghost error to the log once, not on every tick."""

        error = watcher.error
        if error and error != self._logged_ghost_error:
            self.log.error(f"ghost estimate: {error}")
        self._logged_ghost_error = error

    # -- shutdown ----------------------------------------------------------

    def quit(self, reason: str = "menu") -> None:
        """Stop the main loop once, saving the estimate and closing capture.

        The Quit menu entry and the SIGINT/SIGTERM handlers share this path,
        so an interactive Ctrl+C cleans up exactly like the menu.
        """

        if self._quitting:
            return
        self._quitting = True
        self.log.info(f"exiting ({reason})")
        if self.watcher is not None:
            self.watcher.save_state(force=True)
            close = getattr(self.watcher.capturer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # pragma: no cover - portal teardown
                    self.log.warn(f"cannot close the capture: {exc}")
        self._clearer.close()
        self.Gtk.main_quit()

    # -- zone clearing -----------------------------------------------------

    @property
    def clear_settings(self) -> ClearSettings:
        """Current clearing settings (edited in the ghost window)."""

        return ClearSettings.from_mapping(
            self.controller.state.ghost_clear_settings
        )

    def set_ghost_clear(self, enabled: bool) -> None:
        """Toggle automatic zone clearing from the ghost window."""

        self.controller.set_ghost_clear(enabled)
        if not enabled:
            self._clear_note = "automatic clearing off"
        self._refresh_ghost_view()

    def set_clear_settings(self, values) -> None:
        """Persist clearing settings changed in the ghost window."""

        self.controller.set_ghost_clear_settings(values)

    def _refresh_ghost_view(self) -> None:
        if self._ghost_window is not None:
            self._ghost_window.follow(self.watcher)

    def _clear_phases(self):
        return flash_phases(self.clear_settings)

    def _global_clear_rect(self, element):
        """One estimated element (monitor coordinates) as a global rectangle."""

        watcher = self.watcher
        if watcher is None or watcher.result is None:
            return None
        origin = getattr(watcher.capturer, "source_origin", None)
        if origin is None:
            return None
        result = watcher.result
        x0 = max(0, min(result.source_width, int(element.x)))
        y0 = max(0, min(result.source_height, int(element.y)))
        x1 = max(x0, min(result.source_width, int(element.x + element.width)))
        y1 = max(y0, min(result.source_height, int(element.y + element.height)))
        if x1 <= x0 or y1 <= y0:
            return None
        return (origin[0] + x0, origin[1] + y0, x1 - x0, y1 - y0)

    def _wave_rects(self, elements):
        """Global rectangles of a wave, dropping the ones without an origin."""

        rects = [
            rect
            for rect in (
                self._global_clear_rect(element) for element in elements
            )
            if rect is not None
        ]
        return rects

    def _flash_wave(self, rects) -> bool:
        """Start one synchronized flash over every rectangle, or fail."""

        if not rects:
            return False
        return self._clearer.flash(
            rects, self._clear_phases(), partial(self._ghost_cleared, rects)
        )

    def _ghost_clear_now(self):
        """Flash every estimated ghost area together (the window's button)."""

        watcher = self.watcher
        if watcher is None or watcher.result is None:
            self._clear_note = "no estimate to clear yet"
            self._refresh_ghost_view()
            return False
        if not self._clearer.available:
            self._clear_note = self._clearer.unavailable_note
            self._refresh_ghost_view()
            return False
        if self._clearer.busy:
            return False
        rects = self._wave_rects(watcher.result.elements)
        if not rects:
            self._clear_note = "no ghost areas to clear"
            self._refresh_ghost_view()
            return False
        if self._flash_wave(rects):
            self.log.info(f"clear areas: flashing {len(rects)} area(s)")
            self._clear_note = f"clearing {len(rects)} area(s)…"
        else:
            self._clear_note = self._clearer.unavailable_note
        self._refresh_ghost_view()
        return False

    def _ghost_cleared(self, rects) -> None:
        """The overlays are gone: clean the estimate over every flashed area."""

        watcher = self.watcher
        origin = None
        if watcher is not None:
            origin = getattr(watcher.capturer, "source_origin", None)
        if watcher is not None and origin is not None:
            for rect in rects:
                local = (
                    rect[0] - origin[0],
                    rect[1] - origin[1],
                    rect[2],
                    rect[3],
                )
                watcher.reset_area(*local)
        stamp = time.strftime("%H:%M:%S")
        areas = "area" if len(rects) == 1 else "areas"
        self._clear_note = f"{len(rects)} {areas} cleared at {stamp}"
        self._refresh_ghost_view()

    def _maybe_ghost_clear_auto(self) -> None:
        """Flash every due area together.

        The whole policy is the delay: the areas the estimator reports (the
        rectangles in the preview) whose age reached `clear.delay` are
        flashed in one synchronized wave, even while the screen is in use.
        Younger areas wait for a later wave.
        """

        watcher = self.watcher
        if (
            not self.controller.state.ghost_clear
            or watcher is None
            or watcher.result is None
            or self._clearer.busy
            or not self._clearer.available
        ):
            return
        due = due_clear_elements(
            watcher.result.elements, self.clear_settings
        )
        if not due:
            return
        rects = self._wave_rects(due)
        if self._flash_wave(rects):
            apps = {element.app for element in due if element.app}
            label = ", ".join(sorted(apps)[:2]) or "area"
            self.log.info(
                f"auto-clear: flashing {len(rects)} area(s) ({label})"
            )
            self._clear_note = f"auto-clearing {label}…"
            self._refresh_ghost_view()

    def _ghost_test_flash(self):
        """Flash a fixed square with the current settings (tuning aid)."""

        watcher = self.watcher
        if self._clearer.busy:
            return False
        if not self._clearer.available:
            self._clear_note = self._clearer.unavailable_note
            self._refresh_ghost_view()
            return False
        origin = getattr(
            getattr(watcher, "capturer", None), "source_origin", None
        )
        result = getattr(watcher, "result", None)
        if origin is None or result is None:
            self._clear_note = "no capture to preview on yet"
            self._refresh_ghost_view()
            return False
        size = max(
            64,
            round(
                min(result.source_width, result.source_height)
                * TEST_FLASH_FRACTION
            ),
        )
        x = origin[0] + (result.source_width - size) // 2
        y = origin[1] + (result.source_height - size) // 2
        if self._clearer.flash(
            [(x, y, size, size)], self._clear_phases(), None
        ):
            self.log.info(f"test flash sent ({size}x{size} px)")
            self._clear_note = "test flash sent"
            self._refresh_ghost_view()
        return False


def _install_signal_handlers(gtk, on_signal) -> None:
    """Deliver SIGINT/SIGTERM to the GTK main loop once.

    A terminal Ctrl+C and a session logout otherwise never reach a GTK app:
    Python's default handler cannot run while GLib waits in C, so the first
    call uses GLib's Unix signal source. `on_signal(reason)` runs on the main
    loop; the first delivery disarms both signals.
    """

    GLib = gtk["GLib"]
    signals = ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM"))
    state = {"done": False}

    def handle(reason: str):
        if state["done"]:
            return False
        state["done"] = True
        on_signal(reason)
        return False

    if hasattr(GLib, "unix_signal_add"):
        for signum, reason in signals:
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signum, partial(handle, reason)
            )
        return
    # Fallback for a GLib without unix_signal_add: the Python handler can
    # only schedule the quit on the main loop.
    for signum, reason in signals:
        signal.signal(
            signum,
            lambda _signum, _frame, r=reason: GLib.idle_add(handle, r),
        )


def run_tray(
    *,
    config_path: Path | None = None,
    device: str | None = None,
    timeout: float | None = None,
    interval: float | None = None,
    install_autostart: bool = False,
    install_launcher: bool = False,
    log=None,
) -> int:
    """Start the tray; returns a process exit code."""

    if log is None:
        log = logfile.start(command="tray")
    if install_launcher or install_autostart:
        # The launcher first: the autostart entry prefers it when present.
        if install_launcher:
            target = write_launcher()
            print(f"dasungctl tray: launcher installed: {target}")
            log.info(f"launcher installed: {target}")
        if install_autostart:
            target = write_autostart()
            print(f"dasungctl tray: autostart installed: {target}")
            log.info(f"autostart installed: {target}")
        log.close()
        return 0

    gtk = _load_gtk()
    install_gdk_log_filter(gtk["GLib"])
    install_appindicator_log_filter(gtk["GLib"])
    # An application id of our own: on Wayland GTK derives the windows'
    # resource class from the program name, and the KWin script that moves
    # them between virtual desktops matches it (X11 uses the WM class for
    # the same purpose).
    set_prgname = getattr(gtk["GLib"], "set_prgname", None)
    if callable(set_prgname):
        set_prgname("dasungctl")
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"dasungctl tray: error: {exc}", file=sys.stderr)
        log.error(f"configuration error: {exc}")
        log.close()
        return 1

    ok, _argv = gtk["Gtk"].init_check(sys.argv)
    if not ok:
        message = "no graphical session found (DISPLAY/WAYLAND_DISPLAY)"
        print(f"dasungctl tray: {message}", file=sys.stderr)
        log.error(message)
        log.close()
        return 1

    controller = TrayController(
        config, device=device, timeout=timeout, interval=interval, log=log
    )
    settings = ghost_settings(config)
    log.info(
        f"serial: device {controller.device!r}, timeout {controller.timeout}s"
    )
    log.info(
        f"panel: {controller.panel.name} "
        f"(protocol 0x{controller.panel.protocol:02X}, "
        f"{controller.panel.refresh_hz} Hz)"
    )
    log.info(
        f"auto-refresh: interval {interval_label(controller.state.interval)}, "
        f"waveform {'hard' if controller.timer_hard else 'soft'}"
    )
    if settings["enabled"]:
        log.info(
            f"ghost estimate: output {settings['output']!r}, "
            f"width {settings['width']}, "
            f"interval {settings['interval']}s..{settings['max_interval']}s, "
            f"threshold {settings['threshold']}, auto-clear "
            f"{'on' if settings['clear']['enabled'] else 'off'}"
        )
    else:
        log.info("ghost estimate: off")
    watcher = GhostWatcher(
        enabled=settings["enabled"],
        interval=settings["interval"],
        max_interval=settings["max_interval"],
        threshold=settings["threshold"],
        width=settings["width"],
        zones=open_zones(),
    )
    watcher.load_state()
    if settings["enabled"]:
        try:
            watcher.capturer = open_capture(
                settings["output"], panel=controller.panel
            )
        except Exception as exc:  # pragma: no cover - depends on the session
            watcher.error = f"screen capture unavailable: {exc}"
            watcher.capture_failed = True
    if watcher.error:
        if watcher.capture_failed:
            log.error(f"ghost estimate: {watcher.error}")
        else:
            log.warn(f"ghost estimate: {watcher.error}")
    app = TrayApp(
        controller,
        gtk,
        watcher=watcher,
        log=log,
        ghost_output=settings["output"],
    )
    _install_signal_handlers(gtk, app.quit)
    app.start()
    gtk["Gtk"].main()
    log.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The standalone `dasungctl tray` argument parser."""

    parser = argparse.ArgumentParser(
        prog="dasungctl tray",
        description="Tray icon for Dasung monitor control (GNOME/KDE/Cinnamon).",
    )
    parser.add_argument("--device", default=None)
    parser.add_argument("--timeout", default=None, type=float)
    parser.add_argument("--config", default=None)
    parser.add_argument("--interval", default=None, type=float)
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--install-launcher", action="store_true")
    return parser


def main(argv=None) -> int:
    """Entry point for `python -m dasungctl.tray`: run and return an exit code."""

    args = build_parser().parse_args(argv)
    log = logfile.start(argv, command="tray")
    try:
        return run_tray(
            config_path=Path(args.config) if args.config else None,
            device=args.device,
            timeout=args.timeout,
            interval=args.interval,
            install_autostart=args.install_autostart,
            install_launcher=args.install_launcher,
            log=log,
        )
    except TrayDependencyError as exc:
        print(f"dasungctl tray: {exc}", file=sys.stderr)
        log.error(str(exc))
        return 1
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
