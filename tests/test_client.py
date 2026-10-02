"""DasungClient contract tests against the recorded frames in docs/protocol.md."""

import pytest

from dasungctl.client import DasungClient, MonitorInfo, VersionInfo
from dasungctl.protocol import DisplayMode, Parameter, ProtocolError
from dasungctl.transport import TransportError


RESPONSES = {
    b"5FF50A10000000000000A0FA": b"5FF5F00A103010000000A0FA",
    b"5FF50A01000000000000A0FA": b"5FF5F00A010600000000A0FA",
    b"5FF50A02000000000000A0FA": b"5FF5F00A020400000000A0FA",
    b"5FF50A03000000000000A0FA": b"5FF5F00A030100000000A0FA",
    b"5FF50A04000000000000A0FA": b"5FF5F00A040400000000A0FA",
    b"5FF50A07000000000000A0FA": b"5FF5F00A070200000000A0FA",
    b"5FF50A08000000000000A0FA": b"5FF5F00A080000000000A0FA",
    b"5FF50A09000000000000A0FA": b"5FF5F00A092800000000A0FA",
    b"5FF50A0B000000000000A0FA": b"5FF5F00A0B0100000000A0FA",
    b"5FF50A11000000000000A0FA": b"5FF5F00A110200000000A0FA",
}

UNCONFIRMED_RESPONSES = {
    b"5FF50A12000000000000A0FA": b"5FF5F00A120100000000A0FA",
}


class FakeTransport:
    """Answers reads from the recorded map and acknowledges every write."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = RESPONSES if responses is None else responses

    def exchange(self, request):
        self.requests.append(request)
        if request in self.responses:
            return self.responses[request]
        command = bytes.fromhex(request.decode("ascii"))[2]
        return f"5FF5F0{command:02X}000000000000A0FA".encode("ascii")

    def send(self, request):
        self.requests.append(request)

    def receive(self):
        raise TransportError("no additional frame available")


def test_individual_reads_use_confirmed_packets():
    transport = FakeTransport()
    client = DasungClient(transport)

    assert client.read_version() == VersionInfo(0x30, 0x10)
    assert client.read_contrast() == 6
    assert transport.requests == [
        b"5FF50A10000000000000A0FA",
        b"5FF50A01000000000000A0FA",
    ]


def test_info_reads_all_confirmed_parameters_in_order():
    transport = FakeTransport()
    info = DasungClient(transport).read_info()

    assert info == MonitorInfo(
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
    assert transport.requests == [
        b"5FF50A10000000000000A0FA",
        b"5FF50A01000000000000A0FA",
        b"5FF50A02000000000000A0FA",
        b"5FF50A03000000000000A0FA",
        b"5FF50A04000000000000A0FA",
        b"5FF50A07000000000000A0FA",
        b"5FF50A08000000000000A0FA",
        b"5FF50A09000000000000A0FA",
        b"5FF50A0B000000000000A0FA",
        b"5FF50A11000000000000A0FA",
    ]


def test_state_changing_methods_use_official_client_frames():
    transport = FakeTransport()
    client = DasungClient(transport)

    assert client.set_contrast(6) == b"5FF50106000000000000A0FA"
    assert client.set_mode(DisplayMode.VIDEO) == b"5FF50204000000000000A0FA"
    assert client.set_frontlight(40) == b"5FF50928000000000000A0FA"
    assert client.set_temperature(0) == b"5FF50800000000000000A0FA"
    assert client.refresh() == b"5FF50300000000000000A0FA"
    assert client.refresh(hard=True) == b"5FF50301000000000000A0FA"
    assert transport.requests == [
        b"5FF50106000000000000A0FA",
        b"5FF50204000000000000A0FA",
        b"5FF50928000000000000A0FA",
        b"5FF50800000000000000A0FA",
        b"5FF50300000000000000A0FA",
        b"5FF50301000000000000A0FA",
    ]


def test_setters_reject_values_outside_observed_ranges():
    client = DasungClient(FakeTransport())

    for method, value in (
        (client.set_contrast, 0),
        (client.set_contrast, 10),
        (client.set_mode, 0),
        (client.set_mode, 5),
        (client.set_frontlight, 256),
        (client.set_temperature, -1),
    ):
        try:
            method(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{method.__name__} accepted {value}")


def test_new_official_client_setters_use_expected_frames():
    transport = FakeTransport()
    client = DasungClient(transport)

    assert client.set_speed(3) == b"5FF50403000000000000A0FA"
    assert client.set_frontlight_mode(2) == b"5FF50702000000000000A0FA"
    assert transport.requests == [
        b"5FF50403000000000000A0FA",
        b"5FF50702000000000000A0FA",
    ]


def test_new_setters_reject_values_outside_static_ranges():
    client = DasungClient(FakeTransport())

    for method, value in (
        (client.set_speed, 0),
        (client.set_speed, 6),
        (client.set_frontlight_mode, -1),
        (client.set_frontlight_mode, 256),
    ):
        try:
            method(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{method.__name__} accepted {value}")


def test_newly_confirmed_read_selectors_use_official_client_frames():
    transport = FakeTransport()
    client = DasungClient(transport)

    assert client.read_selector(Parameter.SPEED) == 4
    assert client.read_selector(Parameter.FRONTLIGHT_MODE) == 2
    assert client.read_selector(Parameter.MUX) == 1
    assert transport.requests == [
        b"5FF50A04000000000000A0FA",
        b"5FF50A07000000000000A0FA",
        b"5FF50A0B000000000000A0FA",
    ]


def test_unconfirmed_read_selector_uses_official_client_frame():
    transport = FakeTransport(UNCONFIRMED_RESPONSES)
    client = DasungClient(transport)

    assert client.read_selector(Parameter.TEXT_ENHANCEMENT) == 1
    assert transport.requests == [b"5FF50A12000000000000A0FA"]


class QueuedFramesTransport:
    """Returns queued frames in order for exchange and receive calls."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.requests = []

    def exchange(self, request):
        self.requests.append(request)
        return self.frames.pop(0)

    def send(self, request):
        self.requests.append(request)

    def receive(self):
        if not self.frames:
            raise TransportError("no additional frame available")
        return self.frames.pop(0)


