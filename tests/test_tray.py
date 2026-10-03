"""Tray controller and menu-helper tests; never touches a serial device."""

from contextlib import nullcontext
import signal
import sys
import threading
import time
import types

from dasungctl.config import Config
from dasungctl.ghostwatch import GhostElement, GhostResult
from dasungctl.logfile import NullLog
from dasungctl.protocol import CUSTOM_FRONTLIGHT_MODE, FrontlightMode
from dasungctl.state import StateError
from dasungctl.zoneclear import DEFAULT_CLEAR
from dasungctl.tray import (
    FRONTLIGHT_PRESETS,
    FRONTLIGHT_STEP,
    ICON_OK,
    ICON_WARNING,
    POLL_SECONDS,
    TEMPERATURE_LEVEL_PRESETS,
    TEMPERATURE_LEVELS,
    TEST_FLASH_FRACTION,
    TrayApp,
    TrayController,
    _install_signal_handlers,
    autostart_entry,
    autostart_path,
    frontlight_label,
    gdk_noise,
    install_gdk_log_filter,
    nearest_preset,
    status_symbol,
    temperature_byte,
    temperature_level,
    temperature_level_number,
)
from dasungctl.transport import TransportError
from dasungctl.tray_windows import (
    _bring_to_current_desktop,
    _stick_on_map,
    _stick_to_all_desktops,
    _value_text,
)

from fakes import RESPONSES, FakeTransport


class FakeSession:
    """Context manager returning the shared FakeTransport from __enter__."""

    def __init__(self, transport):
        self.transport = transport

    def __enter__(self):
        return self.transport

    def __exit__(self, *args):
        return False


def _controller(transport=None, **kwargs):
    """A controller wired to the shared FakeTransport, without locking."""

    transport = transport or FakeTransport()
    controller = TrayController(
        Config(),
        opener=lambda: FakeSession(transport),
        locker=lambda: nullcontext(),
        **kwargs,
    )
    return controller, transport


def _commands(transport):
    """Frame type byte of every request: 0x0A marks a read, else the command."""

    return [bytes.fromhex(request.decode("ascii"))[2] for request in transport.requests]


def test_read_populates_the_state_from_the_monitor():
    controller, _transport = _controller()

    assert controller.read() is True

    assert controller.state.info.mode == 4
    assert controller.state.info.contrast == 6
    assert controller.state.mode_name == "video"
    assert controller.state.message == "reloaded"


def test_read_requests_only_the_displayed_fields():
    controller, transport = _controller()

    assert controller.read() is True

    selectors = [
        bytes.fromhex(request.decode("ascii"))[3]
        for request in transport.requests
    ]
    # contrast, mode, speed, frontlight mode, temperature, frontlight
    assert selectors == [0x01, 0x02, 0x04, 0x07, 0x08, 0x09]


def test_sync_mirrors_physical_changes_and_stores_them(monkeypatch):
    stored = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last", lambda info, **kwargs: stored.append(info)
    )
    controller, transport = _controller()
    assert controller.read() is True
    assert controller.state.info.mode == 4

    # The user pressed M on the monitor: mode text, speed down to 1.
    transport.responses = dict(RESPONSES)
    transport.responses[b"5FF50A02000000000000A0FA"] = b"5FF5F00A020200000000A0FA"
    transport.responses[b"5FF50A04000000000000A0FA"] = b"5FF5F00A040100000000A0FA"

    assert controller.sync() is True

    assert controller.state.info.mode == 2
    assert controller.state.info.speed == 1
    assert controller.state.message.startswith("monitor changed")
    assert stored and stored[-1].mode == 2


def test_sync_keeps_values_whose_selector_does_not_answer():
    controller, transport = _controller()
    assert controller.read() is True
    assert controller.state.info.contrast == 6

    transport.responses = dict(RESPONSES)
    del transport.responses[b"5FF50A01000000000000A0FA"]
    transport.responses[b"5FF50A02000000000000A0FA"] = b"5FF5F00A020200000000A0FA"

    assert controller.sync() is True

    assert controller.state.info.contrast == 6
    assert controller.state.info.mode == 2


def test_sync_reports_no_change_when_the_monitor_matches():
    controller, _transport = _controller()
    assert controller.read() is True

    assert controller.sync() is False


def test_sync_keeps_custom_when_the_firmware_answers_mixed():
    controller, transport = _controller()
    assert controller.read() is True
    assert controller.apply("temperature", 56) is True
    assert controller.state.info.frontlight_mode == CUSTOM_FRONTLIGHT_MODE

    transport.responses = dict(RESPONSES)
    transport.responses[b"5FF50A07000000000000A0FA"] = b"5FF5F00A070300000000A0FA"
    transport.responses[b"5FF50A08000000000000A0FA"] = b"5FF5F00A083800000000A0FA"

    assert controller.sync() is False

    assert controller.state.info.frontlight_mode == CUSTOM_FRONTLIGHT_MODE
    assert controller.state.info.temperature == 56


