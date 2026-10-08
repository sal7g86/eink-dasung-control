"""The tray's GTK windows: `Controls…` and `Ghost estimate…`.

Split from `tray.py` so the controller, the menu and the startup path stay
readable. The classes are built from the GTK dict of `tray._load_gtk` and
driven by a `TrayApp`; `tray.py` imports this module lazily inside its
open-window methods, so the module-level import from `.tray` below never
forms a cycle.
"""

from __future__ import annotations

import os
import platform
import time

from . import __version__, paths
from .client import MonitorInfo
from .ghostwatch import GhostResult, format_age
from .panels import get_panel
from .tray import (
    CLEAR_TOOLTIPS,
    GHOST_PREVIEW_WIDTH,
    ICON_FALLBACKS,
    ICON_INFO,
    INTERVAL_CHOICES,
    SCALE_APPLY_DELAY_MS,
    SCALE_STEPS,
    TrayApp,
    icon_name,
    icon_path,
    interval_label,
    status_symbol,
)
from .zoneclear import ClearSettings

WINDOW_CSS = """
.dasung-title {
    font-weight: bold;
}
.dasung-subtitle {
    font-size: smaller;
    opacity: 0.65;
}
.dasung-section {
    font-weight: bold;
    opacity: 0.8;
}
.dasung-value {
    font-weight: bold;
}
.dasung-card {
    border: 1px solid alpha(@theme_fg_color, 0.15);
    border-radius: 10px;
}
.dasung-error {
    color: #e01b24;
}
.dasung-ok {
    color: #2ec27e;
}
/* About window: a centered hero over flat sections with hairline
   separators, so the content reads as a page instead of boxes. */
.dasung-about-name {
    font-size: 1.7em;
    font-weight: bold;
}
.dasung-about-version {
    background-color: alpha(@theme_fg_color, 0.08);
    border-radius: 20px;
    padding: 2px 10px;
    font-size: 0.85em;
}
.dasung-about-title {
    font-size: 0.85em;
    font-weight: bold;
    opacity: 0.55;
}
.dasung-about-row {
    padding: 3px 0;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.10);
}
.dasung-about-label {
    opacity: 0.75;
}
.dasung-about-path {
    font-family: monospace;
    font-size: 0.9em;
}
.dasung-about-link {
    background-color: alpha(@theme_fg_color, 0.07);
    border-radius: 20px;
    padding: 6px 14px;
}
.dasung-about-link:hover {
    background-color: alpha(@theme_fg_color, 0.12);
}
"""


def _round_to_step(value: int | None, step: int) -> int | None:
    """Snap a raw monitor value to the slider's step grid."""

    if value is None:
        return None
    return int(round(int(value) / step) * step)


def _value_text(name: str, value: int, panel=None) -> str:
    """Slider label for one field: speed and frontlight use named scales."""

    profile = get_panel(panel)
    if name == "speed":
        return profile.speed_name(value)
    if name == "frontlight":
        return profile.frontlight_label(value) or "–"
    return str(int(value))


