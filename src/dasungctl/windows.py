"""Window zones on the Dasung monitor, used to label estimated ghosts.

The ghost estimator needs to know which application was where when a ghost
area formed. X11 exposes the window list through EWMH (python-xlib, an
optional dependency); KDE Wayland has no standard API for other applications'
windows, but KWin's scripting interface answers a small JavaScript query that
calls back over D-Bus (the same route tools like kdotool use). Both return
global logical rectangles, converted here to monitor-relative zones.

Everything is best-effort: a missing library or a refused channel simply
means no labels, never a failed tray.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
import warnings

# Windows that never deserve a label: invisible helpers and empty classes.
# The tray's own clearing overlay must not label the ghosts it flashes.
IGNORED_APPS = frozenset({"xwaylandvideobridge", "dasungctl-clear"})
# A window covering almost the whole monitor is labelled fullscreen even when
# the compositor does not set the flag (the Dasung's terminal does this).
FULLSCREEN_COVERAGE = 0.98


class ZoneUnavailable(RuntimeError):
    """The window list cannot be read in this session."""


@dataclass(frozen=True)
class Zone:
    """One window rectangle in monitor-relative coordinates."""

    app: str
    x: int
    y: int
    width: int
    height: int
    fullscreen: bool

    def contains(self, x: int, y: int) -> bool:
        """True when the point falls inside the zone rectangle."""

        return (
            self.x <= x < self.x + self.width
            and self.y <= y < self.y + self.height
        )


def normalize_app(resource_class: str) -> str:
    """Drop the reverse-domain prefix KDE uses (`org.kde.konsole`)."""

    name = str(resource_class or "").strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def to_monitor_zones(
    windows,
    origin_x: int,
    origin_y: int,
    monitor_width: int,
    monitor_height: int,
) -> tuple[Zone, ...]:
    """Clip global window rectangles to the monitor and normalize the apps.

    `windows` comes from a provider, topmost first; each item has
    ``resourceClass``, ``fullScreen``, ``x``, ``y``, ``width``, ``height``.
    Windows outside the monitor, invisible ones and empty classes are
    dropped, and the list keeps the provider's stacking order.
    """

    if monitor_width <= 0 or monitor_height <= 0:
        return ()
    monitor_area = monitor_width * monitor_height
    zones: list[Zone] = []
    for window in windows:
        try:
            app = normalize_app(window.get("resourceClass", ""))
            if not app or app.lower() in IGNORED_APPS:
                continue
            left = round(float(window["x"]) - origin_x)
            top = round(float(window["y"]) - origin_y)
            width = round(float(window["width"]))
            height = round(float(window["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        right = min(monitor_width, left + width)
        bottom = min(monitor_height, top + height)
        left = max(0, left)
        top = max(0, top)
        if right <= left or bottom <= top:
            continue
        visible = (right - left) * (bottom - top)
        fullscreen = bool(window.get("fullScreen", False)) or (
            visible >= monitor_area * FULLSCREEN_COVERAGE
        )
        zones.append(
            Zone(
                app=app,
                x=left,
                y=top,
                width=right - left,
                height=bottom - top,
                fullscreen=fullscreen,
            )
        )
    return tuple(zones)


def lookup_zone(zones, x: int, y: int) -> Zone | None:
    """Topmost zone containing the point (providers order topmost first)."""

    for zone in zones:
        if zone.contains(x, y):
            return zone
    return None


class X11Zones:
    """EWMH window list through python-xlib (optional dependency)."""

    name = "x11"

    def __init__(self) -> None:
        """Import python-xlib eagerly, or raise ZoneUnavailable."""

        try:
            from Xlib import display as xdisplay
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise ZoneUnavailable(
                "python-xlib is not installed (install the 'labels' extra)"
            ) from exc
        self._xdisplay = xdisplay
        self._connection = None

    def _display(self):
        if self._connection is None:
            self._connection = self._xdisplay.Display()
        return self._connection

    def zones(
        self, origin_x: int, origin_y: int, width: int, height: int
    ) -> tuple[Zone, ...]:
        """Window zones intersecting the captured region, in local coordinates."""

        from Xlib import X

        connection = self._display()
        root = connection.screen().root
        property_value = None
        for atom_name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            atom = connection.intern_atom(atom_name)
            property_value = root.get_full_property(atom, X.AnyPropertyType)
            if property_value is not None:
                break
        if property_value is None:
            return ()
        fullscreen_atom = connection.intern_atom("_NET_WM_STATE_FULLSCREEN")
        state_atom = connection.intern_atom("_NET_WM_STATE")
        windows = []
        # EWMH lists bottom-to-top; the label lookup wants the topmost first.
        for window_id in reversed(list(property_value.value)):
            try:
                window = connection.create_resource_object("window", window_id)
                attributes = window.get_attributes()
                if attributes.map_state != X.IsViewable:
                    continue
                wm_class = window.get_wm_class()
                resource_class = ""
                if wm_class:
                    resource_class = wm_class[1] or wm_class[0] or ""
                # Position of the window in root coordinates. python-xlib's
                # translate_coords(self, src, x, y) translates from `src` to
                # `self`, so the root must be the caller and the window the
                # source: the reversed call returns the negated position.
                translated = root.translate_coords(window, 0, 0)
                geometry = window.get_geometry()
                state = window.get_full_property(state_atom, X.AnyPropertyType)
                fullscreen = bool(
                    state is not None and fullscreen_atom in list(state.value)
                )
                windows.append(
                    {
                        "resourceClass": resource_class,
                        "fullScreen": fullscreen,
                        "x": translated.x,
                        "y": translated.y,
                        "width": geometry.width,
                        "height": geometry.height,
                    }
                )
            except Exception:  # pragma: no cover - a dead window must not stop us
                continue
        return to_monitor_zones(windows, origin_x, origin_y, width, height)


# The KWin script runs inside the compositor and answers the query with one
# D-Bus call back to this process. `org.kde.kwin.Scripting` does not require
# the screenshot allowlist, which is why this route is usable at all.
KWIN_SCRIPT = """\
var list = workspace.stackingOrder ? workspace.stackingOrder : workspace.windowList();
var data = [];
for (var i = list.length - 1; i >= 0; i--) {
    var w = list[i];
    if (w.minimized) { continue; }
    var geometry = w.frameGeometry;
    var output = "";
    try { output = (w.output && w.output.name) ? String(w.output.name) : ""; } catch (e) {}
    data.push({
        resourceClass: String(w.resourceClass || ""),
        fullScreen: Boolean(w.fullScreen),
        x: geometry.x,
        y: geometry.y,
        width: geometry.width,
        height: geometry.height,
        output: output
    });
}
callDBus("org.dasungctl.Zones", "/zones", "org.dasungctl.Zones", "Report",
         JSON.stringify(data));
