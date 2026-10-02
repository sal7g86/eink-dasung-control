"""Entry point: start the tray, plus two serial diagnostics."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from . import doctor as doctor_module
from . import logfile
from .config import ConfigError, load_config
from .locking import serial_lock
from .protocol import ProtocolError
from .transport import TransportError, serial_devices


def _positive_float(value: str) -> float:
    """argparse type for timeouts/intervals: a number greater than zero."""

    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser: tray (default), devices and doctor."""

    parser = argparse.ArgumentParser(
        prog="dasungctl",
        description=(
            "Control a Dasung Paperlike monitor from a tray icon. "
            "Without a command the tray starts."
        ),
    )
    parser.add_argument(
        "--device",
        default=None,
        help="CH340 serial device, or auto-detect it (default: config or auto)",
    )
    parser.add_argument(
        "--timeout",
        default=None,
        type=_positive_float,
        help="serial timeout in seconds (default: config or 1.0)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="configuration file (default: $XDG_CONFIG_HOME/dasungctl/config.json)",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=None,
        help="auto-refresh interval in seconds (default: config or 300)",
    )

    commands = parser.add_subparsers(dest="command")
    tray = commands.add_parser(
        "tray",
        help="tray icon for GNOME, KDE and Cinnamon (StatusNotifierItem)",
    )
    tray.add_argument(
        "--install-autostart",
        action="store_true",
        help="write ~/.config/autostart/dasungctl-tray.desktop and exit",
    )
    devices = commands.add_parser(
        "devices", help="list serial devices without opening them"
    )
    _add_json_flag(devices)
    doctor = commands.add_parser(
        "doctor", help="diagnose the serial connection and selector support"
    )
    _add_json_flag(doctor)
    return parser


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )


def _show_devices(as_json: bool, log=None) -> int:
    """List serial ports without opening them (exit 1 when none exist)."""

    log = log if log is not None else logfile.NullLog()
    devices = serial_devices()
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "device": device,
                        "vid": vid,
                        "pid": pid,
                        "description": description,
                        "dasung_ch340_candidate": (vid, pid)
                        == (0x1A86, 0x7523),
                    }
                    for device, vid, pid, description in devices
                ]
            )
        )
        log.info(f"devices: {len(devices)} found")
        return 0 if devices else 1
    if not devices:
        print("No serial devices found.")
        log.warn("devices: none found")
        return 1
    for device, vid, pid, description in devices:
        usb_id = (
            f"{vid:04x}:{pid:04x}"
            if vid is not None and pid is not None
            else "unknown"
        )
        marker = (
            " [Dasung CH340 candidate]"
            if (vid, pid) == (0x1A86, 0x7523)
            else ""
        )
        line = f"{device}  {usb_id}  {description}{marker}"
        print(line)
        log.info(line)
    return 0


def _render_doctor(checks: list[doctor_module.Check], as_json: bool, log=None) -> int:
    """Print the doctor results; exit code 1 when any check failed."""

    log = log if log is not None else logfile.NullLog()
    if as_json:
        print(json.dumps([asdict(check) for check in checks]))
    else:
        for check in checks:
            print(f"[{check.status:>4}] {check.name}: {check.detail}")
    for check in checks:
        log.info(f"[{check.status:>4}] {check.name}: {check.detail}")
    return 0 if all(check.status != "fail" for check in checks) else 1


def _tray_arguments(args: argparse.Namespace) -> list[str]:
    """Rebuild the `-m dasungctl.tray` argv for the re-exec interpreter."""

    argv = ["-m", "dasungctl.tray"]
    if args.device:
        argv += ["--device", args.device]
    if args.timeout is not None:
        argv += ["--timeout", str(args.timeout)]
    if args.config:
        argv += ["--config", args.config]
    if args.interval is not None:
        argv += ["--interval", str(args.interval)]
    if getattr(args, "install_autostart", False):
        argv += ["--install-autostart"]
    return argv


