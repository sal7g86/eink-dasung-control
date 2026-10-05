"""Screen capture helpers and token persistence; no display and no hardware."""

import json
import types

import pytest

from dasungctl import screencap
from dasungctl.screencap import (
    edid_monitor_name,
    gray_from_channels,
    load_restore_token,
    monitor_output_present,
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


def test_gray_from_gray_and_padded_stride():
    assert gray_from_channels(bytes([1, 2, 3]), 3, 1, 1) == bytes([1, 2, 3])
    # Two rows of two pixels, each row padded to four bytes.
    data = bytes([10, 20, 0, 0, 30, 40, 0, 0])
    assert gray_from_channels(data, 2, 2, 1, stride=4) == bytes([10, 20, 30, 40])
    # RGB rows with padding: one pixel per row, three channels plus one byte.
    data = bytes([10, 20, 30, 0, 40, 50, 60, 0])
    assert gray_from_channels(data, 1, 2, 3, stride=4) == bytes([20, 50])


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
    def __init__(self, x, y, width, height):
        self._geometry = types.SimpleNamespace(
            x=x, y=y, width=width, height=height
        )

    def get_geometry(self):
        return self._geometry


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


class OutputMonitor:
    """Gdk monitor stand-in with a model name and a geometry."""

    def __init__(self, model, x, y, width, height):
        self._model = model
        self._geometry = types.SimpleNamespace(
            x=x, y=y, width=width, height=height
        )

    def get_model(self):
        return self._model

    def get_geometry(self):
        return self._geometry


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
    assert monitor_output_present(_gdk([dell])) is None


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
