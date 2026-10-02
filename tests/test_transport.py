"""SerialTransport tests with a fake serial backend; never opens a device."""

import pytest

from dasungctl import transport


REQUEST = b"5FF50A01000000000000A0FA"
RESPONSE = b"5FF5F00A010600000000A0FA"


@pytest.fixture(autouse=True)
def _fresh_device_cache(monkeypatch):
    """Every test starts with an empty auto-detection cache."""

    monkeypatch.setattr(transport, "_DEVICE_CACHE", None)


class FakeSerial:
    """In-memory stand-in for serial.Serial that records writes and flushes."""

    def __init__(self, response=RESPONSE, **settings):
        self.response = response
        self.settings = settings
        self.is_open = True
        self.writes = []
        self.flushed = False
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        self.flushed = True

    def read(self, size):
        return self.response[:size]

    def close(self):
        self.closed = True
        self.is_open = False


def test_serial_transport_uses_confirmed_settings(monkeypatch):
    opened = []

    def open_fake(**settings):
        serial_port = FakeSerial(**settings)
        opened.append(serial_port)
        return serial_port

    monkeypatch.setattr(transport.serial, "Serial", open_fake)

    with transport.SerialTransport("/dev/fake", timeout=2.5) as serial_transport:
        assert serial_transport.exchange(REQUEST) == RESPONSE

    serial_port = opened[0]
    assert serial_port.settings == {
        "port": "/dev/fake",
        "baudrate": 115200,
        "bytesize": transport.serial.EIGHTBITS,
        "parity": transport.serial.PARITY_NONE,
        "stopbits": transport.serial.STOPBITS_ONE,
        "timeout": 2.5,
        "write_timeout": 2.5,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    assert serial_port.writes == [REQUEST]
    assert serial_port.flushed
    assert serial_port.closed


def test_auto_detects_the_confirmed_ch340(monkeypatch):
    monkeypatch.setattr(
        transport,
        "serial_devices",
        lambda: [("/dev/ttyUSB7", 0x1A86, 0x7523, "USB Serial")],
    )
    opened = []

    def open_fake(**settings):
        opened.append(settings)
        return FakeSerial(**settings)

    monkeypatch.setattr(transport.serial, "Serial", open_fake)

    with transport.SerialTransport() as serial_transport:
        assert serial_transport.exchange(REQUEST) == RESPONSE

    assert opened[0]["port"] == "/dev/ttyUSB7"


def test_auto_detection_reports_missing_ch340(monkeypatch):
    monkeypatch.setattr(
        transport,
        "serial_devices",
        lambda: [("/dev/ttyACM0", 0x1234, 0x5678, "Other device")],
    )

    with pytest.raises(transport.TransportError, match="1a86:7523.*ttyACM0"):
        transport.SerialTransport().open()


def test_auto_detection_refuses_ambiguous_ch340_devices(monkeypatch):
    monkeypatch.setattr(
        transport,
        "serial_devices",
        lambda: [
            ("/dev/ttyUSB0", 0x1A86, 0x7523, "USB Serial"),
            ("/dev/ttyUSB1", 0x1A86, 0x7523, "USB Serial"),
        ],
    )

    with pytest.raises(transport.TransportError, match="multiple CH340.*--device"):
        transport.SerialTransport().open()


def test_auto_detection_is_cached_between_opens(monkeypatch):
    calls = []

    def devices():
        calls.append(1)
        return [("/dev/ttyUSB7", 0x1A86, 0x7523, "USB Serial")]

    monkeypatch.setattr(transport, "serial_devices", devices)
    monkeypatch.setattr(
        transport.serial, "Serial", lambda **settings: FakeSerial(**settings)
    )

    first = transport.SerialTransport()
    first.open()
    first.close()
    second = transport.SerialTransport()
    second.open()

    assert calls == [1]  # the second open reused the cached path


def test_stale_cached_path_is_refreshed_once(monkeypatch):
    monkeypatch.setattr(transport, "_DEVICE_CACHE", "/dev/ttyUSB7")
    monkeypatch.setattr(
        transport,
        "serial_devices",
        lambda: [("/dev/ttyUSB8", 0x1A86, 0x7523, "USB Serial")],
    )
    attempts = []

    def open_fake(**settings):
        attempts.append(settings["port"])
        if settings["port"] == "/dev/ttyUSB7":
            raise transport.serial.SerialException("device disappeared")
        return FakeSerial(**settings)

    monkeypatch.setattr(transport.serial, "Serial", open_fake)

    with transport.SerialTransport() as serial_transport:
        assert serial_transport.exchange(REQUEST) == RESPONSE

    assert attempts == ["/dev/ttyUSB7", "/dev/ttyUSB8"]


def test_serial_transport_rejects_incomplete_response(monkeypatch):
    monkeypatch.setattr(
        transport.serial,
        "Serial",
        lambda **settings: FakeSerial(response=RESPONSE[:-1], **settings),
    )

    with transport.SerialTransport("/dev/fake") as serial_transport:
        with pytest.raises(transport.TransportError, match="incomplete response"):
            serial_transport.exchange(REQUEST)


def test_send_does_not_wait_for_a_response(monkeypatch):
    opened = []

    def open_fake(**settings):
        serial_port = FakeSerial(response=b"", **settings)
        opened.append(serial_port)
        return serial_port

    monkeypatch.setattr(transport.serial, "Serial", open_fake)

    with transport.SerialTransport("/dev/fake") as serial_transport:
        assert serial_transport.send(REQUEST) is None

    assert opened[0].writes == [REQUEST]


@pytest.mark.parametrize(
    "wire_request", (b"", REQUEST[:-1], REQUEST + b"00", "not bytes")
)
def test_serial_transport_rejects_invalid_request_length(wire_request):
    serial_transport = transport.SerialTransport("/dev/fake")
    with pytest.raises(ValueError, match="exactly 24"):
        serial_transport.exchange(wire_request)