def test_read_skips_unsolicited_frames_before_its_response():
    transport = QueuedFramesTransport(
        [
            b"5FF5091E000000000000A0FA",
            b"5FF50805000000000000A0FA",
            b"5FF5F00A070300000000A0FA",
        ]
    )
    client = DasungClient(transport)

    assert client.read_selector(Parameter.FRONTLIGHT_MODE) == 3
    assert transport.requests == [b"5FF50A07000000000000A0FA"]


def test_read_fails_after_too_many_unsolicited_frames():
    transport = QueuedFramesTransport([b"5FF5091E000000000000A0FA"] * 7)
    client = DasungClient(transport)

    with pytest.raises(ProtocolError, match="unsolicited"):
        client.read_selector(Parameter.FRONTLIGHT_MODE)


class FailingTransport:
    def __init__(self):
        self.error = TransportError(
            "incomplete response: received 0 of 24 ASCII characters"
        )

    def exchange(self, request):
        raise self.error

    def send(self, request):
        raise AssertionError("send should not be used for reads")


def test_unconfirmed_read_failure_explains_it_may_be_unsupported():
    client = DasungClient(FailingTransport())

    with pytest.raises(TransportError, match="not confirmed") as excinfo:
        client.read_selector(Parameter.TEXT_ENHANCEMENT)
    assert "0x12" in str(excinfo.value)
    assert "may mean this firmware does not implement it" in str(excinfo.value)


def test_confirmed_read_failure_keeps_the_plain_transport_error():
    client = DasungClient(FailingTransport())

    for parameter in (Parameter.CONTRAST, Parameter.FRONTLIGHT_MODE):
        with pytest.raises(TransportError, match="incomplete response") as excinfo:
            client.read_selector(parameter)
        assert "not confirmed" not in str(excinfo.value)


class NoAckTransport:
    def __init__(self):
        self.exchanges = []
        self.sent = []

    def exchange(self, request):
        self.exchanges.append(request)
        raise TransportError(
            "incomplete response: received 0 of 24 ASCII characters"
        )

    def send(self, request):
        self.sent.append(request)

    def receive(self):
        raise TransportError(
            "incomplete response: received 0 of 24 ASCII characters"
        )


def test_writes_wait_for_acknowledgement_by_default():
    transport = NoAckTransport()
    client = DasungClient(transport)

    with pytest.raises(TransportError, match="incomplete response"):
        client.set_contrast(6)
    assert transport.exchanges == [b"5FF50106000000000000A0FA"] * 2


class StaleThenAckTransport:
    def exchange(self, request):
        return b"5FF50105000000000000A0FA"

    def receive(self):
        return b"5FF5F001000000000000A0FA"

    def send(self, request):
        raise AssertionError("send should not be used")


def test_stale_frame_before_the_acknowledgement_is_skipped():
    client = DasungClient(StaleThenAckTransport())

    assert client.set_contrast(5) == b"5FF50105000000000000A0FA"


