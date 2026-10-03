"""The per-run log file.

Every command truncates `$XDG_STATE_HOME/dasungctl/dasungctl.log` at startup
and appends timestamped lines there; the tray also mirrors them on stderr when
it is attached to a terminal, so an interactive `dasungctl tray` explains
itself on screen. Logging never keeps a command from starting: when the file
cannot be written, the lines go to the terminal only.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys

from . import paths


class NullLog:
    """No-op logger for objects built without one (tests, library use)."""

    def info(self, message: str) -> None:
        """Discard one informational line."""

    def warn(self, message: str) -> None:
        """Discard one warning line."""

    def error(self, message: str) -> None:
        """Discard one error line."""

    def close(self) -> None:
        """Nothing to close."""


class AppLog:
    """One run's log file, truncated when the logger is created.

    Lines are flushed immediately so `tail -f` follows the tray, and the
    terminal mirror can be turned on independently of the file.
    """

    def __init__(
        self, path: Path | None = None, *, echo: bool = False
    ) -> None:
        """Open and truncate the file; `echo` also mirrors lines on stderr."""

        self.path = Path(path) if path is not None else paths.log_path()
        self.echo = bool(echo)
        self._file = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            # A log file is a convenience, never a requirement: keep the run
            # alive and say on the terminal why the file is missing.
            self.echo = True
            self.warn(f"cannot write the log file {self.path}: {exc}")

    def info(self, message: str) -> None:
        """Write one informational line."""

        self._write("info", message)

    def warn(self, message: str) -> None:
        """Write one warning line."""

        self._write("warn", message)

    def error(self, message: str) -> None:
        """Write one error line."""

        self._write("error", message)

    def _write(self, level: str, message: str) -> None:
        line = (
            f"{datetime.now().isoformat(sep=' ', timespec='seconds')} "
            f"[{level}] {message}"
        )
        if self._file is not None:
            try:
                self._file.write(line + "\n")
                self._file.flush()
            except OSError:
                # The disk filled up or was unmounted: fall back to the
                # terminal without stopping the command.
                self._file = None
                self.echo = True
        if self.echo:
            print(line, file=sys.stderr)

    def close(self) -> None:
        """Close the file; later lines only go to the terminal."""

        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


def start(
    argv: list[str] | None = None,
    *,
    command: str = "",
    echo: bool | None = None,
    path: Path | None = None,
) -> AppLog:
    """Truncate the log file, write the header and return the logger.

    `echo` defaults to "sys.stderr is a terminal": the tray mirrors every
    line on screen when it is started from a terminal. Commands whose normal
    output is already on the terminal (devices, doctor) pass False, so their
    lines are not printed twice.
    """

    if echo is None:
        echo = sys.stderr.isatty()
    log = AppLog(path, echo=echo)
    args = sys.argv[1:] if argv is None else list(argv)
    command = command or "tray"
    if command not in args:
        args = [command, *args]
    log.info(
        " ".join(("started: dasungctl", *args)) + f" (pid {os.getpid()})"
    )
    return log
