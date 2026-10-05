"""Tests for the per-field apply logic shared by tray and state restore."""

from dasungctl import controls
from dasungctl.client import DasungClient, MonitorInfo
from dasungctl.errors import KIND_CONNECTION, SEVERITY_ERROR
from dasungctl.protocol import CUSTOM_FRONTLIGHT_MODE
from dasungctl.transport import TransportError

from fakes import FakeTransport


def _info(**overrides) -> MonitorInfo:
    """A fully populated MonitorInfo; override individual fields as needed."""

    base = dict(
        protocol_version=0x30,
        additional_version_field=0x10,
        contrast=6,
        mode=4,
        selector_03=1,
        speed=4,
        frontlight_mode=2,
        temperature=0,
        frontlight=40,
        mux=1,
        selector_11=2,
    )
    base.update(overrides)
    return MonitorInfo(**base)


class State:
    """Minimal state object exposing info/message, like the tray state."""

    def __init__(self, **overrides):
        self.info = _info(**overrides)
        self.message = ""
        self.custom_temperature: int | None = None


def _commands(transport):
    return [bytes.fromhex(request.decode("ascii"))[2] for request in transport.requests]


class BrokenTransport:
    def exchange(self, request):
        raise TransportError("no answer")

    def send(self, request):
        raise TransportError("no answer")

    def receive(self):
        raise TransportError("no answer")


def test_manual_temperature_also_selects_custom_frontlight_mode():
    transport = FakeTransport()
    state = State(frontlight_mode=0)

    controls.set_temperature(DasungClient(transport), state, 140)

    assert state.info.temperature == 140
    assert state.custom_temperature == 140
    assert state.info.frontlight_mode == CUSTOM_FRONTLIGHT_MODE
    assert _commands(transport)[-2:] == [0x08, 0x07]
    assert "frontlight mode custom" in state.message


def test_frontlight_presets_send_the_temperature_first():
    transport = FakeTransport()
    state = State(frontlight_mode=0)

    controls.set_frontlight_mode(DasungClient(transport), state, 1)

    assert state.info.temperature == 100
    assert state.info.frontlight_mode == 1
    assert _commands(transport)[-2:] == [0x08, 0x07]
    assert "temperature 100" in state.message


def test_off_preset_keeps_the_stored_balance():
    transport = FakeTransport()
    state = State(frontlight_mode=2, temperature=70)

    controls.set_frontlight_mode(DasungClient(transport), state, 0)

    assert state.info.temperature == 70
    assert _commands(transport)[-1] == 0x07
    assert "temperature unchanged" in state.message


def test_custom_mode_reuses_the_last_manual_temperature():
    transport = FakeTransport()
    state = State(frontlight_mode=0)
    state.custom_temperature = 111

    controls.set_frontlight_mode(DasungClient(transport), state, CUSTOM_FRONTLIGHT_MODE)

    assert state.info.temperature == 111
    assert state.info.frontlight_mode == CUSTOM_FRONTLIGHT_MODE


def test_apply_field_updates_plain_fields_and_their_labels():
    for name, value in (("contrast", 3), ("speed", 2), ("frontlight", 35)):
        transport = FakeTransport()
        state = State()

        assert controls.apply_field(DasungClient(transport), state, name, value)

        assert getattr(state.info, name) == value
        assert f"set to {value}" in state.message


def test_apply_field_validates_plain_field_ranges():
    transport = FakeTransport()
    state = State()

    assert not controls.apply_field(DasungClient(transport), state, "contrast", 99)

    assert state.info.contrast == 6
    assert state.severity == SEVERITY_ERROR
    assert "contrast must be" in state.message
    assert state.error_detail


def test_apply_field_mode_reports_the_name():
    transport = FakeTransport()
    state = State()

    assert controls.apply_field(DasungClient(transport), state, "mode", 2)

    assert state.info.mode == 2
    assert state.message == "mode set to text"


def test_write_errors_are_reported_without_raising():
    state = State()

    applied = controls.apply_field(
        DasungClient(BrokenTransport()), state, "temperature", 100
    )

    assert applied is False
    assert state.severity == SEVERITY_ERROR
    assert state.error_kind == KIND_CONNECTION
    assert state.error_detail == "no answer"
    assert state.info.temperature == 0
