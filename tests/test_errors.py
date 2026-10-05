"""Short status text and severity classification; never touches hardware."""

from dasungctl.errors import (
    KIND_CONNECTION,
    KIND_VALUE,
    short_error,
    error_kind,
)
from dasungctl.protocol import ProtocolError
from dasungctl.transport import (
    REASON_IO,
    REASON_LOCKED,
    REASON_MULTIPLE,
    REASON_NO_PERMISSION,
    REASON_NO_RESPONSE,
    REASON_NOT_FOUND,
    REASON_OPEN,
    REASON_PORT_BUSY,
    TransportError,
)


def test_short_error_uses_the_reason_token_never_the_detail():
    exc = TransportError(
        "Dasung CH340 (USB 1a86:7523) was not found; visible serial devices: "
        "none. Reconnect/power on the monitor, then check 'lsusb'",
        reason=REASON_NOT_FOUND,
    )

    text = short_error(exc)

    assert text == "monitor not found (off or unplugged?)"
    assert "CH340" not in text


def test_short_error_labels_every_transport_reason():
    cases = {
        REASON_MULTIPLE: "multiple CH340 ports — pick one with --device",
        REASON_LOCKED: "monitor busy — another dasungctl is running",
        REASON_IO: "monitor not responding",
        REASON_NO_RESPONSE: "monitor not responding",
    }
    for reason, expected in cases.items():
        assert short_error(TransportError("detail", reason=reason)) == expected

    open_error = TransportError(
        "cannot open /dev/ttyUSB0: ...", reason=REASON_OPEN, device="/dev/ttyUSB0"
    )
    assert short_error(open_error) == "cannot open /dev/ttyUSB0"
    assert (
        short_error(
            TransportError(
                "permission denied", reason=REASON_NO_PERMISSION, device="/dev/tty1"
            )
        )
        == "no access to /dev/tty1 (permissions)"
    )
    assert (
        short_error(
            TransportError("busy", reason=REASON_PORT_BUSY, device="/dev/tty2")
        )
        == "/dev/tty2 busy (another program?)"
    )


def test_short_error_reads_protocol_replies_and_truncates_unknown_text():
    assert (
        short_error(ProtocolError("response has an invalid prefix"))
        == "unexpected reply from monitor"
    )

    text = short_error(TransportError("word " * 40))

    assert len(text) <= 64
    assert text.endswith("…")


def test_error_kind_separates_connection_from_rejected_values():
    assert error_kind(TransportError("x")) == KIND_CONNECTION
    assert error_kind(ProtocolError("x")) == KIND_CONNECTION
    assert error_kind(ValueError("bad value")) == KIND_VALUE

    # Another dasungctl holding the lock says nothing about the monitor.
    assert error_kind(TransportError("busy", reason=REASON_LOCKED)) != KIND_CONNECTION
