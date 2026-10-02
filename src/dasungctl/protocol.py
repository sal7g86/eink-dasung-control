"""Encoding and parsing for the observed Dasung serial protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import re

from .panels import DEFAULT_PANEL, get_panel


PACKET_LENGTH = 12
WIRE_LENGTH = PACKET_LENGTH * 2
PREFIX = bytes.fromhex("5F F5")
TAIL = bytes.fromhex("A0 FA")
READ_MARKER = 0x0A
OBSERVED_RESPONSE_MARKER = 0xF0

# Every packet has the same 12-byte layout and travels as 24 uppercase ASCII
# hex characters:
#
#   5F F5 | byte2 | byte3 | 6-byte payload | A0 FA
#
# Reads use byte2=0x0A and byte3=selector; commands use byte2=command and
# byte3=value. Responses start with byte2=0xF0 and repeat byte3 (the replied
# command, or 0x0A for reads) before the payload.
_HEX_PACKET = re.compile(rb"[0-9A-Fa-f]{24}\Z")


class ProtocolError(ValueError):
    """A packet does not match the confirmed protocol framing."""


class AcknowledgementError(ProtocolError):
    """The monitor answered a write without a valid acknowledgement."""


class Parameter(IntEnum):
    """Read selectors observed on the wire (0x03 and 0x11 are unconfirmed)."""

    CONTRAST = 0x01
    MODE = 0x02
    SELECTOR_03 = 0x03
    SPEED = 0x04
    FRONTLIGHT_MODE = 0x07
    TEMPERATURE = 0x08
    FRONTLIGHT = 0x09
    MUX = 0x0B
    VERSION = 0x10
    SELECTOR_11 = 0x11
    TEXT_ENHANCEMENT = 0x12


class Command(IntEnum):
    """Write commands observed on the wire; not all are exposed by the client.

    `RTC`, `TEXT_ENHANCEMENT` and `DITHERING` are documented for reference:
    the target firmware does not implement the latter two.
    """

    CONTRAST = 0x01
    MODE = 0x02
    REFRESH = 0x03
    SPEED = 0x04
    RTC = 0x05
    FRONTLIGHT_MODE = 0x07
    TEMPERATURE = 0x08
    FRONTLIGHT = 0x09
    MUX = 0x0B
    TEXT_ENHANCEMENT = 0x12
    DITHERING = 0x20


class DisplayMode(IntEnum):
    """Modes confirmed using the target monitor's physical controls."""

    AUTO = 0x01
    TEXT = 0x02
    GRAPHIC = 0x03
    VIDEO = 0x04

    @property
    def cli_name(self) -> str:
        """Lower-case stable name for the CLI and the UI."""

        return self.name.lower()


class FrontlightMode(IntEnum):
    """Presets calibrated against the monitor's physical lamp button."""

    OFF = 0
    COLD = 1
    WARM = 2
    MIXED = 3

    @property
    def cli_name(self) -> str:
        """Lower-case stable name for the CLI and the UI."""

        return self.name.lower()


# Project-invented frontlight mode for a manually set temperature, kept
# distinct from the mixed preset. The official clients expose only the
# calibrated presets 0..3, so value 4 is a hypothesis to test on the monitor,
# not confirmed firmware behavior. The authority is the active panel profile
# (see panels.py); these aliases describe the shipped `paperlike-hd-13.3`.
CUSTOM_FRONTLIGHT_MODE = DEFAULT_PANEL.custom_frontlight_mode

# Temperatures sent before a frontlight preset, because the serial
# mode write alone was not observed to apply them on this unit. The firmware
# accepts `0..100` and clamps higher bytes to `100` (docs/protocol.md), so cold
# sends `100` as the coldest value, warm `0`, and mixed `70`, the value
# observed with the physical lamp button in the mixed preset. Off keeps the
# stored balance, and custom reuses the last manually set value.
FRONTLIGHT_PRESET_TEMPERATURES = DEFAULT_PANEL.preset_temperatures


def display_mode_name(value: int, panel=None) -> str:
    """Return a stable CLI name while preserving unknown raw values elsewhere."""

    return get_panel(panel).display_mode_name(value)


def frontlight_mode_name(value: int, panel=None) -> str:
    """Name the calibrated presets plus the project's custom-mode value."""

    return get_panel(panel).frontlight_mode_name(value)


# The macOS client's `speedInfo` combo lists these five labels in this order,
# which the project maps to values 1..5 (docs/research-findings.md). The mapping
# follows the combo order and is not confirmed on the target monitor.
SPEED_LABELS = DEFAULT_PANEL.speed_labels


def speed_name(value: int, panel=None) -> str:
    """Label a speed value (1..5) as the official client's combo does."""

    return get_panel(panel).speed_name(value)


@dataclass(frozen=True)
class Response:
    """A validated response containing its parameter ID and five data bytes."""

    parameter_id: int
    data: bytes


