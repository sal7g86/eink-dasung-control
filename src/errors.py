"""Short, user-facing descriptions of the failures the tray can hit.

The detailed exception message keeps going to the log and to `doctor`; the
tray bar and the menu show one line, built from the stable `reason` token
that transport, locking and client attach where the failure is detected.
Nothing here parses technical text, so the wording can change without the
classification following it.
"""

from __future__ import annotations

from .protocol import ProtocolError
from .transport import (
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

# Status severities carried by the tray state; the icons and the CSS classes
# resolve from these instead of from the message text.
SEVERITY_OK = ""
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"
SEVERITY_WORKING = "working"

# Kinds used to decide whether a failure counts as "the monitor is gone".
KIND_CONNECTION = "connection"
KIND_VALUE = "value"
KIND_OTHER = "other"

# Reasons with a fixed label.
_REASON_TEXT = {
    REASON_NOT_FOUND: "monitor not found (off or unplugged?)",
    REASON_MULTIPLE: "multiple CH340 ports — pick one with --device",
    REASON_LOCKED: "monitor busy — another dasungctl is running",
    REASON_IO: "monitor not responding",
    REASON_NO_RESPONSE: "monitor not responding",
}

# Unknown errors are truncated to one line for the tray bar.
_MAX_FALLBACK = 64


def short_error(exc: BaseException) -> str:
    """One short line describing `exc`, for the tray status and the menu."""

    reason = getattr(exc, "reason", None)
    if reason in _REASON_TEXT:
        return _REASON_TEXT[reason]
    device = getattr(exc, "device", None)
    if reason == REASON_NO_PERMISSION:
        return f"no access to {device or 'the serial port'} (permissions)"
    if reason == REASON_PORT_BUSY:
        return f"{device or 'the serial port'} busy (another program?)"
    if reason == REASON_OPEN:
        return f"cannot open {device or 'the serial port'}"
    if isinstance(exc, ProtocolError):
        return "unexpected reply from monitor"
    text = " ".join(str(exc).split())
    if not text:
        text = type(exc).__name__
    if len(text) > _MAX_FALLBACK:
        text = text[: _MAX_FALLBACK - 1].rstrip() + "…"
    return text


def error_kind(exc: BaseException) -> str:
    """`connection` for serial/protocol failures, `value` for rejected input.

    A lock held by another dasungctl process is neither the monitor missing
    nor a bad value: the monitor may be perfectly reachable, so it must not
    count toward the availability check.
    """

    if (
        isinstance(exc, TransportError)
        and getattr(exc, "reason", None) == REASON_LOCKED
    ):
        return KIND_OTHER
    if isinstance(exc, (TransportError, ProtocolError)):
        return KIND_CONNECTION
    if isinstance(exc, ValueError):
        return KIND_VALUE
    return KIND_OTHER