"""

KWIN_INTERFACE_XML = """\
<node>
  <interface name="org.dasungctl.Zones">
    <method name="Report">
      <arg type="s" direction="in"/>
    </method>
  </interface>
</node>
"""


class KWinZones:
    """Window list from KWin's scripting interface (KDE Wayland and X11)."""

    name = "kwin"
    BUS_NAME = "org.dasungctl.Zones"
    OBJECT_PATH = "/zones"
    INTERFACE = "org.dasungctl.Zones"
    PLUGIN = "dasungzones"
    RETRY_SECONDS = 10.0

    def __init__(self, runtime_dir: Path | None = None) -> None:
        """Remember where the KWin script and its D-Bus objects live."""

        base = runtime_dir
        if base is None:
            value = os.environ.get("XDG_RUNTIME_DIR")
            base = Path(value) if value else Path(tempfile.gettempdir())
        self._script_path = base / "dasungctl-zones.js"
        self._gio = None
        self._glib = None
        self._bus = None
        self._registered = False
        self._report: dict = {}
        self._failed_at: float | None = None

    def _ensure(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self._gio = Gio
        self._glib = GLib
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if not self._registered:
            node = Gio.DBusNodeInfo.new_for_xml(KWIN_INTERFACE_XML)
            interface = node.lookup_interface(self.INTERFACE)
            # PyGObject exposes only the deprecated register_object(); the
            # warning is noise here, the closure variant is not bound.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self._bus.register_object(
                    self.OBJECT_PATH, interface, self._on_call, None, None
                )
            self._registered = True
        self._bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (self.BUS_NAME, 0)),
            GLib.VariantType.new("(u)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Report":
            self._report["json"] = params.unpack()[0]
            invocation.return_value(None)

    def _write_script(self) -> None:
        try:
            current = self._script_path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != KWIN_SCRIPT:
            self._script_path.write_text(KWIN_SCRIPT, encoding="utf-8")

    def _query(self) -> list:
        """Load the script, wait for its report and unload it again."""

        self._ensure()
        Gio, GLib = self._gio, self._glib
        self._report.clear()
        self._write_script()
        scripting = (
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
        )
        try:
            self._bus.call_sync(
                *scripting,
                "loadScript",
                GLib.Variant("(ss)", (str(self._script_path), self.PLUGIN)),
                GLib.VariantType.new("(i)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            self._bus.call_sync(
                *scripting,
                "start",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            loop = GLib.MainLoop()

            def check():
                if "json" in self._report:
                    loop.quit()
                    return False
                return True

            def timeout():
                loop.quit()
                return False

            GLib.timeout_add(50, check)
            GLib.timeout_add(3000, timeout)
            if "json" not in self._report:
                loop.run()
        finally:
            try:
                self._bus.call_sync(
                    *scripting,
                    "unloadScript",
                    GLib.Variant("(s)", (self.PLUGIN,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except Exception:
                pass
        text = self._report.get("json")
        if text is None:
            raise ZoneUnavailable("KWin did not answer the window query")
        data = json.loads(text)
        return data if isinstance(data, list) else []

    def zones(
        self, origin_x: int, origin_y: int, width: int, height: int
    ) -> tuple[Zone, ...]:
        """Window zones from KWin; empty while the plugin is unavailable."""

        now = time.monotonic()
        if self._failed_at is not None and now - self._failed_at < self.RETRY_SECONDS:
            return ()
        try:
            windows = self._query()
        except Exception:
            self._failed_at = now
            return ()
        self._failed_at = None
        return to_monitor_zones(windows, origin_x, origin_y, width, height)


def open_zones():
    """Best provider for this session, or None when labels are unavailable."""

    if os.environ.get("WAYLAND_DISPLAY"):
        if "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
            return KWinZones()
        return None
    try:
        return X11Zones()
    except ZoneUnavailable:
        return None