def _run_tray(args: argparse.Namespace, log=None) -> int:
    """Start the tray, re-executing in the system Python when GTK is missing."""

    from . import tray as tray_module
    from .tray import TrayDependencyError

    try:
        return tray_module.run_tray(
            config_path=Path(args.config) if args.config else None,
            device=args.device,
            timeout=args.timeout,
            interval=args.interval,
            install_autostart=getattr(args, "install_autostart", False),
            log=log,
        )
    except TrayDependencyError as exc:
        return _reexec_tray(args, exc, log)


# Import probe for candidate interpreters: the tray needs GTK, the Ayatana
# indicator bindings and pyserial in the same process.
TRAY_PROBE = (
    "import gi;"
    "gi.require_version('Gtk', '3.0');"
    "gi.require_version('AyatanaAppIndicator3', '0.1');"
    "import serial"
)


def _tray_python() -> str | None:
    """First interpreter other than the current one that can load the tray.

    The project virtualenv usually has no GTK bindings, and when the venv is
    active `python3` on PATH is the venv itself, so candidates are probed one
    by one with a real import test.

    The comparison uses the interpreter paths as invoked, not their resolved
    targets: a virtualenv created with `python3 -m venv` shares the binary
    with the system Python, but the system interpreter still runs in another
    environment (no venv site-packages) and is exactly the one that provides
    the GTK bindings.
    """

    candidates = ["/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3")]
    current = Path(sys.executable)
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path in seen or path == current or not path.exists():
            continue
        seen.add(path)
        try:
            result = subprocess.run(
                [str(path), "-c", TRAY_PROBE],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return str(path.resolve())
    return None


def _reexec_tray(args: argparse.Namespace, exc: Exception, log=None) -> int:
    """Run the tray with the system Python, where GTK bindings usually live.

    The project virtualenv has no system GTK bindings, so the same command
    continues in the interpreter that provides them, with the package source
    on PYTHONPATH.
    """

    python = _tray_python()
    if python is None:
        print(f"dasungctl tray: {exc}", file=sys.stderr)
        if log is not None:
            log.error(str(exc))
        return 1
    import dasungctl

    source = Path(dasungctl.__file__).resolve().parent.parent
    env = dict(os.environ)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        os.pathsep.join([str(source), previous]) if previous else str(source)
    )
    argv = [python, *_tray_arguments(args)]
    if log is not None:
        log.warn(f"GTK not available in this interpreter: restarting with {python}")
        log.close()
    try:
        os.execve(python, argv, env)
    except OSError as error:
        message = f"cannot start {python}: {error}"
        print(f"dasungctl tray: {message}", file=sys.stderr)
        if log is not None:
            log.error(message)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the command line: devices, tray (default) or doctor."""

    args = build_parser().parse_args(argv)
    log = logfile.start(
        argv,
        command=args.command or "tray",
        # The devices/doctor output is already on the terminal: echoing the
        # log line again would only duplicate it.
        echo=False if args.command in ("devices", "doctor") else None,
    )
    try:
        if args.command == "devices":
            return _show_devices(args.json, log)
        if args.command in (None, "tray"):
            return _run_tray(args, log)

        try:
            config = load_config(Path(args.config) if args.config else None)
        except ConfigError as exc:
            print(f"dasungctl: error: {exc}", file=sys.stderr)
            log.error(f"configuration error: {exc}")
            return 1

        device = args.device or config.device
        timeout = args.timeout if args.timeout is not None else config.timeout

        try:
            with serial_lock():
                checks = doctor_module.run_checks(device, timeout, config=config)
                return _render_doctor(checks, args.json, log)
        except (ProtocolError, TransportError, ValueError, OSError) as exc:
            print(f"dasungctl: error: {exc}", file=sys.stderr)
            log.error(str(exc))
            return 1
    finally:
        log.close()
