"""Screen capture helpers and token persistence; no display and no hardware."""

import json
import sys
import types

import pytest

from dasungctl import screencap
from dasungctl.screencap import (
    X11DamageTracker,
    drm_output_present,
    edid_monitor_name,
    gray_from_channels,
    gray_from_pixbuf,
    load_restore_token,
    merge_rects,
    monitor_output_present,
    partial_plan,
    pick_monitor,
    save_restore_token,
    scale_dimensions,
    x11_monitor_aliases,
)


def test_scale_dimensions_keeps_the_aspect_ratio():
    assert scale_dimensions(943, 1257, 480) == (480, 640)
    assert scale_dimensions(1920, 1080, 320) == (320, 180)
    assert scale_dimensions(100, 100, 480) == (480, 480)
    with pytest.raises(ValueError):
        scale_dimensions(0, 10, 100)


def test_gray_from_rgb_and_rgba():
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    assert gray_from_channels(rgb, 4, 1, 3) == bytes([63, 127, 63, 255])
    rgba = bytes([255, 0, 0, 1, 0, 0, 0, 1, 255, 255, 255, 1, 255, 255, 255, 1])
    assert gray_from_channels(rgba, 4, 1, 4) == bytes([63, 0, 255, 255])


class FakePixbuf:
    """GdkPixbuf stand-in: padded rows, luminance in saturate_and_pixelate."""

    def __init__(self, pixels, width, height, channels, stride):
        self.pixels = bytearray(pixels)
        self.width, self.height = width, height
        self.channels, self.stride = channels, stride

    def get_width(self):
        return self.width

    def get_height(self):
        return self.height

    def get_n_channels(self):
        return self.channels

    def get_rowstride(self):
        return self.stride

    def get_pixels(self):
        return bytes(self.pixels)

    def saturate_and_pixelate(self, dest, saturation, pixelate):
        assert dest is self and saturation == 0.0 and pixelate is False
        for y in range(self.height):
            for x in range(self.width):
                at = y * self.stride + x * self.channels
                r, g, b = self.pixels[at : at + 3]
                gray = round(0.30 * r + 0.59 * g + 0.11 * b)
                self.pixels[at : at + 3] = bytes((gray, gray, gray))


def test_gray_from_pixbuf_slices_one_channel_per_padded_row():
    # Two RGB rows of two pixels, each row padded to 8 bytes.
    rgb = bytes([10, 10, 10, 200, 200, 200, 9, 9, 50, 50, 50, 0, 0, 0, 9, 9])
    assert gray_from_pixbuf(FakePixbuf(rgb, 2, 2, 3, 8)) == bytes(
        [10, 200, 50, 0]
    )
    rgba = bytes([255, 0, 0, 1, 255, 255, 255, 1])
    assert gray_from_pixbuf(FakePixbuf(rgba, 2, 1, 4, 8)) == bytes([76, 255])
    with pytest.raises(ValueError):
        gray_from_pixbuf(FakePixbuf(bytes(2), 2, 1, 1, 2))


def test_gray_from_gray_and_padded_stride():
    assert gray_from_channels(bytes([1, 2, 3]), 3, 1, 1) == bytes([1, 2, 3])
    # Two rows of two pixels, each row padded to four bytes.
    data = bytes([10, 20, 0, 0, 30, 40, 0, 0])
    assert gray_from_channels(data, 2, 2, 1, stride=4) == bytes([10, 20, 30, 40])
    # RGB rows with padding: one pixel per row, three channels plus one byte.
    data = bytes([10, 20, 30, 0, 40, 50, 60, 0])
    assert gray_from_channels(data, 1, 2, 3, stride=4) == bytes([20, 50])


def test_x_image_rgb_converts_bgrx_pixels():
    # XGetImage's little-endian 24/32-bpp layout stores B, G, R, X per pixel.
    data = bytes([1, 2, 3, 0, 10, 20, 30, 0])
    assert screencap.x_image_rgb(data, 2, 1, 24, 8) == bytes([3, 2, 1, 30, 20, 10])
    # Row padding is honored.
    padded = bytes([1, 2, 3, 0, 9, 9, 9, 9, 4, 5, 6, 0, 9, 9, 9, 9])
    assert screencap.x_image_rgb(padded, 1, 2, 24, 8) == bytes([3, 2, 1, 6, 5, 4])


def test_x_image_rgb_refuses_layouts_it_cannot_read():
    data = bytes(16)
    assert screencap.x_image_rgb(data, 2, 1, 16, 8) is None  # not 24/32 bpp
    assert screencap.x_image_rgb(data, 2, 1, 24, 4) is None  # stride too small
    assert screencap.x_image_rgb(data, 2, 3, 24, 8) is None  # truncated data


