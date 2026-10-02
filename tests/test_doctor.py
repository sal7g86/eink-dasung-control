"""Doctor diagnostics tests, driven by the recorded fake frames."""

from dasungctl import doctor
from dasungctl.config import Config
from dasungctl.transport import TransportError

from fakes import RESPONSES


class FakeSerialTransport:
    """Context-managed transport double answering from the recorded frames."""

    def __init__(self, device, timeout):
        self.device = device
        self.timeout = timeout
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def exchange(self, request):
        self.requests.append(request)
        if request == b"5FF50A12000000000000A0FA":
            raise TransportError("incomplete response")
        if request in RESPONSES:
            return RESPONSES[request]
        command = bytes.fromhex(request.decode("ascii"))[2]
        return f"5FF5F0{command:02X}000000000000A0FA".encode("ascii")

    def send(self, request):
        self.requests.append(request)

    def receive(self):
        raise TransportError("no additional frame available")


def test_doctor_checks_device_config_reads_and_ack(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "serial_devices",
        lambda: [("/dev/ttyUSB0", 0x1A86, 0x7523, "USB Serial")],
    )
    monkeypatch.setattr(doctor, "SerialTransport", FakeSerialTransport)

    checks = doctor.run_checks("auto", 1.0, config=Config())
    by_name = {check.name: check for check in checks}

    assert by_name["device"].status == "ok"
    assert by_name["config"].status == "ok"
    assert by_name["version"].status == "ok"
    assert by_name["read contrast"].status == "ok"
    assert by_name["read text_enhancement"].status == "warn"
    assert by_name["write ack"].status == "ok"


def test_doctor_reports_missing_device(monkeypatch):
    monkeypatch.setattr(doctor, "serial_devices", lambda: [])

    checks = doctor.run_checks("auto", 1.0)

    assert checks[0].name == "device"
    assert checks[0].status == "fail"


def test_doctor_reports_config_error(monkeypatch):
    monkeypatch.setattr(doctor, "serial_devices", lambda: [])

    checks = doctor.run_checks("auto", 1.0, config_error="bad file")

    assert any(check.name == "config" and check.status == "fail" for check in checks)
