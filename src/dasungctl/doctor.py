"""Read-only diagnostics for the monitor connection and firmware support."""

from __future__ import annotations

from dataclasses import dataclass

from .client import DasungClient
from .config import Config
from .panels import get_panel
from .protocol import Parameter, ProtocolError
from .transport import CH340_PID, CH340_VID, SerialTransport, TransportError, serial_devices


@dataclass(frozen=True)
class Check:
    """One diagnostic line: a name, an ok/warn/fail status and a detail."""

    name: str
    status: str
    detail: str


# Every selector the firmware might answer, in the order the doctor reads
# them; the last one is unconfirmed and typically fails with a warn status.
SELECTORS = (
    ("contrast", Parameter.CONTRAST),
    ("mode", Parameter.MODE),
    ("selector_03", Parameter.SELECTOR_03),
    ("speed", Parameter.SPEED),
    ("frontlight_mode", Parameter.FRONTLIGHT_MODE),
    ("temperature", Parameter.TEMPERATURE),
    ("frontlight", Parameter.FRONTLIGHT),
    ("mux", Parameter.MUX),
    ("version", Parameter.VERSION),
    ("selector_11", Parameter.SELECTOR_11),
    ("text_enhancement", Parameter.TEXT_ENHANCEMENT),
)


def run_checks(
    device: str,
    timeout: float,
    *,
    config: Config | None = None,
    config_error: str | None = None,
) -> list[Check]:
    """Run the read-only diagnostics and return one Check per probe.

    The only write is re-sending the current contrast value (to verify the
    `F0 CC` acknowledgement); everything else reads selectors and never
    changes state.
    """

    checks: list[Check] = []

    devices = serial_devices()
    candidates = [
        name
        for name, vid, pid, _description in devices
        if (vid, pid) == (CH340_VID, CH340_PID)
    ]
    checks.append(
        Check(
            "device",
            "ok" if candidates else "fail",
            ", ".join(candidates) if candidates else "no Dasung CH340 1a86:7523",
        )
    )

    panel = get_panel(config.panel if config is not None else None)
    checks.append(
        Check(
            "panel",
            "ok",
            f"{panel.name} "
            f"(protocol 0x{panel.protocol:02X}, {panel.refresh_hz} Hz)",
        )
    )

    if config_error is not None:
        checks.append(Check("config", "fail", config_error))
    elif config is None:
        checks.append(Check("config", "ok", "defaults (no config file)"))
    else:
        checks.append(
            Check(
                "config",
                "ok",
                f"device={config.device} timeout={config.timeout}",
            )
        )

    try:
        with SerialTransport(device=device, timeout=timeout) as transport:
            client = DasungClient(transport)
            try:
                version = client.read_version()
            except (TransportError, ProtocolError) as exc:
                checks.append(Check("version", "fail", str(exc)))
                return checks
            checks.append(
                Check(
                    "version",
                    "ok",
                    f"protocol 0x{version.protocol_version:02X}, "
                    f"additional 0x{version.additional_field:02X}",
                )
            )
            if version.protocol_version != panel.protocol:
                checks.append(
                    Check(
                        "panel protocol",
                        "warn",
                        f"read 0x{version.protocol_version:02X}, the "
                        f"{panel.key} profile expects 0x{panel.protocol:02X}",
                    )
                )

            for name, parameter in SELECTORS:
                try:
                    value = client.read_selector(parameter)
                except (TransportError, ProtocolError) as exc:
                    checks.append(Check(f"read {name}", "warn", str(exc)))
                else:
                    checks.append(Check(f"read {name}", "ok", f"0x{value:02X}"))

            try:
                contrast = client.read_contrast()
                client.set_contrast(contrast, wait=True)
            except (TransportError, ProtocolError, ValueError) as exc:
                checks.append(Check("write ack", "fail", str(exc)))
            else:
                checks.append(
                    Check(
                        "write ack",
                        "ok",
                        f"contrast {contrast} acknowledged with F0 CC",
                    )
                )
    except TransportError as exc:
        checks.append(Check("serial", "fail", str(exc)))

    return checks