def install_window_css(Gtk, Gdk) -> None:
    """Attach the shared window stylesheet to the default screen."""

    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(WINDOW_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _x11_desktop_message(xid, value) -> bool:
    """Send the EWMH `_NET_WM_DESKTOP` client message to the window manager.

    Cinnamon/Muffin acts on the client message but ignores the equivalent
    property change (a property written by the client is only echoed back by
    `wmctrl`, so it looks set while nothing moves), which is why the
    move-to-current request goes through here. Best-effort: a missing
    python-xlib or an unreachable display simply leaves the window where it
    is.
    """

    try:
        from Xlib import X, display as xdisplay, error as xerror, protocol
    except ImportError:
        return False
    try:
        connection = xdisplay.Display()
    except (xerror.DisplayError, OSError):
        return False
    try:
        event = protocol.event.ClientMessage(
            window=connection.create_resource_object("window", xid),
            client_type=connection.intern_atom("_NET_WM_DESKTOP"),
            data=(32, [value, 0, 0, 0, 0]),
        )
        connection.screen().root.send_event(
            event,
            event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
        )
        connection.sync()
    finally:
        connection.close()
    return True


def _x11_current_desktop() -> int | None:
    """The active EWMH desktop, or None when it cannot be read."""

    try:
        from Xlib import Xatom, display as xdisplay, error as xerror
    except ImportError:
        return None
    try:
        connection = xdisplay.Display()
    except (xerror.DisplayError, OSError):
        return None
    try:
        prop = connection.screen().root.get_full_property(
            connection.intern_atom("_NET_CURRENT_DESKTOP"), Xatom.CARDINAL
        )
        if prop is None or not prop.value:
            return None
        return int(prop.value[0])
    finally:
        connection.close()


def _bring_to_current_desktop(window, app=None) -> bool:
    """Move an already-open window to the desktop in use.

    Windows stay on the workspace where they were opened: only choosing one
    again from the tray menu moves it here. On X11 the EWMH client message
    is used (Cinnamon/Muffin acts on it but ignores the equivalent property
    change); Wayland gives applications no API for this, so on KDE the tray
    asks its KWin scripting provider for the same move. Best-effort.
    """

    gdk_window = window.get_window()
    get_xid = getattr(gdk_window, "get_xid", None)
    if get_xid is not None:
        current = _x11_current_desktop()
        if current is None:
            return False
        return _x11_desktop_message(get_xid(), current)
    move = getattr(app, "move_to_current_desktop", None)
    if callable(move):
        return bool(move())
    return False


class ControlsWindow:
    """GTK window with the monitor's adjustable fields."""

    def __init__(self, app: TrayApp) -> None:
        """Build the window; widgets are created once and reused."""

        Gtk = app.Gtk
        self.app = app
        self.panel = app.controller.panel
        self.Gtk = Gtk
        self.Gdk = app.Gdk
        self._updating = False
        self._scale_sources: dict[str, int] = {}
        self._value_labels: dict[str, object] = {}
        # Last icon name written to the status bar ("" never matches so the
        # first update also hides the empty image left by show_all()).
        self._window_icon_name = ""
        # show_all() runs once: it is recursive and would re-show the spinner
        # and the status icon that update() deliberately keeps hidden.
        self._mapped_once = False
        self._install_css()

        window = Gtk.Window(title="DASUNG monitor control")
        window.set_default_size(430, 620)
        window.connect("delete-event", self._on_delete)
        self.window = window

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        refresh = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON
        )
        refresh.set_tooltip_text("Soft refresh")
        refresh.connect("clicked", lambda _button: self._soft_refresh())
        header.pack_start(refresh)
        title = Gtk.Label(label="DASUNG", xalign=0)
        title.get_style_context().add_class("dasung-title")
        self.subtitle = Gtk.Label(label="reading the monitor…", xalign=0)
        self.subtitle.get_style_context().add_class("dasung-subtitle")
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(self.subtitle, False, False, 0)
        header.set_custom_title(title_box)
        window.set_titlebar(header)
        # GTK clears the window title when a custom titlebar is set (3.24):
        # put it back for the WM, the window list and Alt+Tab.
        window.set_title("DASUNG monitor control")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_border_width(12)
        window.add(content)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        content.pack_start(scrolled, True, True, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_end(4)
        scrolled.add(body)

        self.mode = Gtk.ComboBoxText()
        for value, name in self.panel.modes.items():
            self.mode.append(str(int(value)), name)
        self.mode.connect("changed", self._on_mode)

        contrast_low, contrast_high = self.panel.limits["contrast"]
        self.contrast = self._scale(contrast_low, contrast_high, 1)
        for value in range(contrast_low, contrast_high + 1):
            self.contrast.add_mark(value, Gtk.PositionType.BOTTOM, None)
        self._connect_scale(self.contrast, "contrast")

        speed_low, speed_high = self.panel.limits["speed"]
        self.speed = self._scale(speed_low, speed_high, 1)
        for value in range(speed_low, speed_high + 1):
            self.speed.add_mark(value, Gtk.PositionType.BOTTOM, None)
        self._connect_scale(self.speed, "speed")

        levels = [value for value, _label in self.panel.frontlight_levels]
        self.frontlight = self._scale(
            min(levels), max(levels), self.panel.frontlight_step
        )
        for value, _label in self.panel.frontlight_levels:
            self.frontlight.add_mark(value, Gtk.PositionType.BOTTOM, None)
        self.frontlight.set_tooltip_text(
            "Brightness level; choose a frontlight mode other than Off first"
        )
        self._connect_scale(self.frontlight, "frontlight")

        level_count = len(self.panel.temperature_levels)
        self.temperature = self._scale(1, level_count, 1)
        for value in range(1, level_count + 1):
            self.temperature.add_mark(value, Gtk.PositionType.BOTTOM, None)
        self.temperature.set_tooltip_text(
            "Temperature level 1 (cold) .. 10 (warm); "
            "moving it switches the frontlight to Custom"
        )
        self._connect_scale(self.temperature, "temperature")

        self.frontlight_mode = Gtk.ComboBoxText()
        for value, name in self.panel.frontlight_modes.items():
            self.frontlight_mode.append(str(int(value)), name)
        self.frontlight_mode.append(
            str(self.panel.custom_frontlight_mode), "Custom"
        )
        self.frontlight_mode.connect("changed", self._on_frontlight_mode)

        self.autorefresh = Gtk.Switch()
        self.autorefresh.set_halign(Gtk.Align.START)
        self.autorefresh.connect("notify::active", self._on_autorefresh)
        self.interval = Gtk.ComboBoxText()
        for seconds in INTERVAL_CHOICES:
            self.interval.append(str(seconds), interval_label(seconds))
        self.interval.connect("changed", self._on_interval)

        for name in (*SCALE_STEPS, "temperature"):
            label = Gtk.Label(label="–", xalign=1)
            # Speed shows the official names ("Fast++++" is the longest).
            label.set_width_chars(9 if name == "speed" else 4)
            label.get_style_context().add_class("dasung-value")
            self._value_labels[name] = label

        for title_text, rows in (
            (
                "Image",
                (
                    self._row("Mode", self.mode),
                    self._slider_row("Contrast", "contrast"),
                    self._slider_row("Speed", "speed"),
                ),
            ),
            (
                "Light",
                (
                    self._row("Frontlight mode", self.frontlight_mode),
                    self._slider_row("Frontlight", "frontlight"),
                    self._temperature_row(),
                ),
            ),
            (
                "Automation",
                (
                    self._row("Auto-refresh", self.autorefresh),
                    self._row("Interval", self.interval),
                ),
            ),
            ("Actions", (self._action_buttons(),)),
        ):
            body.pack_start(self._card(title_text, rows), False, False, 0)

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.status_icon = Gtk.Image()
        status_box.pack_start(self.status_icon, False, False, 0)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_line_wrap(True)
        status_box.pack_start(self.status, True, True, 0)
        content.pack_start(status_box, False, False, 0)

    # -- widget helpers ----------------------------------------------------

    def _install_css(self) -> None:
        """Attach the window stylesheet to the default screen."""

        install_window_css(self.Gtk, self.Gdk)

    def _scale(self, low, high, step):
        """Create a horizontal slider without its built-in value label."""

        widget = self.Gtk.Scale.new_with_range(
            self.Gtk.Orientation.HORIZONTAL, low, high, step
        )
        widget.set_draw_value(False)
        widget.set_hexpand(True)
        return widget

    def _connect_scale(self, widget, name: str) -> None:
        """Route a slider's changes to the debounced apply for `name`."""

        widget.connect("value-changed", self._on_scale_changed, name)

    def _card(self, title: str, rows):
        """Wrap a titled group of rows in the rounded card frame."""

        box = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(12)
        heading = self.Gtk.Label(label=title, xalign=0)
        heading.get_style_context().add_class("dasung-section")
        box.pack_start(heading, False, False, 0)
        for row in rows:
            box.pack_start(row, False, False, 0)
        frame = self.Gtk.Frame()
        frame.get_style_context().add_class("dasung-card")
        frame.add(box)
        return frame

    def _row(self, label: str, widget):
        """Label + widget row; the label column is fixed across cards."""

        box = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=10)
        text = self.Gtk.Label(label=label, xalign=0)
        text.set_size_request(120, -1)
        box.pack_start(text, False, False, 0)
        box.pack_start(widget, True, True, 0)
        return box

    def _slider_row(self, label: str, name: str):
        box = self._row(label, getattr(self, name))
        box.pack_end(self._value_labels[name], False, False, 0)
        return box

    def _temperature_row(self):
        """Slider row for the manual temperature, hidden outside Custom mode.

        show_all() runs once on the first present() and is recursive: the
        no_show_all flag keeps the row hidden until update() shows it.
        """

        self.temperature_row = self._slider_row("Temperature", "temperature")
        self.temperature_row.set_no_show_all(True)
        self.temperature_row.hide()
        return self.temperature_row

    def _action_buttons(self):
        Gtk = self.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.get_style_context().add_class("linked")
        for label, icon, tooltip, handler in (
            ("Reload", "document-open-recent-symbolic",
             "Reload from monitor", self._reload),
            ("Soft refresh", "view-refresh-symbolic",
             "Official clients' soft refresh", self._soft_refresh),
            ("Hard refresh", "emblem-synchronizing-symbolic",
             "Official clients' hard refresh", self._hard_refresh),
        ):
            box.pack_start(
                self._action_button(label, icon, tooltip, handler),
                True, True, 0,
            )
        return box

    def _action_button(self, label, icon_name, tooltip, handler):
        Gtk = self.Gtk
        button = Gtk.Button()
        button.set_tooltip_text(tooltip)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON),
            False, False, 0,
        )
        inner.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(inner)
        button.connect("clicked", lambda _button: handler())
        return button

    # -- view updates ------------------------------------------------------

    def present(self) -> None:
        """First show reveals every widget; later calls raise the window.

        show_all() is recursive and re-shows hidden children, so it runs only
        once: after that, present() reopens the hidden window while keeping
        the spinner and the status icon in the state update() computed, and
        brings it back to the workspace in use when needed.
        """

        if not self._mapped_once:
            self.window.show_all()
            self._mapped_once = True
            self.spinner.hide()
            self.status_icon.hide()
        else:
            _bring_to_current_desktop(self.window, self.app)
        self.window.present()

    def update(self) -> None:
        """Mirror the controller state into the window widgets.

        Every write is guarded: Gtk.Scale.set_value() and the combo boxes
        emit change signals and redraw even when the value is the same, and
        refresh_menu() calls this on every state change.
        """

        if not self.window.get_visible():
            # _open_window() calls update() after present(); while the window
            # is hidden the widgets cannot be seen, so redrawing them on every
            # state change is wasted work.
            return
        state = self.app.controller.state
        info = state.info
        self._updating = True
        try:
            self._sync_busy()
            mode_id = None if info.mode is None else str(int(info.mode))
            if mode_id is not None and self.mode.get_active_id() != mode_id:
                self.mode.set_active_id(mode_id)
            for name in SCALE_STEPS:
                value = getattr(info, name)
                if value is None:
                    self._set_text(self._value_labels[name], "–")
                    continue
                rounded = _round_to_step(value, SCALE_STEPS[name])
                if name == "frontlight":
                    # Older saved values (125/190/255) exceed the panel's
                    # real maximum: clamp before showing them.
                    rounded = min(self.panel.frontlight_max, rounded)
                scale = getattr(self, name)
                if int(round(scale.get_value())) != rounded:
                    scale.set_value(rounded)
                self._set_text(
                    self._value_labels[name],
                    _value_text(name, rounded, self.panel),
                )
            level = self.panel.temperature_level_number(info.temperature)
            if level is None:
                self._set_text(self._value_labels["temperature"], "–")
            else:
                if int(round(self.temperature.get_value())) != level:
                    self.temperature.set_value(level)
                self._set_text(
                    self._value_labels["temperature"],
                    _value_text("temperature", level, self.panel),
                )
            if info.frontlight_mode is not None:
                frontlight_id = str(int(info.frontlight_mode))
                if self.frontlight_mode.get_active_id() != frontlight_id:
                    self.frontlight_mode.set_active_id(frontlight_id)
            if self.autorefresh.get_active() != state.autorefresh:
                self.autorefresh.set_active(state.autorefresh)
            interval_id = str(int(state.interval))
            if self.interval.get_active_id() != interval_id:
                self.interval.set_active_id(interval_id)
            self._sync_sensitivity(info)
            self._set_text(self.subtitle, self._summary(info))
            self._set_status(
                state.message, info, state.severity, state.error_detail
            )
        finally:
            self._updating = False

    @staticmethod
    def _set_text(label, text: str) -> None:
        """Write a label only when its text changed (avoids redraws)."""

        if label.get_text() != text:
            label.set_text(text)

    def _sync_busy(self) -> None:
        """Show the spinner only while a serial operation is running."""

        if self.app._busy:
            if not self.spinner.get_visible():
                self.spinner.show()
                self.spinner.start()
        elif self.spinner.get_visible():
            self.spinner.stop()
            self.spinner.hide()

    def _sync_sensitivity(self, info: MonitorInfo) -> None:
        mode = info.frontlight_mode
        # The custom value is written back as `mixed` by the firmware, so both
        # modes show the manual temperature control.
        show_temperature = mode is None or int(mode) in (
            self.panel.mixed_frontlight_mode,
            self.panel.custom_frontlight_mode,
        )
        if self.temperature_row.get_visible() != show_temperature:
            if show_temperature:
                # show_all() skips a no_show_all widget and its subtree, so
                # clear the flag for the explicit reveal.
                self.temperature_row.set_no_show_all(False)
                self.temperature_row.show_all()
            else:
                self.temperature_row.hide()
                self.temperature_row.set_no_show_all(True)
        self.frontlight.set_sensitive(
            mode is None or int(mode) != self.panel.off_frontlight_mode
        )

    def _summary(self, info: MonitorInfo) -> str:
        if info.mode is None:
            return "waiting for a monitor read"
        parts = [f"mode {self.panel.display_mode_name(info.mode)}"]
        if info.contrast is not None:
            parts.append(f"contrast {info.contrast}")
        if info.speed is not None:
            parts.append(f"speed {self.panel.speed_name(info.speed)}")
        if info.frontlight is not None:
            parts.append(f"frontlight {self.panel.frontlight_label(info.frontlight)}")
        return " · ".join(parts)

    def _set_status(
        self, message: str, info: MonitorInfo, severity: str = "", detail: str = ""
    ) -> None:
        """Update the status line, its icon and its tooltip when they changed."""

        text = message
        icon = None
        css = ""
        if not text:
            if info.mode is None:
                text = "Read the monitor to fill the controls"
                icon = icon_name(self.Gtk, ICON_INFO)
            else:
                text = "Ready"
        else:
            candidates, css = status_symbol(message, severity)
            icon = icon_name(self.Gtk, candidates)
        self._set_text(self.status, text)
        # The short line sits in the window; the exact error stays here.
        self.status.set_tooltip_text(detail or None)
        for target in (self.status, self.status_icon):
            context = target.get_style_context()
            for css_class in ("dasung-error", "dasung-ok"):
                wanted = css_class == css
                if context.has_class(css_class) != wanted:
                    if wanted:
                        context.add_class(css_class)
                    else:
                        context.remove_class(css_class)
        if icon != self._window_icon_name:
            self._window_icon_name = icon
            if icon is None:
                self.status_icon.hide()
            else:
                self.status_icon.set_from_icon_name(icon, self.Gtk.IconSize.MENU)
                self.status_icon.show()

    # -- actions -----------------------------------------------------------

    def _on_delete(self, *_args):
        """Hide the window instead of destroying it: reopening is instant."""

        self.window.hide()
        return True

    def _reload(self) -> None:
        self.app._run(self.app.controller.read)

    def _soft_refresh(self) -> None:
        self.app._run(self.app._refresh_action, False)

    def _hard_refresh(self) -> None:
        self.app._run(self.app._refresh_action, True)

    def _apply(self, name: str, value: int) -> None:
        self.app._run(self.app.controller.apply, name, value)

    def _on_mode(self, combo) -> None:
        """Apply the selected mode, ignoring the programmatic sync in update()."""

        if not self._updating and combo.get_active_id() is not None:
            self._apply("mode", int(combo.get_active_id()))

    def _on_frontlight_mode(self, combo) -> None:
        """Apply the selected frontlight preset (programmatic syncs ignored)."""

        if not self._updating and combo.get_active_id() is not None:
            self._apply("frontlight_mode", int(combo.get_active_id()))

    def _on_autorefresh(self, switch, _spec) -> None:
        """Apply the switch state; no serial traffic is involved."""

        if self._updating:
            return
        self.app.controller.set_autorefresh(switch.get_active())
        self.app.refresh_menu()

    def _on_interval(self, combo) -> None:
        """Apply the selected interval; no serial traffic is involved."""

        if self._updating or combo.get_active_id() is None:
            return
        self.app.controller.set_interval(float(combo.get_active_id()))
        self.app.refresh_menu()

    def _on_scale_changed(self, scale, name: str) -> None:
        """Apply a slider change after the user stops moving it.

        "value-changed" also fires for the wheel and the keyboard, which
        "button-release-event" missed; the short delay turns a drag into a
        single write and collapses repeated wheel steps.
        """

        self._value_labels[name].set_text(
            _value_text(name, int(round(scale.get_value())))
        )
        if self._updating:
            return
        previous = self._scale_sources.pop(name, None)
        if previous is not None:
            self.app.GLib.source_remove(previous)
        self._scale_sources[name] = self.app.GLib.timeout_add(
            SCALE_APPLY_DELAY_MS, self._apply_scale, name
        )

    def _apply_scale(self, name: str) -> bool:
        self._scale_sources.pop(name, None)
        if not self._updating:
            value = int(round(getattr(self, name).get_value()))
            if name == "temperature":
                # The slider shows the menu's 1..10 levels; the firmware
                # wants the 0..100 byte.
                value = self.panel.temperature_byte(value)
            self._apply(name, value)
        return False


