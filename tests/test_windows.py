"""Window-zone tests: normalization, clipping, lookup; no display needed."""

import sys
import types

import pytest

from dasungctl.windows import (
    IGNORED_APPS,
    KWIN_MOVE_SCRIPT,
    KWinZones,
    X11Zones,
    Zone,
    ZoneUnavailable,
    lookup_zone,
    normalize_app,
    to_monitor_zones,
)


def window(app, x, y, width, height, fullscreen=False):
    return {
        "resourceClass": app,
        "fullScreen": fullscreen,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


def test_normalize_app_drops_the_reverse_domain_prefix():
    assert normalize_app("org.kde.konsole") == "konsole"
    assert normalize_app("org.mozilla.firefox") == "firefox"
    assert normalize_app("plasmashell") == "plasmashell"
    assert normalize_app("") == ""
    assert normalize_app(None) == ""


def test_to_monitor_zones_converts_and_clips():
    windows = [
        window("org.kde.konsole", 3440, 182, 943, 1257),
        window("org.mozilla.firefox", 100, 100, 1000, 800),
        window("org.kde.okular", 3500, 400, 400, 300),
        window("org.kde.kate", 4300, 180, 200, 200),
    ]

    zones = to_monitor_zones(windows, 3440, 182, 943, 1257)

    assert [zone.app for zone in zones] == ["konsole", "okular", "kate"]
    assert (zones[0].x, zones[0].y) == (0, 0)
    assert (zones[0].width, zones[0].height) == (943, 1257)
    assert zones[0].fullscreen is True
    assert (zones[1].x, zones[1].y) == (60, 218)
    assert zones[1].fullscreen is False
    # The last window sticks out on the right: it is clipped to the monitor.
    assert (zones[2].x, zones[2].width) == (860, 83)


def test_to_monitor_zones_ignores_hidden_and_empty_windows():
    windows = [
        window("xwaylandvideobridge", 0, 0, 400, 200),
        window("", 0, 0, 400, 200),
        window("org.kde.konsole", -5000, -5000, 100, 100),
        {"resourceClass": "broken"},
        window("org.kde.kate", 10, 10, 200, 200),
    ]

    zones = to_monitor_zones(windows, 0, 0, 943, 1257)

    assert [zone.app for zone in zones] == ["kate"]
    assert "xwaylandvideobridge" in IGNORED_APPS


def test_to_monitor_zones_flags_the_fullscreen_flag():
    windows = [window("org.kde.konsole", 0, 0, 100, 100, fullscreen=True)]

    zones = to_monitor_zones(windows, 0, 0, 943, 1257)

    assert zones[0].fullscreen is True
    assert (zones[0].width, zones[0].height) == (100, 100)


def test_to_monitor_zones_requires_a_valid_monitor():
    assert to_monitor_zones([window("a", 0, 0, 1, 1)], 0, 0, 0, 10) == ()


def test_lookup_zone_returns_the_topmost_match():
    top = Zone("konsole", 0, 0, 100, 100, False)
    under = Zone("plasmashell", 0, 0, 943, 1257, False)

    assert lookup_zone((top, under), 50, 50) is top
    assert lookup_zone((under,), 50, 50) is under
    # Outside the top window, the one below still matches.
    assert lookup_zone((top, under), 200, 10) is under
    assert lookup_zone((top,), 2000, 10) is None
    assert lookup_zone((), 0, 0) is None


def test_zone_contains_is_half_open():
    zone = Zone("kate", 10, 20, 100, 50, False)

    assert zone.contains(10, 20)
    assert zone.contains(109, 69)
    assert not zone.contains(110, 20)
    assert not zone.contains(10, 70)


# -- X11Zones against a fake python-xlib --------------------------------------

IS_VIEWABLE = 2


class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakeAttributes:
    def __init__(self, map_state):
        self.map_state = map_state


class FakeWindow:
    """A window that only answers; coordinates must come from the root."""

    def __init__(self, window_id, app, x, y, width, height, fullscreen=False):
        self.window_id = window_id
        self.app = app
        self.global_x = x
        self.global_y = y
        self.width = width
        self.height = height
        self.fullscreen = fullscreen
        self.map_state = IS_VIEWABLE
        self.connection = None

    def get_attributes(self):
        return FakeAttributes(self.map_state)

    def get_wm_class(self):
        return (None, self.app) if self.app else None

    def get_geometry(self):
        return types.SimpleNamespace(width=self.width, height=self.height)

    def get_full_property(self, atom, _type):
        atoms = self.connection.atoms
        if not self.fullscreen or atom != atoms.get("_NET_WM_STATE"):
            return None
        return FakeProperty([atoms["_NET_WM_STATE_FULLSCREEN"]])

    def translate_coords(self, *args):
        raise AssertionError("the window must not translate root coordinates")


class FakeRoot:
    def __init__(self, connection):
        self.connection = connection

    def get_full_property(self, atom, _type):
        atoms = self.connection.atoms
        if atom == atoms.get("_NET_CLIENT_LIST_STACKING"):
            return FakeProperty(self.connection.stacking)
        return None

    def translate_coords(self, window, x, y):
        assert (x, y) == (0, 0)
        return types.SimpleNamespace(x=window.global_x, y=window.global_y)


class FakeConnection:
    """Minimal X connection: interned atoms, root property, window lookup."""

    def __init__(self, windows, stacking):
        self.atoms = {}
        self.stacking = list(stacking)
        self.windows = {item.window_id: item for item in windows}
        for item in windows:
            item.connection = self

    def intern_atom(self, name):
        return self.atoms.setdefault(name, len(self.atoms) + 100)

    def screen(self):
        return types.SimpleNamespace(root=FakeRoot(self))

    def create_resource_object(self, kind, window_id):
        assert kind == "window"
        return self.windows[window_id]


def install_fake_xlib(monkeypatch, connection):
    X = types.SimpleNamespace(AnyPropertyType=0, IsViewable=IS_VIEWABLE)
    display = types.SimpleNamespace(Display=lambda: connection)
    fake = types.ModuleType("Xlib")
    fake.X = X
    fake.display = display
    monkeypatch.setitem(sys.modules, "Xlib", fake)


def test_x11_zones_use_root_relative_window_positions(monkeypatch):
    windows = [
        FakeWindow(1, "org.kde.konsole", 3760, 565, 942, 806),
        FakeWindow(2, "konsole", 3760, 1403, 942, 386),
        FakeWindow(3, "Nemo-desktop", 3760, 533, 942, 1256, fullscreen=True),
        FakeWindow(4, "firefox", 0, 0, 1200, 900),
        FakeWindow(5, "xwaylandvideobridge", 3760, 533, 942, 1256),
        FakeWindow(6, "kate", 4300, 180, 500, 400),
    ]
    connection = FakeConnection(windows, stacking=[4, 5, 3, 2, 1, 6])
    install_fake_xlib(monkeypatch, connection)

    zones = X11Zones().zones(3760, 533, 942, 1256)

    assert [zone.app for zone in zones] == ["kate", "konsole", "konsole", "Nemo-desktop"]
    # The root-relative position minus the monitor origin.
    assert (zones[1].x, zones[1].y) == (0, 32)
    assert (zones[1].width, zones[1].height) == (942, 806)
    assert (zones[2].x, zones[2].y) == (0, 870)
    assert (zones[3].fullscreen, zones[3].width) == (True, 942)
    # Clipped on the right edge of the monitor.
    assert (zones[0].x, zones[0].width) == (540, 402)


def test_x11_zones_skip_unmapped_windows(monkeypatch):
    hidden = FakeWindow(1, "kate", 3760, 533, 100, 100)
    hidden.map_state = 0  # IsUnmapped
    visible = FakeWindow(2, "konsole", 3760, 533, 100, 100)
    connection = FakeConnection([hidden, visible], stacking=[1, 2])
    install_fake_xlib(monkeypatch, connection)

    zones = X11Zones().zones(3760, 533, 942, 1256)

    assert [zone.app for zone in zones] == ["konsole"]


def test_x11_zones_convert_device_coordinates(monkeypatch):
    # A doubled GTK scale (Cinnamon scale-ui-down): EWMH reports device
    # pixels while the estimate works in application pixels.
    windows = [FakeWindow(1, "konsole", 7520, 1162, 2512, 1820)]
    connection = FakeConnection(windows, stacking=[1])
    install_fake_xlib(monkeypatch, connection)

    zones = X11Zones(scale=2).zones(3760, 549, 1256, 942)

    assert [(zone.app, zone.x, zone.y) for zone in zones] == [("konsole", 0, 32)]
    assert (zones[0].width, zones[0].height) == (1256, 910)


# -- KWinZones without a compositor -------------------------------------------


def test_kwin_zones_convert_the_reported_windows(monkeypatch):
    provider = KWinZones()
    monkeypatch.setattr(
        provider,
        "_query",
        lambda: [
            window("org.kde.konsole", 3440, 182, 943, 1257),
            window("firefox", 100, 100, 400, 300),
        ],
    )

    zones = provider.zones(3440, 182, 943, 1257)

    assert [zone.app for zone in zones] == ["konsole"]
    assert zones[0].fullscreen is True


def test_kwin_zones_surface_the_query_failure(monkeypatch):
    provider = KWinZones()
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("no reply")

    monkeypatch.setattr(provider, "_query", broken)

    with pytest.raises(ZoneUnavailable) as first:
        provider.zones(0, 0, 100, 100)
    assert "no reply" in str(first.value)

    # The retry window reports the same failure without querying again.
    with pytest.raises(ZoneUnavailable) as second:
        provider.zones(0, 0, 100, 100)
    assert "no reply" in str(second.value)
    assert len(calls) == 1


def test_kwin_zones_recover_after_a_failure(monkeypatch):
    provider = KWinZones()
    state = {"fail": True}

    def query():
        if state["fail"]:
            raise RuntimeError("not ready")
        return [window("kate", 0, 0, 100, 100)]

    monkeypatch.setattr(provider, "_query", query)

    with pytest.raises(ZoneUnavailable):
        provider.zones(0, 0, 100, 100)

    state["fail"] = False
    provider._failed_at = 0.0  # the backoff expired
    zones = provider.zones(0, 0, 100, 100)

    assert [zone.app for zone in zones] == ["kate"]
    assert provider._last_error is None


def test_kwin_move_to_current_desktop_runs_the_action(monkeypatch):
    provider = KWinZones()
    loaded = []
    unloaded = []
    monkeypatch.setattr(
        provider, "_load_and_start", lambda script: loaded.append(script)
    )
    monkeypatch.setattr(provider, "_unload", lambda: unloaded.append(1))

    assert provider.move_to_current_desktop() is True
    assert loaded == [KWIN_MOVE_SCRIPT]
    assert unloaded == [1]

    def broken(_script):
        raise RuntimeError("no compositor")

    monkeypatch.setattr(provider, "_load_and_start", broken)
    assert provider.move_to_current_desktop() is False
    # The script is still unloaded after a failed start.
    assert len(unloaded) == 2