def test_sync_reports_mixed_when_the_temperature_is_not_the_custom_one():
    controller, transport = _controller()
    assert controller.read() is True
    assert controller.apply("temperature", 89) is True

    transport.responses = dict(RESPONSES)
    transport.responses[b"5FF50A07000000000000A0FA"] = b"5FF5F00A070300000000A0FA"
    transport.responses[b"5FF50A08000000000000A0FA"] = b"5FF5F00A084600000000A0FA"

    assert controller.sync() is True

    assert controller.state.info.frontlight_mode == int(FrontlightMode.MIXED)
    assert controller.state.info.temperature == 70
    assert "frontlight mode mixed" in controller.state.message


def test_poll_due_waits_for_the_interval():
    now = [0.0]
    controller, _transport = _controller(clock=lambda: now[0])

    assert controller.poll_due() is False
    now[0] = POLL_SECONDS
    assert controller.poll_due() is True


def test_apply_uses_the_shared_control_logic():
    controller, transport = _controller()
    controller.read()

    assert controller.apply("contrast", 3) is True
    assert controller.state.info.contrast == 3
    assert "set to 3" in controller.state.message
    assert _commands(transport)[-1] == 0x01


def test_apply_stores_the_last_configuration(monkeypatch):
    stored = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last", lambda info, **kwargs: stored.append(info)
    )
    controller, _transport = _controller()
    controller.read()

    assert controller.apply("contrast", 3) is True

    assert stored[-1].contrast == 3


def test_failed_apply_does_not_store_the_last_configuration():
    def broken_opener():
        raise TransportError("monitor busy")

    controller = TrayController(
        Config(),
        opener=broken_opener,
        locker=lambda: nullcontext(),
    )

    assert controller.apply("contrast", 3) is False
    assert controller.state.message.startswith("error:")


def test_apply_does_not_rewrite_an_identical_last_configuration(monkeypatch):
    stored = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last", lambda info, **kwargs: stored.append(info)
    )
    controller, _transport = _controller()
    controller.read()

    assert controller.apply("contrast", 3) is True
    assert len(stored) == 1

    # Re-selecting the current value must not touch the state file again.
    assert controller.apply("contrast", 3) is True
    assert len(stored) == 1