def test_x11_capture_falls_back_without_xlib(monkeypatch):
    monkeypatch.setitem(sys.modules, "Xlib", None)
    capture = screencap.X11Capture(object(), object())
    assert capture._xlib_pixbuf(0, 0, 10, 10) is None


def test_partial_plan_reads_whole_blocks_around_the_damage():
    # 2200x1650 device pixels onto 480x360: 55 device pixels per 12 model.
    source, block, inner = partial_plan((600, 300, 640, 320), (2200, 1650), (480, 360))

    assert inner == (129, 64, 141, 71)
    assert block == (120, 60, 144, 72)
    assert source == (550, 275, 660, 330)
    # The block keeps at least one model pixel of slack around the copy.
    assert block[0] < inner[0] and inner[2] < block[2] or block[2] == 480
    # Blocks map exactly: 12 model pixels per 55 device pixels.
    assert (block[2] - block[0]) * 55 == (source[2] - source[0]) * 12


def test_partial_plan_clamps_at_the_frame_edges_and_ignores_misses():
    _source, block, inner = partial_plan(
        (2190, 1640, 2200, 1650), (2200, 1650), (480, 360)
    )
    assert block[2:] == (480, 360) and inner[2:] == (480, 360)
    assert partial_plan((2300, 0, 2400, 10), (2200, 1650), (480, 360)) is None


def test_merge_rects_joins_overlaps_and_caps_the_count():
    assert merge_rects([(0, 0, 10, 10), (5, 5, 20, 20), (50, 50, 60, 60)]) == [
        (0, 0, 20, 20),
        (50, 50, 60, 60),
    ]
    many = [(x * 100, 0, x * 100 + 10, 10) for x in range(10)]
    assert merge_rects(many, limit=8) == [(0, 0, 910, 10)]
    assert merge_rects([(5, 5, 5, 9)]) == []


class FakeXWindow:
    def __init__(self, display, window_id, rect, viewable=True):
        self.display = display
        self.id = window_id
        self.rect = rect
        self.viewable = viewable

    def get_attributes(self):
        from Xlib import X

        state = X.IsViewable if self.viewable else X.IsUnmapped
        return types.SimpleNamespace(map_state=state)

    def get_geometry(self):
        x, y, width, height = self.rect
        return types.SimpleNamespace(
            x=x, y=y, width=width, height=height, border_width=0
        )

    def damage_create(self, level):
        self.display.created.append(self.id)
        return 1000 + self.id


class FakeXDisplay:
    DAMAGE_EVENT = 91

    def __init__(self, windows):
        self.created = []
        self.subtracted = []
        self.destroyed = []
        self.events = []
        self.windows = [FakeXWindow(self, *window) for window in windows]
        root = types.SimpleNamespace(
            id=1,
            change_attributes=lambda **kwargs: None,
            query_tree=lambda: types.SimpleNamespace(children=self.windows),
        )
        self.root = root
        self.extension_event = types.SimpleNamespace(DamageNotify=self.DAMAGE_EVENT)

    def has_extension(self, name):
        return name == "DAMAGE"

    def damage_query_version(self):
        return None

    def set_error_handler(self, handler):
        self.handler = handler

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def flush(self):
        pass

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        return self.events.pop(0)

    def damage_subtract(self, damage):
        self.subtracted.append(damage)

    def damage_destroy(self, damage):
        self.destroyed.append(damage)

    def damage(self, window, x, y, width, height):
        geometry = window.get_geometry()
        self.events.append(
            types.SimpleNamespace(
                type=self.DAMAGE_EVENT,
                damage=1000 + window.id,
                area=types.SimpleNamespace(x=x, y=y, width=width, height=height),
                drawable_geometry=geometry,
            )
        )


def test_damage_tracker_reports_window_damage_clipped_to_the_monitor():
    pytest.importorskip("Xlib")
    display = FakeXDisplay([(10, (0, 0, 3000, 2000)), (11, (100, 100, 50, 50), False)])
    tracker = X11DamageTracker(display, (2000, 0, 1000, 1000))

    assert display.created == [10]  # unmapped windows are not watched
    assert tracker.pending is False
    window = display.windows[0]
    display.damage(window, 2100, 50, 30, 20)  # on the monitor
    display.damage(window, 10, 10, 30, 20)  # on another monitor
    tracker.drain()

    assert tracker.pending is True
    assert tracker.take() == [(2100, 50, 2130, 70)]
    assert tracker.pending is False
    assert display.subtracted == [1010, 1010]