class GhostWindow:
    """Diagnostic window with the estimated ghost map and its areas.

    The preview is the model's ghost-only rendering: dark shapes on a neutral
    background, with the estimated areas outlined. It is deliberately not a
    copy of the screen content: the point is to compare where the estimator
    thinks the ghosts are with the physical e-ink panel.
    """

    def __init__(self, app: TrayApp) -> None:
        """Build the diagnostic window and its toolbar."""

        Gtk = app.Gtk
        self.app = app
        self.Gtk = Gtk
        self._mapped_once = False
        self._signature = None
        # Model identity and version of the preview on screen.
        self._preview_key = None
        # Guards the switch's notify::active while the view mirrors the state.
        self._clear_updating = False
        # Pending debounce of the clearing-settings editor.
        self._clear_apply_source = None
        install_window_css(app.Gtk, app.Gdk)

        window = Gtk.Window(title="DASUNG ghost estimate")
        window.set_default_size(380, 760)
        window.connect("delete-event", self._on_delete)
        self.window = window

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title = Gtk.Label(label="Ghost estimate", xalign=0)
        title.get_style_context().add_class("dasung-title")
        subtitle = Gtk.Label(label="model estimate, not a measurement", xalign=0)
        subtitle.get_style_context().add_class("dasung-subtitle")
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        header.set_custom_title(title_box)
        self.estimate_button = self._estimate_button()
        header.pack_end(self.estimate_button)
        window.set_titlebar(header)
        # GTK clears the window title when a custom titlebar is set (3.24):
        # put it back for the WM, the window list and Alt+Tab.
        window.set_title("DASUNG ghost estimate")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_border_width(12)
        window.add(content)

        self.summary = Gtk.Label(label="waiting for the first sample…", xalign=0)
        self.summary.get_style_context().add_class("dasung-title")
        self.summary.set_line_wrap(True)
        content.pack_start(self.summary, False, False, 0)
        self.detail = Gtk.Label(label="", xalign=0)
        self.detail.get_style_context().add_class("dasung-subtitle")
        self.detail.set_line_wrap(True)
        content.pack_start(self.detail, False, False, 0)

        frame = Gtk.Frame()
        frame.get_style_context().add_class("dasung-card")
        self.preview = Gtk.Image()
        self.preview.set_halign(Gtk.Align.CENTER)
        self.preview.set_tooltip_text(
            "Ghost-only map: dark residue in grey, light residue in amber; "
            "the boxes outline the estimated areas"
        )
        frame.add(self.preview)
        content.pack_start(frame, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        self.elements = Gtk.Label(label="", xalign=0)
        self.elements.set_line_wrap(True)
        self.elements.set_selectable(True)
        scrolled.add(self.elements)
        content.pack_start(scrolled, True, True, 0)

        clear_card = Gtk.Frame()
        clear_card.get_style_context().add_class("dasung-card")
        clear_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        clear_box.set_border_width(8)
        clear_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear_label = Gtk.Label(label="Automatic clearing", xalign=0)
        self.clear_switch = Gtk.Switch()
        self.clear_switch.set_valign(Gtk.Align.CENTER)
        self.clear_switch.connect("notify::active", self._on_clear_toggled)
        clear_head.pack_start(clear_label, True, True, 0)
        clear_head.pack_start(self.clear_switch, False, False, 0)
        clear_box.pack_start(clear_head, False, False, 0)
        self.clear_expander = Gtk.Expander(label="Clearing settings")
        self.clear_expander.set_expanded(True)
        self.clear_expander.add(self._build_clear_grid())
        clear_box.pack_start(self.clear_expander, False, False, 0)
        clear_card.add(clear_box)
        content.pack_start(clear_card, False, False, 0)
        self._load_clear_settings()

        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_line_wrap(True)
        content.pack_start(self.status, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        buttons.get_style_context().add_class("linked")
        clear = self._button(
            "Clear areas",
            icon_name(
                self.Gtk, ("edit-clear-all-symbolic", "edit-clear-symbolic")
            )
            or "edit-clear-symbolic",
            "Flash every estimated area once, worst first",
        )
        clear.connect("clicked", lambda _button: self.app._ghost_clear_now())
        self.clear_button = clear
        reset = self._button(
            "Reset estimate",
            "edit-clear-symbolic",
            "The panel was refreshed: clear the estimate and re-arm the notification",
        )
        reset.connect("clicked", lambda _button: self.app._ghost_reset())
        retry = self._button(
            "Retry capture",
            "view-refresh-symbolic",
            "Ask again for the screen share permission",
        )
        retry.connect("clicked", lambda _button: self._retry())
        buttons.pack_start(clear, True, True, 0)
        buttons.pack_start(reset, True, True, 0)
        buttons.pack_start(retry, True, True, 0)
        content.pack_start(buttons, False, False, 0)

    def _button(self, label, icon_name, tooltip):
        Gtk = self.Gtk
        button = Gtk.Button()
        button.set_tooltip_text(tooltip)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.pack_start(
            Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        inner.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(inner)
        return button

    def _estimate_button(self):
        """Header button that stops and restarts the estimate's sampling."""

        Gtk = self.Gtk
        button = Gtk.Button()
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._estimate_icon_name = "media-playback-pause-symbolic"
        self.estimate_icon = Gtk.Image.new_from_icon_name(
            self._estimate_icon_name, Gtk.IconSize.BUTTON
        )
        self.estimate_label = Gtk.Label(label="Stop estimate")
        inner.pack_start(self.estimate_icon, False, False, 0)
        inner.pack_start(self.estimate_label, False, False, 0)
        button.add(inner)
        button.connect("clicked", self._on_estimate_clicked)
        return button

    def _on_estimate_clicked(self, _button) -> None:
        """Flip the running state and let the tray persist the choice."""

        running = bool(self.app.controller.state.ghost_estimate)
        self.app.set_ghost_estimate(not running)

    def _sync_estimate_controls(self, watcher) -> None:
        """Mirror the running state into the header button and its tooltip."""

        available = watcher is not None and bool(watcher.enabled)
        running = bool(self.app.controller.state.ghost_estimate)
        monitor_ok = self.app.controller.monitor_available is True
        if not available:
            label = "Start estimate"
            icon = "media-playback-start-symbolic"
            tooltip = (
                "Disabled in the configuration"
                if watcher is not None
                else "The ghost estimate is unavailable"
            )
        elif not monitor_ok:
            label = "Start estimate"
            icon = "media-playback-start-symbolic"
            tooltip = "The monitor is unavailable; the estimate cannot start"
        elif running:
            label = "Stop estimate"
            icon = "media-playback-pause-symbolic"
            tooltip = (
                "Stop sampling; the last result stays on screen and the "
                "automatic clearing pauses too"
            )
        else:
            label = "Start estimate"
            icon = "media-playback-start-symbolic"
            tooltip = "Resume sampling and the automatic clearing"
        self._set(self.estimate_label, label)
        if self._estimate_icon_name != icon:
            self._estimate_icon_name = icon
            self.estimate_icon.set_from_icon_name(icon, self.Gtk.IconSize.BUTTON)
        self.estimate_button.set_sensitive(available and (running or monitor_ok))
        self.estimate_button.set_tooltip_text(tooltip)

    # -- clearing settings editor -----------------------------------------

    def _build_clear_grid(self):
        """Editor for the flash colors, timings and selection thresholds."""

        Gtk = self.Gtk
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_border_width(4)
        self._clear_widgets = {}
        self._level_widgets = {"white": [], "black": [], "grey": []}
        row = 0

        style = Gtk.ComboBoxText()
        for value, text in (
            ("white-black", "White–black"),
            ("single", "Single grey"),
        ):
            style.append(value, text)
        style.set_tooltip_text(CLEAR_TOOLTIPS["style"])
        style.connect("changed", lambda _widget: self._queue_clear_apply())
        self._clear_widgets["style"] = style
        grid.attach(Gtk.Label(label="Style", xalign=0), 0, row, 1, 1)
        grid.attach(style, 1, row, 3, 1)
        row += 1

        for key, text in (
            ("white", "White"),
            ("black", "Black"),
            ("grey", "Grey"),
        ):
            level = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 255, 1
            )
            level.set_draw_value(True)
            level.set_hexpand(True)
            level.set_tooltip_text(CLEAR_TOOLTIPS[f"{key}_level"])
            ms = Gtk.SpinButton.new_with_range(1, 2000, 1)
            ms.set_tooltip_text(CLEAR_TOOLTIPS[f"{key}_ms"])
            ms_label = Gtk.Label(label="ms", xalign=0)
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(18, 14)
            swatch.set_valign(Gtk.Align.CENTER)
            swatch.connect("draw", self._draw_swatch)
            level.connect("value-changed", self._on_level_changed, key)
            ms.connect("value-changed", lambda _widget: self._queue_clear_apply())
            self._clear_widgets[f"{key}_level"] = level
            self._clear_widgets[f"{key}_ms"] = ms
            self._clear_widgets[f"{key}_swatch"] = swatch
            self._level_widgets[key] = [level, ms, ms_label, swatch]
            grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            grid.attach(level, 1, row, 1, 1)
            grid.attach(ms, 2, row, 1, 1)
            grid.attach(ms_label, 3, row, 1, 1)
            grid.attach(swatch, 4, row, 1, 1)
            row += 1

        delay = Gtk.SpinButton.new_with_range(1, 600, 1)
        delay.set_hexpand(True)
        delay.set_tooltip_text(CLEAR_TOOLTIPS["delay"])
        delay.connect("value-changed", lambda _widget: self._queue_clear_apply())
        self._clear_widgets["delay"] = delay
        grid.attach(Gtk.Label(label="Clean after (s)", xalign=0), 0, row, 1, 1)
        grid.attach(delay, 1, row, 3, 1)
        row += 1

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        actions.get_style_context().add_class("linked")
        defaults = self._button(
            "Defaults",
            "edit-undo-symbolic",
            "Restore the built-in flash levels, timings and thresholds",
        )
        defaults.connect("clicked", lambda _button: self._restore_clear_defaults())
        test = self._button(
            "Test flash",
            "media-playback-start-symbolic",
            "Flash a large square (half the panel's smaller side) at the "
            "center of the monitor with these settings",
        )
        test.connect("clicked", lambda _button: self.app._ghost_test_flash())
        self.test_flash_button = test
        actions.pack_start(defaults, True, True, 0)
        actions.pack_start(test, True, True, 0)
        grid.attach(actions, 0, row, 5, 1)
        return grid

    def _on_level_changed(self, scale, key) -> None:
        swatch = self._clear_widgets[f"{key}_swatch"]
        swatch._level = int(scale.get_value())
        swatch.queue_draw()
        self._queue_clear_apply()

    def _draw_swatch(self, widget, cr):
        shade = getattr(widget, "_level", 0) / 255.0
        cr.set_source_rgb(shade, shade, shade)
        cr.paint()
        return False

    def _update_style_sensitivity(self) -> None:
        widgets = getattr(self, "_clear_widgets", None)
        if not widgets or widgets.get("style") is None:
            return
        style = widgets["style"].get_active_id()
        for key in ("white", "black"):
            for widget in self._level_widgets[key]:
                widget.set_sensitive(style == "white-black")
        for widget in self._level_widgets["grey"]:
            widget.set_sensitive(style == "single")

    def _queue_clear_apply(self) -> None:
        """Debounce editor changes into one saved update."""

        self._update_style_sensitivity()
        if self._clear_updating:
            return
        if self._clear_apply_source is not None:
            self.app.GLib.source_remove(self._clear_apply_source)
        self._clear_apply_source = self.app.GLib.timeout_add(
            SCALE_APPLY_DELAY_MS, self._apply_clear_settings
        )

    def _apply_clear_settings(self):
        self._clear_apply_source = None
        if not self._clear_updating:
            self.app.set_clear_settings(self._collect_clear_values())
        return False

    def _collect_clear_values(self) -> dict:
        widgets = self._clear_widgets
        return {
            "style": widgets["style"].get_active_id(),
            "white_level": int(widgets["white_level"].get_value()),
            "white_ms": widgets["white_ms"].get_value_as_int(),
            "black_level": int(widgets["black_level"].get_value()),
            "black_ms": widgets["black_ms"].get_value_as_int(),
            "grey_level": int(widgets["grey_level"].get_value()),
            "grey_ms": widgets["grey_ms"].get_value_as_int(),
            "delay": float(widgets["delay"].get_value_as_int()),
        }

    def _load_clear_settings(self, settings=None) -> None:
        settings = settings or self.app.clear_settings
        widgets = self._clear_widgets
        self._clear_updating = True
        try:
            widgets["style"].set_active_id(settings.style)
            for key in ("white", "black", "grey"):
                level = widgets[f"{key}_level"]
                level.set_value(getattr(settings, f"{key}_level"))
                widgets[f"{key}_ms"].set_value(getattr(settings, f"{key}_ms"))
                swatch = widgets[f"{key}_swatch"]
                swatch._level = int(level.get_value())
                swatch.queue_draw()
            widgets["delay"].set_value(settings.delay)
        finally:
            self._clear_updating = False
        self._update_style_sensitivity()

    def _restore_clear_defaults(self) -> None:
        self._load_clear_settings(ClearSettings())
        if self._clear_apply_source is not None:
            self.app.GLib.source_remove(self._clear_apply_source)
            self._clear_apply_source = None
        self._apply_clear_settings()

    def present(self) -> None:
        """First show reveals every widget; later calls raise the window."""

        if not self._mapped_once:
            self.window.show_all()
            self._mapped_once = True
        else:
            _bring_to_current_desktop(self.window, self.app)
        self.window.present()

    def _on_delete(self, *_args):
        """Hide the window instead of destroying it: reopening is instant."""

        self.window.hide()
        return True

    def _retry(self) -> None:
        watcher = self.app.watcher
        if watcher is not None:
            watcher.retry()
            self.app.log.info("capture retried")
            self.follow(watcher)

    def _on_clear_toggled(self, switch, _param) -> None:
        if self._clear_updating:
            return
        self.app.set_ghost_clear(switch.get_active())

    def _sync_clear_controls(self, result) -> None:
        """Mirror the clearing switch and enable the manual buttons."""

        app = self.app
        clearer = getattr(app, "_clearer", None)
        available = clearer is not None and clearer.available
        busy = clearer is not None and clearer.busy
        self._clear_updating = True
        self.clear_switch.set_active(bool(app.controller.state.ghost_clear))
        self._clear_updating = False
        self.clear_switch.set_sensitive(available)
        if available:
            note = (
                "Flash the worst estimated area automatically once the "
                "screen is quiet. On by default; the choice is remembered."
            )
        else:
            note = getattr(clearer, "unavailable_note", None) or (
                "Automatic clearing is not available in this session"
            )
        self.clear_switch.set_tooltip_text(note)
        self.clear_button.set_sensitive(
            available and not busy and bool(result is not None and result.elements)
        )
        self.test_flash_button.set_sensitive(available and not busy)

    def follow(self, watcher) -> None:
        """Mirror the watcher state; unchanged estimates cost no redraw."""

        if watcher is None:
            self._signature = "disabled"
            self._set(self.summary, "The ghost estimate is disabled in the configuration.")
            self._set(self.detail, "")
            self._set(self.elements, "")
            self._set(self.status, "")
            self._clear_preview()
            self._sync_clear_controls(None)
            self._sync_estimate_controls(None)
            return
        result = watcher.result
        signature = (
            result.sampled_at if result is not None else None,
            watcher.error,
            watcher.capture_failed,
            getattr(self.app, "_clear_note", None),
            bool(self.app.controller.state.ghost_estimate),
            self.app.controller.monitor_available,
        )
        if signature == self._signature and self._mapped_once:
            return
        self._signature = signature
        self._show(watcher, result)

    def _show(self, watcher, result: GhostResult | None) -> None:
        if result is None:
            saved = watcher.saved
            if saved is not None:
                areas = "area" if len(saved.elements) == 1 else "areas"
                light = sum(
                    1 for element in saved.elements if not element.dark
                )
                kind = f" ({light} light)" if light else ""
                self._set(
                    self.summary,
                    f"Last saved: {len(saved.elements)} ghost {areas}{kind}, "
                    f"level {saved.level}/100",
                )
                self._set(self.detail, f"saved {saved.saved_at}")
                self._set(self.elements, self._element_text(saved.elements))
            else:
                self._set(self.summary, "Waiting for the first sample…")
                self._set(self.detail, "")
                self._set(self.elements, "")
            self._clear_preview()
        else:
            count = len(result.elements)
            areas = "area" if count == 1 else "areas"
            light = sum(1 for element in result.elements if not element.dark)
            kind = f" ({light} light)" if light else ""
            self._set(
                self.summary,
                f"Ghosts: {count} {areas}{kind} — level {result.level}/100",
            )
            clock = time.strftime("%H:%M:%S", time.localtime(result.sampled_at))
            detail = (
                f"sampled {clock} · {result.dirty_fraction * 100:.1f}% of the "
                f"panel dirty · model {result.model_width}×{result.model_height}"
            )
            if result.light_level:
                detail += f" · light level {result.light_level}/100"
            self._set(self.detail, detail)
            self._set(self.elements, self._element_text(result.elements))
            self._show_preview(watcher, result)
        self._sync_clear_controls(result)
        self._sync_estimate_controls(watcher)
        self._show_status(watcher)

    def _clear_preview(self) -> None:
        self._preview_key = None
        self.preview.clear()

    def _show_preview(self, watcher, result: GhostResult) -> None:
        model = watcher.model
        if model is None:
            self._clear_preview()
            return
        width = GHOST_PREVIEW_WIDTH
        height = max(1, round(width * model.height / model.width))
        # Rendering the preview is pure Python: redo it only when the model
        # moved (a sample without changes leaves `version` alone).
        key = (id(model), model.version, width, height)
        if key == self._preview_key:
            return
        self._preview_key = key
        rgb = model.preview_rgb(width, height)
        pixbuf = self.app.GdkPixbuf.Pixbuf.new_from_bytes(
            self.app.GLib.Bytes.new(rgb),
            self.app.GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            width * 3,
        )
        self.preview.set_from_pixbuf(pixbuf)

    def _show_status(self, watcher) -> None:
        text = ""
        error = False
        if watcher.capture_failed:
            text = (
                (watcher.error or "the screen capture is unavailable")
                + " — press Retry capture"
            )
            error = True
        elif watcher.error:
            text = watcher.error
            error = True
        elif self.app.controller.monitor_available is not True:
            text = "The monitor is unavailable; the estimate is off."
        elif getattr(watcher, "paused", False):
            text = "Estimate stopped — press Start estimate"
        elif getattr(watcher, "zone_error", None):
            text = watcher.zone_error
        elif getattr(self.app, "_clear_note", None):
            text = self.app._clear_note
        elif watcher.model is None:
            text = "The screen share opens on the first sample."
        elif not watcher.enabled:
            text = "Disabled in the configuration."
        self._set(self.status, text)
        context = self.status.get_style_context()
        if context.has_class("dasung-error") != error:
            if error:
                context.add_class("dasung-error")
            else:
                context.remove_class("dasung-error")

    @staticmethod
    def _element_text(elements) -> str:
        if not elements:
            return "No ghost areas estimated."
        lines = []
        for index, element in enumerate(elements, start=1):
            label = ""
            if element.app:
                label = f" — {element.app}"
                if element.fullscreen:
                    label += " (fullscreen)"
            lines.append(
                f"{index}. {element.width}×{element.height} px at "
                f"({element.x}, {element.y}){label} — severity "
                f"{element.severity}, {'dark' if element.dark else 'light'}, "
                f"{format_age(element.age)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _set(label, text: str) -> None:
        """Write a label only when its text changed (avoids redraws)."""

        if label.get_text() != text:
            label.set_text(text)


ABOUT_DESCRIPTION = (
    "System-tray control and diagnostics for Dasung Paperlike e-ink "
    "monitors over a CH340 serial port"
)
ABOUT_HOMEPAGE = "https://github.com/sal7g86/eink-dasung-control"
ABOUT_LICENSE = "Apache License 2.0 — © 2026 sal7g86"


def version_text() -> str:
    """The version pill under the program name."""

    return f"version {__version__}"


def _availability(controller) -> str:
    """Human wording for the tray's monitor reachability tri-state."""

    if controller.monitor_available is True:
        return "available"
    if controller.monitor_available is False:
        return "unavailable"
    return "not checked yet"


def about_sections(controller, runtime, files):
    """The About window's text sections as `(title, ((label, value), ...))`.

    Pure data (no GTK) so the content stays testable: `runtime` carries the
    versions probed from the loaded bindings and `files` the visible paths.
    """

    panel = controller.panel
    return (
        (
            "Monitor",
            (
                ("Model", panel.name),
                ("Protocol", f"0x{panel.protocol:02X}"),
                ("Refresh rate", f"{panel.refresh_hz} Hz"),
                ("Serial port", controller.device),
                ("Availability", _availability(controller)),
            ),
        ),
        (
            "Runtime",
            (
                ("Python", runtime["python"]),
                ("GTK", runtime["gtk"]),
                ("PyGObject", runtime["pygobject"]),
                ("Session", runtime["session"]),
            ),
        ),
        (
            "Files",
            (
                ("Configuration", files["config"]),
                ("Last state", files["state"]),
                ("Log", files["log"]),
            ),
        ),
    )


def _session_label(Gdk) -> str:
    """`X11`/`Wayland` plus the display name, from the environment if set."""

    display = Gdk.Display.get_default()
    name = display.get_name() if display is not None else None
    kind = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if kind not in ("x11", "wayland"):
        if name is None:
            kind = ""
        elif name.startswith("wayland"):
            kind = "wayland"
        elif name.startswith(":"):
            kind = "x11"
    label = {"x11": "X11", "wayland": "Wayland"}.get(kind, kind or "unknown")
    return f"{label} ({name})" if name else label


class AboutWindow:
    """Informational window: version, monitor, runtime and file paths.

    A centered hero over flat sections, so the content reads as a page
    instead of a stack of boxes; only the monitor availability is refreshed
    on every `update()`. Opening it never touches the serial port: every
    value comes from the loaded bindings or the tray's own state.
    """

    def __init__(self, app: TrayApp) -> None:
        """Build the window; nothing here talks to the monitor."""

        Gtk = app.Gtk
        self.app = app
        self.Gtk = Gtk
        self._mapped_once = False
        self._value_labels: dict[str, object] = {}
        install_window_css(app.Gtk, app.Gdk)

        window = Gtk.Window(title="About dasungctl")
        # Width fixed, height driven by the content, so every section is
        # visible at once and the window is exactly as tall as it needs.
        # The minimum width matters: GTK computes the minimum height at the
        # narrowest possible width, where the file paths wrap into many
        # lines, and without the floor the window grows past its natural
        # height. -1 keeps the natural height.
        window.set_size_request(500, -1)
        window.set_default_size(500, -1)
        window.connect("delete-event", self._on_delete)
        self.window = window

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        window.set_titlebar(header)
        # GTK clears the window title when a custom titlebar is set (3.24):
        # put it back for the WM, the window list and Alt+Tab.
        window.set_title("About dasungctl")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_border_width(12)
        window.add(content)

        # No ScrolledWindow here: one would not propagate the wrapped
        # labels' height-for-width, and this page is small and static. The
        # window sizes itself to the content; every section is visible at
        # once on the tested screens.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_margin_end(4)
        content.pack_start(body, False, False, 0)

        body.pack_start(self._hero(), False, False, 0)

        runtime = {
            "python": platform.python_version(),
            "gtk": "{}.{}.{}".format(
                Gtk.get_major_version(),
                Gtk.get_minor_version(),
                Gtk.get_micro_version(),
            ),
            "pygobject": getattr(app.gi, "__version__", "unknown"),
            "session": _session_label(app.Gdk),
        }
        files = {
            "config": str(paths.config_path()),
            "state": str(paths.last_state_path()),
            "log": str(paths.log_path()),
        }
        for section_title, rows in about_sections(app.controller, runtime, files):
            body.pack_start(
                self._section(section_title, rows), False, False, 0
            )

        content.pack_start(self._footer(), False, False, 0)

    # -- widget helpers ----------------------------------------------------

    def _hero(self):
        """Centered icon, name, version pill and description."""

        Gtk = self.Gtk
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hero.set_margin_top(12)
        hero.set_margin_bottom(12)
        hero.pack_start(self._program_icon(), False, False, 0)
        name = Gtk.Label(label="dasungctl")
        name.get_style_context().add_class("dasung-about-name")
        hero.pack_start(name, False, False, 0)
        version = Gtk.Label(label=version_text())
        version.get_style_context().add_class("dasung-about-version")
        version.set_halign(Gtk.Align.CENTER)
        hero.pack_start(version, False, False, 0)
        description = Gtk.Label(label=ABOUT_DESCRIPTION)
        description.set_line_wrap(True)
        description.set_justify(Gtk.Justification.CENTER)
        description.set_max_width_chars(56)
        description.get_style_context().add_class("dasung-subtitle")
        hero.pack_start(description, False, False, 0)
        return hero

    def _program_icon(self):
        """The project icon at hero size, theme icon as fallback."""

        Gtk = self.Gtk
        image = Gtk.Image()
        image.set_halign(Gtk.Align.CENTER)
        bitmap = icon_path()
        if bitmap.exists():
            image.set_from_pixbuf(
                self.app.GdkPixbuf.Pixbuf.new_from_file_at_size(
                    str(bitmap), 72, 72
                )
            )
            return image
        name = icon_name(Gtk, ICON_FALLBACKS)
        if name is not None:
            image.set_from_icon_name(name, Gtk.IconSize.DIALOG)
            image.set_pixel_size(72)
        return image

    def _section(self, title: str, rows):
        """Flat section: a small heading over label/value rows."""

        Gtk = self.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(10)
        heading = Gtk.Label(label=title.upper(), xalign=0)
        heading.get_style_context().add_class("dasung-about-title")
        heading.set_margin_bottom(2)
        box.pack_start(heading, False, False, 0)
        for label, value in rows:
            box.pack_start(
                self._row(label, value, path=title == "Files"),
                False,
                False,
                0,
            )
        return box

    def _row(self, label: str, value: str, *, path: bool = False):
        """Label + selectable value row, closed by a hairline separator."""

        Gtk = self.Gtk
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.get_style_context().add_class("dasung-about-row")
        name = Gtk.Label(label=label, xalign=0)
        name.set_size_request(105, -1)
        name.get_style_context().add_class("dasung-about-label")
        text = Gtk.Label(label=value, xalign=1)
        text.set_selectable(True)
        # Selectable labels are focusable, and focusing one selects its whole
        # text: without this the first value opens highlighted. Mouse
        # selection and copy keep working without keyboard focus.
        text.set_can_focus(False)
        text.set_line_wrap(True)
        if path:
            text.get_style_context().add_class("dasung-about-path")
        self._value_labels[label] = text
        row.pack_start(name, False, False, 0)
        row.pack_start(text, True, True, 0)
        return row

    def _footer(self):
        """Centered homepage pill and license line."""

        Gtk = self.Gtk
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        footer.set_margin_top(12)
        footer.set_margin_bottom(4)
        link = Gtk.Button()
        link.set_relief(Gtk.ReliefStyle.NONE)
        link.get_style_context().add_class("dasung-about-link")
        link.set_halign(Gtk.Align.CENTER)
        link.set_tooltip_text(ABOUT_HOMEPAGE)
        link.connect("clicked", self._on_homepage)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = icon_name(
            Gtk,
            (
                "link-symbolic",
                "applications-internet-symbolic",
                "network-workgroup-symbolic",
            ),
        )
        if icon is not None:
            inner.pack_start(
                Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU),
                False,
                False,
                0,
            )
        inner.pack_start(Gtk.Label(label="Project homepage"), False, False, 0)
        link.add(inner)
        footer.pack_start(link, False, False, 0)
        license_label = Gtk.Label(label=ABOUT_LICENSE)
        license_label.get_style_context().add_class("dasung-subtitle")
        footer.pack_start(license_label, False, False, 0)
        return footer

    def _on_homepage(self, _button) -> None:
        """Open the project page in the user's browser."""

        self.Gtk.show_uri_on_window(
            self.window, ABOUT_HOMEPAGE, self.Gtk.get_current_event_time()
        )

    # -- view updates ------------------------------------------------------

    def present(self) -> None:
        """First show reveals every widget; later calls raise the window."""

        if not self._mapped_once:
            self.window.show_all()
            self._mapped_once = True
        else:
            _bring_to_current_desktop(self.window, self.app)
        self.window.present()

    def update(self) -> None:
        """Refresh the monitor availability wording and its colour."""

        label = self._value_labels.get("Availability")
        if label is None:
            return
        text = _availability(self.app.controller)
        if label.get_text() != text:
            label.set_text(text)
        context = label.get_style_context()
        for css_class, wanted in (
            ("dasung-ok", text == "available"),
            ("dasung-error", text == "unavailable"),
        ):
            if context.has_class(css_class) != wanted:
                if wanted:
                    context.add_class(css_class)
                else:
                    context.remove_class(css_class)

    def _on_delete(self, *_args):
        """Hide the window instead of destroying it: reopening is instant."""

        self.window.hide()
        return True
