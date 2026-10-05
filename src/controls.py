"""Monitor control operations used by the tray application.

The functions mutate a small state object exposing `info`, `message` and
`custom_temperature` (plus `severity`, `error_detail` and `error_kind`, which
carry the short status text, the detail for the window tooltip and the kind
of failure), and keep the serial quirks documented in `docs/protocol.md`:
a manual temperature marks the project's custom frontlight mode, presets send
their temperature before the mode, and no read-back follows a mode write.
"""

from __future__ import annotations

from dataclasses import replace

from .client import DasungClient
from .errors import SEVERITY_ERROR, error_kind, short_error
from .panels import get_panel
from .protocol import DisplayMode, ProtocolError
from .transport import TransportError

# Failures that become a status message instead of propagating: the tray has
# to survive a busy monitor, a rejected value, or a failed write.
WRITE_ERRORS = (TransportError, ProtocolError, ValueError)

# Human labels for the generic `"{label} set to {value}"` message.
LABELS = {
    "mode": "Mode",
    "contrast": "Contrast",
    "speed": "Speed",
    "frontlight": "Frontlight",
    "temperature": "Temperature",
    "frontlight_mode": "Frontlight mode",
}


def _ok(state, message: str) -> None:
    """Publish a successful operation on the shared state object."""

    state.message = message
    state.severity = ""
    state.error_detail = ""
    state.error_kind = None


def _error(state, exc: Exception, prefix: str = "") -> None:
    """Publish a failed operation: short message, full detail for the tooltip."""

    state.message = f"{prefix}{short_error(exc)}"
    state.severity = SEVERITY_ERROR
    state.error_detail = str(exc)
    state.error_kind = error_kind(exc)


def set_temperature(client: DasungClient, state, value: int, panel=None) -> bool:
    """Set a manual temperature and mark the frontlight mode as custom.

    No read-back follows: the monitor ignores reads that arrive right after a
    write, and the write acknowledgement already confirms the command.
    """

    profile = get_panel(panel)
    try:
        client.set_temperature(value, wait=True)
    except WRITE_ERRORS as exc:
        _error(state, exc)
        return False
    state.info = replace(state.info, temperature=value)
    state.custom_temperature = value
    try:
        client.set_frontlight_mode(profile.custom_frontlight_mode, wait=True)
    except WRITE_ERRORS as exc:
        _error(state, exc, prefix="temperature set; frontlight mode failed: ")
        return False
    state.info = replace(
        state.info, frontlight_mode=profile.custom_frontlight_mode
    )
    _ok(state, f"Temperature set to {value}; frontlight mode custom")
    return True


def set_frontlight_mode(
    client: DasungClient, state, value: int, panel=None
) -> bool:
    """Apply a frontlight preset, sending its temperature first when defined.

    The custom value reuses the last temperature the user set manually, so
    switching to another preset and back does not lose it. No read-back follows
    the mode write: the firmware does not update the stored temperature from a
    serial mode write, and reading it right after stalls the interface.
    """

    profile = get_panel(panel)
    if value == profile.custom_frontlight_mode:
        preset_temperature = state.custom_temperature
    else:
        preset_temperature = profile.preset_temperatures.get(value)
    if preset_temperature is not None:
        try:
            client.set_temperature(preset_temperature, wait=True)
        except WRITE_ERRORS as exc:
            _error(state, exc)
            return False
        state.info = replace(state.info, temperature=preset_temperature)
    try:
        client.set_frontlight_mode(value, wait=True)
    except WRITE_ERRORS as exc:
        _error(state, exc)
        return False
    state.info = replace(state.info, frontlight_mode=value)
    message = (
        f"frontlight mode set to {profile.frontlight_mode_name(value)}"
    )
    if preset_temperature is not None:
        message += f", temperature {preset_temperature}"
    else:
        message += ", temperature unchanged"
    _ok(state, message)
    return True


def apply_field(
    client: DasungClient, state, name: str, value: int, panel=None
) -> bool:
    """Apply one monitor field, reporting failures through `state.message`."""

    profile = get_panel(panel)
    if name == "temperature":
        return set_temperature(client, state, value, profile)
    if name == "frontlight_mode":
        return set_frontlight_mode(client, state, value, profile)
    try:
        if name == "mode":
            client.set_mode(DisplayMode(value), wait=True)
        else:
            setter = getattr(client, f"set_{name}")
            setter(value, wait=True)
    except WRITE_ERRORS as exc:
        _error(state, exc)
        return False
    state.info = replace(state.info, **{name: value})
    if name == "mode":
        _ok(state, f"mode set to {profile.display_mode_name(value)}")
    else:
        _ok(state, f"{LABELS.get(name, name)} set to {value}")
    return True
