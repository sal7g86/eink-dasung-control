"""High-level monitor queries and official-client command constructions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .protocol import (
    AcknowledgementError,
    Command,
    DisplayMode,
    Parameter,
    ProtocolError,
    encode_command,
    encode_read,
    parse_ack,
    parse_response,
)
from .transport import Transport, TransportError


UNCONFIRMED_READ_PARAMETERS = frozenset({Parameter.TEXT_ENHANCEMENT})
# A write is re-sent only when the serial exchange itself times out. Stale
# frames are skipped instead, so rapid changes do not multiply the commands on
# the wire while the monitor is still catching up.
ACK_SEND_ATTEMPTS = 2
ACK_FRAME_SKIPS = 8
# The monitor emits unsolicited command-like frames after some writes (for
# example frontlight level `5FF5091E...` and temperature `5FF50805...`). They
# pile up in front of the next response, so a read skips frames that are not
# its own response instead of failing or waiting for a timeout.
UNSOLICITED_FRAME_SKIPS = 6

# MonitorInfo fields that each cost one read selector. `protocol_version` and
# `additional_version_field` are absent on purpose: both come from the single
# VERSION read handled separately in read_info().
_INFO_PARAMETERS: dict[str, Parameter] = {
    "contrast": Parameter.CONTRAST,
    "mode": Parameter.MODE,
    "selector_03": Parameter.SELECTOR_03,
    "speed": Parameter.SPEED,
    "frontlight_mode": Parameter.FRONTLIGHT_MODE,
    "temperature": Parameter.TEMPERATURE,
    "frontlight": Parameter.FRONTLIGHT,
    "mux": Parameter.MUX,
    "selector_11": Parameter.SELECTOR_11,
}
# Wire order of read_info(). It matches the historical full read (VERSION,
# then the image fields, then the frontlight fields) and is kept stable so the
# callers that request a subset still send the selectors in a predictable
# order, and the recorded test transcripts do not change.
_INFO_ORDER = (
    "contrast",
    "mode",
    "selector_03",
    "speed",
    "frontlight_mode",
    "temperature",
    "frontlight",
    "mux",
    "selector_11",
)
_VERSION_FIELDS = ("protocol_version", "additional_version_field")
_ALL_INFO_FIELDS = frozenset(_INFO_ORDER + _VERSION_FIELDS)


@dataclass(frozen=True)
class VersionInfo:
    """The two bytes answered by the VERSION selector."""

    protocol_version: int
    additional_field: int


@dataclass(frozen=True)
class MonitorInfo:
    """Known monitor values; None means unknown or not requested."""

    protocol_version: int | None
    additional_version_field: int | None
    contrast: int | None
    mode: int | None
    selector_03: int | None
    speed: int | None
    frontlight_mode: int | None
    temperature: int | None
    frontlight: int | None
    mux: int | None
    selector_11: int | None


class DasungClient:
    """Read confirmed parameters and send explicitly requested commands."""

    def __init__(self, transport: Transport) -> None:
        """Drive one transport; the caller owns its open/close lifecycle."""

        self._transport = transport

    def _read(self, parameter: Parameter | int) -> bytes:
        """Exchange one read request, skipping frames that are not its answer.

        The monitor sometimes sends unsolicited command-like frames before the
        response (see UNSOLICITED_FRAME_SKIPS), so a mismatching frame is
        dropped and the next one is read instead of failing immediately.
        """

        request = encode_read(parameter)
        try:
            response = self._transport.exchange(request)
        except TransportError as exc:
            if parameter in UNCONFIRMED_READ_PARAMETERS:
                raise TransportError(
                    f"{exc}; selector 0x{int(parameter):02X} is not confirmed on "
                    "this model, so a missing response may mean this firmware "
                    "does not implement it",
                    reason=getattr(exc, "reason", None),
                ) from exc
            raise
        for _skip in range(UNSOLICITED_FRAME_SKIPS):
            try:
                return parse_response(
                    response, expected_parameter=parameter
                ).data
            except ProtocolError:
                response = self._transport.receive()
        raise ProtocolError(
            f"no response for parameter 0x{int(parameter):02X} after skipping "
            f"{UNSOLICITED_FRAME_SKIPS} unsolicited frames"
        )

    def _try_read(self, parameter: Parameter | int) -> bytes | None:
        """Read in lenient mode: a missing or invalid answer becomes None."""

        try:
            return self._read(parameter)
        except (TransportError, ProtocolError):
            return None

    def read_selector(self, parameter: Parameter | int) -> int:
        """Read one selector and return its first data byte."""

        return self._read(parameter)[0]

    def read_version(self) -> VersionInfo:
        """Read the protocol version and the second version byte."""

        data = self._read(Parameter.VERSION)
        return VersionInfo(protocol_version=data[0], additional_field=data[1])

    def read_contrast(self) -> int:
        """Read the contrast step (1..9)."""

        return self._read(Parameter.CONTRAST)[0]

    def _value(self, parameter: Parameter | int, lenient: bool) -> int | None:
        """Read one byte, or None when lenient and the selector fails."""

        data = self._value_bytes(parameter, lenient)
        return None if data is None else data[0]

    def _value_bytes(
        self, parameter: Parameter | int, lenient: bool
    ) -> bytes | None:
        """Read the full data bytes of a selector, or None when lenient."""

        if not lenient:
            return self._read(parameter)
        return self._try_read(parameter)

    def read_info(
        self,
        *,
        lenient: bool = False,
        fields: Iterable[str] | None = None,
    ) -> MonitorInfo:
        """Read the monitor parameters and return them as a MonitorInfo.

        `fields=None` reads every selector (used by the doctor and tests).
        Naming a subset of MonitorInfo fields reads only those selectors and
        reports the rest as None: each selector is one serial round trip, so
        the tray asks for its six display/persistence fields and skips
        VERSION, SELECTOR_03, MUX and SELECTOR_11, which it never shows.
        """

        if fields is None:
            wanted = _ALL_INFO_FIELDS
        else:
            wanted = frozenset(fields)
            unknown = wanted - _ALL_INFO_FIELDS
            if unknown:
                raise ValueError(
                    "unknown monitor info fields: " + ", ".join(sorted(unknown))
                )

        values: dict[str, int | None] = {}
        if wanted.isdisjoint(_VERSION_FIELDS):
            for name in _VERSION_FIELDS:
                values[name] = None
        else:
            # Both version fields share one VERSION read.
            version_data = self._value_bytes(Parameter.VERSION, lenient)
            values["protocol_version"] = (
                version_data[0] if version_data else None
            )
            values["additional_version_field"] = (
                version_data[1] if version_data else None
            )
        for name in _INFO_ORDER:
            values[name] = (
                self._value(_INFO_PARAMETERS[name], lenient)
                if name in wanted
                else None
            )
        return MonitorInfo(**values)

    def _set(
        self,
        command: Command,
        value: int,
        payload: bytes = bytes(6),
        *,
        wait: bool = True,
    ) -> bytes:
        """Send one state-changing command and return the encoded frame.

        With `wait=False` the frame is written without reading anything, for
        callers that do not need the acknowledgement. Otherwise the exchange
        waits for the monitor's `F0 CC` ack: stale or unsolicited frames are
        skipped (up to ACK_FRAME_SKIPS) and the idempotent write is re-sent
        only when the serial exchange itself timed out (ACK_SEND_ATTEMPTS).
        """

        request = encode_command(command, value, payload)
        if not wait:
            self._transport.send(request)
            return request

        error: Exception | None = None
        for _send_attempt in range(ACK_SEND_ATTEMPTS):
            try:
                response = self._transport.exchange(request)
            except TransportError as exc:
                # Nothing came back at all: re-send, the write may be lost.
                error = exc
                continue
            for _read_attempt in range(ACK_FRAME_SKIPS):
                try:
                    parse_ack(response, expected_command=command)
                except AcknowledgementError as exc:
                    # A frame from another command: drop it and read again
                    # without re-sending the write.
                    error = exc
                    try:
                        response = self._transport.receive()
                    except TransportError as read_exc:
                        error = read_exc
                        break
                else:
                    return request
        if error is not None:
            raise error
        raise AssertionError("unreachable")

    def set_contrast(self, value: int, *, wait: bool = True) -> bytes:
        """Set contrast (official-client range 1..9)."""

        if not 1 <= value <= 9:
            raise ValueError("contrast must be in the official-client range 1..9")
        return self._set(Command.CONTRAST, value, wait=wait)

    def set_mode(self, mode: DisplayMode, *, wait: bool = True) -> bytes:
        """Set the display mode; confirms the enum value before sending."""

        try:
            confirmed_mode = DisplayMode(mode)
        except ValueError as exc:
            raise ValueError(
                "mode must be auto, text, graphic, or video"
            ) from exc
        return self._set(Command.MODE, int(confirmed_mode), wait=wait)

    def set_frontlight(self, value: int, *, wait: bool = True) -> bytes:
        """Set the frontlight level (byte range 0..255)."""

        if not 0 <= value <= 0xFF:
            raise ValueError("frontlight must be in the byte range 0..255")
        return self._set(Command.FRONTLIGHT, value, wait=wait)

    def set_temperature(self, value: int, *, wait: bool = True) -> bytes:
        """Set the frontlight temperature balance (byte range 0..255)."""

        if not 0 <= value <= 0xFF:
            raise ValueError("temperature must be in the byte range 0..255")
        return self._set(Command.TEMPERATURE, value, wait=wait)

    def set_speed(self, value: int, *, wait: bool = True) -> bytes:
        """Set the ink speed (official-client range 1..5)."""

        if not 1 <= value <= 5:
            raise ValueError("speed must be in the official-client range 1..5")
        return self._set(Command.SPEED, value, wait=wait)

    def set_frontlight_mode(self, value: int, *, wait: bool = True) -> bytes:
        """Set the raw frontlight mode byte (semantics not fully recovered)."""

        if not 0 <= value <= 0xFF:
            raise ValueError("frontlight mode must be in the byte range 0..255")
        return self._set(Command.FRONTLIGHT_MODE, value, wait=wait)

    def refresh(self, *, hard: bool = False, wait: bool = True) -> bytes:
        """Send the official clients' soft (or hard) global refresh frame."""

        return self._set(Command.REFRESH, int(hard), wait=wait)