def test_damage_tracker_marks_moves_with_the_shadow_margin():
    xlib = pytest.importorskip("Xlib.X")
    display = FakeXDisplay([(10, (2100, 100, 200, 100))])
    tracker = X11DamageTracker(display, (2000, 0, 1000, 1000))
    window = display.windows[0]
    display.events.append(
        types.SimpleNamespace(
            type=xlib.ConfigureNotify,
            window=window,
            x=2500,
            y=400,
            width=200,
            height=100,
            border_width=0,
        )
    )

    tracker.drain()

    margin = screencap.SHADOW_MARGIN
    assert tracker.take() == [
        (2100 - margin, 100 - margin, 2300 + margin, 200 + margin),
        (2500 - margin, 400 - margin, 2700 + margin, 500 + margin),
    ]


def test_damage_tracker_forgets_windows_reparented_into_a_frame():
    xlib = pytest.importorskip("Xlib.X")
    display = FakeXDisplay([(10, (2100, 100, 200, 100))])
    tracker = X11DamageTracker(display, (2000, 0, 1000, 1000))
    window = display.windows[0]
    display.events.append(
        types.SimpleNamespace(
            type=xlib.ReparentNotify,
            window=window,
            parent=types.SimpleNamespace(id=99),
        )
    )

    tracker.drain()

    assert display.destroyed == [1010]
    assert 10 not in tracker._windows


def test_damage_tracker_needs_the_extension():
    pytest.importorskip("Xlib")
    display = FakeXDisplay([])
    display.has_extension = lambda name: False
    with pytest.raises(screencap.CaptureError):
        X11DamageTracker(display, (0, 0, 10, 10))