def test_apply_reports_when_the_last_configuration_cannot_be_stored(monkeypatch):
    def broken_store(_info, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dasungctl.tray.save_last", broken_store)
    controller, _transport = _controller()

    assert controller.apply("contrast", 3) is True

    assert "not saved: disk full" in controller.state.message


def test_soft_and_hard_refresh_consume_the_expected_frames():
    controller, transport = _controller()

    assert controller.refresh(hard=False) is True
    assert controller.refresh(hard=True) is True

    refreshes = [
        request for request in transport.requests
        if bytes.fromhex(request.decode("ascii"))[2] == 0x03
    ]
    assert bytes.fromhex(refreshes[-2].decode("ascii"))[3] == 0
    assert bytes.fromhex(refreshes[-1].decode("ascii"))[3] == 1


def test_auto_refresh_waits_for_the_interval():
    now = [100.0]
    controller, _transport = _controller(clock=lambda: now[0], interval=100.0)

    assert controller.due() is False
    controller.set_autorefresh(True)
    assert controller.state.last_refresh == 100.0
    assert controller.due(199.0) is False
    assert controller.due(200.0) is True
    assert controller.tick(200.0) is True
    assert controller.state.last_refresh == 100.0  # clock() did not advance
    assert controller.state.message == "soft refresh sent"
    controller.set_autorefresh(False)
    assert controller.tick(1000.0) is False


def test_config_can_make_the_timer_use_the_hard_refresh():
    transport = FakeTransport()
    controller = TrayController(
        Config(autorefresh={"interval": 1, "hard": True}),
        opener=lambda: FakeSession(transport),
        locker=lambda: nullcontext(),
        clock=lambda: 0.0,
    )
    controller.set_autorefresh(True)

    assert controller.tick(1.0) is True

    refreshes = [
        request for request in transport.requests
        if bytes.fromhex(request.decode("ascii"))[2] == 0x03
    ]
    assert bytes.fromhex(refreshes[-1].decode("ascii"))[3] == 1


def test_interval_and_autorefresh_messages():
    controller, _transport = _controller()

    controller.set_interval(900)
    assert controller.state.interval == 900.0
    assert "15 min" in controller.state.message
    controller.set_autorefresh(True)
    assert controller.state.autorefresh is True
    controller.set_autorefresh(False)
    assert controller.state.autorefresh is False


def test_auto_refresh_preferences_are_stored(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last",
        lambda info, prefs=None, **kwargs: saved.append(prefs),
    )
    controller, _transport = _controller()

    controller.set_interval(30)
    controller.set_autorefresh(True)

    assert saved == [
        {
            "autorefresh": False,
            "autorefresh_interval": 30.0,
            "ghost_clear": dict(DEFAULT_CLEAR),
            "ghost_estimate": True,
        },
        {
            "autorefresh": True,
            "autorefresh_interval": 30.0,
            "ghost_clear": dict(DEFAULT_CLEAR),
            "ghost_estimate": True,
        },
    ]


def test_ghost_clear_preference_is_stored(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last",
        lambda info, prefs=None, **kwargs: saved.append(prefs),
    )
    controller, _transport = _controller()

    controller.set_ghost_clear(True)

    assert saved == [
        {
            "autorefresh": False,
            "autorefresh_interval": 300.0,
            "ghost_clear": {**DEFAULT_CLEAR, "enabled": True},
            "ghost_estimate": True,
        }
    ]
    assert controller.state.ghost_clear is True


def test_ghost_estimate_preference_is_stored(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last",
        lambda info, prefs=None, **kwargs: saved.append(prefs),
    )
    controller, _transport = _controller()

    controller.set_ghost_estimate(False)

    assert saved == [
        {
            "autorefresh": False,
            "autorefresh_interval": 300.0,
            "ghost_clear": dict(DEFAULT_CLEAR),
            "ghost_estimate": False,
        }
    ]
    assert controller.state.ghost_estimate is False
    assert "stopped" in controller.state.message


def test_ghost_clear_settings_are_merged_and_stored(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last",
        lambda info, prefs=None, **kwargs: saved.append(prefs),
    )
    controller, _transport = _controller()

    controller.set_ghost_clear(True)
    controller.set_ghost_clear_settings({"white_ms": 300, "delay": 7})

    assert saved[-1]["ghost_clear"] == {
        **DEFAULT_CLEAR,
        "enabled": True,
        "white_ms": 300,
        "delay": 7,
    }
    assert controller.state.ghost_clear_settings["delay"] == 7
    # An update that changes nothing does not rewrite the file again.
    controller.set_ghost_clear_settings({"white_ms": 300, "delay": 7})
    assert len(saved) == 2


def test_identical_auto_refresh_preferences_are_not_rewritten(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "dasungctl.tray.save_last",
        lambda info, prefs=None, **kwargs: saved.append(prefs),
    )
    controller, _transport = _controller()

    controller.set_interval(30)
    controller.set_interval(30)

    assert len(saved) == 1


def test_restore_applies_the_saved_configuration(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last", lambda **kwargs: {"mode": "text", "contrast": 3}
    )
    controller, transport = _controller()

    assert controller.restore() is True

    commands = _commands(transport)
    assert 0x02 in commands and 0x01 in commands
    assert controller.state.message == "last configuration restored"
    assert controller.state.info.mode == 4  # the monitor read follows the writes


def test_restore_brings_back_the_auto_refresh_timer(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last",
        lambda **kwargs: {"autorefresh": True, "autorefresh_interval": 5.0},
    )
    controller, _transport = _controller(clock=lambda: 1000.0)

    assert controller.restore() is True

    assert controller.state.autorefresh is True
    assert controller.state.interval == 5.0
    assert controller.state.last_refresh == 1000.0  # countdown starts at startup
    assert controller.due(1004.99) is False
    assert controller.due(1005.0) is True


def test_restore_without_preferences_keeps_the_timer_off(monkeypatch):
    monkeypatch.setattr("dasungctl.tray.load_last", lambda **kwargs: {"mode": "text"})
    controller, _transport = _controller()

    assert controller.restore() is True

    assert controller.state.autorefresh is False
    assert controller.state.interval == 300.0  # config default


def test_restore_brings_back_the_ghost_clear_switch(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last",
        lambda **kwargs: {"ghost_clear": {"enabled": True}},
    )
    controller, _transport = _controller()

    assert controller.restore() is True

    assert controller.state.ghost_clear is True
    assert controller.state.ghost_clear_settings["enabled"] is True
    assert controller.state.ghost_clear_settings["white_ms"] == 15


def test_restore_brings_back_the_ghost_estimate_switch(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last",
        lambda **kwargs: {"ghost_estimate": False},
    )
    controller, _transport = _controller()

    assert controller.restore() is True

    assert controller.state.ghost_estimate is False


def test_ghost_estimate_defaults_to_running(monkeypatch):
    monkeypatch.setattr("dasungctl.tray.load_last", lambda **kwargs: None)
    controller, _transport = _controller()

    assert controller.state.ghost_estimate is True


def test_restore_merges_the_saved_clear_settings(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last",
        lambda **kwargs: {"ghost_clear": {"enabled": True, "white_ms": 300}},
    )
    controller, _transport = _controller()

    assert controller.restore() is True

    assert controller.state.ghost_clear is True
    assert controller.state.ghost_clear_settings["white_ms"] == 300
    # Keys the saved file does not carry keep the config defaults.
    assert controller.state.ghost_clear_settings["black_ms"] == 15


def test_ghost_clear_defaults_to_the_config_switch(monkeypatch):
    disabled = Config(ghost={"clear": {"enabled": False}})
    controller = TrayController(
        disabled,
        opener=lambda: FakeSession(FakeTransport()),
        locker=lambda: nullcontext(),
    )
    plain, _transport = _controller()

    assert controller.state.ghost_clear is False
    assert plain.state.ghost_clear is True


def test_restore_seeds_the_custom_temperature_and_keeps_custom(monkeypatch):
    monkeypatch.setattr(
        "dasungctl.tray.load_last",
        lambda **kwargs: {
            "temperature": 56,
            "frontlight_mode": CUSTOM_FRONTLIGHT_MODE,
        },
    )
    controller, transport = _controller()
    transport.responses = dict(RESPONSES)
    transport.responses[b"5FF50A07000000000000A0FA"] = b"5FF5F00A070300000000A0FA"
    transport.responses[b"5FF50A08000000000000A0FA"] = b"5FF5F00A083800000000A0FA"

    assert controller.restore() is True

    assert controller.state.custom_temperature == 56
    assert controller.state.info.frontlight_mode == CUSTOM_FRONTLIGHT_MODE
    assert controller.state.info.temperature == 56


def test_restore_without_a_saved_configuration_only_reads(monkeypatch):
    monkeypatch.setattr("dasungctl.tray.load_last", lambda **kwargs: None)
    controller, transport = _controller()

    assert controller.restore() is True

    assert controller.state.message == "reloaded"
    commands = {
        bytes.fromhex(request.decode("ascii"))[2]
        for request in transport.requests
    }
    assert commands == {0x0A}


def test_restore_ignores_an_invalid_last_configuration(monkeypatch):
    def broken_load(**kwargs):
        raise StateError("bad file")

    monkeypatch.setattr("dasungctl.tray.load_last", broken_load)
    controller, _transport = _controller()

    assert controller.restore() is True

    assert controller.state.message.startswith("last configuration ignored")


def test_restore_reports_transport_failures(monkeypatch):
    monkeypatch.setattr("dasungctl.tray.load_last", lambda **kwargs: None)

    def broken_opener():
        raise TransportError("monitor busy")

    controller = TrayController(
        Config(),
        opener=broken_opener,
        locker=lambda: nullcontext(),
    )

    assert controller.restore() is False
    assert "monitor busy" in controller.state.message


def test_transport_failures_become_state_messages():
    def broken_opener():
        raise TransportError("monitor busy")

    controller = TrayController(
        Config(),
        opener=broken_opener,
        locker=lambda: nullcontext(),
    )

    assert controller.read() is False
    assert "monitor busy" in controller.state.message
    assert controller.apply("contrast", 3) is False


class _FakeState:
    message = ""


def _app_without_gtk():
    """TrayApp with only the pieces _run/_finished need, and a fake GLib."""

    app = TrayApp.__new__(TrayApp)
    app._busy = False
    app._pending = None
    app._updating = False
    app.log = NullLog()
    app.controller = types.SimpleNamespace(
        state=_FakeState(), apply=lambda *args: None
    )
    app.refresh_menu = lambda: None
    callbacks: list = []
    app.GLib = types.SimpleNamespace(
        idle_add=lambda callback: callbacks.append(callback)
    )
    return app, callbacks


def _drain(callbacks, done, timeout=5.0):
    """Run queued GLib callbacks until `done()` or the timeout."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while callbacks:
            callbacks.pop(0)()
        if done():
            return True
        time.sleep(0.01)
    return False


def test_action_during_a_busy_monitor_is_queued_not_dropped():
    app, callbacks = _app_without_gtk()
    release = threading.Event()
    ran = []

    def slow():
        release.wait(5)
        ran.append("slow")

    def follow_up(value):
        ran.append(("follow-up", value))

    app._run(slow)
    app._run(follow_up, 6)  # arrives while the monitor is busy

    assert app._pending is not None

    release.set()
    assert _drain(callbacks, lambda: ran == ["slow", ("follow-up", 6)])

    assert ran == ["slow", ("follow-up", 6)]
    assert app._pending is None
    assert app._busy is False


def test_only_the_latest_queued_action_runs():
    app, callbacks = _app_without_gtk()
    release = threading.Event()
    ran = []

    def slow():
        release.wait(5)

    app._run(slow)
    app._run(lambda value: ran.append(value), 1)
    app._run(lambda value: ran.append(value), 2)

    release.set()
    assert _drain(callbacks, lambda: ran == [2])

    assert ran == [2]


class _FakeItem:
    """Menu item double with just the active flag used by handlers."""

    def __init__(self, active: bool) -> None:
        self.active = active

    def get_active(self) -> bool:
        return self.active

    def set_active(self, active: bool) -> None:
        self.active = active


def test_radio_toggled_applies_only_the_newly_active_item():
    app, _callbacks = _app_without_gtk()
    calls = []
    app._run = lambda *args: calls.append(args)

    app._radio_toggled(_FakeItem(False), "contrast", 6)  # old item turning off
    assert calls == []

    app._radio_toggled(_FakeItem(True), "contrast", 5)
    assert calls == [(app.controller.apply, "contrast", 5)]


def test_radio_toggled_ignores_refresh_synchronization():
    app, _callbacks = _app_without_gtk()
    calls = []
    app._run = lambda *args: calls.append(args)

    app._updating = True  # refresh_menu() set_active() emits "toggled" too
    app._radio_toggled(_FakeItem(True), "contrast", 5)

    assert calls == []


def test_interval_selection_only_applies_the_checked_entry():
    app, _callbacks = _app_without_gtk()
    chosen = []
    app.controller.set_interval = chosen.append

    app._updating = True
    app._on_interval(_FakeItem(True), 900)
    assert chosen == []

    app._updating = False
    app._on_interval(_FakeItem(False), 900)
    assert chosen == []

    app._on_interval(_FakeItem(True), 900)
    assert chosen == [900]


def test_nearest_preset_picks_the_closest_entry():
    presets = ((0, "Off"), (60, "Low"), (125, "Medium"), (190, "High"))

    assert nearest_preset(presets, 0) == (0, "Off")
    assert nearest_preset(presets, 40) == (60, "Low")
    assert nearest_preset(presets, 150) == (125, "Medium")
    assert nearest_preset(presets, 255) == (190, "High")
    assert nearest_preset(presets, None) is None


def test_status_symbol_classifies_the_last_message():
    assert status_symbol("error: monitor busy")[1] == "dasung-error"
    assert status_symbol("Temperature set to 140; custom mode failed: x")[1] == (
        "dasung-error"
    )
    assert status_symbol("working…")[1] == ""
    assert status_symbol("reloaded") == (ICON_OK, "dasung-ok")
    assert status_symbol("last configuration ignored: bad file")[0] == ICON_WARNING
    assert status_symbol("set to 3 (not saved: disk full)")[0] == ICON_WARNING


def test_gdk_noise_matches_only_the_known_gtk_critical():
    known = (
        "gdk_window_thaw_toplevel_updates: assertion "
        "'window->update_and_descendants_freeze_count > 0' failed"
    )

    assert gdk_noise(known) is True
    assert gdk_noise("some other gdk critical") is False
    assert gdk_noise("") is False


class _FakeGLib:
    """Captures the log handler and the messages forwarded to the default."""

    class LogLevelFlags:
        LEVEL_CRITICAL = 8

    def __init__(self):
        self.handler = None
        self.domain = None
        self.levels = None
        self.forwarded = []

    def log_set_handler(self, domain, levels, func, user_data):
        self.domain = domain
        self.levels = levels
        self.handler = func
        return 1

    def log_default_handler(self, domain, level, message, _user_data):
        self.forwarded.append((domain, level, message))


def test_gdk_log_filter_drops_only_the_known_message():
    glib = _FakeGLib()

    install_gdk_log_filter(glib)

    assert glib.domain == "Gdk"
    assert glib.levels == _FakeGLib.LogLevelFlags.LEVEL_CRITICAL
    glib.handler(
        "Gdk",
        8,
        "gdk_window_thaw_toplevel_updates: assertion 'x > 0' failed",
        None,
    )
    glib.handler("Gdk", 8, "a genuine gdk error", None)

    assert glib.forwarded == [("Gdk", 8, "a genuine gdk error")]


def test_menu_presets_stay_within_range_and_on_the_step_grid():
    assert all(0 <= value <= 100 for value, _label in FRONTLIGHT_PRESETS)
    assert all(
        value % FRONTLIGHT_STEP == 0 for value, _label in FRONTLIGHT_PRESETS
    )
    assert all(0 <= value <= 255 for value in TEMPERATURE_LEVELS)
    assert list(TEMPERATURE_LEVELS) == sorted(TEMPERATURE_LEVELS, reverse=True)


def test_frontlight_levels_match_the_measured_scale():
    assert list(FRONTLIGHT_PRESETS) == [
        (0, "Off"),
        (10, "1"),
        (20, "2"),
        (30, "3"),
        (40, "4"),
        (50, "5"),
        (60, "6"),
        (70, "7"),
        (80, "8"),
        (90, "9"),
        (100, "10"),
    ]


def test_frontlight_label_maps_raw_bytes_to_the_calibrated_level():
    assert frontlight_label(0) == "Off"
    assert frontlight_label(10) == "1"
    assert frontlight_label(30) == "3"
    assert frontlight_label(100) == "10"
    assert frontlight_label(125) == "10"
    assert frontlight_label(255) == "10"
    assert frontlight_label(None) is None


def test_value_text_labels_each_slider_with_its_own_scale():
    assert _value_text("frontlight", 0) == "Off"
    assert _value_text("frontlight", 20) == "2"
    assert _value_text("frontlight", 100) == "10"
    assert _value_text("speed", 4) == "Fast+++"
    assert _value_text("contrast", 5) == "5"
    assert _value_text("temperature", 4) == "4"


class _FakeDisplayError(Exception):
    """Stand-in for `Xlib.error.DisplayError` in the tests."""


class _FakeXWindow:
    """The window an EWMH client message is addressed to."""


class _FakeXRoot:
    def __init__(self):
        self.current_desktop = 0
        self.events = []

    def get_full_property(self, atom, type_atom):
        if self.current_desktop is None:
            return None
        return types.SimpleNamespace(value=[self.current_desktop])

    def send_event(self, event, event_mask):
        self.events.append((event, event_mask))


class _FakeXConnection:
    """python-xlib stand-in recording EWMH client messages and reads."""

    def __init__(self):
        self.target = _FakeXWindow()
        self.root = _FakeXRoot()
        self.xid = None
        self.synced = False
        self.closed = False

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def intern_atom(self, name):
        return {"_NET_WM_DESKTOP": 0xD00D, "_NET_CURRENT_DESKTOP": 0xC0DE}[name]

    def create_resource_object(self, kind, xid):
        assert kind == "window"
        self.xid = xid
        return self.target

    def sync(self):
        self.synced = True

    def close(self):
        self.closed = True


def _install_fake_xlib(monkeypatch, display_factory):
    """Install a python-xlib stand-in whose Display() runs `display_factory`."""

    fake = types.ModuleType("Xlib")
    fake.X = types.SimpleNamespace(
        SubstructureRedirectMask=1, SubstructureNotifyMask=2
    )
    fake.Xatom = types.SimpleNamespace(CARDINAL=0xCA4D)
    fake.display = types.SimpleNamespace(Display=display_factory)
    fake.error = types.SimpleNamespace(DisplayError=_FakeDisplayError)
    fake.protocol = types.SimpleNamespace(
        event=types.SimpleNamespace(
            ClientMessage=lambda **kwargs: types.SimpleNamespace(**kwargs)
        )
    )
    monkeypatch.setitem(sys.modules, "Xlib", fake)


def _fake_window(xid=0x1234):
    return types.SimpleNamespace(
        get_window=lambda: types.SimpleNamespace(get_xid=lambda: xid)
    )


def test_windows_are_stuck_to_every_desktop_through_the_ewmh_message(monkeypatch):
    connection = _FakeXConnection()
    _install_fake_xlib(monkeypatch, lambda: connection)

    assert _stick_to_all_desktops(_fake_window()) is True

    event, mask = connection.root.events[0]
    assert event.client_type == 0xD00D  # _NET_WM_DESKTOP
    assert event.window is connection.target
    assert event.data == (32, [0xFFFFFFFF, 0, 0, 0, 0])
    assert mask == 3  # redirect | notify
    assert connection.xid == 0x1234
    assert connection.synced is True
    assert connection.closed is True


def test_sticky_is_a_noop_without_python_xlib(monkeypatch):
    monkeypatch.setitem(sys.modules, "Xlib", None)

    assert _stick_to_all_desktops(_fake_window()) is False


def test_sticky_ignores_a_window_without_an_xid():
    wayland_like = types.SimpleNamespace(get_window=lambda: types.SimpleNamespace())
    unmapped = types.SimpleNamespace(get_window=lambda: None)

    assert _stick_to_all_desktops(wayland_like) is False
    assert _stick_to_all_desktops(unmapped) is False


def test_sticky_survives_an_unreachable_display(monkeypatch):
    def explode():
        raise _FakeDisplayError("no display")

    _install_fake_xlib(monkeypatch, explode)

    assert _stick_to_all_desktops(_fake_window()) is False


def test_map_handler_sticks_and_keeps_the_gtk_event_flow(monkeypatch):
    from dasungctl import tray_windows

    seen = []
    monkeypatch.setattr(tray_windows, "_stick_to_all_desktops", seen.append)

    assert _stick_on_map("window", None) is False
    assert seen == ["window"]


def test_requested_window_moves_to_the_current_desktop_then_sticks(monkeypatch):
    connection = _FakeXConnection()
    connection.root.current_desktop = 2
    _install_fake_xlib(monkeypatch, lambda: connection)

    assert _bring_to_current_desktop(_fake_window()) is True

    first, second = connection.root.events
    assert first[0].data == (32, [2, 0, 0, 0, 0])
    assert second[0].data == (32, [0xFFFFFFFF, 0, 0, 0, 0])
    assert first[0].client_type == second[0].client_type == 0xD00D
    assert connection.closed is True


def test_bring_to_current_needs_a_current_desktop(monkeypatch):
    connection = _FakeXConnection()
    connection.root.current_desktop = None
    _install_fake_xlib(monkeypatch, lambda: connection)

    assert _bring_to_current_desktop(_fake_window()) is False
    assert connection.root.events == []


def test_bring_to_current_is_a_noop_without_python_xlib(monkeypatch):
    monkeypatch.setitem(sys.modules, "Xlib", None)

    assert _bring_to_current_desktop(_fake_window()) is False


def test_bring_to_current_ignores_windows_without_an_xid():
    wayland_like = types.SimpleNamespace(get_window=lambda: types.SimpleNamespace())
    unmapped = types.SimpleNamespace(get_window=lambda: None)

    assert _bring_to_current_desktop(wayland_like) is False
    assert _bring_to_current_desktop(unmapped) is False


def test_temperature_levels_span_cold_to_warm_with_ten_radio_labels():
    assert TEMPERATURE_LEVELS == (100, 89, 78, 67, 56, 44, 33, 22, 11, 0)
    assert len(TEMPERATURE_LEVEL_PRESETS) == 10
    assert TEMPERATURE_LEVEL_PRESETS[0] == (100, "1 (cold)")
    assert TEMPERATURE_LEVEL_PRESETS[-1] == (0, "10 (warm)")
    assert [label for _value, label in TEMPERATURE_LEVEL_PRESETS[1:-1]] == [
        str(level) for level in range(2, 10)
    ]


def test_temperature_level_maps_a_byte_to_the_nearest_grid_value():
    assert temperature_level(None) is None
    assert temperature_level(100) == 100
    assert temperature_level(0) == 0
    assert temperature_level(99) == 100
    assert temperature_level(70) == 67
    assert temperature_level(255) == 100


def test_temperature_byte_maps_the_ten_levels_to_the_firmware_scale():
    assert temperature_byte(1) == 100
    assert temperature_byte(4) == 67
    assert temperature_byte(10) == 0
    assert temperature_byte(0) == 100
    assert temperature_byte(11) == 0


def test_temperature_level_number_maps_a_byte_back_to_its_level():
    assert temperature_level_number(None) is None
    assert temperature_level_number(100) == 1
    assert temperature_level_number(70) == 4
    assert temperature_level_number(0) == 10
    assert temperature_level_number(255) == 1


def test_autostart_entry_uses_the_system_python_and_source_tree(
    monkeypatch, tmp_path
):
    from dasungctl import tray

    monkeypatch.setattr(tray.Path, "home", classmethod(lambda cls: tmp_path))

    entry = autostart_entry()

    assert "--module dasungctl.tray" in entry
    assert "_source_run.py" in entry
    assert "PYTHONPATH=" not in entry
    assert "X-GNOME-Autostart-enabled=true" in entry


def test_autostart_entry_prefers_a_launcher_in_local_bin(monkeypatch, tmp_path):
    from dasungctl import tray

    launcher = tmp_path / ".local" / "bin" / "dasungctl"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")
    launcher.chmod(0o755)
    monkeypatch.setattr(tray.Path, "home", classmethod(lambda cls: tmp_path))

    entry = tray.autostart_entry()

    assert f"Exec={launcher} tray" in entry
    assert "PYTHONPATH=" not in entry


def test_autostart_entry_quotes_arguments_with_spaces(monkeypatch, tmp_path):
    from dasungctl import tray

    source = tmp_path / "checkout dir" / "src"
    runner = (source / "_source_run.py").resolve()
    monkeypatch.setattr(tray.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(tray, "__file__", str(source / "tray.py"))
    python = tmp_path / "venv dir" / "python3"
    monkeypatch.setattr(tray.shutil, "which", lambda _name: str(python))

    entry = tray.autostart_entry()

    assert f'"{python.resolve()}" "{runner}" --module dasungctl.tray' in entry


def test_autostart_path_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert autostart_path() == tmp_path / "autostart" / "dasungctl-tray.desktop"


# -- zone clearing (TrayApp's waves and auto policy) --------------------------


class _FakeClearer:
    def __init__(self, available=True):
        self.available = available
        self.busy = False
        self.calls = []

    def flash(self, rects, phases, on_done):
        self.calls.append((list(rects), phases, on_done))
        self.busy = True
        return True


def _element(x=0, y=0, width=100, height=100, age=60.0):
    return GhostElement(
        x=x,
        y=y,
        width=width,
        height=height,
        severity=50,
        dark=True,
        age=age,
        app="konsole",
    )


def _ghost_app(origin=(3760, 533), elements=(), available=True):
    """TrayApp with only the pieces the clearing code needs."""

    app = TrayApp.__new__(TrayApp)
    result = GhostResult(
        level=30,
        elements=tuple(elements),
        dirty_fraction=0.1,
        changed_pixels=0,
        samples=5,
        sampled_at=100.0,
        source_width=942,
        source_height=1256,
        model_width=480,
        model_height=640,
    )
    app.watcher = types.SimpleNamespace(
        result=result,
        capturer=types.SimpleNamespace(source_origin=origin),
        idle_steps=5,
        reset_area=lambda *args: None,
    )
    app._clearer = _FakeClearer(available=available)
    app._clear_note = None
    app.log = NullLog()
    app._refresh_ghost_view = lambda: None
    app.GLib = types.SimpleNamespace(timeout_add=lambda delay, callback: None)
    app.controller = types.SimpleNamespace(
        state=types.SimpleNamespace(
            ghost_clear=False,
            ghost_clear_settings=dict(DEFAULT_CLEAR),
            ghost_estimate=True,
        )
    )
    app._ghost_window = None
    return app


def test_clear_now_flashes_every_area_in_one_wave():
    app = _ghost_app(elements=[_element(0, 0), _element(500, 600)])

    app._ghost_clear_now()

    assert len(app._clearer.calls) == 1
    rects, _phases, _on_done = app._clearer.calls[0]
    # Both rectangles, in monitor coordinates plus the captured origin.
    assert sorted(rects) == [(3760, 533, 100, 100), (4260, 1133, 100, 100)]
    assert "clearing 2 area(s)" in app._clear_note


def test_clear_now_reports_when_only_available_on_x11():
    app = _ghost_app(elements=[_element()], available=False)

    app._ghost_clear_now()

    assert app._clearer.calls == []
    assert "X11" in app._clear_note


def test_clear_now_without_areas_says_so():
    app = _ghost_app(elements=[])

    app._ghost_clear_now()

    assert app._clearer.calls == []
    assert "no ghost areas" in app._clear_note


def test_auto_clear_flashes_every_due_area_in_one_wave():
    young = _element(x=0, y=0, age=2.0)
    old_a = _element(x=200, y=200, age=30.0)
    old_b = _element(x=500, y=600, age=60.0)
    app = _ghost_app(elements=[young, old_a, old_b])

    app._maybe_ghost_clear_auto()  # switch off
    assert app._clearer.calls == []

    app.controller.state.ghost_clear = True
    app._maybe_ghost_clear_auto()

    assert len(app._clearer.calls) == 1
    rects, _phases, _on_done = app._clearer.calls[0]
    # Only the areas past the delay; the young one waits.
    assert sorted(rects) == [(3960, 733, 100, 100), (4260, 1133, 100, 100)]
    assert "auto-clearing konsole" in app._clear_note


def test_auto_clear_waits_for_the_delay():
    app = _ghost_app(elements=[_element()])
    app.controller.state.ghost_clear = True
    app.controller.state.ghost_clear_settings = {
        **dict(DEFAULT_CLEAR),
        "delay": 120.0,
    }

    app._maybe_ghost_clear_auto()

    assert app._clearer.calls == []


class _FakeWatcher:
    """Minimal GhostWatcher stand-in for the ticking tests."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.paused = False
        self.error = None
        self.capture_failed = False
        self.model = None
        self.result = None
        self.samples = 0
        self.force_base = None

    def set_force_base(self, active):
        self.force_base = active

    def set_paused(self, active):
        self.paused = bool(active)

    def due(self):
        return self.enabled and not self.paused

    def sample(self):
        self.samples += 1
        return self.result

    def maybe_alert(self):
        return None


def test_ghost_tick_honours_the_stopped_estimate():
    app = _ghost_app()
    watcher = _FakeWatcher()
    app.watcher = watcher
    app.controller.state.ghost_estimate = False

    app._ghost_tick()

    assert watcher.paused is True
    assert watcher.samples == 0

    app.controller.state.ghost_estimate = True
    app._ghost_tick()

    assert watcher.paused is False
    assert watcher.samples == 1


def test_app_set_ghost_estimate_pauses_the_watcher():
    app = _ghost_app()
    watcher = _FakeWatcher()
    app.watcher = watcher
    recorded = []
    app.controller.set_ghost_estimate = recorded.append

    app.set_ghost_estimate(False)

    assert recorded == [False]
    assert watcher.paused is True


def test_test_flash_uses_the_center_of_the_monitor():
    app = _ghost_app()

    app._ghost_test_flash()

    assert len(app._clearer.calls) == 1
    rects, phases, on_done = app._clearer.calls[0]
    size = round(min(942, 1256) * TEST_FLASH_FRACTION)
    expected_x = 3760 + (942 - size) // 2
    expected_y = 533 + (1256 - size) // 2
    assert size > 400  # clearly bigger than the old fixed 200 px square
    assert rects == [(expected_x, expected_y, size, size)]
    assert phases == ((168, 30), (None, 0))
    assert on_done is None
    assert app._clear_note == "test flash sent"


def test_test_flash_reports_when_unavailable():
    app = _ghost_app(available=False)
    app._ghost_test_flash()
    assert app._clearer.calls == []
    assert "X11" in app._clear_note

    app = _ghost_app()
    app.watcher.capturer.source_origin = None
    app._ghost_test_flash()
    assert app._clearer.calls == []
    assert "capture" in app._clear_note


# -- logging and shutdown ----------------------------------------------------


class _RecordingLog:
    """Captures the lines a real AppLog would write."""

    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("info", message))

    def warn(self, message):
        self.lines.append(("warn", message))

    def error(self, message):
        self.lines.append(("error", message))


