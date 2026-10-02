"""Shared in-memory transports for unit tests."""

from __future__ import annotations

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


class FakeTransport:
    """Answers reads from a recorded map and acknowledges every write."""

    def __init__(self, responses=None):
        self.requests = []
        self.sent = []
        self.responses = RESPONSES if responses is None else responses

    def exchange(self, request):
        self.requests.append(request)
        if request in self.responses:
            return self.responses[request]
        command = bytes.fromhex(request.decode("ascii"))[2]
        return f"5FF5F0{command:02X}000000000000A0FA".encode("ascii")

    def send(self, request):
        self.sent.append(request)

    def receive(self):
        raise TransportError("no additional frame available")