def encode_read(parameter: Parameter | int) -> bytes:
    """Return a confirmed read request as 24 uppercase ASCII hex bytes."""

    parameter_id = int(parameter)
    if not 0 <= parameter_id <= 0xFF:
        raise ValueError("parameter ID must fit in one byte")

    packet = PREFIX + bytes((READ_MARKER, parameter_id)) + bytes(6) + TAIL
    return packet.hex().upper().encode("ascii")


def encode_command(
    command: Command | int, value: int, payload: bytes = bytes(6)
) -> bytes:
    """Encode one command using the frame layout found in official clients."""

    command_id = int(command)
    if not 0 <= command_id <= 0xFF:
        raise ValueError("command ID must fit in one byte")
    if not 0 <= value <= 0xFF:
        raise ValueError("command value must fit in one byte")
    if not isinstance(payload, bytes) or len(payload) != 6:
        raise ValueError("command payload must be exactly 6 bytes")
    packet = PREFIX + bytes((command_id, value)) + payload + TAIL
    return packet.hex().upper().encode("ascii")


def parse_ack(wire: bytes | str, *, expected_command: Command | int) -> int:
    """Validate the monitor's `5FF5F0CC...` write acknowledgement."""

    if isinstance(wire, str):
        # Accept strings too, so callers can pass trace text directly; the
        # framing below is checked on the ASCII bytes either way.
        try:
            encoded = wire.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AcknowledgementError(
                "acknowledgement contains non-ASCII characters"
            ) from exc
    elif isinstance(wire, bytes):
        encoded = wire
    else:
        raise TypeError("acknowledgement must be bytes or str")

    # Cheap length/charset checks before decoding the 24 hex characters.
    if len(encoded) != WIRE_LENGTH:
        raise AcknowledgementError(
            f"acknowledgement must be exactly {WIRE_LENGTH} ASCII characters; "
            f"received {len(encoded)}"
        )
    if _HEX_PACKET.fullmatch(encoded) is None:
        raise AcknowledgementError(
            "acknowledgement is not 24 hexadecimal ASCII characters"
        )

    packet = bytes.fromhex(encoded.decode("ascii"))
    # Structure check: prefix, tail, response marker, then the replied ID.
    if packet[:2] != PREFIX:
        raise AcknowledgementError("acknowledgement has an invalid prefix")
    if packet[-2:] != TAIL:
        raise AcknowledgementError("acknowledgement has an invalid tail")
    if packet[2] != OBSERVED_RESPONSE_MARKER:
        raise AcknowledgementError(
            "acknowledgement is missing the 0xF0 marker: "
            f"got 0x{packet[2]:02X}"
        )
    if packet[3] == READ_MARKER:
        # Valid frame, wrong direction: this answers a read, not the write.
        raise AcknowledgementError(
            "response is a read response, not a write acknowledgement"
        )

    command_id = packet[3]
    if command_id != int(expected_command):
        raise AcknowledgementError(
            f"acknowledgement command 0x{command_id:02X} does not match request "
            f"0x{int(expected_command):02X}"
        )
    return command_id


def parse_response(
    wire: bytes | str, *, expected_parameter: Parameter | int | None = None
) -> Response:
    """Validate and decode one observed 24-character ASCII response."""

    if isinstance(wire, str):
        # Accept strings too, so callers can pass trace text directly.
        try:
            encoded = wire.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProtocolError("response contains non-ASCII characters") from exc
    elif isinstance(wire, bytes):
        encoded = wire
    else:
        raise TypeError("response must be bytes or str")

    # Cheap length/charset checks before decoding the 24 hex characters.
    if len(encoded) != WIRE_LENGTH:
        raise ProtocolError(
            f"response must be exactly {WIRE_LENGTH} ASCII characters; "
            f"received {len(encoded)}"
        )
    if _HEX_PACKET.fullmatch(encoded) is None:
        raise ProtocolError("response is not 24 hexadecimal ASCII characters")

    packet = bytes.fromhex(encoded.decode("ascii"))
    # Structure check: prefix, tail, response marker, then the read marker.
    if packet[:2] != PREFIX:
        raise ProtocolError("response has an invalid prefix")
    if packet[-2:] != TAIL:
        raise ProtocolError("response has an invalid tail")
    if packet[2] != OBSERVED_RESPONSE_MARKER:
        raise ProtocolError(
            "response has an unexpected byte after the prefix: "
            f"0x{packet[2]:02X}"
        )
    if packet[3] != READ_MARKER:
        raise ProtocolError(
            f"response is not a read response (marker 0x{packet[3]:02X})"
        )

    # The selector follows the marker; the five data bytes come after it.
    parameter_id = packet[4]
    if expected_parameter is not None and parameter_id != int(expected_parameter):
        raise ProtocolError(
            f"response parameter 0x{parameter_id:02X} does not match request "
            f"0x{int(expected_parameter):02X}"
        )

    return Response(parameter_id=parameter_id, data=packet[5:10])