def test_controller_records_serial_events_in_the_log():
    log = _RecordingLog()
    controller, _transport = _controller(log=log)

    controller.read()
    controller.apply("contrast", 7)
    controller.refresh(hard=False)

    assert "error" not in [level for level, _message in log.lines]
    text = " | ".join(message for _level, message in log.lines)
    assert "reloaded from the monitor" in text
    assert "applied contrast 7" in text
    assert "soft refresh sent" in text


def test_controller_logs_serial_errors():
    class Failing:
        def __enter__(self):
            raise TransportError("no monitor")

        def __exit__(self, *args):
            return False

    log = _RecordingLog()
    controller, _transport = _controller(log=log)
    controller._opener = lambda: Failing()

    assert controller.read() is False
    assert log.lines[0][0] == "error"
    assert "no monitor" in log.lines[0][1]


class _FakeGLibSources:
    PRIORITY_DEFAULT = 0

    def __init__(self):
        self.sources = []

    def unix_signal_add(self, _priority, signum, callback):
        self.sources.append((signum, callback))


def test_signal_handlers_quit_once_on_the_first_signal():
    GLib = _FakeGLibSources()
    reasons = []

    _install_signal_handlers({"GLib": GLib}, reasons.append)

    assert [signum for signum, _callback in GLib.sources] == [
        signal.SIGINT,
        signal.SIGTERM,
    ]
    for _signum, callback in GLib.sources:
        callback()
        callback()
    assert reasons == ["SIGINT"]


class _ClosableClearer(_FakeClearer):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


def test_quit_saves_the_estimate_and_closes_everything_once():
    app = _ghost_app()
    app.log = _RecordingLog()
    app._quitting = False
    saved = []
    closed = []
    quits = []
    app.watcher.save_state = lambda force=False: saved.append(force)
    app.watcher.capturer.close = lambda: closed.append(True)
    app._clearer = _ClosableClearer()
    app.Gtk = types.SimpleNamespace(main_quit=lambda: quits.append(True))

    app.quit("SIGINT")
    app.quit("SIGINT")

    assert saved == [True]
    assert closed == [True]
    assert app._clearer.closed is True
    assert quits == [True]
    assert app.log.lines[-1] == ("info", "exiting (SIGINT)")