class PiledFramesTransport:
    """Queues several unsolicited frames in front of the acknowledgement."""

    def __init__(self):
        self.exchanges = []
        self.frames = [
            b"5FF5091E000000000000A0FA",
            b"5FF50805000000000000A0FA",
            b"5FF5091E000000000000A0FA",
            b"5FF50805000000000000A0FA",
            b"5FF5091E000000000000A0FA",
            b"5FF5F001000000000000A0FA",
        ]

    def exchange(self, request):
        self.exchanges.append(request)
        return self.frames.pop(0)

    def receive(self):
        if not self.frames:
            raise TransportError("no additional frame available")
        return self.frames.pop(0)

    def send(self, request):
        raise AssertionError("send should not be used")


def test_write_skips_a_pile_of_unsolicited_frames_without_resending():
    transport = PiledFramesTransport()
    client = DasungClient(transport)

    assert client.set_contrast(5) == b"5FF50105000000000000A0FA"
    assert transport.exchanges == [b"5FF50105000000000000A0FA"]


class TimeoutThenAckTransport:
    def __init__(self):
        self.exchanges = []

    def exchange(self, request):
        self.exchanges.append(request)
        if len(self.exchanges) == 1:
            raise TransportError("incomplete response")
        return b"5FF5F001000000000000A0FA"

    def receive(self):
        raise TransportError("incomplete response")

    def send(self, request):
        raise AssertionError("send should not be used")


def test_write_is_retried_once_after_a_missing_acknowledgement():
    transport = TimeoutThenAckTransport()
    client = DasungClient(transport)

    assert client.set_contrast(5) == b"5FF50105000000000000A0FA"
    assert transport.exchanges == [b"5FF50105000000000000A0FA"] * 2


def test_no_wait_sends_without_reading_an_acknowledgement():
    transport = NoAckTransport()
    client = DasungClient(transport)

    assert client.set_contrast(6, wait=False) == b"5FF50106000000000000A0FA"
    assert transport.sent == [b"5FF50106000000000000A0FA"]
    assert transport.exchanges == []


class ScriptedTransport:
    def __init__(self, response):
        self.response = response

    def exchange(self, request):
        return self.response

    def send(self, request):
        raise AssertionError("send should not be used")

    def receive(self):
        return self.response


def test_write_with_mismatched_acknowledgement_is_rejected():
    client = DasungClient(ScriptedTransport(b"5FF5F007000000000000A0FA"))

    with pytest.raises(ProtocolError, match="does not match"):
        client.set_contrast(6)


def test_write_with_non_acknowledgement_response_is_rejected():
    client = DasungClient(ScriptedTransport(b"5FF50001060000000000A0FA"))

    with pytest.raises(ProtocolError, match="0xF0"):
        client.set_contrast(6)


def test_read_info_lenient_reports_missing_selectors_as_unknown():
    class PartialTransport(FakeTransport):
        def exchange(self, request):
            if request == b"5FF50A07000000000000A0FA":
                self.requests.append(request)
                raise TransportError("incomplete response")
            return super().exchange(request)

    info = DasungClient(PartialTransport()).read_info(lenient=True)

    assert info.frontlight_mode is None
    assert info.contrast == 6
    assert info.mux == 1


def test_read_info_strict_raises_on_missing_selector():
    class PartialTransport(FakeTransport):
        def exchange(self, request):
            if request == b"5FF50A07000000000000A0FA":
                raise TransportError("incomplete response")
            return super().exchange(request)

    with pytest.raises(TransportError, match="incomplete response"):
        DasungClient(PartialTransport()).read_info()


def test_read_info_subset_reads_only_the_requested_selectors():
    transport = FakeTransport()
    info = DasungClient(transport).read_info(
        lenient=True,
        fields=(
            "mode",
            "contrast",
            "speed",
            "frontlight_mode",
            "temperature",
            "frontlight",
        ),
    )

    assert info.mode == 4
    assert info.contrast == 6
    assert info.frontlight == 40
    assert info.protocol_version is None
    assert info.selector_03 is None
    assert info.mux is None
    assert info.selector_11 is None
    # The subset keeps the historical wire order of the full read.
    assert transport.requests == [
        b"5FF50A01000000000000A0FA",
        b"5FF50A02000000000000A0FA",
        b"5FF50A04000000000000A0FA",
        b"5FF50A07000000000000A0FA",
        b"5FF50A08000000000000A0FA",
        b"5FF50A09000000000000A0FA",
    ]


def test_read_info_subset_can_include_the_shared_version_read():
    transport = FakeTransport()
    info = DasungClient(transport).read_info(fields=("protocol_version",))

    assert info.protocol_version == 0x30
    assert info.additional_version_field == 0x10
    assert info.contrast is None
    assert transport.requests == [b"5FF50A10000000000000A0FA"]


def test_read_info_subset_rejects_unknown_fields():
    client = DasungClient(FakeTransport())

    with pytest.raises(ValueError, match="unknown monitor info fields: nope"):
        client.read_info(fields=("mode", "nope"))
