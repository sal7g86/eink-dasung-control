"""Transport interfaces and the CH340 serial implementation."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

import serial
from serial.tools import list_ports

from .protocol import WIRE_LENGTH


class TransportError(RuntimeError):
    """Communication with the monitor failed.

    `reason` is a stable token for the user interface (see `errors.py`) and
    `device` the serial path the failure is about; the message keeps the
    technical detail for the log and for `doctor`.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        device: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.device = device


# Stable failure tokens attached to TransportError; errors.short_error turns
# them into the short tray messages and never parses the detailed text.
REASON_NOT_FOUND = "not_found"
REASON_MULTIPLE = "multiple"
REASON_OPEN = "open"
REASON_NO_PERMISSION = "no_permission"
REASON_PORT_BUSY = "port_busy"
REASON_LOCKED = "locked"
REASON_IO = "io"
REASON_NO_RESPONSE = "no_response"


CH340_VID = 0x1A86
CH340_PID = 0x7523


def _open_error(device: str, exc: Exception) -> TransportError:
    """Wrap a failed open with the reason the tray shows in its status line."""

    text = str(exc).lower()
    if "permission" in text or "access is denied" in text:
        reason = REASON_NO_PERMISSION
    elif "busy" in text:
        reason = REASON_PORT_BUSY
    else:
        reason = REASON_OPEN
    return TransportError(
        f"cannot open {device}: {exc}", reason=reason, device=device
    )


def serial_devices() -> list[tuple[str, int | None, int | None, str]]:
    """Return serial-port metadata without opening or writing to any device."""

    return [
        (port.device, port.vid, port.pid, port.description or "")
        for port in list_ports.comports()
    ]


# Cached result of find_monitor_device(); see its docstring for the
# invalidation rules.
_DEVICE_CACHE: str | None = None


def find_monitor_device(*, refresh: bool = False) -> str:
    """Locate one CH340 with the confirmed Dasung USB VID/PID.

    Auto-detection enumerates every serial port (`list_ports.comports()` walks
    /sys), which is not free, so the resolved path is cached between calls.
    Pass `refresh=True` to re-enumerate after a device was replugged or the
    cached path failed to open.
    """

    global _DEVICE_CACHE
    if _DEVICE_CACHE is not None and not refresh:
        return _DEVICE_CACHE

    devices = serial_devices()
    matches = [
        device
        for device, vid, pid, _description in devices
        if vid == CH340_VID and pid == CH340_PID
    ]
    if len(matches) == 1:
        _DEVICE_CACHE = matches[0]
        return matches[0]
    if len(matches) > 1:
        rendered = ", ".join(matches)
        raise TransportError(
            "multiple CH340 devices with USB ID 1a86:7523 were found: "
            f"{rendered}; select the monitor with --device",
            reason=REASON_MULTIPLE,
        )

    visible = ", ".join(device for device, *_rest in devices) or "none"
    raise TransportError(
        "Dasung CH340 (USB 1a86:7523) was not found; "
        f"visible serial devices: {visible}. Reconnect/power on the monitor, "
        "then check 'lsusb' and the kernel log",
        reason=REASON_NOT_FOUND,
    )


class Transport(Protocol):
    """The minimal request/response interface the client relies on."""

    def send(self, request: bytes) -> None:
        """Send one wire-format request without assuming a response."""

    def exchange(self, request: bytes) -> bytes:
        """Send one wire-format request and return one wire-format response."""

    def receive(self) -> bytes:
        """Read one wire-format frame without sending anything."""


class SerialTransport:
    """A conservative request/response transport for the monitor's CH340."""

    def __init__(self, device: str = "auto", timeout: float = 1.0) -> None:
        """Validate the timeout and remember the device (not opened yet)."""

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.device = device
        self.timeout = timeout
        self._serial: serial.Serial | None = None

    def open(self) -> None:
        """Open the serial port; repeated calls while it is open are no-ops."""

        if self._serial is not None and self._serial.is_open:
            return
        device = find_monitor_device() if self.device == "auto" else self.device
        try:
            self._serial = self._create_serial(device)
        except (OSError, serial.SerialException) as exc:
            if self.device != "auto":
                raise _open_error(device, exc) from exc
            # The cached auto-detected path may be stale (replug or port
            # renumbering): re-enumerate once and retry.
            device = find_monitor_device(refresh=True)
            try:
                self._serial = self._create_serial(device)
            except (OSError, serial.SerialException) as retry_exc:
                raise _open_error(device, retry_exc) from retry_exc

    def _create_serial(self, device: str) -> serial.Serial:
        """Open one CH340 with the confirmed conservative settings."""

        return serial.Serial(
            port=device,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def close(self) -> None:
        """Close the port and forget it, so a later call reopens it."""

        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def _write(self, request: bytes) -> None:
        """Validate, send and flush one complete wire frame."""

        if not isinstance(request, bytes) or len(request) != WIRE_LENGTH:
            raise ValueError(
                f"serial requests must be exactly {WIRE_LENGTH} wire bytes"
            )

        self.open()
        assert self._serial is not None
        try:
            written = self._serial.write(request)
            if written != len(request):
                raise TransportError(
                    f"short serial write: wrote {written} of {len(request)} bytes",
                    reason=REASON_IO,
                )
            # flush() waits for the bytes to leave the host buffer; the
            # monitor answers only after a complete frame, so this keeps the
            # following read from racing the write.
            self._serial.flush()
        except TransportError:
            raise
        except (OSError, serial.SerialException) as exc:
            raise TransportError(
                f"serial communication failed: {exc}", reason=REASON_IO
            ) from exc

    def send(self, request: bytes) -> None:
        """Write one frame and return after it has left the host buffer."""

        self._write(request)

    def receive(self) -> bytes:
        """Read one wire-format frame and validate its length."""

        assert self._serial is not None
        try:
            response = self._serial.read(WIRE_LENGTH)
        except (OSError, serial.SerialException) as exc:
            raise TransportError(
                f"serial communication failed: {exc}", reason=REASON_IO
            ) from exc

        if len(response) != WIRE_LENGTH:
            raise TransportError(
                f"incomplete response: received {len(response)} of "
                f"{WIRE_LENGTH} ASCII characters",
                reason=REASON_NO_RESPONSE,
            )
        return response

    def exchange(self, request: bytes) -> bytes:
        """Write one request and read its response."""

        self._write(request)
        return self.receive()

    def __enter__(self) -> SerialTransport:
        """Open the port and return self for context-manager use."""

        self.open()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the port; exceptions are not suppressed."""

        self.close()