def test_x11_partial_read_copies_back_only_the_damaged_pixels():
    capture = screencap.X11Capture(object(), object())
    capture._gray = bytearray(480 * 360)
    reads = []

    def fake_read(x, y, width, height):
        reads.append((x, y, width, height))
        return "pixbuf"

    capture._xlib_pixbuf = fake_read
    capture._scaled_gray = lambda pixbuf, width, height: bytes([200]) * (
        width * height
    )
    device = (5120, 1230, 2200, 1650)

    gray = capture._read_partial(device, 480, 360, [(5720, 1530, 5760, 1550)])

    assert reads == [(5120 + 550, 1230 + 275, 110, 55)]
    changed = {(i % 480, i // 480) for i, value in enumerate(gray) if value}
    assert changed == {(x, y) for x in range(129, 141) for y in range(64, 71)}


def test_x11_partial_read_gives_up_on_large_damage():
    capture = screencap.X11Capture(object(), object())
    capture._gray = bytearray(480 * 360)
    capture._xlib_pixbuf = lambda *args: pytest.fail("no read expected")
    device = (0, 0, 2200, 1650)

    assert capture._read_partial(device, 480, 360, [(0, 0, 2200, 1200)]) is None


def test_gray_rejects_bad_shapes():
    with pytest.raises(ValueError):
        gray_from_channels(b"", 0, 1, 1)
    with pytest.raises(ValueError):
        gray_from_channels(b"x", 1, 1, 2)
    with pytest.raises(ValueError):
        gray_from_channels(b"x" * 3, 1, 1, 3, stride=2)


class FakeMonitor:
    def __init__(self, model):
        self._model = model

    def get_model(self):
        return self._model


def test_pick_monitor_matches_the_paperlike_by_default():
    cf = FakeMonitor("CF791")
    dasung = FakeMonitor("Paperlike H D")

    assert pick_monitor([cf, dasung], "auto") is dasung
    assert pick_monitor([cf], "auto") is cf
    assert pick_monitor([cf, dasung], "CF791") is cf
    assert pick_monitor([FakeMonitor("a"), FakeMonitor("b")], "auto") is None
    assert pick_monitor([FakeMonitor(None)], "auto") is not None


def test_pick_monitor_matches_extra_names_before_failing():
    dell = FakeMonitor("DP-2")
    dasung = FakeMonitor("DP-1")
    aliases = [(), ("Paperlike H D",)]

    assert pick_monitor([dell, dasung], "auto") is None
    assert pick_monitor([dell, dasung], "auto", aliases) is dasung
    assert pick_monitor([dell, dasung], "Paperlike", aliases) is dasung
    # A direct Gdk match still wins over the alias.
    assert pick_monitor([dell, dasung], "DP-2", aliases) is dell


def test_edid_monitor_name_reads_the_descriptor():
    edid = bytearray(128)
    edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    edid[54:72] = b"\x00\x00\x00\xfc\x00Paperlike H D"
    assert edid_monitor_name(bytes(edid)) == "Paperlike H D"

    # A newline terminator and trailing spaces are stripped.
    edid[54:72] = b"\x00\x00\x00\xfc\x00CF7 91      \n"
    assert edid_monitor_name(bytes(edid)) == "CF7 91"

    # No name descriptor, empty and truncated data.
    edid[54:72] = b"\x00" * 18
    assert edid_monitor_name(bytes(edid)) is None
    assert edid_monitor_name(b"") is None
    assert edid_monitor_name(bytes(10)) is None


class GeoMonitor:
    def __init__(self, x, y, width, height, scale=1):
        self._geometry = types.SimpleNamespace(
            x=x, y=y, width=width, height=height
        )
        self._scale = scale

    def get_geometry(self):
        return self._geometry

    def get_scale_factor(self):
        return self._scale


def test_x11_monitor_aliases_require_a_geometry_match(monkeypatch):
    monkeypatch.setattr(
        screencap,
        "x11_edid_monitor_names",
        lambda: {(3760, 533, 942, 1256): "Paperlike H D"},
    )
    dasung = GeoMonitor(3760, 533, 942, 1256)
    dell = GeoMonitor(1200, 193, 2560, 1440)

    assert x11_monitor_aliases([dasung, dell]) == [("Paperlike H D",), ()]

    # Without EDID names (no xlib, no property) no monitor gets aliases.
    monkeypatch.setattr(screencap, "x11_edid_monitor_names", lambda: {})
    assert x11_monitor_aliases([dasung, dell]) == []


def test_x11_monitor_aliases_follow_the_window_scale(monkeypatch):
    # Cinnamon's fractional scaling in scale-ui-down mode: Gdk reports
    # application pixels while RandR keys the EDID names by the doubled
    # device rectangle.
    monkeypatch.setattr(
        screencap,
        "x11_edid_monitor_names",
        lambda: {(7520, 1098, 2512, 1884): "Paperlike H D"},
    )
    dasung = GeoMonitor(3760, 549, 1256, 942, scale=2)

    assert x11_monitor_aliases([dasung]) == [("Paperlike H D",)]


class OutputMonitor:
    """Gdk monitor stand-in with a model name and a geometry."""

    def __init__(self, model, x, y, width, height, scale=1):
        self._model = model
        self._geometry = types.SimpleNamespace(
            x=x, y=y, width=width, height=height
        )
        self._scale = scale

    def get_model(self):
        return self._model

    def get_geometry(self):
        return self._geometry

    def get_scale_factor(self):
        return self._scale


class FakeGdkDisplay:
    def __init__(self, monitors):
        self._monitors = monitors

    def get_n_monitors(self):
        return len(self._monitors)

    def get_monitor(self, index):
        return self._monitors[index]


def _gdk(monitors):
    """A fake Gdk module whose default display lists `monitors`."""

    display = FakeGdkDisplay(monitors) if monitors is not None else None
    return types.SimpleNamespace(
        Display=types.SimpleNamespace(get_default=lambda: display)
    )


def test_monitor_output_present_follows_the_edid_names(monkeypatch):
    monkeypatch.setattr(
        screencap,
        "x11_edid_monitor_names",
        lambda: {
            (0, 0, 2560, 1440): "DELL S2725DS",
            (2560, 0, 942, 1256): "Paperlike H D",
        },
    )
    dell = OutputMonitor("DP-2", 0, 0, 2560, 1440)
    dasung = OutputMonitor("DP-1", 2560, 0, 942, 1256)

    assert monitor_output_present(_gdk([dell, dasung])) is True
    # The panel's output is gone: the desktop monitor alone is not the panel.
    assert monitor_output_present(_gdk([dell])) is False
    # A configured ghost.output matches the Gdk output name directly.
    assert monitor_output_present(_gdk([dell]), wanted="DP-2") is True
    assert monitor_output_present(_gdk([dell]), wanted="HDMI-0") is False


def test_monitor_output_present_handles_a_scaled_session(monkeypatch):
    # Cinnamon's fractional scaling in scale-ui-down mode doubles the
    # framebuffer: the RandR rectangles are twice the Gdk geometries.
    monkeypatch.setattr(
        screencap,
        "x11_edid_monitor_names",
        lambda: {
            (0, 0, 5120, 2880): "DELL S2725DS",
            (7520, 1098, 2512, 1884): "Paperlike H D",
        },
    )
    dell = OutputMonitor("DP-2", 0, 0, 2560, 1440, scale=2)
    dasung = OutputMonitor("DP-1", 3760, 549, 1256, 942, scale=2)

    # The panel is on even though its Gdk geometry is in application pixels.
    assert monitor_output_present(_gdk([dell, dasung])) is True
    # Its absence is still seen on the remaining monitor.
    assert monitor_output_present(_gdk([dell])) is False


def test_monitor_output_present_gives_up_when_it_cannot_tell(monkeypatch):
    monkeypatch.setattr(screencap, "x11_edid_monitor_names", lambda: {})
    dell = OutputMonitor("DP-2", 0, 0, 2560, 1440)

    # Without EDID names `auto` cannot identify the panel...
    assert monitor_output_present(_gdk([dell])) is None
    # ...but a named output still matches the Gdk model.
    assert monitor_output_present(_gdk([dell]), wanted="DP-2") is True
    assert monitor_output_present(_gdk([])) is None
    assert monitor_output_present(_gdk(None)) is None

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(screencap, "drm_output_names", lambda root=None: {})
    assert monitor_output_present(_gdk([dell])) is None


# -- DRM sysfs outputs (the Wayland presence check) ---------------------------


def fake_edid(name: str) -> bytes:
    edid = bytearray(128)
    edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    padded = name.encode("latin-1")[:13].ljust(13, b" ")
    edid[54:72] = b"\x00\x00\x00\xfc\x00" + padded
    return bytes(edid)


def fake_drm(tmp_path, outputs):
    """Build a fake /sys/class/drm tree: (entry, status, model) tuples."""

    root = tmp_path / "drm"
    for entry, status, model in outputs:
        path = root / entry
        path.mkdir(parents=True)
        (path / "status").write_text(status + "\n", encoding="utf-8")
        (path / "edid").write_bytes(fake_edid(model) if model else b"")
    return root


def test_drm_output_present_matches_the_edid_names(tmp_path):
    root = fake_drm(
        tmp_path,
        [
            ("card2-HDMI-A-3", "connected", "CF791"),
            ("card1-DP-1", "connected", "Paperlike H D"),
            ("card1-DP-2", "disconnected", "Paperlike H D"),
        ],
    )

    assert drm_output_present(root=root) is True
    # The panel's output is gone: the desktop monitor alone is not the panel.
    only_desktop = fake_drm(
        tmp_path / "desk", [("card2-HDMI-A-3", "connected", "CF791")]
    )
    assert drm_output_present(root=only_desktop) is False
    # A configured ghost.output matches the output name directly.
    assert drm_output_present(wanted="DP-1", root=root) is True
    assert drm_output_present(wanted="DP-9", root=root) is False


def test_drm_output_present_gives_up_when_it_cannot_tell(tmp_path):
    # No readable DRM entries at all.
    assert drm_output_present(root=tmp_path / "missing") is None
    # A connected output whose EDID name cannot be read: `auto` cannot tell...
    no_edid = fake_drm(tmp_path / "noedid", [("card1-DP-1", "connected", None)])
    assert drm_output_present(root=no_edid) is None
    # ...but a named output still matches the output name.
    assert drm_output_present(wanted="DP-1", root=no_edid) is True
    # Disconnected connectors are not candidates; with no connected output
    # at all the check cannot tell and keeps the serial-only behaviour.
    off = fake_drm(tmp_path / "off", [("card1-DP-1", "disconnected", "Paperlike")])
    assert drm_output_present(root=off) is None
    assert drm_output_present(wanted="DP-1", root=off) is None


def test_monitor_output_present_uses_the_drm_check_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        screencap, "drm_output_names", lambda root=None: {"DP-1": "Paperlike H D"}
    )

    assert monitor_output_present(_gdk(None)) is True

    monkeypatch.setattr(screencap, "drm_output_names", lambda root=None: {})
    assert monitor_output_present(_gdk(None)) is None


def test_restore_token_round_trip(tmp_path):
    path = tmp_path / "screencast.json"

    assert load_restore_token(path) is None
    save_restore_token("tok-123", path)

    assert json.loads(path.read_text()) == {"restore_token": "tok-123"}
    assert load_restore_token(path) == "tok-123"


def test_restore_token_corrupt_or_wrong_type(tmp_path):
    path = tmp_path / "screencast.json"
    path.write_text("{not json")
    assert load_restore_token(path) is None
    path.write_text(json.dumps({"restore_token": 42}))
    assert load_restore_token(path) is None
    path.write_text(json.dumps({"restore_token": ""}))
    assert load_restore_token(path) is None
